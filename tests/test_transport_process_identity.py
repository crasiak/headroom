from __future__ import annotations

import ctypes
import errno
import os
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from headroom.transport import _process_darwin, protocol


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS kernel identity reader")
def test_darwin_identity_survives_unavailable_ps_without_admitting_stale_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = os.getpid()
    output = subprocess.check_output(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
        env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        text=True,
        timeout=2,
    )
    epoch = (
        datetime.strptime(output.strip(), "%a %b %d %H:%M:%S %Y")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    ps_calls = []

    def unavailable_ps(*args, **kwargs):
        ps_calls.append(args)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(protocol, "subprocess_run", unavailable_ps)
    monkeypatch.setattr(protocol, "_psutil_process_identity", lambda pid: None)
    monkeypatch.setattr(protocol, "_proc_process_identity", lambda pid: None)

    identity = protocol.current_process_identity(pid)
    assert identity.to_dict() == {"pid": pid, "start_source": "ps", "start_time": epoch}
    assert identity.matches_live_process() is True
    assert protocol.ProcessIdentity(pid, "ps", epoch - 1).matches_live_process() is False
    assert protocol._ps_process_identity(1 << 30) is None
    with pytest.raises(ProcessLookupError, match="cannot establish process identity"):
        protocol.current_process_identity(1 << 30)
    assert ps_calls == [], "macOS identity verification still forks ps"


@pytest.mark.parametrize("microseconds", [0, 999_999])
def test_native_reader_keeps_portable_whole_second_identity(
    monkeypatch: pytest.MonkeyPatch, microseconds: int
) -> None:
    pid = os.getpid()

    def read(target, flavor, argument, buffer, size):
        assert (target, flavor, argument, size) == (pid, 3, 0, 136)
        info = ctypes.cast(buffer, ctypes.POINTER(_process_darwin._BSDInfo)).contents
        info.pid, info.start_sec, info.start_usec = pid, 1234, microseconds
        return size

    monkeypatch.setattr(_process_darwin, "_pidinfo", lambda: read)
    monkeypatch.setattr(protocol, "sys", SimpleNamespace(platform="darwin"))
    identity = protocol._ps_process_identity(pid)
    assert identity == protocol.ProcessIdentity(pid, "ps", 1234.0)
    assert identity.matches_live_process() is True
    assert protocol.ProcessIdentity(pid, "ps", 1235.0).matches_live_process() is False


@pytest.mark.parametrize(
    ("length", "native_errno", "fields", "reason"),
    [
        (0, errno.ESRCH, {}, "missing_pid"),
        (0, errno.EPERM, {}, "permission_denied"),
        (-1, errno.EINTR, {}, "os_error"),
        (0, 0, {}, "os_error"),
        (128, 0, {}, "os_error"),
        (144, 0, {}, "os_error"),
        (136, 0, {"pid": 0}, "os_error"),
        (136, 0, {"start_sec": 0}, "os_error"),
        (136, 0, {"start_usec": 1_000_000}, "os_error"),
    ],
)
def test_native_failure_stays_unavailable_and_never_falls_back_to_ps(
    monkeypatch: pytest.MonkeyPatch, length: int, native_errno: int, fields: dict, reason: str
) -> None:
    pid = os.getpid()

    def read(target, flavor, argument, buffer, size):
        info = ctypes.cast(buffer, ctypes.POINTER(_process_darwin._BSDInfo)).contents
        info.pid, info.start_sec, info.start_usec = pid, 1234, 0
        for key, value in fields.items():
            setattr(info, key, value)
        ctypes.set_errno(native_errno)
        return length

    def forbidden_ps(*args, **kwargs):
        pytest.fail("native read failure must not fork ps or admit an unverified identity")

    monkeypatch.setattr(_process_darwin, "_pidinfo", lambda: read)
    monkeypatch.setattr(protocol, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(protocol, "subprocess_run", forbidden_ps)
    monkeypatch.setattr(protocol, "_psutil_process_identity", lambda pid: None)
    monkeypatch.setattr(protocol, "_proc_process_identity", lambda pid: None)
    recorded = protocol.ProcessIdentity(pid, "ps", 1234.0)
    observed = recorded.observe_diagnostics()
    assert observed.state == "unavailable"
    assert observed.reader_error == reason
    assert recorded.matches_live_process() is False
    with pytest.raises(ProcessLookupError, match="cannot establish process identity"):
        protocol.current_process_identity(pid)


@pytest.mark.parametrize("pid", [0, -1, 1 << 31, True])
def test_native_reader_rejects_pid_truncation_before_calling_kernel(
    monkeypatch: pytest.MonkeyPatch, pid: int
) -> None:
    def forbidden_reader():
        pytest.fail("invalid PID reached the kernel")

    monkeypatch.setattr(_process_darwin, "_pidinfo", forbidden_reader)
    with pytest.raises(ValueError, match="native range"):
        _process_darwin.process_start_seconds(pid)


def test_native_binding_uses_the_system_library_and_typed_cached_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = Mock()
    load = Mock(return_value=SimpleNamespace(proc_pidinfo=reader))
    monkeypatch.setattr(_process_darwin.ctypes, "CDLL", load)
    _process_darwin._pidinfo.cache_clear()
    try:
        assert _process_darwin._pidinfo() is reader
        assert _process_darwin._pidinfo() is reader
        load.assert_called_once_with("/usr/lib/libproc.dylib", use_errno=True)
        assert reader.argtypes == [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        assert reader.restype is ctypes.c_int
    finally:
        _process_darwin._pidinfo.cache_clear()


def test_missing_native_symbol_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_process_darwin.ctypes, "CDLL", lambda *a, **kw: SimpleNamespace())
    _process_darwin._pidinfo.cache_clear()
    try:
        with pytest.raises(OSError) as failure:
            _process_darwin._pidinfo()
        assert failure.value.errno == errno.ENOSYS
    finally:
        _process_darwin._pidinfo.cache_clear()


def test_other_platforms_keep_the_existing_bounded_ps_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def read(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "Fri Nov 27 10:00:00 2026\n", "")

    def forbidden_native():
        pytest.fail("non-macOS path attempted native loading")

    monkeypatch.setattr(protocol, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(protocol, "subprocess_run", read)
    monkeypatch.setattr(_process_darwin, "_pidinfo", forbidden_native)
    identity = protocol._ps_process_identity(1234)
    assert identity == protocol.ProcessIdentity(1234, "ps", datetime(2026, 11, 27, 10).timestamp())
    assert calls == [
        (
            (["ps", "-o", "lstart=", "-p", "1234"],),
            {"check": True, "capture_output": True, "text": True, "timeout": 2},
        )
    ]
