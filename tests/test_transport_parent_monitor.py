from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time
from contextlib import suppress
from types import SimpleNamespace

import pytest

from headroom.transport.protocol import ProcessIdentity, _ps_process_identity
from headroom.transport.runtime import (
    TransportLease,
    _ControlChannel,
    _monitor_control,
    _monitor_parent,
)


@pytest.fixture
def parent() -> ProcessIdentity:
    identity = _ps_process_identity(os.getpid())
    assert identity is not None
    return identity


def _timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd=["ps"], timeout=2)


async def _wait_until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=2)


def test_unknown_observation_keeps_acquisition_fail_closed(parent, monkeypatch):
    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", _timeout)
    assert parent.matches_live_process() is False
    assert parent.observe_live_process() is None


def test_missing_process_is_confirmed_dead(monkeypatch):
    parent = ProcessIdentity(pid=999999, start_source="ps", start_time=1)
    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", _timeout)
    assert parent.observe_live_process() is False


def test_reused_pid_is_confirmed_mismatch(parent, monkeypatch):
    stamp = time.strftime("%a %b %d %H:%M:%S %Y", time.localtime(parent.start_time + 1))
    monkeypatch.setattr(
        "headroom.transport.protocol.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["ps"], 0, stamp, ""),
    )
    assert parent.observe_live_process() is False


def test_permission_error_is_not_parent_death(parent, monkeypatch):
    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", _timeout)

    def denied(pid, sig):
        raise PermissionError("cannot inspect process")

    monkeypatch.setattr("headroom.transport.protocol.os.kill", denied)
    assert parent.observe_live_process() is None


@pytest.mark.asyncio
async def test_single_ps_timeout_recovers_without_stopping_live_transport(
    parent, monkeypatch, caplog
):
    run = subprocess.run
    calls = 0

    def transient(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _timeout()
        return run(*args, **kwargs)

    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", transient)
    caplog.set_level(logging.INFO, logger="headroom.transport.runtime")
    server = SimpleNamespace(should_exit=False)
    task = asyncio.create_task(_monitor_parent(parent, server, interval=0.001))
    try:
        await _wait_until(lambda: "parent_observation_recovered" in caplog.text or task.done())
        os.kill(parent.pid, 0)
        assert server.should_exit is False
        assert not task.done()
        assert "parent_observation_unavailable" in caplog.text
        assert "parent_observation_recovered" in caplog.text
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_slow_parent_observation_does_not_block_event_loop(parent, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(timeout=0.5)
        finished.set()
        return _timeout()

    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", slow)
    server = SimpleNamespace(should_exit=False)
    task = asyncio.create_task(_monitor_parent(parent, server, interval=0.001))
    try:
        await _wait_until(entered.is_set)
        assert not finished.is_set(), "parent observation blocked the event loop"
        assert not server.should_exit
    finally:
        release.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_confirmed_parent_death_stops_transport(monkeypatch, caplog):
    parent = ProcessIdentity(pid=999999, start_source="ps", start_time=1)
    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", _timeout)
    caplog.set_level(logging.INFO, logger="headroom.transport.runtime")
    server = SimpleNamespace(should_exit=False)
    await asyncio.wait_for(_monitor_parent(parent, server, interval=0.001), timeout=2)
    assert server.should_exit is True
    assert "reason=parent_not_live" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("release", [False, True])
async def test_control_channel_still_ends_lease(release, caplog):
    control, peer = socket.socketpair()
    caplog.set_level(logging.INFO, logger="headroom.transport.runtime")
    server = SimpleNamespace(should_exit=False, force_exit=False)
    lease = TransportLease("lease-test")
    try:
        if release:
            peer.sendall(
                json.dumps(
                    {"schema": "headroom.transport.release.v1", "lease_id": "lease-test"}
                ).encode()
                + b"\n"
            )
        else:
            peer.close()
        await asyncio.wait_for(_monitor_control(_ControlChannel(control), lease, server), 2)
        assert server.should_exit is True
        assert lease.released is release
        assert f"reason={'lease_release' if release else 'control_eof'}" in caplog.text
    finally:
        control.close()
        peer.close()
