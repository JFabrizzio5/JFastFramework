"""Five-field cron expressions, evaluated in a time zone.

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-7 (0 and 7 are Sunday)

Each field takes ``*``, a number, a list (``1,15``), a range (``9-17``) and a
step over either (``*/15``, ``9-17/2``, ``5/10`` meaning ``5-59/10``). Months
and weekdays also take their English three-letter names (``jan``, ``mon-fri``).
``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly`` and
``@yearly``/``@annually`` are shorthands. Quartz's ``?``, ``L``, ``W`` and
``#`` are refused rather than half-understood.

Day-of-month and day-of-week follow Vixie cron, which is what anyone who has
written a crontab expects: when **both** are restricted a day matches if
**either** does (``0 0 1 * mon`` is the first of the month *and* every
Monday); when one is ``*`` only the other counts. A field counts as ``*`` when
it starts with one, so ``*/2`` in day-of-month leaves day-of-week in charge.

**Daylight saving time**, for a zone that has it:

- A wall-clock time the clocks skip -- 02:30 on the night they jump from 02:00
  to 03:00 -- fires once, moved forward by the length of the jump: 03:30. A
  daily job is not silently dropped for the year's shortest night, and an
  hourly one does not fire twice, because the moved time is the same instant
  as the real 03:30.
- A wall-clock time that happens twice -- 01:30 on the night they go back --
  fires on its first occurrence only, unless the hour field is ``*``. A job
  pinned to an hour runs once a day; one that runs every hour keeps running
  every real hour through the repeat.

Nothing here reads the system clock or the machine's zone; callers pass both.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from zoneinfo import ZoneInfo

__all__ = ["Cron", "CronError"]

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

MONTH_NAMES = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
WEEKDAY_NAMES = {
    name: number for number, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}

# February counts as 29: an expression that can only fire on a leap day is
# rare, not impossible.
_LONGEST_MONTH = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30}

#: No offset change anywhere moves a wall clock by more than this, so a wall
#: time further than this from a candidate cannot produce an earlier (or later)
#: instant than it. It bounds how far the search looks past its first answer.
_MAX_SHIFT = timedelta(hours=3)

#: How far ahead or behind the search looks for a matching day. Eight years
#: covers the longest legitimate gap -- February 29 across a skipped leap year
#: like 2100 -- and a search that ran unbounded on an expression that never
#: fires would hang the scheduler instead of failing.
_HORIZON_DAYS = 366 * 8 + 2


class CronError(ValueError):
    """An expression this parser does not accept, with the reason."""


def _value(text: str, names: dict[str, int] | None, field: str, expression: str) -> int:
    if text.isdigit():
        return int(text)
    if names is not None and text.lower() in names:
        return names[text.lower()]
    if text.upper() in ("L", "LW") or (text[:-1].isdigit() and text[-1:].upper() in ("L", "W")):
        raise CronError(
            f"{field} field of {expression!r}: {text!r} is Quartz syntax, which this "
            f"five-field parser does not support"
        )
    raise CronError(f"{field} field of {expression!r}: {text!r} is not a number")


def _parse_field(
    text: str,
    *,
    field: str,
    low: int,
    high: int,
    expression: str,
    names: dict[str, int] | None = None,
) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{field} field of {expression!r} has an empty list item")
        if "?" in part or "#" in part:
            raise CronError(
                f"{field} field of {expression!r}: {part!r} is Quartz syntax, which this "
                f"five-field parser does not support"
            )
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"{field} field of {expression!r}: step {step_text!r} is invalid")
            step = int(step_text)

        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _value(first, names, field, expression)
            end = _value(last, names, field, expression)
            if start > end:
                raise CronError(
                    f"{field} field of {expression!r}: range {base!r} runs backwards; "
                    f"a range that wraps around is two ranges, e.g. 22-23,0-2"
                )
        else:
            start = _value(base, names, field, expression)
            # `5/10` is `5-59/10`: a step from a single value runs to the end.
            end = high if slash else start

        if start < low or end > high:
            raise CronError(f"{field} field of {expression!r}: {part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    """A parsed expression. Build one with :meth:`parse`."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    # Vixie's rule: a field that starts with `*` does not restrict the day.
    days_restricted: bool
    weekdays_restricted: bool
    every_hour: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        text = expression.strip()
        expanded = MACROS.get(text.lower(), text)
        if expanded.startswith("@"):
            raise CronError(f"{expression!r} is not a known shorthand: {', '.join(MACROS)}")
        fields = expanded.split()
        if len(fields) != 5:
            raise CronError(
                f"{expression!r} has {len(fields)} fields; cron takes five: "
                f"minute hour day-of-month month day-of-week"
            )
        minute, hour, day, month, weekday = fields
        weekdays = _parse_field(
            weekday,
            field="day-of-week",
            low=0,
            high=7,
            expression=expression,
            names=WEEKDAY_NAMES,
        )
        cron = cls(
            expression=expression,
            minutes=_parse_field(minute, field="minute", low=0, high=59, expression=expression),
            hours=_parse_field(hour, field="hour", low=0, high=23, expression=expression),
            days=_parse_field(day, field="day-of-month", low=1, high=31, expression=expression),
            months=_parse_field(
                month, field="month", low=1, high=12, expression=expression, names=MONTH_NAMES
            ),
            # 7 is Sunday too, so that `mon-sun` reads the way it is written.
            weekdays=frozenset(0 if value == 7 else value for value in weekdays),
            days_restricted=not day.startswith("*"),
            weekdays_restricted=not weekday.startswith("*"),
            every_hour=hour == "*",
        )
        cron._refuse_impossible_dates()
        return cron

    def _refuse_impossible_dates(self) -> None:
        # With day-of-week restricted too, the days are an OR and some weekday
        # always comes round. Otherwise `0 0 31 4 *` -- April 31 -- would send
        # the search to the end of its horizon on every call.
        if not self.days_restricted or self.weekdays_restricted:
            return
        if any(day <= _LONGEST_MONTH.get(month, 31) for month in self.months for day in self.days):
            return
        raise CronError(f"{self.expression!r} names no date that exists; it would never fire")

    # -- matching ------------------------------------------------------

    def matches_date(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        by_day = day.day in self.days
        # isoweekday: Monday 1 .. Sunday 7, and cron's Sunday is 0.
        by_weekday = day.isoweekday() % 7 in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return by_day or by_weekday
        if self.days_restricted:
            return by_day
        if self.weekdays_restricted:
            return by_weekday
        return True

    def _walls(self, start: datetime, *, forward: bool) -> Iterator[datetime]:
        """Matching wall-clock minutes from ``start`` (inclusive), in order."""
        hours = sorted(self.hours, reverse=not forward)
        minutes = sorted(self.minutes, reverse=not forward)
        step = timedelta(days=1 if forward else -1)
        day = start.date()
        for _ in range(_HORIZON_DAYS):
            if self.matches_date(day):
                for hour in hours:
                    for minute in minutes:
                        wall = datetime.combine(day, clock_time(hour, minute))
                        if (wall >= start) if forward else (wall <= start):
                            yield wall
            day += step

    def _instants(self, wall: datetime, zone: ZoneInfo) -> list[datetime]:
        """The instants a wall-clock time names in ``zone``: none extra, one, or two."""
        first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
        second = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
        if first < second and self.every_hour:
            # The wall time happens twice and the job runs every hour: both.
            return [first, second]
        # Ambiguous with a fixed hour: the first occurrence. Skipped by a jump
        # forward: zoneinfo's fold=0 reading, which is the time moved forward
        # by the length of the jump.
        return [first]

    # -- the two questions a scheduler asks ------------------------------

    def next_after(self, moment: datetime, zone: ZoneInfo) -> datetime:
        """The first firing strictly after ``moment``, in UTC."""
        moment = _aware(moment)
        local = moment.astimezone(zone).replace(tzinfo=None, fold=0)
        start = (local - _MAX_SHIFT).replace(second=0, microsecond=0)
        best: datetime | None = None
        best_wall: datetime | None = None
        for wall in self._walls(start, forward=True):
            if best_wall is not None and wall > best_wall + _MAX_SHIFT:
                break
            for instant in self._instants(wall, zone):
                if instant > moment and (best is None or instant < best):
                    best = instant
                    best_wall = instant.astimezone(zone).replace(tzinfo=None)
        if best is None:
            raise CronError(f"{self.expression!r} does not fire within eight years of {moment}")
        return best

    def latest_at_or_before(self, moment: datetime, zone: ZoneInfo) -> datetime | None:
        """The last firing at or before ``moment``, in UTC, or None."""
        moment = _aware(moment)
        local = moment.astimezone(zone).replace(tzinfo=None, fold=0)
        start = local + _MAX_SHIFT
        best: datetime | None = None
        best_wall: datetime | None = None
        for wall in self._walls(start, forward=False):
            if best_wall is not None and wall < best_wall - _MAX_SHIFT:
                break
            for instant in self._instants(wall, zone):
                if instant <= moment and (best is None or instant > best):
                    best = instant
                    best_wall = instant.astimezone(zone).replace(tzinfo=None)
        return best

    def __str__(self) -> str:
        return self.expression


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(
            f"{moment!r} has no time zone. A naive datetime could be any of "
            f"twenty-odd instants; pass an aware one, e.g. datetime.now(UTC)."
        )
    return moment.astimezone(UTC)
