#!/usr/bin/env python3

# sys-line - a simple status line generator
# Copyright (C) 2019-2020  Julian Heng
#
# This file is part of sys-line.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# pylint: disable=invalid-name

""" Abstract classes for getting system info """

import os
import re
import sys
import time

from abc import ABC, abstractmethod
from copy import copy
from datetime import datetime
from functools import lru_cache
from importlib import import_module
from pathlib import Path as p

from ..tools.storage import Storage
from ..tools.utils import percent, run, unix_epoch_to_str, round_trim
from ..tools.df import DfEntry


class AbstractGetter(ABC):
    """
    Abstract Getter class to store both options and the implementations for the
    information under the getter
    """

    def __init__(self, domain_name, options):
        super(AbstractGetter, self).__init__()

        self.domain_name = domain_name
        self.options = options

    @property
    @abstractmethod
    def _valid_info(self):
        """ Returns list of info in getter """

    def query(self, info, options):
        """ Returns the value of info """
        if info not in self._valid_info:
            msg = f"info name '{info}' is not in domain"
            raise RuntimeError(msg)
        return self._query(info, options)

    def _query(self, info, options):
        """ Returns the value of info """
        if options is None:
            val = getattr(self, info)
        else:
            # Copy current options to tmp
            tmp = copy(self.options)

            # Set options and get value
            for k, v in self._parse_options(info, options).items():
                setattr(self.options, k, v)
            val = getattr(self, info)

            # Restore options
            self.options = tmp
        return val

    def _parse_options(self, info, options):
        opts = dict()
        if options:
            for i in options.split(","):
                if not i:
                    continue

                # An option with "=" requires a value, unless it is a boolean
                # option
                if "=" not in i:
                    i = self._handle_missing_option_value(i)

                k, v = i.split("=", 1)

                # A prefix option needs to be one of the prefixes
                if k == "prefix":
                    if v not in Storage.PREFIXES:
                        msg = f"invalid value for prefix: {v}"
                        raise RuntimeError(msg)

                if (hasattr(self.options, k) or
                        hasattr(self.options, f"{info}_{k}")):
                    # Boolean options does not require the info name as part of
                    # the option
                    if v in ["True", "False"]:
                        v = v == "True"
                        key = k
                    else:
                        if v.isnumeric():
                            v = int(v)
                        key = f"{info}_{k}"
                    opts[key] = v
                else:
                    msg = f"no such option in domain: {i}"
                    raise RuntimeError(msg)

        return opts

    def _handle_missing_option_value(self, opt_name):
        """ Handle options in format string if no value is given """
        if hasattr(self.options, opt_name):
            # Check if the option name is actually a boolean and set to true
            if isinstance(getattr(self.options, opt_name), bool):
                opt_name = f"{opt_name}=True"
            else:
                msg = f"option requires value: {opt_name}"
                raise RuntimeError(msg)
        else:
            msg = f"no such option in domain: {opt_name}"
            raise RuntimeError(msg)
        return opt_name

    def __str__(self):
        """ The string representation of the getter would return all values """
        return "\n".join([
            f"{self.domain_name}.{i}: {getattr(self, i)}"
            for i in self._valid_info
        ])


class AbstractStorage(AbstractGetter):
    """
    AbstractStorage for info that fetches used, total and percent attributes
    """

    @property
    def _valid_info(self):
        return ["used", "total", "percent"]

    @abstractmethod
    def _used(self):
        """
        Abstract used method that returns Storage arguments to be implemented
        by subclass
        """

    @property
    def used(self):
        """
        Returns a Storage class representing the amount used in storage
        """
        value, prefix = self._used()
        used = Storage(value=value, prefix=prefix,
                       rounding=self.options.used_round)
        used.prefix = self.options.used_prefix
        return used

    @abstractmethod
    def _total(self):
        """
        Abstract total method that returns Storage arguments to be implemented
        by subclass
        """

    @property
    def total(self):
        """
        Returns a Storage class representing the total amount in storage
        """
        value, prefix = self._total()
        total = Storage(value=value, prefix=prefix,
                        rounding=self.options.total_round)
        total.prefix = self.options.total_prefix
        return total

    @property
    def percent(self):
        """ Abstract percent property """
        perc = percent(self.used.bytes, self.total.bytes)
        if perc is None:
            perc = 0.0
        else:
            perc = round_trim(perc, self.options.percent_round)
        return perc


class AbstractCpu(AbstractGetter):
    """ Abstract cpu class to be implemented by subclass """

    @property
    def _valid_info(self):
        return ["cores", "cpu", "load_avg",
                "cpu_usage", "fan", "temp", "uptime"]

    @property
    @abstractmethod
    def cores(self):
        """ Abstract cores method to be implemented by subclass """

    @abstractmethod
    def _cpu_string(self):
        """
        Private abstract cpu string method to be implemented by subclass
        """

    @abstractmethod
    def _cpu_speed(self):
        """
        Private abstract cpu speed method to be implemented by subclass
        """

    @property
    def cpu(self):
        """ Returns cpu string """
        cpu_reg = re.compile(r"\s+@\s+(\d+\.)?\d+GHz")
        trim_reg = re.compile(r"CPU|\((R|TM)\)")

        cores = self.cores
        cpu = self._cpu_string()
        speed = self._cpu_speed()
        cpu = trim_reg.sub("", cpu.strip())

        if speed is not None:
            fmt = fr" ({cores}) @ {speed}GHz"
            cpu = cpu_reg.sub(fmt, cpu)
        else:
            fmt = fr"({cores}) @"
            cpu = re.sub(r"@", fmt, cpu)

        cpu = re.sub(r"\s+", " ", cpu)

        return cpu

    @abstractmethod
    def _load_avg(self):
        """ Abstract load average method to be implemented by subclass """

    @property
    def load_avg(self):
        """ Load average method """
        load = self._load_avg()
        if load is not None:
            return load[0] if self.options.load_short else " ".join(load)
        return None

    @property
    def cpu_usage(self):
        """ Cpu usage method """
        cores = self.cores
        ps_out = run(["ps", "-e", "-o", "%cpu"]).strip().split("\n")[1:]
        cpu_usage = sum(float(i) for i in ps_out) / cores
        return round_trim(cpu_usage, self.options.usage_round)

    @property
    @abstractmethod
    def fan(self):
        """ Abstract fan method to be implemented by subclass """

    @property
    @abstractmethod
    def temp(self):
        """ Abstract temperature method to be implemented by subclass """

    @abstractmethod
    def _uptime(self):
        """ Abstract uptime method to be implemented by subclass """

    @property
    def uptime(self):
        """ Uptime method """
        return unix_epoch_to_str(self._uptime())


class AbstractMemory(AbstractStorage):
    """ Abstract memory class to be implemented by subclass """

    @abstractmethod
    def _used(self):
        pass

    @abstractmethod
    def _total(self):
        pass


class AbstractSwap(AbstractStorage):
    """ Abstract swap class to be implemented by subclass """

    @abstractmethod
    def _used(self):
        pass

    @abstractmethod
    def _total(self):
        pass


class AbstractDisk(AbstractStorage):
    """ Abstract disk class to be implemented by subclass """

    def __init__(self, domain_name, options):
        super(AbstractDisk, self).__init__(domain_name, options)
        self._df_entries = None

    @property
    def _valid_info(self):
        return super(AbstractDisk, self)._valid_info + ["dev", "mount",
                                                        "name", "partition"]

    def _query(self, info, options):
        # Key for which device to get information from
        key = None
        new_opts = list()

        if options:
            for i in options.split(","):
                if "=" in i:
                    if i.split("=", 1)[0] not in ["disk", "mount"]:
                        new_opts.append(i)
                else:
                    if hasattr(self.options, i):
                        new_opts.append(i)
                    else:
                        key = i
        new_opts = ",".join(new_opts)

        if key is None:
            # Set key to the first available mount or disk. By default, it
            # should be "/" if no disks or mounts are set. Otherwise, it is set
            # to the first disk or mount set in the arguments
            key = next(iter(getattr(self, info)), "/")
        elif not (key in self.options.disk or key in self.options.mount):
            if p(key).is_block_device():
                self.options.disk.append(key)
            else:
                self.options.mount.append(key)

        if not p(key).is_block_device():
            key = self._mount_to_devname(key)

        return super(AbstractDisk, self)._query(info, new_opts)[key]

    def _mount_to_devname(self, mount_path):
        return next(k for k, v in self.mount.items() if mount_path == v)

    @property
    @abstractmethod
    def _DF_FLAGS(self):
        pass

    @property
    @lru_cache(maxsize=1)
    def _df(self):
        return run(self._DF_FLAGS).strip().split("\n")[1:]

    @property
    def df_entries(self):
        """
        Return df entries

        If any modifications to options.disk or options.mount is made,
        _df_entries is updated to reflect these changes
        """
        if self._df_entries is None:
            self._df_entries = dict()

        reg = list()
        if self.options.disk:
            disks = r"|".join(self.options.disk)
            reg.append(fr"^({disks})")
        if self.options.mount:
            mounts = r"|".join(self.options.mount)
            reg.append(fr"({mounts})$")
        reg = re.compile(r"|".join(reg))

        if self._df is not None:
            for i in self._df:
                if reg.search(i):
                    split = i.split()
                    if split[0] not in self._df_entries.keys():
                        df_entry = DfEntry(*split)
                        self._df_entries[df_entry.filesystem] = df_entry

        return self._df_entries

    @property
    def original_dev(self):
        """ Disk device without modification """
        dev = None
        if self.df_entries is not None:
            dev = {k: v.filesystem for k, v in self.df_entries.items()}
        return dev

    @property
    def dev(self):
        """ Disk device method """
        dev = self.original_dev
        if self.options.short_dev:
            dev = {k: v.split("/")[-1] for k, v in dev.items()}
        return dev

    @property
    @abstractmethod
    def name(self):
        """ Abstract disk name method to be implemented by subclass """

    @property
    def mount(self):
        """ Disk mount method """
        mount = None
        if self.df_entries is not None:
            mount = {k: v.mount for k, v in self.df_entries.items()}
        return mount

    @property
    @abstractmethod
    def partition(self):
        """ Abstract disk partition method to be implemented by subclass """

    def _used(self):
        pass

    @property
    def used(self):
        """ Disk used method """
        used = None
        if self.df_entries is not None:
            used = dict()
            for k, v in self.df_entries.items():
                stor = Storage(int(v.used), "KiB",
                               rounding=self.options.used_round)
                stor.prefix = self.options.used_prefix
                used[k] = stor
        return used

    def _total(self):
        pass

    @property
    def total(self):
        """ Disk total method """
        total = None
        if self.df_entries is not None:
            total = dict()
            for k, v in self.df_entries.items():
                stor = Storage(int(v.blocks), "KiB",
                               rounding=self.options.total_round)
                stor.prefix = self.options.total_prefix
                total[k] = stor
        return total

    @property
    def percent(self):
        """ Disk percent property """
        perc = None
        if self.original_dev is not None:
            perc = dict()
            used = self.used
            total = self.total
            for dev in self.original_dev.keys():
                value = percent(used[dev].bytes, total[dev].bytes)
                if value is None:
                    value = 0.0
                else:
                    value = round_trim(value, self.options.percent_round)
                perc[dev] = value
        return perc


class AbstractBattery(AbstractGetter):
    """ Abstract battery class to be implemented by subclass """

    @property
    def _valid_info(self):
        return ["is_present", "is_charging", "is_full", "percent",
                "time", "power"]

    @property
    @abstractmethod
    def is_present(self):
        """ Abstract battery present method to be implemented by subclass """

    @property
    @abstractmethod
    def is_charging(self):
        """ Abstract battery charging method to be implemented by subclass """

    @property
    @abstractmethod
    def is_full(self):
        """ Abstract battery full method to be implemented by subclass """

    @property
    @abstractmethod
    def percent(self):
        """ Abstract battery percent method to be implemented by subclass """

    @abstractmethod
    def _time(self):
        """
        Abstract battery time remaining method to be implemented by subclass
        """

    @property
    def time(self):
        """ Battery time method """
        return unix_epoch_to_str(self._time())

    @property
    @abstractmethod
    def power(self):
        """
        Abstract battery power usage method to be implemented by subclass
        """


class AbstractNetwork(AbstractGetter):
    """ Abstract network class to be implemented by subclass """

    @property
    def _valid_info(self):
        return ["dev", "ssid", "local_ip", "download", "upload"]

    @property
    @abstractmethod
    def _LOCAL_IP_CMD(self):
        pass

    @property
    @lru_cache(maxsize=1)
    @abstractmethod
    def dev(self):
        """ Abstract network device method to be implemented by subclass """

    @abstractmethod
    def _ssid(self):
        """ Abstract ssid resource method to be implemented by subclass """

    @property
    def ssid(self):
        """ Network ssid method """
        ssid = None
        cmd, reg = self._ssid()
        if not (cmd is None or reg is None):
            ssid = (reg.match(i.strip()) for i in run(cmd).split("\n"))
            ssid = next((i.group(1) for i in ssid if i), None)

        return ssid

    @property
    def local_ip(self):
        """ Network local ip method """
        ip_out = None
        if self.dev is not None:
            reg = re.compile(r"^inet\s+((?:[0-9]{1,3}\.){3}[0-9]{1,3})")
            ip_out = run(self._LOCAL_IP_CMD + [self.dev]).strip().split("\n")
            ip_out = (reg.match(line.strip()) for line in ip_out)
            ip_out = next((i.group(1) for i in ip_out if i), None)
        return ip_out

    @abstractmethod
    def _bytes_delta(self, dev, mode):
        """
        Abstract network bytes delta method to fetch the change in bytes on
        a device depending on mode
        """

    def _bytes_rate(self, mode):
        """
        Abstract network bytes rate method to fetch the rate of change in bytes
        on a device depending on mode
        """
        if self.dev is None:
            return 0.0

        start = self._bytes_delta(self.dev, mode)
        start_time = time.time()

        # Timeout after 2 seconds
        while (self._bytes_delta(self.dev, mode) <= start and
               time.time() - start_time < 2):
            time.sleep(0.01)

        end = self._bytes_delta(self.dev, mode)
        if end == start:
            return 0.0

        end_time = time.time()
        delta_bytes = end - start
        delta_time = end_time - start_time

        return delta_bytes / delta_time

    @property
    def download(self):
        """ Network download method """
        download = Storage(self._bytes_rate("down"), "B",
                           rounding=self.options.download_round)
        download.prefix = self.options.download_prefix
        return download

    @property
    def upload(self):
        """ Network upload method """
        upload = Storage(self._bytes_rate("up"), "B",
                         rounding=self.options.upload_round)
        upload.prefix = self.options.upload_prefix
        return upload


class Date(AbstractGetter):
    """ Date class to fetch date and time """

    @property
    def _valid_info(self):
        return ["date", "time"]

    @staticmethod
    def _format(fmt):
        """ Wrapper for printing date and time format """
        return "{{:{}}}".format(fmt).format(datetime.now())

    @property
    def date(self):
        """ Returns the date as a string from a specified format """
        return Date._format(self.options.date_format)

    @property
    def time(self):
        """ Returns the time as a string from a specified format """
        return Date._format(self.options.time_format)


class AbstractWindowManager(AbstractGetter):
    """ Abstract window manager class to be implemented by subclass """

    @property
    def _valid_info(self):
        return ["desktop_index", "desktop_name", "app_name", "window_name"]

    @property
    @abstractmethod
    def desktop_index(self):
        """ Abstract desktop index method to be implemented by subclass """

    @property
    @abstractmethod
    def desktop_name(self):
        """ Abstract desktop name method to be implemented by subclass """

    @property
    @abstractmethod
    def app_name(self):
        """
        Abstract focused application name method to be implemented by subclass
        """

    @property
    @abstractmethod
    def window_name(self):
        """
        Abstract focused window name method to be implemented by subclass
        """


class AbstractMisc(AbstractGetter):
    """ Misc class for fetching miscellaneous information """

    @property
    def _valid_info(self):
        return ["vol", "scr"]

    @property
    @abstractmethod
    def vol(self):
        """ Abstract volume method to be implemented by subclass """

    @property
    @abstractmethod
    def scr(self):
        """ Abstract screen brightness method to be implemented by subclass """


class BatteryStub(AbstractBattery):
    """ Sub-Battery class for systems that has no battery """

    @property
    def is_present(self):
        return False

    @property
    def is_charging(self):
        return None

    @property
    def is_full(self):
        return None

    @property
    def percent(self):
        return None

    def _time(self):
        return 0

    @property
    def power(self):
        return None


class WindowManagerStub(AbstractWindowManager):
    """ Placeholder window manager """

    @property
    def desktop_index(self):
        return None

    @property
    def desktop_name(self):
        return None

    @property
    def app_name(self):
        return None

    @property
    def window_name(self):
        return None


class System(ABC):
    """
    Abstract System class to store all the assigned getters from the sub class
    that implements this class
    """

    DOMAINS = ("cpu", "memory", "swap", "disk",
               "battery", "network", "date", "window manager", "misc")
    SHORT_DOMAINS = ("cpu", "mem", "swap", "disk",
                     "bat", "net", "date", "wm", "misc")

    def __init__(self, options, **kwargs):
        super(System, self).__init__()
        self._getters = dict(kwargs, date=Date)
        self.options = {k: getattr(options, k, None) for k in self._getters}
        self._getters_cache = {k: None for k in self._getters}

    @property
    @abstractmethod
    def _SUPPORTED_WMS(self):
        """
        Abstract property containing the list of supported window managers for
        this system
        """

    @staticmethod
    def create_instance(options):
        """
        Instantialises an implementation of the System class by dynamically
        importing the module
        """
        os_name = os.uname().sysname

        # Module system files format is the output of "uname -s" in lowercase
        mod_prefix = __name__.split(".")[:-1]
        mod_name = ".".join(mod_prefix + [os_name.lower()])
        system = None

        try:
            mod = import_module(mod_name)
            system = getattr(mod, os_name)(options)
        except ModuleNotFoundError:
            print(f"Unknown system: '{os_name}'", "Exiting...",
                  sep="\n", file=sys.stderr)

        return system

    def detect_window_manager(self):
        """ Detects which supported window manager is currently running """
        ps_out = run(["ps", "-e", "-o", "command"])
        return next((v for k, v in self._SUPPORTED_WMS.items() if k in ps_out),
                    WindowManagerStub)

    def query(self, domain):
        """ Queries a system for a domain and info """
        if domain not in self._getters.keys():
            msg = f"domain name '{domain}' not in system"
            raise RuntimeError(msg)

        if self._getters_cache[domain] is None:
            opts = self.options[domain]
            self._getters_cache[domain] = self._getters[domain](domain, opts)

        return self._getters_cache[domain]
