import asyncio
import math
from collections.abc import Awaitable, Callable
from enum import StrEnum
from string import Formatter
from typing import Annotated, Self

from pydantic import Field, TypeAdapter, model_validator

from dst_server.models.base import NonNegativeFloat, RevalidatedFrozenModel
from dst_server.timeouts import Timeout

DEFAULT_DELAY = 60.0
DEFAULT_INTERVAL = 30.0
DEFAULT_DURATION = 300.0
_SECONDS = TypeAdapter(NonNegativeFloat)
_TEXT = TypeAdapter(Annotated[str, Field(min_length=1)])
_DYNAMIC_FIELDS = frozenset({"remaining", "minutes", "when"})


class Repeat(RevalidatedFrozenModel):
    """Fixed text; submit once and let the game's c_announce repeat it."""

    message: Annotated[str, Field(min_length=1)]
    count: Annotated[int, Field(ge=1, le=2**53 - 1)] = 1
    interval: Timeout = DEFAULT_INTERVAL


class Template(StrEnum):
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    MOD_UPDATE = "mod_update"
    DEPLOYMENT = "deployment"
    SCHEDULED_CLOSE = "scheduled_close"


_TEMPLATES = {
    Template.SHUTDOWN: "{subject}{when}关闭，请提前安排游戏进度。",  # ruff: ignore[ambiguous-unicode-character-string]
    Template.RESTART: (
        "{subject}{when}维护重启，预计耗时 {duration}，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
    ),
    Template.MOD_UPDATE: (
        "{subject}{when}重启更新 MOD，预计耗时 {duration}，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
    ),
    Template.DEPLOYMENT: (
        "{subject}{when}部署更新并重启，预计耗时 {duration}，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
    ),
    Template.SCHEDULED_CLOSE: (
        "{subject}{when}定时关闭，请提前安排游戏进度。{next_opening}"  # ruff: ignore[ambiguous-unicode-character-string]
    ),
}


def _duration(seconds: float) -> str:
    return (
        f"约 {math.ceil(seconds / 60)} 分钟"
        if seconds >= 60  # ruff: ignore[magic-value-comparison]
        else f"{math.ceil(seconds)} 秒"
    )


class Countdown(RevalidatedFrozenModel):
    """Render named placeholders against a monotonic maintenance deadline."""

    template: Annotated[str, Field(min_length=1)]
    delay: NonNegativeFloat = DEFAULT_DELAY
    interval: Timeout = DEFAULT_INTERVAL
    parameters: dict[str, str | int | float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_template(self) -> Self:
        if _DYNAMIC_FIELDS.intersection(self.parameters):
            msg = "countdown parameters cannot replace remaining, minutes or when"
            raise ValueError(msg)
        allowed = _DYNAMIC_FIELDS | self.parameters.keys()
        for _, name, specification, conversion in Formatter().parse(self.template):
            if name is not None and (
                not name.isidentifier()
                or name not in allowed
                or specification
                or conversion
            ):
                msg = "announcement templates only support known named placeholders"
                raise ValueError(msg)
        return self

    def render(self, remaining: float) -> str:
        seconds = math.ceil(_SECONDS.validate_python(remaining, strict=True))
        return self.template.format(
            **self.parameters,
            remaining=seconds,
            minutes=math.ceil(seconds / 60),
            when=f"将在{_duration(seconds)}后" if seconds else "即将",
        )

    async def run(self, send: Callable[[str], Awaitable[bool | None]]) -> bool:
        """Send through the deadline; False from send cancels further notices."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.delay
        while True:
            remaining = max(0.0, deadline - loop.time())
            if await send(self.render(remaining)) is False:
                return False
            if remaining == 0:
                return True
            # Align notices to the deadline; slow sends never extend the countdown.
            next_notice = (
                deadline - (math.ceil(remaining / self.interval) - 1) * self.interval
            )
            await asyncio.sleep(max(0.0, next_notice - loop.time()))


def maintenance(
    reason: Template,
    *,
    delay: float = DEFAULT_DELAY,
    interval: float = DEFAULT_INTERVAL,
    estimated_duration: float = DEFAULT_DURATION,
    next_opening: str | None = None,
    subject: str = "本房间",
) -> Countdown:
    duration = _SECONDS.validate_python(estimated_duration, strict=True)
    subject = _TEXT.validate_python(subject, strict=True)
    if next_opening is not None:
        next_opening = _TEXT.validate_python(next_opening, strict=True)
    return Countdown(
        template=_TEMPLATES[Template(reason)],
        delay=delay,
        interval=interval,
        parameters={
            "subject": subject,
            "duration": _duration(duration),
            "next_opening": f"下次开放时间：{next_opening}。" if next_opening else "",  # ruff: ignore[ambiguous-unicode-character-string]
        },
    )


SHUTDOWN_NOTICE = maintenance(Template.SHUTDOWN)
RESTART_NOTICE = maintenance(Template.RESTART)
MOD_UPDATE_NOTICE = maintenance(Template.MOD_UPDATE)
