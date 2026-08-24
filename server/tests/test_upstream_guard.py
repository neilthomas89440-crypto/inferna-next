"""Unit tests for SSRF guard (normalize + policy)."""

from __future__ import annotations

import ipaddress

import pytest

from inferna_server.config import Settings
from inferna_server.services import upstream_guard
from inferna_server.services.upstream_guard import (
    assert_upstream_allowed,
    normalize_worker_address,
    validate_worker_address,
)


def _prod_settings(**overrides) -> Settings:
    base: dict[str, object] = {
        "environment": "production",
        "jwt_secret": "x",
        "admin_password": "y",
        "registration_token": "z",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return Settings(**base)  # type: ignore[arg-type]


def _dev_settings(**overrides) -> Settings:
    base: dict[str, object] = {
        "environment": "development",
        "jwt_secret": "x",
        "admin_password": "y",
        "registration_token": "z",
    }
    base.update(overrides)  # type: ignore[arg-type]
    return Settings(**base)  # type: ignore[arg-type]


# --- normalize ---


def test_normalize_bare_host() -> None:
    assert normalize_worker_address("example.com") == "http://example.com"


def test_normalize_https_stays() -> None:
    assert normalize_worker_address("https://example.com") == "https://example.com"


def test_normalize_file_rejected() -> None:
    with pytest.raises(ValueError, match="scheme"):
        normalize_worker_address("file:///etc/passwd")


def test_normalize_ftp_rejected() -> None:
    with pytest.raises(ValueError, match="scheme"):
        normalize_worker_address("ftp://example.com")


def test_normalize_port_not_allowed() -> None:
    with pytest.raises(ValueError, match="port not allowed"):
        normalize_worker_address("host:8080")
    with pytest.raises(ValueError, match="port not allowed"):
        normalize_worker_address("http://host:8080")


def test_normalize_userpass_rejected() -> None:
    with pytest.raises(ValueError, match="username"):
        normalize_worker_address("http://user:pass@host")


def test_normalize_path_rejected() -> None:
    with pytest.raises(ValueError, match="path not allowed"):
        normalize_worker_address("http://host/path")


def test_normalize_query_rejected() -> None:
    with pytest.raises(ValueError, match="query"):
        normalize_worker_address("http://host?q=1")


def test_normalize_fragment_rejected() -> None:
    with pytest.raises(ValueError, match="fragment"):
        normalize_worker_address("http://host#frag")


def test_normalize_empty() -> None:
    assert normalize_worker_address("") == ""
    assert normalize_worker_address("   ") == ""


def test_normalize_trailing_slash_allowed() -> None:
    # "/" path is allowed and stripped
    assert normalize_worker_address("http://host/") == "http://host"


# --- assert_upstream_allowed / validate_worker_address policy ---

_PROD_BLOCKED = [
    "127.0.0.1",
    "169.254.169.254",
    "10.1.2.3",
    "192.168.1.1",
    "::1",
    "::ffff:127.0.0.1",
]
_PROD_ALLOWED = ["8.8.8.8", "1.1.1.1"]


@pytest.mark.parametrize("ip", _PROD_BLOCKED)
async def test_assert_blocked_ips_production(ip: str) -> None:
    settings = _prod_settings()
    with pytest.raises(ValueError, match="blocked"):
        await assert_upstream_allowed(ip, settings)


@pytest.mark.parametrize("ip", _PROD_ALLOWED)
async def test_assert_allowed_ips_production(ip: str) -> None:
    settings = _prod_settings()
    # should not raise
    await assert_upstream_allowed(ip, settings)


async def test_assert_development_allows_private() -> None:
    settings = _dev_settings()
    await assert_upstream_allowed("127.0.0.1", settings)
    await assert_upstream_allowed("10.1.2.3", settings)


async def test_assert_allowlist_cidr_allows_and_rejects() -> None:
    settings = _prod_settings(gateway_upstream_allowlist="127.0.0.0/8")
    await assert_upstream_allowed("127.0.0.1", settings)
    with pytest.raises(ValueError, match="allowlist"):
        await assert_upstream_allowed("8.8.8.8", settings)


async def test_validate_blocked_and_allowed() -> None:
    prod = _prod_settings()
    for ip in _PROD_BLOCKED:
        with pytest.raises(ValueError):
            await validate_worker_address(ip, prod)
    for ip in _PROD_ALLOWED:
        out = await validate_worker_address(ip, prod)
        assert out == f"http://{ip}" if ":" not in ip else f"http://{ip}"


async def test_validate_development_allows() -> None:
    dev = _dev_settings()
    out = await validate_worker_address("127.0.0.1", dev)
    assert out == "http://127.0.0.1"


async def test_hostname_resolves_to_blocked_rejected(monkeypatch) -> None:
    settings = _prod_settings()

    async def fake_resolve(host: str):
        return [ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_resolve)
    with pytest.raises(ValueError, match="blocked"):
        await assert_upstream_allowed("evil.example", settings)
    with pytest.raises(ValueError):
        await validate_worker_address("evil.example", settings)


async def test_hostname_allowlist_exact_match(monkeypatch) -> None:
    settings = _prod_settings(gateway_upstream_allowlist="metadata.local")

    async def fake_resolve(host: str):
        return [ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_resolve)
    # hostname exactly matches allowlist → allowed even though IP is blocked
    await assert_upstream_allowed("metadata.local", settings)
    await assert_upstream_allowed("METADATA.LOCAL.", settings)  # case-insensitive + trailing dot
    # different hostname → rejected
    with pytest.raises(ValueError, match="allowlist"):
        await assert_upstream_allowed("other.local", settings)


async def test_hostname_allowlist_ip_cidr_via_resolve(monkeypatch) -> None:
    # allowlist is CIDR, hostname resolves to IP inside CIDR → allowed
    settings = _prod_settings(gateway_upstream_allowlist="169.254.0.0/16")

    async def fake_resolve(host: str):
        return [ipaddress.ip_address("169.254.169.254")]

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_resolve)
    await assert_upstream_allowed("some.host", settings)
    out = await validate_worker_address("some.host", settings)
    assert out == "http://some.host"


async def test_allowlist_ip_literal_match(monkeypatch) -> None:
    settings = _prod_settings(gateway_upstream_allowlist="8.8.8.8")

    # 8.8.8.8 literal should be allowed via IP match
    await assert_upstream_allowed("8.8.8.8", settings)
    # different IP should be rejected
    with pytest.raises(ValueError):
        await assert_upstream_allowed("1.1.1.1", settings)


async def test_unresolvable_hostname_rejected_in_production(monkeypatch) -> None:
    settings = _prod_settings()

    async def fake_empty(host: str):
        return []

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_empty)
    # Unresolvable in production → reject: passing the hostname through would
    # re-resolve DNS at connect time (DNS-rebinding bypass).
    with pytest.raises(ValueError, match="unresolvable"):
        await assert_upstream_allowed("unresolvable.invalid", settings)
    with pytest.raises(ValueError, match="unresolvable"):
        await validate_worker_address("unresolvable.invalid", settings)


async def test_unresolvable_hostname_allowed_in_development(monkeypatch) -> None:
    settings = _dev_settings()

    async def fake_empty(host: str):
        return []

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_empty)
    # Dev keeps the old behaviour: pass the hostname through (connection
    # fails naturally if it truly does not resolve).
    await assert_upstream_allowed("unresolvable.invalid", settings)
    out = await validate_worker_address("unresolvable.invalid", settings)
    assert out == "http://unresolvable.invalid"


async def test_allowlisted_hostname_unresolvable_rejected_in_production(
    monkeypatch,
) -> None:
    settings = _prod_settings(gateway_upstream_allowlist="trusted.local")

    async def fake_empty(host: str):
        return []

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_empty)
    # Exact allowlist hostname match with DNS failure: no IP to pin → reject.
    with pytest.raises(ValueError, match="unresolvable"):
        await assert_upstream_allowed("trusted.local", settings)


# --- multi-IP resolution semantics (security review T2) ---


def _patched_resolver(monkeypatch, ips: list[str]) -> None:
    async def fake_resolve(host: str):
        return [ipaddress.ip_address(ip) for ip in ips]

    monkeypatch.setattr(upstream_guard, "resolve_host_ips", fake_resolve)


async def test_allowlist_multi_ip_all_match_required(monkeypatch) -> None:
    # Allowlist policy uses ALL-match semantics: one non-allowlisted IP rejects.
    settings = _prod_settings(gateway_upstream_allowlist="8.8.8.8")
    _patched_resolver(monkeypatch, ["8.8.8.8", "1.1.1.1"])
    with pytest.raises(ValueError, match="non-allowlisted"):
        await assert_upstream_allowed("multi.example", settings)


async def test_allowlist_blocked_ip_alone_rejected(monkeypatch) -> None:
    settings = _prod_settings(gateway_upstream_allowlist="8.8.8.8")
    _patched_resolver(monkeypatch, ["169.254.169.254"])
    with pytest.raises(ValueError, match="non-allowlisted"):
        await assert_upstream_allowed("evil.example", settings)


async def test_allowlist_allowed_ip_alone_accepted(monkeypatch) -> None:
    settings = _prod_settings(gateway_upstream_allowlist="8.8.8.8")
    _patched_resolver(monkeypatch, ["8.8.8.8"])
    assert await upstream_guard.resolve_and_validate("ok.example", settings) == "8.8.8.8"


async def test_prod_empty_allowlist_any_blocked_rejected(monkeypatch) -> None:
    # Production, empty allowlist: ANY blocked IP among the resolved set rejects.
    settings = _prod_settings()
    _patched_resolver(monkeypatch, ["8.8.8.8", "169.254.169.254"])
    with pytest.raises(ValueError, match="blocked"):
        await assert_upstream_allowed("mixed.example", settings)
