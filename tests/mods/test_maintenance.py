import pytest
from ulid import ULID

from dst_server.models.cluster import ShardDesired, ShardPhase, ShardRuntimeStatus
from dst_server.mods.maintenance import RETRY_DELAY, ModMaintenance


@pytest.fixture
def maintenance(monkeypatch: pytest.MonkeyPatch) -> ModMaintenance:
    monkeypatch.delenv("DST_SERVER_MOD_AUTO_UPDATE", raising=False)
    return ModMaintenance()


def shard(name: str = "forest", *, outdated: bool = True) -> ShardRuntimeStatus:
    return ShardRuntimeStatus(
        name=name,
        is_master=name == "forest",
        desired=ShardDesired.RUNNING,
        phase=ShardPhase.RUNNING,
        agent_incarnation=ULID(),
        game_attempt=ULID(),
        outdated_mods=("Insight",) if outdated else (),
        ready=True,
        telemetry_profile="off",
    )


def test_download_failure_retries_every_five_minutes_until_success(
    maintenance: ModMaintenance,
) -> None:
    maintenance.observe((shard(),))
    assert maintenance.pending
    for now in (0, 300, 600, 900):
        maintenance.begin()
        maintenance.finish(now, failed=True)
        maintenance.observe(())
        status = maintenance.status(now)
        assert status.pending
        assert status.error
        assert not status.updating
        assert status.retry_in_seconds == RETRY_DELAY == 300
    maintenance.begin()
    maintenance.updated(1200)
    maintenance.finish(1200, failed=False)
    assert not maintenance.pending
    assert maintenance.error is None
    maintenance.observe((shard(),))
    assert maintenance.status(1201).retry_in_seconds == 299


def test_only_current_process_can_report_outdated_mods(
    maintenance: ModMaintenance,
) -> None:
    maintenance.observe((shard().replace(game_attempt=None),))
    assert not maintenance.pending
    maintenance.begin()
    maintenance.updated(1)
    maintenance.observe((shard(),))
    assert not maintenance.pending
    maintenance.finish(2, failed=False)
    maintenance.observe((shard(outdated=False),))
    assert not maintenance.pending


@pytest.mark.parametrize(
    ("setting", "enabled"), [(None, True), ("true", True), ("false", False)]
)
def test_auto_update_environment(
    monkeypatch: pytest.MonkeyPatch,
    setting: str | None,
    enabled: bool,
) -> None:
    if setting is None:
        monkeypatch.delenv("DST_SERVER_MOD_AUTO_UPDATE", raising=False)
    else:
        monkeypatch.setenv("DST_SERVER_MOD_AUTO_UPDATE", setting)
    maintenance = ModMaintenance()
    maintenance.observe((shard(),))
    assert maintenance.enabled is enabled
    assert maintenance.status(0).enabled is enabled
    assert maintenance.pending


@pytest.mark.parametrize("setting", ["", "TRUE", "False", "1", " true"])
def test_invalid_auto_update_environment(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setenv("DST_SERVER_MOD_AUTO_UPDATE", setting)
    with pytest.raises(ValueError, match="DST_SERVER_MOD_AUTO_UPDATE"):
        ModMaintenance()


def test_replaced_process_clears_obsolete_update_demand(
    maintenance: ModMaintenance,
) -> None:
    maintenance.updated(0)
    maintenance.observe((shard(),))
    assert maintenance.pending
    maintenance.observe((shard(outdated=False),))
    assert not maintenance.pending
    assert maintenance.retry_at == RETRY_DELAY
