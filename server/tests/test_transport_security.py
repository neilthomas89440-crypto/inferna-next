"""Transport security and version compatibility: the two things a release gate has to prove.

The TLS test is a real handshake, not a config assertion: it generates a self-signed certificate for
`localhost` with openssl, starts the server's gRPC endpoint with it through `serve_grpc`, and talks to
it with a client that trusts that CA. If the credentials were wired wrong (certificate and key
swapped, insecure port used, wrong authority), the RPC cannot arrive at the servicer — so the
assertions below are about a working channel, not about configuration plumbing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import grpc
import pytest
from sqlalchemy import select

from inferna_server.config import Settings
from inferna_server.grpc_server import _grpc_credentials, serve_grpc
from inferna_server.models import Cluster
from inferna_server.proto import cluster_pb2, cluster_pb2_grpc
from inferna_server.services.workers_svc import register_worker
from inferna_server.version import MIN_WORKER_VERSION, PROTOCOL_VERSION


def _register_request(**overrides) -> cluster_pb2.RegisterRequest:
    payload = {
        "cluster_token": "inferna-registration-token",
        "hostname": "tls-host",
        "worker_name": "",
        "cluster_name": "default",
        "version": "0.2.0",
        "protocol_version": PROTOCOL_VERSION,
        "address": "",
    }
    payload.update(overrides)
    return cluster_pb2.RegisterRequest(**payload)


# --- version gate (previously untested) ---


async def test_register_rejects_an_unsupported_protocol_version(db) -> None:
    with pytest.raises(grpc.RpcError) as excinfo:
        await register_worker(db, _register_request(protocol_version=PROTOCOL_VERSION + 1))

    assert excinfo.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "protocol version" in (excinfo.value.details() or "")


async def test_register_rejects_too_old_and_unparsable_worker_versions(db) -> None:
    older = f"{MIN_WORKER_VERSION[0]}.{max(0, MIN_WORKER_VERSION[1] - 1)}.0"

    with pytest.raises(grpc.RpcError) as too_old:
        await register_worker(db, _register_request(version=older))
    with pytest.raises(grpc.RpcError) as garbage:
        await register_worker(db, _register_request(version="not-a-version"))

    assert too_old.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "too old" in (too_old.value.details() or "")
    # An unparsable version is treated as older than the minimum rather than trusted.
    assert garbage.value.code() == grpc.StatusCode.FAILED_PRECONDITION


async def test_register_accepts_the_current_versions(db) -> None:
    response = await register_worker(db, _register_request())

    assert response.worker_id
    assert response.worker_token
    cluster = (await db.execute(select(Cluster).where(Cluster.name == "default"))).scalar_one()
    assert cluster.id  # the registration landed in the default cluster


# --- TLS material ---


def _settings(
    *,
    environment: str = "development",
    grpc_tls_cert: str = "",
    grpc_tls_key: str = "",
    grpc_tls_ca: str = "",
    grpc_tls_require_client_cert: bool = False,
) -> Settings:
    """Explicit kwargs: `extra="ignore"` on Settings means a typo would silently do nothing."""
    return Settings(
        environment=environment,
        grpc_tls_cert=grpc_tls_cert,
        grpc_tls_key=grpc_tls_key,
        grpc_tls_ca=grpc_tls_ca,
        grpc_tls_require_client_cert=grpc_tls_require_client_cert,
    )


def test_tls_settings_require_a_matching_key_and_ca() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        _settings(grpc_tls_cert="/tmp/cert.pem")
    with pytest.raises(ValueError, match="REQUIRE_CLIENT_CERT needs"):
        _settings(grpc_tls_require_client_cert=True)

    assert _settings().grpc_tls_enabled is False
    assert _settings(grpc_tls_cert="c", grpc_tls_key="k").grpc_tls_enabled is True


def test_credentials_are_none_without_material(tmp_path: Path) -> None:
    assert _grpc_credentials(_settings()) is None


def test_credentials_fail_fast_on_unreadable_files(tmp_path: Path) -> None:
    settings = _settings(grpc_tls_cert=str(tmp_path / "missing.pem"), grpc_tls_key="k")

    with pytest.raises(RuntimeError, match="cannot read the gRPC TLS material"):
        _grpc_credentials(settings)


def _make_certificates(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Self-signed certificate for `localhost` (+SAN), its key and a CA copy of the same cert."""
    cert = tmp_path / "server.pem"
    key = tmp_path / "server.key"
    openssl = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-subj",
        "/CN=localhost",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ]
    try:
        subprocess.run(openssl, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"openssl unavailable: {exc}")
    return cert, key, cert


async def test_secure_grpc_endpoint_accepts_a_tls_client(tmp_path: Path, monkeypatch) -> None:
    """A real handshake: the RPC reaches the servicer only if the credentials are wired correctly."""
    from inferna_server.grpc_server import WorkerService

    cert, key, ca = _make_certificates(tmp_path)
    settings = _settings(grpc_tls_cert=str(cert), grpc_tls_key=str(key), grpc_tls_ca=str(ca))
    credentials = _grpc_credentials(settings)
    assert credentials is not None
    server = grpc.aio.server()
    cluster_pb2_grpc.add_WorkerServiceServicer_to_server(WorkerService(), server)
    port = server.add_secure_port("127.0.0.1:0", credentials)
    await server.start()
    client_credentials = grpc.ssl_channel_credentials(root_certificates=ca.read_bytes())
    try:
        async with grpc.aio.secure_channel(
            f"localhost:{port}",
            client_credentials,
            options=[("grpc.ssl_target_name_override", "localhost")],
        ) as channel:
            stub = cluster_pb2_grpc.WorkerServiceStub(channel)
            with pytest.raises(grpc.RpcError) as excinfo:
                await stub.Register(_register_request(cluster_token="wrong"))
    finally:
        await server.stop(0)

    # The handshake succeeded (otherwise the error would be UNAVAILABLE), and the servicer ran.
    assert excinfo.value.code() == grpc.StatusCode.PERMISSION_DENIED
    assert "invalid cluster token" in (excinfo.value.details() or "")


async def test_serve_grpc_binds_the_secure_port_when_tls_is_configured(monkeypatch) -> None:
    """The wiring choice itself: secure when a certificate is configured, insecure otherwise."""
    calls: list[str] = []

    class _FakeServer:
        """Records the port choice; every other server method grpc calls is a no-op."""

        def __getattr__(self, name: str):
            def _noop(*args: object, **kwargs: object) -> int:
                return 1

            return _noop

        def add_insecure_port(self, target: str) -> int:
            calls.append("insecure")
            return 1

        def add_secure_port(self, target: str, credentials: object) -> int:
            calls.append("secure")
            return 1

        async def start(self) -> None:
            return None

        async def wait_for_termination(self) -> None:
            raise asyncio.CancelledError

    monkeypatch.setattr("inferna_server.grpc_server.grpc.aio.server", lambda: _FakeServer())
    monkeypatch.setattr(
        "inferna_server.grpc_server._grpc_credentials",
        lambda settings: object() if settings.grpc_tls_enabled else None,
    )

    monkeypatch.setattr(
        "inferna_server.grpc_server.get_settings",
        lambda: _settings(grpc_tls_cert="cert.pem", grpc_tls_key="key.pem"),
    )
    with pytest.raises(asyncio.CancelledError):
        await serve_grpc()
    monkeypatch.setattr(
        "inferna_server.grpc_server.get_settings", lambda: _settings()
    )
    with pytest.raises(asyncio.CancelledError):
        await serve_grpc()

    assert calls == ["secure", "insecure"]
