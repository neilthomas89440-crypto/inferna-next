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


async def assert_upstream_allowed(target: str, settings: Settings) -> None:
    """Raise ValueError if *target* is not allowed under current policy."""
    host = _extract_host(target)
    allowlist = settings.gateway_upstream_allowlist_entries

    if allowlist:
        host_norm = host.lower().rstrip(".")
        # 1) exact hostname match (entries that are hostnames)
        for entry in allowlist:
            if "/" in entry:
                continue
            try:
                ipaddress.ip_address(entry)
                continue  # IP literal, not hostname
            except ValueError:
                pass
            if host_norm == entry.lower().rstrip("."):
                return
        # 2) IP / CIDR match via DNS
        ips = await resolve_host_ips(host)
        if not ips:
            try:
                literal = ipaddress.ip_address(host)
                ips = [literal]
            except ValueError:
                pass
        for ip in ips:
            check_ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ip
            try:
                if isinstance(check_ip, ipaddress.IPv6Address) and check_ip.ipv4_mapped is not None:
                    check_ip = check_ip.ipv4_mapped
            except Exception:
                pass
            for entry in allowlist:
                if "/" in entry:
                    try:
                        net = ipaddress.ip_network(entry, strict=False)
                    except ValueError:
                        continue
                    try:
                        if check_ip in net:
                            return
                    except TypeError:
                        continue
                else:
                    try:
                        lit = ipaddress.ip_address(entry)
                    except ValueError:
                        continue
                    if check_ip == lit or ip == lit:
                        return
                    try:
                        if (
                            isinstance(ip, ipaddress.IPv6Address)
                            and ip.ipv4_mapped is not None
                            and ip.ipv4_mapped == lit
                        ):
                            return
                    except Exception:
                        pass
        raise ValueError(f"upstream target {host!r} not in allowlist")

    # Empty allowlist
    if settings.environment == "development":
        return
    # production: reject if any resolved IP is blocked
    ips = await resolve_host_ips(host)
    if not ips:
        try:
            lit = ipaddress.ip_address(host)
            if _ip_is_blocked(lit):
                raise ValueError(f"upstream target {host!r} resolves to blocked address {lit}")
            return
        except ValueError:
            return
    for ip in ips:
        if _ip_is_blocked(ip):
            raise ValueError(f"upstream target {host!r} resolves to blocked address {ip}")
    return


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
