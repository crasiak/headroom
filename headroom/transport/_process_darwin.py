"""Kernel start times for the portable whole-second process identity."""

from __future__ import annotations

import ctypes
import errno
from functools import cache
from typing import Any


class _BSDInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("jobc", ctypes.c_uint32),
        ("tdev", ctypes.c_uint32),
        ("tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


@cache
def _pidinfo() -> Any:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    try:
        reader = library.proc_pidinfo
    except AttributeError as error:
        raise OSError(errno.ENOSYS, "macOS process identity reader is unavailable") from error
    reader.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    reader.restype = ctypes.c_int
    return reader


def process_start_seconds(pid: int) -> int:
    """Read kernel start seconds, rejecting incomplete or mismatched process info."""
    if type(pid) is not int or not 0 < pid <= 0x7FFFFFFF:
        raise ValueError("process PID is outside the native range")
    info = _BSDInfo()
    reader = _pidinfo()
    ctypes.set_errno(0)
    length = reader(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if length != ctypes.sizeof(info):
        error = ctypes.get_errno() if length <= 0 else errno.EIO
        raise OSError(error or errno.EIO, "cannot read macOS process identity")
    if info.pid != pid or not info.start_sec or info.start_usec >= 1_000_000:
        raise OSError(errno.EIO, "invalid macOS process identity")
    return int(info.start_sec)
