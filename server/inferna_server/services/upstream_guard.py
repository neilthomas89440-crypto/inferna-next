"""SSRF guard for worker-registered upstreams and gateway proxy targets."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from inferna_server.config import Settings

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
]


def normalize_worker_address(raw: str) -> str:
    """Validate and normalize a worker-registered address.

    Returns "" for empty input, otherwise ``scheme://hostname`` with
    scheme in {http, https} and no port/path/query/fragment/userinfo.
    """
    s = raw.strip()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        parsed = urlsplit(s)
    except ValueError as exc:
        raise ValueError(f"invalid worker address {raw!r}: {exc}") from None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme {scheme!r}; only http and https are allowed")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("missing hostname in worker address")
    if parsed.username or parsed.password:
        raise ValueError("username/password not allowed in worker address")
    if parsed.path not in ("", "/"):
        raise ValueError(f"path not allowed in worker address: {parsed.path!r}")
    if parsed.query:
        raise ValueError("query not allowed in worker address")
    if parsed.fragment:
        raise ValueError("fragment not allowed in worker address")
    if parsed.port is not None:
        raise ValueError("port not allowed in worker address; server allocates instance ports")
    return f"{scheme}://{hostname}"


async def resolve_host_ips(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve *host* to unique IP addresses; unresolvable → []."""
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]  # type: ignore[index]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if *ip* is loopback/link-local/private/unspecified/broadcast/multicast."""
    try:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
    except Exception:
        pass
    for net in _BLOCKED_NETWORKS:
        try:
            if ip in net:  # type: ignore[operator]
                return True
        except TypeError:
            # version mismatch (v4 vs v6)
            continue
    return False


def _extract_host(target: str) -> str:
    raw = target.strip()
    if not raw:
        raise ValueError("empty upstream target")
    parse_target = raw if "://" in raw else "http://" + raw
    try:
        parsed = urlsplit(parse_target)
    except ValueError as exc:
        raise ValueError(f"invalid upstream target {target!r}: {exc}") from None
    host = parsed.hostname
    if host:
        return host
    # Fallback for bare IPv6 literals without brackets (e.g. "::1", "::ffff:127.0.0.1")
    # urlsplit treats them as missing hostname; try direct IP parsing.
    candidate = raw
    # Strip scheme if present and fallback still failed (e.g. "http://::1")
    if "://" in raw:
        # Extract after "://"
        candidate = raw.split("://", 1)[1].split("/")[0].split("?")[0].split("#")[0]
    # Remove brackets
    candidate = candidate.strip()
    if candidate.startswith("["):
        try:
            end = candidate.index("]")
            ip_part = candidate[1:end]
            ipaddress.ip_address(ip_part)
            return ip_part
        except Exception:
            pass
    # Try bare IP (with optional port for IPv4/hostname)
    # For IPv6 without brackets, the whole candidate may be IP
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    # Try host:port split for IPv4/hostname
    if ":" in candidate and candidate.count(":") == 1:
        host_part, port_part = candidate.rsplit(":", 1)
        if port_part.isdigit():
            try:
                ipaddress.ip_address(host_part)
                return host_part
            except ValueError:
                return host_part
    raise ValueError(f"invalid upstream target {target!r}: missing hostname")


def _unwrap_ipv4_mapped(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the embedded IPv4 for IPv4-mapped IPv6 addresses; else *ip*."""
    try:
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
    except Exception:
        pass
    return ip


def _ip_allowed_by_allowlist(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allowlist: list[str]
) -> bool:
    """True if *ip* (already IPv4-mapped-unwrapped) matches a literal/CIDR entry."""
    for entry in allowlist:
        if "/" in entry:
            try:
                net = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            try:
                if ip in net:
                    return True
            except TypeError:
                # version mismatch (v4 vs v6)
                continue
        else:
            try:
                lit = ipaddress.ip_address(entry)
            except ValueError:
                continue
            if ip == lit:
                return True
    return False


def hostname_in_allowlist(host: str, settings: Settings) -> bool:
    """True if *host* exactly matches a hostname entry of the allowlist."""
    host_norm = host.lower().rstrip(".")
    for entry in settings.gateway_upstream_allowlist_entries:
        if "/" in entry:
            continue
        try:
            ipaddress.ip_address(entry)
            continue  # IP literal entry, not hostname
        except ValueError:
            pass
        if host_norm == entry.lower().rstrip("."):
            return True
    return False


# Alias kept for callers referring to the helper by its descriptive name.
hostname_allowlisted = hostname_in_allowlist


def _prefer_ipv4(
    ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Prefer the first IPv4 address; fall back to the first address overall."""
    for ip in ips:
        if isinstance(ip, ipaddress.IPv4Address):
            return ip
    return ips[0]


def _ip_to_pin(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str:
    """Render an IP for a URL: bracketed for IPv6, plain for IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        return f"[{ip}]"
    return str(ip)


async def resolve_and_validate(target: str, settings: Settings) -> str:
    """Resolve *target*, enforce SSRF policy, and return a pinned connect address.

    The returned string is either a concrete IP (IPv6 wrapped in ``[...]``) to
    connect to, or — in the development unresolvable-hostname case and the
    HTTPS no-pin case handled by callers — the original hostname string, which
    the caller passes through unchanged. Pinning the validated IP in the
    request URL prevents a connect-time DNS re-resolution: the class of SSRF
    where a hostname resolves to an allowed IP at check time but to a blocked
    IP at connect time.

    Behaviour:
    * Literal IP: checked directly against the allowlist (if set) or blocked
      networks (production, empty allowlist). IPv4-mapped IPv6 is unwrapped.
    * Allowlist set:
        - exact hostname match (case-insensitive, trailing dot stripped) pins
          the first resolved IP without further IP validation — the hostname is
          the trust anchor; if DNS fails, reject in production (no IP to pin)
          and pass the hostname through in development;
        - otherwise EVERY resolved IP must be allowlisted (not ANY): reject if
          any is not; if unresolvable, reject.
    * Allowlist empty + development: return first resolved IP, or the host if
      DNS fails.
    * Allowlist empty + production: reject if ANY resolved IP is blocked; reject
      unresolvable hostnames (no IP to validate — allowing them would re-resolve
      DNS at connect time, a DNS-rebinding bypass).
    """
    host = _extract_host(target)
    allowlist = settings.gateway_upstream_allowlist_entries

    # Literal-IP fast path (also unwraps IPv4-mapped IPv6).
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        ip = _unwrap_ipv4_mapped(literal_ip)
        if allowlist:
            if _ip_allowed_by_allowlist(ip, allowlist):
                return _ip_to_pin(ip)
            raise ValueError(f"upstream target {host!r} not in allowlist")
        if settings.environment == "development":
            return _ip_to_pin(ip)
        if _ip_is_blocked(ip):
            raise ValueError(f"upstream target {host!r} resolves to blocked address {ip}")
        return _ip_to_pin(ip)

    # Hostname (non-literal) path.
    if allowlist:
        host_norm = host.lower().rstrip(".")
        # 1) exact hostname allowlist match → trust the hostname; pin first IP.
        for entry in allowlist:
            if "/" in entry:
                continue
            try:
                ipaddress.ip_address(entry)
                continue  # IP literal, not a hostname entry
            except ValueError:
                pass
            if host_norm == entry.lower().rstrip("."):
                ips = await resolve_host_ips(host)
                if ips:
                    return _ip_to_pin(_prefer_ipv4(ips))
                # DNS failed: no IP to pin. Passing the hostname through would
                # re-resolve DNS at connect time (TOCTOU rebinding), so reject
                # in production; development may pass through.
                if settings.environment == "production":
                    raise ValueError(
                        f"upstream target {host!r} unresolvable (allowlist hostname)"
                    )
                return host
        # 2) IP/CIDR match: EVERY resolved IP must be allowlisted.
        ips = await resolve_host_ips(host)
        if not ips:
            raise ValueError(f"upstream target {host!r} not in allowlist (unresolvable)")
        for raw_ip in ips:
            ip = _unwrap_ipv4_mapped(raw_ip)
            if not _ip_allowed_by_allowlist(ip, allowlist):
                raise ValueError(
                    f"upstream target {host!r} resolves to non-allowlisted address {ip}"
                )
        return _ip_to_pin(_prefer_ipv4(ips))

    # Empty allowlist.
    if settings.environment == "development":
        ips = await resolve_host_ips(host)
        if ips:
            return _ip_to_pin(_prefer_ipv4(ips))
        return host
    # production: reject if ANY resolved IP is blocked.
    ips = await resolve_host_ips(host)
    if not ips:
        # Production: an unresolvable hostname cannot be validated here, so the
        # connection would re-resolve DNS at connect time — the DNS-rebinding
        # hole where a later lookup returns a blocked (e.g. link-local) address.
        raise ValueError(f"upstream target {host!r} unresolvable in production")
    for raw_ip in ips:
        ip = _unwrap_ipv4_mapped(raw_ip)
        if _ip_is_blocked(ip):
            raise ValueError(f"upstream target {host!r} resolves to blocked address {ip}")
    return _ip_to_pin(_prefer_ipv4(ips))


async def assert_upstream_allowed(target: str, settings: Settings) -> None:
    """Raise ValueError if *target* is not allowed under current policy.

    Legacy API kept for backward compatibility; delegates to
    :func:`resolve_and_validate` (which raises on rejection).
    """
    await resolve_and_validate(target, settings)


async def validate_worker_address(raw: str, settings: Settings) -> str:
    """Normalize and policy-check a worker address; returns normalized form."""
    normalized = normalize_worker_address(raw)
    if not normalized:
        return normalized
    # If host is IP literal, use assert_upstream_allowed directly; otherwise resolve.
    # The spec requires hostname path to resolve and check blocked/allowlist via same logic.
    # Delegating to assert_upstream_allowed satisfies both.
    await assert_upstream_allowed(normalized, settings)
    return normalized
