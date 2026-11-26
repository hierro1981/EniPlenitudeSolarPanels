"""Executor reads reuse timed-out work without private Recorder tasks."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.plenitude_panels import recovery_statistics


def test_read_timeout_shields_and_reuses_inflight_executor_job():
    async def run():
        future = asyncio.get_running_loop().create_future()
        instance = SimpleNamespace(async_add_executor_job=AsyncMock(side_effect=lambda *_: future))
        hass = SimpleNamespace(data={})
        job = lambda *_: None
        # async executor mock awaits its side effect's future only in HA; use an
        # explicit coroutine here to model a slow executor.
        async def execute(*_):
            return await future
        instance.async_add_executor_job.side_effect = execute
        with patch.object(recovery_statistics, "get_instance", return_value=instance), patch.object(
            recovery_statistics, "_RECORDER_TIMEOUT", .001,
        ):
            for _ in range(2):
                with pytest.raises(recovery_statistics.RecorderTimeoutError):
                    await recovery_statistics.async_recorder_job(hass, job, "entry")
            instance.async_add_executor_job.assert_awaited_once()
            assert not future.cancelled()
            future.set_result(42)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not hass.data[recovery_statistics._READ_JOBS]
    asyncio.run(run())
