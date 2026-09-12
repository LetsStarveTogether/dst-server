import asyncio
import os

from dst_server.models.cluster import ModUpdateStatus, ShardRuntimeStatus

STATUS_INTERVAL = 30.0
RETRY_DELAY = 300.0


class ModMaintenance:
    """One pending update, with a fixed cooldown between download rounds."""

    def __init__(self) -> None:
        setting = os.environ.get("DST_SERVER_MOD_AUTO_UPDATE", "true")
        if setting not in {"true", "false"}:
            msg = "DST_SERVER_MOD_AUTO_UPDATE must be 'true' or 'false'"
            raise ValueError(msg)
        self.enabled = setting == "true"
        self.pending = False
        self.updating = False
        self.retry_at = 0.0
        self.error: str | None = None
        self._wakeup = asyncio.Event()

    def wake(self) -> None:
        self._wakeup.set()

    async def wait(self) -> None:
        try:
            async with asyncio.timeout(STATUS_INTERVAL):
                await self._wakeup.wait()
        except TimeoutError:
            pass
        self._wakeup.clear()

    def observe(self, statuses: tuple[ShardRuntimeStatus, ...]) -> None:
        if not self.updating:
            self.pending = self.error is not None or any(
                status.game_attempt is not None and status.outdated_mods
                for status in statuses
            )

    def begin(self) -> None:
        self.updating = True
        self.pending = True

    def updated(self, now: float) -> None:
        self.pending = False
        self.error = None
        self.retry_at = now + RETRY_DELAY

    def finish(self, now: float, *, failed: bool) -> None:
        self.updating = False
        self.retry_at = now + RETRY_DELAY
        if failed:
            self.pending = True
            self.error = "MOD maintenance failed; retrying in five minutes"

    def status(self, now: float) -> ModUpdateStatus:
        return ModUpdateStatus(
            enabled=self.enabled,
            pending=self.pending,
            updating=self.updating,
            retry_in_seconds=max(0.0, self.retry_at - now) if self.pending else 0.0,
            error=self.error,
        )
