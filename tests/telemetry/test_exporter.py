import asyncio
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsPartialSuccess,
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.logs.v1.logs_service_pb2_grpc import (
    LogsServiceServicer,
    add_LogsServiceServicer_to_server,
)

from dst_server.telemetry.exporter import LogsExporter, OTLPSettings


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    ("values", "target", "insecure", "timeout", "compression"),
    [
        ({}, "localhost:4317", True, 10, "none"),
        (
            {"ENDPOINT": "https://collector:4317"},
            "collector:4317",
            False,
            10,
            "none",
        ),
        (
            {"ENDPOINT": "collector:4317", "INSECURE": "true"},
            "collector:4317",
            True,
            10,
            "none",
        ),
        (
            {"ENDPOINT": "http://collector:4317", "INSECURE": "false"},
            "collector:4317",
            False,
            10,
            "none",
        ),
        (
            {"ENDPOINT": "https://collector:4317", "INSECURE": "true"},
            "collector:4317",
            False,
            10,
            "none",
        ),
        (
            {
                "ENDPOINT": "https://generic:4317",
                "LOGS_ENDPOINT": "http://[::1]:4318",
                "TIMEOUT": "30",
                "LOGS_TIMEOUT": "0.25",
                "COMPRESSION": "deflate",
                "LOGS_COMPRESSION": "gzip",
                "INSECURE": "false",
                "LOGS_INSECURE": "true",
            },
            "[::1]:4318",
            True,
            0.25,
            "gzip",
        ),
    ],
)
def test_standard_environment_precedence(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    target: str,
    insecure: bool,
    timeout: float,
    compression: str,
) -> None:
    for name, value in values.items():
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{name}", value)
    settings = OTLPSettings.from_environment()
    assert settings.target == target
    assert settings.use_insecure is insecure
    assert settings.timeout == timeout
    assert settings.compression == compression


@pytest.mark.parametrize("signal", ["", "LOGS_"])
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TIMEOUT", "0"),
        ("TIMEOUT", "-1"),
        ("TIMEOUT", "nan"),
        ("TIMEOUT", "inf"),
        ("TIMEOUT", "invalid"),
        ("INSECURE", "invalid"),
        ("COMPRESSION", "brotli"),
        ("ENDPOINT", ""),
        ("ENDPOINT", "https://"),
    ],
)
def test_invalid_configuration_fails_before_opening_transport(
    monkeypatch: pytest.MonkeyPatch, signal: str, name: str, value: str
) -> None:
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{signal}{name}", value)
    with pytest.raises(ValueError, match="validation error"):
        OTLPSettings.from_environment()


def test_tls_files_and_headers_follow_signal_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("CERTIFICATE", "CLIENT_KEY", "CLIENT_CERTIFICATE"):
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_{name}", str(tmp_path / "generic"))
        monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_LOGS_{name}", str(tmp_path / name))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-generic=ignored")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "authorization=Bearer%20secret,x-test=a%3Db"
    )
    settings = OTLPSettings.from_environment()
    assert settings.certificate == tmp_path / "CERTIFICATE"
    assert settings.client_key == tmp_path / "CLIENT_KEY"
    assert settings.client_certificate == tmp_path / "CLIENT_CERTIFICATE"
    assert dict(settings.headers) == {"authorization": "Bearer secret", "x-test": "a=b"}


@pytest.mark.parametrize(
    "value",
    ["authorization", "=secret", "bad key=secret", "x=%0A", "x=%00", "x-bin=secret"],
)
def test_invalid_headers_fail_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", value)
    with pytest.raises(ValueError, match="OTLP header") as error:
        OTLPSettings.from_environment()
    assert "secret" not in str(error.value)


def test_header_values_are_not_in_settings_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "authorization=Bearer%20secret"
    )
    settings = OTLPSettings.from_environment()
    assert "secret" not in repr(settings)
    assert dict(settings.headers)["authorization"] == "Bearer secret"


@pytest.mark.parametrize("name", ["CLIENT_KEY", "CLIENT_CERTIFICATE"])
def test_mutual_tls_needs_both_key_and_certificate(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(f"OTEL_EXPORTER_OTLP_LOGS_{name}", "/missing/test.pem")
    with pytest.raises(ValueError, match="together"):
        OTLPSettings.from_environment()


class Receiver(LogsServiceServicer):
    def __init__(self) -> None:
        self.received: asyncio.Queue[dict[str, str | bytes]] = asyncio.Queue()
        self.release = asyncio.Event()
        self.release.set()

    async def Export(  # ruff: ignore[invalid-function-name]
        self, request: ExportLogsServiceRequest, context: grpc.aio.ServicerContext
    ) -> ExportLogsServiceResponse:
        del request
        self.received.put_nowait(dict(context.invocation_metadata() or ()))
        await self.release.wait()
        return ExportLogsServiceResponse(
            partial_success=ExportLogsPartialSuccess(
                rejected_log_records=1, error_message="private diagnostics"
            )
        )


@pytest.fixture
async def receiver() -> AsyncIterator[tuple[Receiver, str]]:
    service = Receiver()
    server = grpc.aio.server()
    add_LogsServiceServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield service, f"http://127.0.0.1:{port}"
    finally:
        service.release.set()
        await server.stop(None)


async def test_sender_preserves_partial_response_and_metadata(
    receiver: tuple[Receiver, str],
) -> None:
    service, endpoint = receiver
    sender = LogsExporter(
        OTLPSettings(endpoint=endpoint, headers=(("x-test", "value with spaces"),))
    )
    try:
        response = await sender.export(ExportLogsServiceRequest().SerializeToString())
        assert response.partial_success.rejected_log_records == 1
        assert (await service.received.get())["x-test"] == "value with spaces"
    finally:
        await sender.aclose()


async def test_close_aborts_inflight_rpc_without_blocking_event_loop(
    receiver: tuple[Receiver, str],
) -> None:
    service, endpoint = receiver
    service.release.clear()
    sender = LogsExporter(OTLPSettings(endpoint=endpoint, timeout=60))
    pending = asyncio.create_task(sender.export(b""))
    try:
        async with asyncio.timeout(2):
            await service.received.get()
            await sender.aclose()
            await sender.aclose()
            result = await asyncio.gather(pending, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sender.aclose()


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        if os.environ.get("CI"):
            pytest.fail("OpenSSL is required for the TLS contract in CI")
        pytest.skip("OpenSSL is not installed")
    directory = tmp_path_factory.mktemp("otel-tls")
    key, certificate = directory / "key.pem", directory / "certificate.pem"
    subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        capture_output=True,
        check=True,
        timeout=10,
    )
    return key, certificate


@pytest.mark.parametrize("mutual_tls", [False, True])
@pytest.mark.parametrize("compression", ["none", "gzip", "deflate"])
async def test_real_tls_transport_preserves_partial_success(
    certificate: tuple[Path, Path], mutual_tls: bool, compression: str
) -> None:
    key, cert = certificate
    service = Receiver()
    server = grpc.aio.server()
    add_LogsServiceServicer_to_server(service, server)
    credentials = grpc.ssl_server_credentials(
        ((key.read_bytes(), cert.read_bytes()),),
        root_certificates=cert.read_bytes() if mutual_tls else None,
        require_client_auth=mutual_tls,
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
    await server.start()
    settings = OTLPSettings.model_validate({
        "endpoint": f"https://127.0.0.1:{port}",
        "certificate": cert,
        "client_key": key if mutual_tls else None,
        "client_certificate": cert if mutual_tls else None,
        "compression": compression,
        "timeout": 2,
    })
    sender = LogsExporter(settings)
    try:
        response = await sender.export(b"")
        assert response.partial_success.rejected_log_records == 1
    finally:
        await sender.aclose()
        await server.stop(None)
