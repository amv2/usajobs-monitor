"""A small foreground scheduler for the Docker image: ``python -m usajobs_monitor.schedule``.

Containers usually have no cron, so this sleeps until the next scheduled time, runs
``usajobs-monitor`` with the configured arguments, and repeats. Settings come from
environment variables so a compose file can hold them:

- ``USAJOBS_SCHEDULE_DAYS``: days to run, e.g. ``Mon,Wed,Fri`` (the default) or ``daily``.
- ``USAJOBS_SCHEDULE_TIMES``: 24-hour times, e.g. ``07:00`` (the default) or ``07:00,17:00``.
- ``USAJOBS_SCHEDULE_ARGS``: arguments for each run (default ``run --all``).
- ``USAJOBS_RUN_ON_START``: ``1`` also runs once right away.

Times use the container's local time, so set ``TZ`` (for example ``America/New_York``).
A failed run is logged and the schedule carries on; the run itself handles its own
failure notice.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import os
import shlex
import signal
import subprocess
import sys
import time as clock
from typing import Mapping, Sequence

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_DAYS = "Mon,Wed,Fri"
DEFAULT_TIMES = "07:00"
DEFAULT_ARGS = "run --all"


class ScheduleError(ValueError):
    pass


def parse_days(value: str) -> frozenset[int]:
    """Weekday numbers (Monday is 0) from ``Mon,Wed,Fri``, ``mon-fri`` or ``daily``."""
    text = value.strip().lower()
    if text in ("daily", "*", "every day"):
        return frozenset(range(7))
    if text in ("weekdays", "weekday"):
        return frozenset(range(5))
    days: set[int] = set()
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "-" in part:
            start, _, end = part.partition("-")
            first, last = _day(start), _day(end)
            if first > last:
                raise ScheduleError(f"day range {part!r} must run Monday to Sunday, e.g. mon-fri")
            days.update(range(first, last + 1))
        else:
            days.add(_day(part))
    if not days:
        raise ScheduleError("USAJOBS_SCHEDULE_DAYS names no days")
    return frozenset(days)


def _day(name: str) -> int:
    key = name.strip()[:3]
    if key not in DAY_NAMES:
        raise ScheduleError(f"unknown day {name.strip()!r}; use Mon, Tue, Wed, Thu, Fri, Sat or Sun")
    return DAY_NAMES.index(key)


def parse_times(value: str) -> tuple[time, ...]:
    """Sorted times from ``07:00`` or ``07:00,17:30``."""
    times: set[time] = set()
    for part in filter(None, (p.strip() for p in value.split(","))):
        try:
            hour, minute = (int(n) for n in part.split(":"))
            times.add(time(hour, minute))
        except ValueError:
            raise ScheduleError(f"time {part!r} must look like 07:00 (24-hour)") from None
    if not times:
        raise ScheduleError("USAJOBS_SCHEDULE_TIMES names no times")
    return tuple(sorted(times))


def next_run(now: datetime, days: frozenset[int], times: Sequence[time]) -> datetime:
    """The first scheduled moment strictly after ``now``."""
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        for at in times:
            candidate = datetime.combine(day, at, now.tzinfo)
            if candidate > now:
                return candidate
    raise ScheduleError("no scheduled time found in the next week")  # unreachable with valid input


def load(env: Mapping[str, str]) -> tuple[frozenset[int], tuple[time, ...], list[str]]:
    days = parse_days(env.get("USAJOBS_SCHEDULE_DAYS") or DEFAULT_DAYS)
    times = parse_times(env.get("USAJOBS_SCHEDULE_TIMES") or DEFAULT_TIMES)
    args = shlex.split(env.get("USAJOBS_SCHEDULE_ARGS") or DEFAULT_ARGS)
    return days, times, args


def run_once(args: Sequence[str]) -> int:
    log(f"starting: usajobs-monitor {shlex.join(args)}")
    code = subprocess.call([sys.executable, "-m", "usajobs_monitor", *args])
    log(f"finished with exit code {code}")
    return code


def log(message: str) -> None:
    print(f"[schedule {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def main(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    # As a container's PID 1 the default SIGTERM action is ignored, so `docker stop`
    # would wait out its timeout. Exit promptly instead; a run in progress stops
    # with the container.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        days, times, args = load(env)
    except ScheduleError as exc:
        print(f"usajobs-monitor schedule: {exc}", file=sys.stderr)
        return 2
    day_text = ",".join(DAY_NAMES[d].title() for d in sorted(days))
    log(f"days {day_text} at {', '.join(t.strftime('%H:%M') for t in times)} (TZ={env.get('TZ') or 'UTC'})")
    if env.get("USAJOBS_RUN_ON_START", "").strip().lower() in ("1", "true", "yes"):
        run_once(args)
    while True:
        due = next_run(datetime.now(), days, times)
        log(f"next run {due.strftime('%a %Y-%m-%d %H:%M')}")
        # Sleep in short steps so a clock change or a suspended host doesn't overshoot.
        while (remaining := (due - datetime.now()).total_seconds()) > 0:
            clock.sleep(min(remaining, 300))
        run_once(args)


if __name__ == "__main__":
    sys.exit(main())
