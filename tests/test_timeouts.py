import asyncio

import pytest

from dst_server.timeouts import operation_deadline, timeout_scope


async def test_inherited_deadline_is_enforced_after_parent_scope_exits() -> None:
    async def child_operation() -> None:
        async with timeout_scope(60):
            await asyncio.sleep(0)

    async with timeout_scope(0):
        child = asyncio.create_task(child_operation())

    with pytest.raises(TimeoutError):
        await child
    assert operation_deadline.get() is None


async def test_nested_default_does_not_shorten_or_restart_operation_budget() -> None:
    async with timeout_scope(60) as deadline:
        async with timeout_scope(0) as inherited:
            assert inherited == deadline
            await asyncio.sleep(0)
        assert operation_deadline.get() == deadline
    assert operation_deadline.get() is None
