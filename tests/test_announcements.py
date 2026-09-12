import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dst_server import announcements
from dst_server.announcements import Countdown, Repeat, Template, maintenance


@pytest.mark.parametrize(
    "values",
    [
        {"message": ""},
        {"message": "notice", "count": 0},
        {"message": "notice", "count": True},
        {"message": "notice", "interval": 0},
        {"message": "notice", "interval": float("inf")},
    ],
)
def test_repeat_rejects_invalid_native_arguments(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Repeat.model_validate(values)


@pytest.mark.parametrize(
    ("template", "parameters"),
    [
        ("{missing}", {}),
        ("{remaining.real}", {}),
        ("{remaining[0]}", {}),
        ("{remaining!r}", {}),
        ("{remaining:1000000000}", {}),
        ("{remaining:{minutes}}", {}),
        ("{}", {}),
        ("{remaining", {}),
        ("{remaining}", {"remaining": 4}),
        ("{duration}", {"duration": float("nan")}),
        ("{duration}", {"duration": float("inf")}),
        ("{duration}", {"duration": True}),
    ],
)
def test_countdown_rejects_unsafe_or_missing_fields(
    template: str, parameters: dict[str, str | int | float]
) -> None:
    with pytest.raises(ValidationError):
        Countdown(template=template, parameters=parameters)


def test_templates_render_deadline_duration_and_next_opening() -> None:
    update = maintenance(Template.MOD_UPDATE, estimated_duration=180)
    assert update.render(60) == (
        "本房间将在约 1 分钟后重启更新 MOD，预计耗时 约 3 分钟，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
    )
    assert update.render(0).startswith("本房间即将重启更新 MOD")
    scheduled = maintenance(Template.SCHEDULED_CLOSE, next_opening="18:30")
    assert scheduled.render(59.1) == (
        "本房间将在约 1 分钟后定时关闭，请提前安排游戏进度。下次开放时间：18:30。"  # ruff: ignore[ambiguous-unicode-character-string]
    )
    assert (
        Countdown(
            template="{{ready}} {remaining} {minutes} {name}",
            parameters={"name": "players"},
        ).render(60.1)
        == "{ready} 61 2 players"
    )


def test_maintenance_subject_is_literal_and_survives_rpc_serialization() -> None:
    notice = maintenance(Template.RESTART, subject="分片「Caves {1}」")
    restored = Countdown.model_validate_json(notice.model_dump_json())
    assert restored.render(30).startswith("分片「Caves {1}」将在30 秒后维护重启")
    assert Repeat.model_validate_json(
        Repeat(message="ready", count=3).model_dump_json()
    ) == Repeat(message="ready", count=3)


def test_new_maintenance_parameters_are_independent_from_shared_defaults() -> None:
    default = announcements.RESTART_NOTICE
    fresh = maintenance(Template.RESTART)
    assert default == fresh
    assert default.parameters is not fresh.parameters
    original = default.parameters["subject"]
    try:
        default.parameters["subject"] = "changed"
        assert maintenance(Template.RESTART).parameters["subject"] == "本房间"
    finally:
        default.parameters["subject"] = original
    values: dict[str, str | int | float] = {"name": "players"}
    notice = Countdown(template="{name}", parameters=values)
    values["name"] = "changed"
    assert notice.render(0) == "players"
    assert Countdown.model_validate(default).parameters == default.parameters
    assert Countdown.model_validate_json(default.model_dump_json()) == default
    assert fresh.parameters["subject"] == "本房间"


@pytest.mark.parametrize(
    "values",
    [
        {"delay": -1},
        {"delay": float("inf")},
        {"interval": 0},
        {"estimated_duration": float("nan")},
        {"estimated_duration": True},
        {"subject": ""},
        {"subject": 123},
        {"next_opening": 123},
    ],
)
def test_maintenance_rejects_invalid_parameters(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        maintenance(Template.RESTART, **values)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("remaining", [-1, float("inf"), float("nan"), True])
def test_render_rejects_invalid_remaining_time(remaining: float) -> None:
    with pytest.raises(ValidationError):
        Countdown(template="{remaining}").render(remaining)


async def test_deadline_does_not_drift_or_replay_missed_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    elapsed = 0.0
    messages: list[str] = []
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:  # ruff: ignore[unused-async]
        nonlocal elapsed
        sleeps.append(delay)
        elapsed += delay

    async def send(message: str) -> None:  # ruff: ignore[unused-async]
        nonlocal elapsed
        messages.append(message)
        if len(messages) == 1:
            elapsed += 75

    monkeypatch.setattr(
        announcements,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed),
            sleep=sleep,
        ),
    )
    plan = Countdown(template="{remaining}", delay=90, interval=30)
    assert await plan.run(send)
    assert messages == ["90", "15", "0"]
    assert sleeps == [0, 15]
    assert elapsed == 90


async def test_zero_delay_sends_once_and_callback_can_cancel() -> None:
    send = AsyncMock(return_value=None)
    assert await Countdown(template="{remaining}", delay=0).run(send)
    send.assert_awaited_once_with("0")
    cancelled = AsyncMock(return_value=False)
    assert not await Countdown(template="{remaining}", delay=60).run(cancelled)
    assert cancelled.await_count == 1


async def test_cancellation_propagates_without_a_final_notice() -> None:
    send = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await Countdown(template="{remaining}", delay=60).run(send)
    assert send.await_count == 1
