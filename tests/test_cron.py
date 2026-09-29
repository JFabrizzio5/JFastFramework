"""The cron parser, and when an expression fires -- in UTC and across DST.

Dates are chosen, not generated: 2026-03-08 and 2026-11-01 are the nights New
York moves its clocks, 2026-03-29 is London's, and 2100 is the leap year the
Gregorian calendar skips. Each assertion names the instant in UTC, because
that is what the scheduler stores and compares.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jfastframework.queues.cron import Cron, CronError

NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
MEXICO = ZoneInfo("America/Mexico_City")


def at(text: str) -> datetime:
    """An instant written in UTC: ``at("2026-03-08 07:30")``."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def upcoming(expression: str, start: str, count: int, zone: ZoneInfo = NEW_YORK) -> list[str]:
    cron = Cron.parse(expression)
    moment = at(start)
    found: list[str] = []
    for _ in range(count):
        moment = cron.next_after(moment, zone)
        found.append(moment.strftime("%Y-%m-%d %H:%M"))
    return found


# -- parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        ("minutes", "*/15 * * * *", {0, 15, 30, 45}),
        ("minutes", "5/20 * * * *", {5, 25, 45}),
        ("minutes", "1,2,3 * * * *", {1, 2, 3}),
        ("minutes", "10-12,50 * * * *", {10, 11, 12, 50}),
        ("hours", "0 9-17/2 * * *", {9, 11, 13, 15, 17}),
        ("hours", "0 22-23,0-2 * * *", {22, 23, 0, 1, 2}),
        ("days", "0 0 1,15 * *", {1, 15}),
        ("months", "0 0 1 jan,JUL,Dec *", {1, 7, 12}),
        ("months", "0 0 1 */3 *", {1, 4, 7, 10}),
        ("weekdays", "0 0 * * mon-fri", {1, 2, 3, 4, 5}),
        ("weekdays", "0 0 * * 7", {0}),
        ("weekdays", "0 0 * * 5-7", {5, 6, 0}),
        ("weekdays", "0 0 * * SAT,sun", {6, 0}),
        ("weekdays", "0 0 * * Wed", {3}),
    ],
)
def test_fields_parse_to_the_values_they_name(field: str, text: str, expected: set[int]) -> None:
    assert getattr(Cron.parse(text), field) == frozenset(expected)


@pytest.mark.parametrize(
    ("macro", "expanded"),
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
        ("@ANNUALLY", "0 0 1 1 *"),
    ],
)
def test_shorthands_mean_what_crontab_means(macro: str, expanded: str) -> None:
    a, b = Cron.parse(macro), Cron.parse(expanded)
    assert (a.minutes, a.hours, a.days, a.months, a.weekdays) == (
        b.minutes,
        b.hours,
        b.days,
        b.months,
        b.weekdays,
    )


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("* * * *", "has 4 fields"),
        ("* * * * * *", "has 6 fields"),
        ("", "has 0 fields"),
        ("60 * * * *", "outside 0-59"),
        ("* 24 * * *", "outside 0-23"),
        ("* * 0 * *", "outside 1-31"),
        ("* * 32 * *", "outside 1-31"),
        ("* * * 13 *", "outside 1-12"),
        ("* * * * 8", "outside 0-7"),
        ("30-10 * * * *", "runs backwards"),
        ("*/0 * * * *", "step '0' is invalid"),
        ("*/x * * * *", "step 'x' is invalid"),
        ("1,,2 * * * *", "empty list item"),
        ("* * ? * *", "Quartz"),
        ("* * L * *", "Quartz"),
        ("* * 15W * *", "Quartz"),
        ("* * * * 5#2", "Quartz"),
        ("* * * * 5L", "Quartz"),
        ("* * * foo *", "not a number"),
        ("@every_minute", "not a known shorthand"),
        ("0 0 31 4 *", "never fire"),
        ("0 0 30,31 2 *", "never fire"),
    ],
)
def test_malformed_expressions_are_refused_with_the_reason(expression: str, reason: str) -> None:
    with pytest.raises(CronError, match=reason):
        Cron.parse(expression)


def test_a_leap_day_is_rare_not_impossible() -> None:
    assert Cron.parse("0 0 29 2 *").days == frozenset({29})


def test_april_31st_is_fine_when_a_weekday_can_carry_it() -> None:
    # Both day fields restricted: either matches, and Mondays exist.
    Cron.parse("0 0 31 4 mon")


def test_a_cron_error_is_a_value_error() -> None:
    assert issubclass(CronError, ValueError)


# -- which days --------------------------------------------------------------


def test_day_of_month_and_day_of_week_are_an_or_when_both_are_restricted() -> None:
    cron = Cron.parse("0 0 1 * mon")
    assert cron.matches_date(date(2026, 9, 1))  # a Tuesday, but the first
    assert cron.matches_date(date(2026, 9, 7))  # a Monday
    assert not cron.matches_date(date(2026, 9, 8))


def test_a_starred_day_field_leaves_the_other_in_charge() -> None:
    # `*/2` starts with `*`, so Vixie treats day-of-month as unrestricted.
    cron = Cron.parse("0 0 */2 * mon")
    assert cron.matches_date(date(2026, 9, 7))  # Monday the 7th
    assert not cron.matches_date(date(2026, 9, 3))  # Thursday the 3rd


def test_only_day_of_week_restricted() -> None:
    cron = Cron.parse("0 0 * * sat,sun")
    assert cron.matches_date(date(2026, 10, 3))
    assert not cron.matches_date(date(2026, 10, 5))


# -- next and previous, in UTC -----------------------------------------------


def test_next_is_strictly_after() -> None:
    cron = Cron.parse("*/15 * * * *")
    assert cron.next_after(at("2026-09-29 10:07"), UTC) == at("2026-09-29 10:15")
    assert cron.next_after(at("2026-09-29 10:15"), UTC) == at("2026-09-29 10:30")
    assert cron.next_after(at("2026-09-29 23:50"), UTC) == at("2026-09-30 00:00")


def test_latest_is_at_or_before() -> None:
    cron = Cron.parse("*/15 * * * *")
    assert cron.latest_at_or_before(at("2026-09-29 10:15"), UTC) == at("2026-09-29 10:15")
    assert cron.latest_at_or_before(at("2026-09-29 10:14:59"), UTC) == at("2026-09-29 10:00")


def test_seconds_do_not_leak_into_the_next_tick() -> None:
    cron = Cron.parse("0 3 * * *")
    assert cron.next_after(at("2026-09-29 03:00:00.5"), UTC) == at("2026-09-30 03:00")


def test_weekdays_skip_the_weekend() -> None:
    assert upcoming("30 9 * * mon-fri", "2026-10-02 10:00", 2, UTC) == [
        "2026-10-05 09:30",
        "2026-10-06 09:30",
    ]


def test_the_first_of_the_year() -> None:
    assert upcoming("0 0 1 1 *", "2026-09-29 00:00", 1, UTC) == ["2027-01-01 00:00"]


def test_february_29th_skips_2100() -> None:
    assert upcoming("0 0 29 2 *", "2097-03-01 00:00", 1, UTC) == ["2104-02-29 00:00"]
    latest = Cron.parse("0 0 29 2 *").latest_at_or_before(at("2104-02-28 00:00"), UTC)
    assert latest == at("2096-02-29 00:00")


def test_a_naive_moment_is_refused() -> None:
    with pytest.raises(ValueError, match="no time zone"):
        Cron.parse("* * * * *").next_after(datetime(2026, 1, 1), UTC)


def test_a_zone_without_dst_is_a_fixed_offset() -> None:
    # Mexico City dropped DST in 2022: 03:00 local is 09:00 UTC all year.
    assert upcoming("0 3 * * *", "2026-06-01 00:00", 1, MEXICO) == ["2026-06-01 09:00"]
    assert upcoming("0 3 * * *", "2026-12-01 00:00", 1, MEXICO) == ["2026-12-01 09:00"]


# -- daylight saving time ----------------------------------------------------
#
# New York, 2026: clocks jump 02:00 EST -> 03:00 EDT on 8 March (07:00 UTC),
# and fall back 02:00 EDT -> 01:00 EST on 1 November (06:00 UTC).


def test_a_wall_time_the_jump_skips_fires_moved_forward_by_the_jump() -> None:
    # 02:30 does not exist on 8 March; it fires at 03:30 EDT, which is 07:30 UTC.
    assert upcoming("30 2 * * *", "2026-03-07 12:00", 2) == [
        "2026-03-08 07:30",
        "2026-03-09 06:30",
    ]


def test_a_daily_job_at_the_jump_runs_at_the_moment_after_it() -> None:
    assert upcoming("0 2 * * *", "2026-03-07 12:00", 1) == ["2026-03-08 07:00"]


def test_an_hourly_job_runs_once_an_hour_through_the_jump() -> None:
    # 01:30 EST, then 03:30 EDT (where 02:30 would have been), then 04:30 EDT.
    assert upcoming("30 * * * *", "2026-03-08 06:00", 3) == [
        "2026-03-08 06:30",
        "2026-03-08 07:30",
        "2026-03-08 08:30",
    ]


def test_a_job_pinned_to_the_repeated_hour_runs_once() -> None:
    # 01:30 happens twice on 1 November; the job runs on the first, EDT.
    assert upcoming("30 1 * * *", "2026-10-31 12:00", 2) == [
        "2026-11-01 05:30",
        "2026-11-02 06:30",
    ]
    assert upcoming("*/30 1 * * *", "2026-11-01 04:00", 3) == [
        "2026-11-01 05:00",
        "2026-11-01 05:30",
        "2026-11-02 06:00",
    ]


def test_an_hourly_job_keeps_running_every_real_hour_through_the_repeat() -> None:
    # 00:30 EDT, 01:30 EDT, 01:30 EST, 02:30 EST.
    assert upcoming("30 * * * *", "2026-11-01 04:00", 4) == [
        "2026-11-01 04:30",
        "2026-11-01 05:30",
        "2026-11-01 06:30",
        "2026-11-01 07:30",
    ]


def test_latest_through_the_repeated_hour() -> None:
    hourly = Cron.parse("30 * * * *")
    assert hourly.latest_at_or_before(at("2026-11-01 06:45"), NEW_YORK) == at("2026-11-01 06:30")
    pinned = Cron.parse("30 1 * * *")
    assert pinned.latest_at_or_before(at("2026-11-01 06:45"), NEW_YORK) == at("2026-11-01 05:30")


def test_latest_through_the_jump() -> None:
    cron = Cron.parse("30 2 * * *")
    assert cron.latest_at_or_before(at("2026-03-08 07:29"), NEW_YORK) == at("2026-03-07 07:30")
    assert cron.latest_at_or_before(at("2026-03-08 07:30"), NEW_YORK) == at("2026-03-08 07:30")


def test_london_at_one_in_the_morning_on_the_night_it_does_not_exist() -> None:
    # 29 March 2026: 01:00 GMT jumps to 02:00 BST; 01:00 moves to 02:00 BST,
    # which is 01:00 UTC.
    assert upcoming("0 1 * * *", "2026-03-28 12:00", 2, LONDON) == [
        "2026-03-29 01:00",
        "2026-03-30 00:00",
    ]


@pytest.mark.parametrize("zone", [UTC, NEW_YORK, LONDON, ZoneInfo("Australia/Lord_Howe")])
@pytest.mark.parametrize("expression", ["*/7 * * * *", "30 * * * *", "15 1,2,3 * * *"])
def test_next_and_latest_agree_everywhere(expression: str, zone: ZoneInfo) -> None:
    """Walk a year's worth of DST transitions: ticks rise strictly, and each is
    the latest at its own instant and not a microsecond before."""
    cron = Cron.parse(expression)
    # Hours before each transition: New York's two, London's two, and Lord
    # Howe's, whose clocks move by thirty minutes.
    transitions = (
        "2026-03-08 00:00",
        "2026-11-01 00:00",
        "2026-03-28 18:00",
        "2026-10-24 18:00",
        "2026-04-04 06:00",
        "2026-10-03 06:00",
    )
    for start in transitions:
        moment = at(start)
        for _ in range(150):
            following = cron.next_after(moment, zone)
            assert following > moment
            assert cron.latest_at_or_before(following, zone) == following
            before = cron.latest_at_or_before(following - timedelta(microseconds=1), zone)
            assert before is not None and before < following
            moment = following
