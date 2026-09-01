from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.cluster import cli as cluster_cli
from dst_server.cluster import service
from dst_server.runtime import ServerConfig
from dst_server.telemetry import TelemetrySettings, otel


@pytest.mark.parametrize("profile", ["off", "critical", "history"])
def test_cli_reads_telemetry_profile_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    serving = AsyncMock(return_value=7)
    monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)
    monkeypatch.setattr(cluster_cli, "serve", serving)

    assert cluster_cli.main(("serve", "forest")) == 7
    serving.assert_awaited_once_with(
        telemetry=TelemetrySettings.model_validate({"profile": profile}),
        shard="forest",
        external_port=None,
    )


@pytest.mark.parametrize(
    ("command", "target", "expected"),
    [
        (("prepare",), "prepare_shared", {}),
        (
            ("serve", "--external-port", "30007", "cave"),
            "serve",
            {
                "telemetry": TelemetrySettings(),
                "shard": "cave",
                "external_port": 30007,
            },
        ),
        (
            ("master", "--external-port", "30000"),
            "master",
            {"telemetry": TelemetrySettings(), "external_port": 30000},
        ),
    ],
)
def test_cli_routes_commands_once(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    target: str,
    expected: dict[str, object],
) -> None:
    action = AsyncMock(return_value=0)
    monkeypatch.setattr(cluster_cli, target, action)

    assert cluster_cli.main(command) == 0
    action.assert_awaited_once_with(**expected)


@pytest.mark.parametrize(
    ("command", "profile", "error"),
    [
        (("serve", "--external-port", "1023", "cave"), None, SystemExit),
        (("serve", "--master", "forest"), None, SystemExit),
        (("serve", "forest"), "verbose", ValueError),
    ],
)
def test_cli_rejects_invalid_arguments_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    profile: str | None,
    error: type[BaseException],
) -> None:
    if profile is not None:
        monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)

    with pytest.raises(error):
        cluster_cli.main(command)


def test_otel_resource_uses_explicit_cluster_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = object()
    configure = Mock(return_value=pipeline)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    monkeypatch.setattr(otel, "configure", configure)

    config = ServerConfig(shard="forest", telemetry_cluster="dst-042")
    assert service.configure_otel(config) is pipeline
    configure.assert_called_once_with(
        resource_attributes={"dst.cluster.name": "dst-042"}
    )
