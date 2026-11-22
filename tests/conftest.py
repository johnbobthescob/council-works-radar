"""Shared fixtures. The network guard (PLAN §9.12 / §11.2) blocks non-loopback sockets unless RADAR_LIVE=1."""

import ipaddress
import os
import socket

import pytest

_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect


def _is_local(host) -> bool:
    if host in ("localhost", None, ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class NetworkBlocked(RuntimeError):
    pass


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs the real network (RADAR_LIVE=1)")


@pytest.fixture(autouse=True)
def network_guard(monkeypatch):
    if os.environ.get("RADAR_LIVE") == "1":
        yield
        return

    def guarded_getaddrinfo(host, *a, **kw):
        if not _is_local(host):
            raise NetworkBlocked(f"network access blocked in tests: {host}")
        return _real_getaddrinfo(host, *a, **kw)

    def guarded_connect(self, address):
        if isinstance(address, tuple) and not _is_local(address[0]):
            raise NetworkBlocked(f"network access blocked in tests: {address[0]}")
        return _real_connect(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
