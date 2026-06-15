"""SSRF-hardening regression tests for url_fetch.fetch_url.

Covers the two bypasses a naive guard leaves open: DNS rebinding / TOCTOU (closed by
single-resolution IP pinning) and redirect-to-internal (closed by per-hop re-validation).
No network is touched: DNS and the connection layer are substituted.
"""

import ipaddress
import socket

import pytest

from obsidian_vault_mcp import url_fetch


class _FakeHeaders:
    def __init__(self, mapping):
        self._m = {k.lower(): v for k, v in mapping.items()}

    def get(self, key, default=None):
        return self._m.get(key.lower(), default)


class _FakeResponse:
    def __init__(self, status, headers, body=b""):
        self.status = status
        self.headers = _FakeHeaders(headers)
        self._body = body
        self._read = False

    def read(self, n=-1):
        if self._read:
            return b""
        self._read = True
        return self._body


class _FakeConn:
    def __init__(self, response):
        self._response = response

    def putrequest(self, *a, **k):
        pass

    def putheader(self, *a, **k):
        pass

    def endheaders(self, *a, **k):
        pass

    def getresponse(self):
        return self._response

    def close(self):
        pass


def _install_dns(monkeypatch, mapping, counter=None):
    def fake_getaddrinfo(host, port, *a, **k):
        if counter is not None:
            counter.append(host)
        if host not in mapping:
            raise socket.gaierror(f"no fake DNS entry for {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (mapping[host], port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _install_conns(monkeypatch, responses, recorder=None):
    state = {"i": 0}

    def fake_open(scheme, host, pinned_ip, port, timeout):
        if recorder is not None:
            recorder.append({"scheme": scheme, "host": host, "pinned_ip": pinned_ip, "port": port})
        resp = responses[state["i"]]
        state["i"] += 1
        return _FakeConn(resp)

    monkeypatch.setattr(url_fetch, "_open_connection", fake_open)


_KW = dict(allow_private=False, allowed_ports={80, 443}, max_bytes=1_000_000, max_redirects=5, timeout=5)


@pytest.mark.parametrize("addr", ["8.8.8.8", "93.184.216.34"])
def test_public_ips_pass(addr):
    assert url_fetch._ip_is_public(ipaddress.ip_address(addr)) is True


@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fe80::1", "::ffff:169.254.169.254"],
)
def test_non_public_ips_rejected(addr):
    assert url_fetch._ip_is_public(ipaddress.ip_address(addr)) is False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://host/x", "gopher://h/"])
def test_validate_url_rejects_bad_scheme(url):
    with pytest.raises(url_fetch.ImportSecurityError):
        url_fetch._validate_url(url, {80, 443})


def test_validate_url_rejects_disallowed_port():
    with pytest.raises(url_fetch.ImportSecurityError):
        url_fetch._validate_url("http://example.com:8080/x", {80, 443})


def test_rebinding_closed_resolves_once_and_pins(monkeypatch):
    """SSRF #1: resolve exactly once and connect to that validated IP (no TOCTOU gap)."""
    calls, conns = [], []
    _install_dns(monkeypatch, {"good.example": "93.184.216.34"}, counter=calls)
    _install_conns(monkeypatch, [_FakeResponse(200, {"Content-Type": "image/png"}, b"PNG")], recorder=conns)

    ctype, data = url_fetch.fetch_url("http://good.example/a.png", **_KW)
    assert (ctype, data) == ("image/png", b"PNG")
    assert calls == ["good.example"]
    assert conns[0]["pinned_ip"] == "93.184.216.34"


def test_private_resolution_rejected(monkeypatch):
    _install_dns(monkeypatch, {"evil.example": "169.254.169.254"})
    with pytest.raises(url_fetch.ImportSecurityError):
        url_fetch.fetch_url("http://evil.example/x.png", **_KW)


def test_redirect_to_metadata_rejected(monkeypatch):
    """SSRF #2: a redirect to an internal target is re-validated and refused."""
    _install_dns(monkeypatch, {"good.example": "93.184.216.34", "169.254.169.254": "169.254.169.254"})
    _install_conns(monkeypatch, [_FakeResponse(302, {"Location": "http://169.254.169.254/latest/meta-data/"})])
    with pytest.raises(url_fetch.ImportSecurityError):
        url_fetch.fetch_url("http://good.example/a.png", **_KW)


def test_redirect_budget_enforced(monkeypatch):
    _install_dns(monkeypatch, {"good.example": "93.184.216.34"})
    responses = [_FakeResponse(302, {"Location": "http://good.example/again"}) for _ in range(10)]
    _install_conns(monkeypatch, responses)
    with pytest.raises(url_fetch.ImportSecurityError):
        url_fetch.fetch_url("http://good.example/a.png", **{**_KW, "max_redirects": 2})


def test_size_cap_enforced(monkeypatch):
    _install_dns(monkeypatch, {"good.example": "93.184.216.34"})
    _install_conns(monkeypatch, [_FakeResponse(200, {"Content-Type": "image/png"}, b"x" * 50)])
    with pytest.raises(url_fetch.ImportFetchError):
        url_fetch.fetch_url("http://good.example/a.png", **{**_KW, "max_bytes": 10})
