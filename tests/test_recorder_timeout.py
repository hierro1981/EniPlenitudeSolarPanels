"""Recorder timeout and retry behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.plenitude_panels import manual_repair, recovery_statistics
from custom_components.plenitude_panels.recovery import ProductionRecovery
from custom_components.plenitude_panels.statistics_repair import repair_status


def test_status_read_timeout_reuses_queued_job_and_allows_new_reads():
    async def run():
        loop = asyncio.get_running_loop()
        hass = SimpleNamespace(loop=loop, data={})
        recorder = SimpleNamespace(hass=hass, queue_task=AsyncMock())
        queued = []
        recorder.queue_task = queued.append
        with (
            patch.object(recovery_statistics, "get_instance", return_value=recorder),
            patch.object(recovery_statistics, "_RECORDER_TIMEOUT", 0.001),
        ):
            for _ in range(2):
                with pytest.raises(recovery_statistics.RecorderTimeoutError, match="queued"):
                    await recovery_statistics.async_recorder_job(hass, repair_status, "entry")
            assert len(queued) == 1
            queued[0].job = lambda *_: 42
            queued[0].run(recorder)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            with pytest.raises(recovery_statistics.RecorderTimeoutError):
                await recovery_statistics.async_recorder_job(hass, repair_status, "entry")
            assert len(queued) == 2
    asyncio.run(run())


def test_status_read_reports_running_and_observes_late_failure():
    async def run():
        hass = SimpleNamespace(loop=asyncio.get_running_loop(), data={})
        queued = []
        recorder = SimpleNamespace(hass=hass, queue_task=queued.append)
        with (
            patch.object(recovery_statistics, "get_instance", return_value=recorder),
            patch.object(recovery_statistics, "_RECORDER_TIMEOUT", 0.001),
        ):
            with pytest.raises(recovery_statistics.RecorderTimeoutError):
                await recovery_statistics.async_recorder_job(hass, repair_status, "entry")
            queued[0].started_at = recovery_statistics.monotonic()
            with pytest.raises(recovery_statistics.RecorderTimeoutError, match="running"):
                await recovery_statistics.async_recorder_job(hass, repair_status, "entry")
            queued[0]._finish(None, ValueError("late"))
            await asyncio.sleep(0)
            assert not hass.data[recovery_statistics._READ_JOBS]
    asyncio.run(run())


def test_reconciliation_timeout_warning_is_throttled(caplog):
    async def run():
        recovery = SimpleNamespace(
            hass=SimpleNamespace(config=SimpleNamespace(components={"recorder"})),
            state={"repair_backup": {"phase": "applied"}, "pending": []},
        )
        job = AsyncMock(side_effect=recovery_statistics.RecorderTimeoutError("queued"))
        with patch.object(manual_repair, "async_reconcile_journal", job):
            await ProductionRecovery.async_correct_statistics(recovery)
            await ProductionRecovery.async_correct_statistics(recovery)
            assert caplog.text.count("Statistics repair reconciliation deferred") == 1
            job.side_effect = None
            await ProductionRecovery.async_correct_statistics(recovery)
            job.side_effect = recovery_statistics.RecorderTimeoutError("queued")
            await ProductionRecovery.async_correct_statistics(recovery)
            assert caplog.text.count("Statistics repair reconciliation deferred") == 2
    asyncio.run(run())
