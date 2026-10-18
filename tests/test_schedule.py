"""Offline checks for the Docker image's built-in scheduler."""

from datetime import datetime, time
import unittest
from unittest.mock import patch

from usajobs_monitor import schedule


class ParseTests(unittest.TestCase):
    def test_days(self):
        self.assertEqual(schedule.parse_days("Mon,Wed,Fri"), {0, 2, 4})
        self.assertEqual(schedule.parse_days("monday, friday"), {0, 4})
        self.assertEqual(schedule.parse_days("mon-fri"), set(range(5)))
        self.assertEqual(schedule.parse_days("weekdays"), set(range(5)))
        self.assertEqual(schedule.parse_days("daily"), set(range(7)))

    def test_bad_days(self):
        for value in ("", "funday", "fri-mon", ","):
            with self.subTest(value=value), self.assertRaises(schedule.ScheduleError):
                schedule.parse_days(value)

    def test_times(self):
        self.assertEqual(schedule.parse_times("17:30, 07:00"), (time(7, 0), time(17, 30)))
        for value in ("", "7am", "25:00", "07:00:00"):
            with self.subTest(value=value), self.assertRaises(schedule.ScheduleError):
                schedule.parse_times(value)

    def test_defaults(self):
        days, times, args = schedule.load({})
        self.assertEqual(days, {0, 2, 4})
        self.assertEqual(times, (time(7, 0),))
        self.assertEqual(args, ["run", "--all"])

    def test_args_are_shell_split(self):
        _, _, args = schedule.load({"USAJOBS_SCHEDULE_ARGS": 'run --profile "spouse-dc" --no-deliver'})
        self.assertEqual(args, ["run", "--profile", "spouse-dc", "--no-deliver"])


class NextRunTests(unittest.TestCase):
    MWF = frozenset({0, 2, 4})
    SEVEN = (time(7, 0),)

    def test_later_today(self):  # 2026-10-19 is a Monday
        self.assertEqual(schedule.next_run(datetime(2026, 10, 19, 6, 59), self.MWF, self.SEVEN),
                         datetime(2026, 10, 19, 7, 0))

    def test_exactly_on_time_moves_on(self):
        self.assertEqual(schedule.next_run(datetime(2026, 10, 19, 7, 0), self.MWF, self.SEVEN),
                         datetime(2026, 10, 21, 7, 0))

    def test_second_time_same_day(self):
        times = (time(7, 0), time(17, 0))
        self.assertEqual(schedule.next_run(datetime(2026, 10, 19, 8, 0), self.MWF, times),
                         datetime(2026, 10, 19, 17, 0))

    def test_wraps_to_next_week(self):  # Friday evening -> Monday
        self.assertEqual(schedule.next_run(datetime(2026, 10, 23, 9, 0), self.MWF, self.SEVEN),
                         datetime(2026, 10, 26, 7, 0))

    def test_single_day_a_week_later(self):
        self.assertEqual(schedule.next_run(datetime(2026, 10, 19, 8, 0), frozenset({0}), self.SEVEN),
                         datetime(2026, 10, 26, 7, 0))


class MainTests(unittest.TestCase):
    def setUp(self):
        signals = patch.object(schedule.signal, "signal")  # keep the test process's handlers
        signals.start()
        self.addCleanup(signals.stop)

    def test_bad_settings_exit_2_without_running(self):
        with patch.object(schedule, "run_once") as run, patch("sys.stderr"):
            self.assertEqual(schedule.main({"USAJOBS_SCHEDULE_DAYS": "someday"}), 2)
        run.assert_not_called()

    def test_run_on_start_then_waits(self):
        class Stop(Exception):
            pass

        with patch.object(schedule, "run_once") as run, patch.object(schedule, "log"), \
                patch.object(schedule.clock, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):
                schedule.main({"USAJOBS_RUN_ON_START": "1", "USAJOBS_SCHEDULE_DAYS": "daily"})
        run.assert_called_once_with(["run", "--all"])


if __name__ == "__main__":
    unittest.main()
