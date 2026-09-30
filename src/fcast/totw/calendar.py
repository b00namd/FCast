"""TOTW weeks: EA releases a new Team of the Week every Wednesday evening.

TOTW n covers the matches since the previous release. The first release date and the time
are configurable; FUTBIN numbers the weeks the same way (TOTW1, TOTW2, ...).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

WEEK = timedelta(days=7)


@dataclass(frozen=True)
class TotwWeek:
    number: int
    release: datetime  # aware UTC
    window_start: datetime  # previous release; matches after this count for this week

    @property
    def window_end(self) -> datetime:
        return self.release


def parse_release_time(value: str) -> time:
    """ "19:00" -> time(19, 0) (validated in the settings)."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def release_at(first_release: date, release_time: time, tz: tzinfo, number: int) -> datetime:
    local_day = first_release + WEEK * (number - 1)
    return datetime.combine(local_day, release_time, tzinfo=tz).astimezone(UTC)


def week(first_release: date, release_time: time, tz: tzinfo, number: int) -> TotwWeek:
    release = release_at(first_release, release_time, tz, number)
    start = release_at(first_release, release_time, tz, number - 1)
    return TotwWeek(number=number, release=release, window_start=start)


def upcoming_week(now: datetime, first_release: date, release_time: time, tz: tzinfo) -> TotwWeek:
    """The next TOTW that has not been released yet at `now`."""
    number = 1
    while release_at(first_release, release_time, tz, number) <= now:
        number += 1
    return week(first_release, release_time, tz, number)


def last_released_week(
    now: datetime, first_release: date, release_time: time, tz: tzinfo
) -> TotwWeek | None:
    upcoming = upcoming_week(now, first_release, release_time, tz)
    if upcoming.number <= 1:
        return None
    return week(first_release, release_time, tz, upcoming.number - 1)
