from pathlib import Path
from unittest.mock import AsyncMock, Mock

import orjson
import pytest
from pydantic import SecretStr

from dst_server.cli import main
from dst_server.configuration.models import ClusterConfig
from dst_server.deployment import QuadletApplication
from dst_server.host import Host
from dst_server.presets.lst import TOKEN_ENVIRONMENT


@pytest.mark.parametrize("json_output", [False, True])
def test_invalid_password_is_not_exposed_in_errors(
    cli_host: Host, capsys: pytest.CaptureFixture[str], *, json_output: bool
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    before = cli_host.rooms.load(0)
    capsys.readouterr()
    value = "PRIVATE_PASSWORD\nINVALID"
    assert (
        main([
            *(["--json"] if json_output else []),
            "room",
            "edit",
            "0",
            "--password",
            value,
        ])
        == 1
    )
    output = capsys.readouterr()
    assert "PRIVATE_PASSWORD" not in output.out + output.err
    assert "test-cluster-token" not in output.out + output.err
    assert "cluster_password" in output.out
    assert cli_host.rooms.load(0) == before


def test_create_any_template_writes_native_configuration_and_deployment(
    cli_host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main([
            "--json",
            "room",
            "create",
            "299",
            "--template",
            "forge",
            "--name",
            "Example",
            "--max-players",
            "9",
        ])
        == 0
    )
    definition = cli_host.rooms.load(299)
    game = ClusterConfig.load(cli_host.rooms.path(299))
    application = QuadletApplication.load(cli_host.quadlet_dir, name="dst-299")
    assert definition.template == "forge"
    assert (
        game.settings.cluster_name
        == definition.cluster.settings.cluster_name
        == "Example"
    )
    assert game.settings.max_players == definition.cluster.settings.max_players == 9
    assert game.token == definition.cluster.token == SecretStr("test-cluster-token")
    assert game.settings.game_mode == "lavaarena"
    assert application == definition.application(cli_host.rooms.path(299))
    assert orjson.loads(capsys.readouterr().out)["number"] == 299
    before = {
        path: path.read_bytes()
        for path in cli_host.rooms.path(299).rglob("*")
        if path.is_file()
    }
    assert main(["room", "create", "299", "--template", "pure_survival"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert main(["--json", "room", "show", "299"]) == 0
    output = capsys.readouterr().out
    assert "test-cluster-token" not in output
    assert orjson.loads(output)["cluster"]["token"] == str(definition.cluster.token)


def test_edit_fields_and_mods_preserves_unrelated_configuration(
    cli_host: Host, cli_systemd: Mock
) -> None:
    assert main(["room", "create", "0", "--template", "semi_survival"]) == 0
    before = cli_host.rooms.load(0)
    assert (
        main([
            "room",
            "edit",
            "000",
            "--max-players",
            "12",
            "--set",
            "/cluster/settings/pvp=true",
            "--set",
            "/recycle=true",
        ])
        == 0
    )
    changed = cli_host.rooms.load(0)
    assert changed.cluster.settings.max_players == 12
    assert changed.cluster.settings.pvp is True
    assert changed.recycle is True
    assert changed.cluster.shards == before.cluster.shards
    assert changed.cluster.token == before.cluster.token
    assert changed.cluster.settings.cluster_key == before.cluster.settings.cluster_key
    assert (
        main([
            "mod",
            "set",
            "1803285852",
            '{"example":true}',
            "--room",
            "000",
            "--shard",
            "forest",
        ])
        == 0
    )
    assert (
        main(["mod", "disable", "1803285852", "--room", "000", "--shard", "forest"])
        == 0
    )
    changed = cli_host.rooms.load(0)
    mod = changed.cluster.shards["forest"].mods.entries["workshop-1803285852"]
    assert mod.enabled is False
    assert dict(mod.configuration_options or {}) == {"example": True}
    assert changed.cluster.shards["cave"] == before.cluster.shards["cave"]
    game = ClusterConfig.load(cli_host.rooms.path(0))
    assert game.settings == changed.cluster.settings
    assert game.shards["forest"].mods.entries["workshop-1803285852"] == mod
    cli_systemd.start.assert_not_awaited()
    cli_systemd.stop.assert_not_awaited()


def test_show_reads_direct_native_edits(
    cli_host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    capsys.readouterr()
    directory = cli_host.rooms.path(0)
    path = directory / "cluster.ini"
    path.write_text(path.read_text().replace("max_players = 9", "max_players = 12"))
    assert (
        main([
            "--json",
            "room",
            "show",
            "0",
            "--field",
            "/cluster/settings/max_players",
        ])
        == 0
    )
    assert orjson.loads(capsys.readouterr().out) == 12
    assert not (directory / ".dst-room.json").exists()


def test_template_apply_preserves_identity_and_updates_game_and_quadlets(
    cli_host: Host,
) -> None:
    assert main(["deployment", "lst", "--room", "016"]) == 0
    assert (
        main([
            "room",
            "edit",
            "016",
            "--name",
            "My room",
            "--description",
            "My description",
            "--password",
            "PRIVATE_PASSWORD",
        ])
        == 0
    )
    before = cli_host.rooms.load(16)
    assert main(["template", "apply", "forge", "--room", "016"]) == 0
    after = cli_host.rooms.load(16)
    assert after.template == "forge"
    assert after.cluster.settings.game_mode == "lavaarena"
    assert after.deployment == before.deployment
    assert after.schedule == before.schedule
    assert after.recycle == before.recycle
    assert after.cluster.settings.cluster_name == before.cluster.settings.cluster_name
    assert (
        after.cluster.settings.cluster_description
        == before.cluster.settings.cluster_description
    )
    assert (
        after.cluster.settings.cluster_password
        == before.cluster.settings.cluster_password
    )
    assert after.cluster.token == before.cluster.token
    assert after.cluster.settings.cluster_key == before.cluster.settings.cluster_key
    assert ClusterConfig.load(cli_host.rooms.path(16)).settings.game_mode == "lavaarena"
    assert QuadletApplication.load(
        cli_host.quadlet_dir, name="dst-016"
    ) == after.application(cli_host.rooms.path(16))


def test_mutations_require_targets_and_parse_ranges(
    cli_host: Host, cli_systemd: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deployment", "lst", "--room", "000-001"]) == 0
    capsys.readouterr()
    assert main(["room", "start"]) == 1
    assert main(["deployment", "lst"]) == 1
    assert main(["room", "start", "000", "--all"]) == 1
    cli_systemd.start.assert_not_awaited()
    assert (
        main(["--json", "room", "start", "000-001", "--no-wait", "--timeout", "123"])
        == 0
    )
    output = orjson.loads(capsys.readouterr().out)
    assert [record["room"] for record in output] == [0, 1]
    assert all(record["ok"] and record["result"]["waiting"] for record in output)
    assert {call.args[0] for call in cli_systemd.start.await_args_list} == {
        cli_host.unit(0),
        cli_host.unit(1),
    }
    cli_systemd.wait_idle.assert_not_awaited()
    cli_systemd.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["room", "edit", "000", "--set", "/cluster/settings/pvp"],
        ["mod", "set", "123", "[]", "--room", "000"],
    ],
)
def test_invalid_changes_leave_native_files_untouched(
    cli_host: Host, arguments: list[str]
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    before = cli_host.rooms.load(0)
    files = {
        path: (cli_host.rooms.path(0) / path).read_bytes()
        for path in before.game_files()
    }
    assert main(arguments) == 1
    assert cli_host.rooms.load(0) == before
    assert all(
        (cli_host.rooms.path(0) / path).read_bytes() == value
        for path, value in files.items()
    )


def test_related_pointer_changes_validate_only_the_final_result(cli_host: Host) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    assert (
        main([
            "room",
            "edit",
            "000",
            "--set",
            "/cluster/settings/whitelist_slots=12",
            "--set",
            "/cluster/settings/max_players=12",
        ])
        == 0
    )
    settings = cli_host.rooms.load(0).cluster.settings
    assert settings.max_players == settings.whitelist_slots == 12
    assert (
        main([
            "room",
            "edit",
            "000",
            "--max-players",
            "9",
            "--set",
            "/cluster/settings/whitelist_slots=9",
        ])
        == 0
    )
    settings = ClusterConfig.load(cli_host.rooms.path(0)).settings
    assert settings.max_players == settings.whitelist_slots == 9


def test_missing_token_never_reads_existing_room_token(
    cli_host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    monkeypatch.delenv(TOKEN_ENVIRONMENT)
    assert main(["room", "create", "1", "--template", "pure_survival"]) == 1
    assert not cli_host.rooms.path(1).exists()
    token = tmp_path / "token"
    token.write_text("explicit-token\n")
    assert (
        main([
            "room",
            "create",
            "1",
            "--template",
            "pure_survival",
            "--token-file",
            str(token),
        ])
        == 0
    )
    assert cli_host.rooms.load(1).cluster.token == SecretStr("explicit-token")


@pytest.mark.parametrize(
    ("mapping_options", "volume_idmap", "userns"),
    [
        ((), None, None),
        (
            ("--volume-idmap", "uids=0-1000-1;gids=0-1000-1"),
            "uids=0-1000-1;gids=0-1000-1",
            None,
        ),
        (("--userns", "keep-id:uid=1000,gid=1000"), None, "keep-id:uid=1000,gid=1000"),
    ],
)
def test_deployment_generates_selected_rooms_with_explicit_token_and_mapping(
    cli_host: Host,
    tmp_path: Path,
    mapping_options: tuple[str, ...],
    volume_idmap: str | None,
    userns: str | None,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("explicit-token\n")
    assert (
        main([
            "deployment",
            "lst",
            "--room",
            "0,215",
            "--token-file",
            str(token_file),
            *mapping_options,
        ])
        == 0
    )
    assert cli_host.rooms.numbers() == (0, 215)
    assert {path.name for path in cli_host.quadlet_dir.glob("*.pod")} == {
        "dst-000.pod",
        "dst-215.pod",
    }
    for number in (0, 215):
        application = QuadletApplication.load(
            cli_host.quadlet_dir, name=f"dst-{number:03d}"
        )
        assert application.pod.userns == userns
        assert all(
            volume.idmap == volume_idmap
            for unit in (application.master, *application.secondaries)
            for volume in unit.volumes
        )
        token = cli_host.rooms.path(number) / "cluster_token.txt"
        assert token.read_text() == "explicit-token\n"
        assert token.stat().st_mode & 0o777 == 0o600


def test_deployment_uses_environment_token_and_requested_image(cli_host: Host) -> None:
    assert (
        main([
            "deployment",
            "lst",
            "--room",
            "0",
            "--image",
            "quay.io/wh2099/dst-server:beta",
        ])
        == 0
    )
    application = QuadletApplication.load(cli_host.quadlet_dir)
    for unit in (application.master, *application.secondaries):
        assert unit.image == "quay.io/wh2099/dst-server:beta"
        assert unit.pull == "always"
    token = cli_host.rooms.path(0) / "cluster_token.txt"
    assert token.read_text() == "test-cluster-token\n"
    assert token.stat().st_mode & 0o777 == 0o600


def test_deployment_all_uses_only_the_new_fleet_numbers(cli_host: Host) -> None:
    assert main(["deployment", "lst", "--all"]) == 0
    assert cli_host.rooms.numbers() == (*range(100), *range(200, 216))


def test_deployment_rejects_an_unassigned_number_before_writing(
    cli_host: Host, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["deployment", "lst", "--room", "0,100"]) == 1
    assert "000-099 or 200-215" in capsys.readouterr().err
    assert not cli_host.cluster_root.exists()
    assert not cli_host.quadlet_dir.exists()


@pytest.mark.parametrize(
    ("token", "from_file"),
    [(None, False), ("", False), ("invalid token", False), ("\n", True)],
    ids=("missing", "empty-environment", "invalid-environment", "empty-file"),
)
def test_deployment_rejects_invalid_token_before_writing(
    cli_host: Host,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    from_file: bool,
) -> None:
    arguments = ["deployment", "lst", "--room", "0"]
    if from_file:
        assert token is not None
        token_file = tmp_path / "token"
        token_file.write_text(token)
        arguments.extend(("--token-file", str(token_file)))
    elif token is None:
        monkeypatch.delenv(TOKEN_ENVIRONMENT)
    else:
        monkeypatch.setenv(TOKEN_ENVIRONMENT, token)
    assert main(arguments) == 1
    assert not cli_host.cluster_root.exists()
    assert not cli_host.quadlet_dir.exists()


@pytest.mark.parametrize(
    "command",
    [
        ["room", "edit", "0", "--max-players", "12"],
        ["room", "edit", "0", "--set", "/recycle=true"],
        ["template", "apply", "forge", "--room", "0"],
        ["mod", "enable", "123", "--room", "0"],
        ["mod", "disable", "123", "--room", "0"],
        ["mod", "set", "123", '{"difficulty":2}', "--room", "0"],
    ],
)
def test_configuration_commands_require_explicit_stop_and_start(
    cli_host: Host,
    cli_systemd: Mock,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    assert main(["room", "start", "0", "--no-wait"]) == 0
    before = cli_host.rooms.load(0)
    cli_systemd.start.reset_mock()
    capsys.readouterr()
    assert main(["--json", *command]) == 1
    (record,) = orjson.loads(capsys.readouterr().out)
    assert "stopped room" in record["error"]
    assert cli_host.rooms.load(0) == before
    cli_systemd.stop.assert_not_awaited()
    cli_systemd.start.assert_not_awaited()

    assert main(["room", "stop", "0"]) == 0
    cli_systemd.stop.reset_mock()
    assert main(command) == 0
    assert cli_host.rooms.load(0) != before
    cli_systemd.stop.assert_not_awaited()
    cli_systemd.start.assert_not_awaited()

    assert main(["room", "start", "0", "--no-wait"]) == 0
    cli_systemd.start.assert_awaited()


@pytest.mark.parametrize("apply", [False, True])
def test_deployment_migration_defaults_to_preview(
    cli_host: Host, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    migration = AsyncMock(return_value={"status": "migrated" if apply else "preview"})
    monkeypatch.setattr("dst_server.host.migration.migrate", migration)
    assert main(["deployment", "migrate", *(["--apply"] if apply else [])]) == 0
    assert migration.await_args is not None
    assert migration.await_args.args[0].cluster_root == cli_host.cluster_root
    assert migration.await_args.kwargs == {"apply": apply}
