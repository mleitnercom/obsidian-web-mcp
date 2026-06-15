"""SSRF-hardened HTTP(S) fetcher for ``vault_import_url``.

A naive "resolve the hostname, check the IP, then hand the URL to urllib" guard is defeatable
two ways, both closed here:

1. DNS rebinding / TOCTOU. The validation resolves the name and the HTTP client resolves it
   again independently; a malicious DNS returns a public IP to the check and a private IP
   (169.254.169.254, 127.0.0.1) to the connect. Closed by resolving exactly once and *pinning*
   the connection to the validated IP, while preserving the original Host header and TLS
   SNI/cert hostname.

2. Redirects. ``urlopen`` follows 30x automatically and only the first URL is validated, so a
   public URL can redirect to an internal one. Closed by disabling auto-redirects and
   re-validating + re-pinning every hop, with a hop budget.

Defense in depth: scheme allowlist (http/https), port allowlist, public-IP-only by default
(``is_global``, IPv4-mapped IPv6 unwrapped), and a hard byte cap on the actual read.

stdlib only. This module is mirrored by the obsidian-vault-mcp-ext ImportExtension so the
hardening stays in sync between fork and extension.
"""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlparse

_REDIRECT_CODES = {301, 302, 303, 307, 308}
_DEFAULT_PORTS = {"http": 80, "https": 443}


class ImportSecurityError(Exception):
    """Raised when a URL (or a redirect hop) is rejected by the SSRF guards."""


class ImportFetchError(Exception):
    """Raised on a transport/protocol failure or a non-success status."""


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """Positive allowlist: only globally-routable unicast addresses pass.

    IPv4-mapped IPv6 is unwrapped so ``::ffff:169.254.169.254`` cannot smuggle a link-local
    target past the check."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def _resolve_and_pin(host: str, port: int, allow_private: bool) -> tuple[int, str]:
    """Resolve the host ONCE, validate EVERY returned address, return (family, ip) to pin."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ImportFetchError(f"Could not resolve hostname: {host}") from exc
    if not infos:
        raise ImportFetchError(f"Could not resolve hostname: {host}")
    pinned: tuple[int, str] | None = None
    for family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        if not allow_private:
            ip = ipaddress.ip_address(ip_str)
            if not _ip_is_public(ip):
                raise ImportSecurityError(
                    f"URL resolves to a non-public address ({ip_str}); set "
                    "VAULT_IMPORT_URL_ALLOW_PRIVATE=true to opt in"
                )
        if pinned is None:
            pinned = (family, ip_str)
    assert pinned is not None
    return pinned


def _validate_url(url: str, allowed_ports: set[int]) -> tuple[str, str, int]:
    """Validate scheme/host/port of one URL (or hop). Returns (scheme, host, port)."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ImportSecurityError(f"Only http and https URLs are supported (got {scheme!r})")
    host = parsed.hostname
    if not host:
        raise ImportSecurityError("URL must include a hostname")
    port = parsed.port or _DEFAULT_PORTS[scheme]
    if port not in allowed_ports:
        raise ImportSecurityError(
            f"Port {port} is not in the allowed import ports {sorted(allowed_ports)}"
        )
    return scheme, host, port


def _open_connection(
    scheme: str, host: str, pinned_ip: str, port: int, timeout: float
) -> http.client.HTTPConnection:
    """Connect to the pinned IP but speak to the original host (Host header + TLS SNI/cert)."""
    if scheme == "https":
        context = ssl.create_default_context()

        class _PinnedHTTPSConnection(http.client.HTTPSConnection):
            def connect(self_inner):  # noqa: N805
                sock = socket.create_connection((pinned_ip, port), timeout)
                self_inner.sock = context.wrap_socket(sock, server_hostname=host)

        return _PinnedHTTPSConnection(host, port, timeout=timeout)

    class _PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self_inner):  # noqa: N805
            self_inner.sock = socket.create_connection((pinned_ip, port), timeout)

    return _PinnedHTTPConnection(host, port, timeout=timeout)


def _read_capped(response, max_bytes: int) -> bytes:
    """Read the body in chunks, raising once the cap is exceeded (do not trust headers)."""
    data = bytearray()
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ImportFetchError(f"Downloaded content exceeds limit of {max_bytes} bytes")
    return bytes(data)


def fetch_url(
    url: str,
    *,
    allow_private: bool,
    allowed_ports: set[int],
    max_bytes: int,
    max_redirects: int,
    timeout: float,
    user_agent: str = "obsidian-web-mcp/attachment-import",
) -> tuple[str | None, bytes]:
    """Fetch a URL with full SSRF hardening. Returns (content_type, body_bytes)."""
    current = url
    for _hop in range(max_redirects + 1):
        scheme, host, port = _validate_url(current, allowed_ports)
        _family, pinned_ip = _resolve_and_pin(host, port, allow_private)
        conn = _open_connection(scheme, host, pinned_ip, port, timeout)
        try:
            parsed = urlparse(current)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"
            conn.putrequest("GET", target, skip_accept_encoding=True)
            conn.putheader("User-Agent", user_agent)
            conn.putheader("Accept", "*/*")
            conn.endheaders()
            response = conn.getresponse()
            status = response.status
            if status in _REDIRECT_CODES:
                location = response.headers.get("Location")
                response.read()
                if not location:
                    raise ImportFetchError(f"Redirect {status} without a Location header")
                current = urljoin(current, location)
                continue
            if status != 200:
                raise ImportFetchError(f"URL returned HTTP {status}")
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() or None
            data = _read_capped(response, max_bytes)
            return content_type, data
        finally:
            conn.close()
    raise ImportSecurityError(f"Too many redirects (limit {max_redirects})")
