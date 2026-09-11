import json
from pathlib import Path
from unittest.mock import Mock

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
    assert json.loads(capsys.readouterr().out)["number"] == 299
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
    assert json.loads(output)["cluster"]["token"] == str(definition.cluster.token)


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
    assert json.loads(capsys.readouterr().out) == 12
    assert not (directory / ".dst-room.json").exists()


def test_template_apply_preserves_identity_and_updates_game_and_quadlets(
    cli_host: Host,
) -> None:
    assert main(["deployment", "lst", "--room", "004"]) == 0
    assert (
        main([
            "room",
            "edit",
            "004",
            "--name",
            "My room",
            "--description",
            "My description",
            "--password",
            "PRIVATE_PASSWORD",
        ])
        == 0
    )
    before = cli_host.rooms.load(4)
    assert main(["template", "apply", "forge", "--room", "004"]) == 0
    after = cli_host.rooms.load(4)
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
    assert ClusterConfig.load(cli_host.rooms.path(4)).settings.game_mode == "lavaarena"
    assert QuadletApplication.load(
        cli_host.quadlet_dir, name="dst-004"
    ) == after.application(cli_host.rooms.path(4))


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
    output = json.loads(capsys.readouterr().out)
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


def test_running_game_edits_require_restart_but_policy_edits_do_not(
    cli_host: Host, cli_systemd: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["room", "create", "0", "--template", "pure_survival"]) == 0
    before = cli_host.rooms.load(0)
    assert main(["room", "start", "0", "--no-wait"]) == 0
    capsys.readouterr()
    assert main(["--json", "room", "edit", "0", "--max-players", "12"]) == 1
    (record,) = json.loads(capsys.readouterr().out)
    assert "--restart" in record["error"]
    assert cli_host.rooms.load(0) == before
    assert main(["room", "edit", "0", "--set", "/recycle=true"]) == 0
    assert cli_host.rooms.policy(0).recycle
    cli_systemd.stop.assert_not_awaited()
