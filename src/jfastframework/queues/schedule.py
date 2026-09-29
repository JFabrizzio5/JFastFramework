"""A recurring task: which task, when, and with what payload.

Declared on the :class:`~jfastframework.queues.worker.TaskRegistry` and run by
the :class:`~jfastframework.queues.scheduler.Scheduler`::

    @tasks.task("refresh_rates", every=timedelta(minutes=5))
    async def refresh_rates(payload: dict) -> None: ...

    @tasks.task("nightly_report", cron="0 3 * * *", timezone="America/Mexico_City")
    async def nightly_report(payload: dict) -> None: ...

    tasks.schedule("purge_sessions", cron="@hourly", payload={"older_than_days": 30})

A schedule only decides *when*. Every tick becomes an ordinary job on the
queue, so retries, dead-lettering and at-least-once delivery are the queue's,
unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jfastframework.queues.cron import Cron

__all__ = ["Schedule"]

#: Interval ticks count from here, so every replica computes the same tick
#: times without talking to the others. Counting from each process's start
#: would give two replicas two different sets of ticks, and nothing to collide
#: on when they claim one.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Namespace for the job ids of scheduled ticks. Fixed forever: a job id that
#: changed between releases would stop deduplicating a tick enqueued by the
#: previous one during a rolling deploy.
_TICK_NAMESPACE = uuid.UUID("5b0b6c64-41a8-4c38-9d52-3e6c3f0a7d11")

_ONE_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True)
class Schedule:
    """One recurring task. Exactly one of ``every`` and ``cron``."""

    name: str
    task: str
    every: timedelta | None = None
    cron: Cron | None = None
    # A zoneinfo name. Cron fields are wall-clock times in it; an interval is
    # the same length in every zone and takes none.
    timezone: str = "UTC"
    payload: Mapping[str, Any] = field(default_factory=dict)
    # After downtime, run the most recent missed tick once on start-up. Never
    # more than once: a service down for a day does not owe 288 runs of a
    # five-minute job, and delivering them at once is how a restart becomes
    # an outage.
    catch_up: bool = True
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if (self.every is None) == (self.cron is None):
            raise ValueError(f"schedule {self.name!r} needs exactly one of every= or cron=")
        if self.every is not None:
            if self.every < timedelta(seconds=1):
                raise ValueError(
                    f"schedule {self.name!r}: every={self.every} is shorter than a second; "
                    f"a queue is the wrong tool for that"
                )
            if self.timezone != "UTC":
                raise ValueError(
                    f"schedule {self.name!r}: timezone= applies to cron schedules. An "
                    f"interval is the same length in every zone; for 'every day at 03:00 "
                    f"local time' use cron='0 3 * * *' with the zone."
                )
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"schedule {self.name!r}: unknown time zone {self.timezone!r}"
            ) from exc

    @classmethod
    def build(
        cls,
        name: str,
        task: str,
        *,
        every: timedelta | None = None,
        cron: str | None = None,
        timezone: str = "UTC",
        payload: Mapping[str, Any] | None = None,
        catch_up: bool = True,
        max_attempts: int = 3,
    ) -> Schedule:
        return cls(
            name=name,
            task=task,
            every=every,
            cron=Cron.parse(cron) if cron is not None else None,
            timezone=timezone,
            payload=dict(payload or {}),
            catch_up=catch_up,
            max_attempts=max_attempts,
        )

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def latest_at_or_before(self, moment: datetime) -> datetime | None:
        """The most recent tick at or before ``moment``, in UTC."""
        if self.every is not None:
            return self._interval_floor(moment, self.every)
        return self._cron.latest_at_or_before(moment, self.zone)

    def next_after(self, moment: datetime) -> datetime:
        """The first tick strictly after ``moment``, in UTC."""
        if self.every is not None:
            return self._interval_floor(moment, self.every) + self.every
        return self._cron.next_after(moment, self.zone)

    @staticmethod
    def _interval_floor(moment: datetime, every: timedelta) -> datetime:
        # Integer microseconds: float seconds would drift off the tick after
        # enough of them, and two replicas would disagree about which one it is.
        step = every // _ONE_MICROSECOND
        elapsed = (_utc(moment) - EPOCH) // _ONE_MICROSECOND
        return EPOCH + (elapsed // step) * step * _ONE_MICROSECOND

    @property
    def _cron(self) -> Cron:
        if self.cron is None:
            raise ValueError(f"schedule {self.name!r} has no cron expression")
        return self.cron

    def job_id(self, fire_at: datetime) -> str:
        """The id of the job for one tick: the same in every replica.

        Two replicas that both enqueue a tick -- the claim store was
        unreachable, or answered after a timeout -- enqueue one job id, which
        the PostgreSQL queue inserts once and a handler can deduplicate on
        with ``claim_once``.
        """
        stamp = _utc(fire_at).isoformat()
        return uuid.uuid5(_TICK_NAMESPACE, f"{self.name}@{stamp}").hex

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "task": self.task,
            "every": self.every.total_seconds() if self.every is not None else None,
            "cron": str(self.cron) if self.cron is not None else None,
            "timezone": self.timezone,
            "catch_up": self.catch_up,
        }


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(f"{moment!r} has no time zone; pass an aware datetime")
    return moment.astimezone(UTC)
