import asyncio
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

from test_support import TempProjectCaseMixin, reload_modules


class ImmediatePriorityBookingTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.monitor = reload_modules(
            "config_store",
            "monitor_store",
            "usvisa_appointment_monitor",
        )

    def test_job_attempts_priority_booking_before_fetching_ordinary_times(self):
        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        calls = []
        state = self.monitor.MonitorState()

        async def fetch_times(session, runtime_state, date):
            calls.append(f"times:{date}")
            return ["10:15"] if date == "2026-07-09" else ["11:30"]

        async def try_booking(
            session,
            runtime_state,
            current_dates,
            current_appointments,
            timing,
        ):
            calls.append(f"book:{next(iter(current_appointments))}")
            return None

        async def refresh_session(session, runtime_state):
            calls.append("refresh-session")
            return True

        self.monitor.PRIORITY_DATES = ["2026-07-09"]
        self.monitor.PREPARATION_DAYS = 1
        with (
            mock.patch.object(self.monitor.aiohttp, "ClientSession", FakeClientSession),
            mock.patch.object(self.monitor, "_read_state", return_value=state),
            mock.patch.object(self.monitor, "_write_state"),
            mock.patch.object(self.monitor, "_schedule_next_check"),
            mock.patch.object(
                self.monitor,
                "_booking_allowed_from_date",
                return_value="2026-07-01",
            ),
            mock.patch.object(
                self.monitor,
                "fetch_dates_with_cookie_candidates",
                new=mock.AsyncMock(return_value=["2026-07-09", "2026-07-10"]),
            ),
            mock.patch.object(self.monitor, "fetch_available_times", new=fetch_times),
            mock.patch.object(
                self.monitor,
                "refresh_session_from_appointment_page",
                new=refresh_session,
            ),
            mock.patch.object(self.monitor, "_try_autobook_priority_dates", new=try_booking),
            mock.patch.object(
                self.monitor,
                "notify_new_dates",
                new=mock.AsyncMock(return_value=True),
            ),
        ):
            asyncio.run(self.monitor.job(force_run=True))

        self.assertLess(calls.index("refresh-session"), calls.index("times:2026-07-09"))
        self.assertLess(calls.index("book:2026-07-09"), calls.index("times:2026-07-10"))
        self.assertEqual(calls.count("times:2026-07-09"), 1)

    def test_timing_log_contains_wall_clocks_and_elapsed_milliseconds(self):
        timing = {"run_started": 10.0, "slot_discovered": 11.5}
        output = io.StringIO()

        with (
            mock.patch.object(
                self.monitor,
                "_utc_now",
                return_value=datetime(2026, 6, 19, 14, 15, 1, 234000, tzinfo=timezone.utc),
            ),
            mock.patch.object(self.monitor.time, "perf_counter", return_value=12.0),
            redirect_stdout(output),
        ):
            self.monitor._timing_log(
                "booking_post_start",
                timing=timing,
                date="2026-07-09",
                time_value="10:15",
            )

        line = output.getvalue()
        self.assertIn("utc=2026-06-19T14:15:01.234+00:00", line)
        self.assertIn("msk=2026-06-19T17:15:01.234+03:00", line)
        self.assertIn("event=booking_post_start", line)
        self.assertIn("run_elapsed_ms=2000", line)
        self.assertIn("slot_elapsed_ms=500", line)

    def test_successful_priority_booking_stops_later_time_requests(self):
        class FakeClientSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        calls = []
        state = self.monitor.MonitorState()

        async def fetch_times(session, runtime_state, date):
            calls.append(f"times:{date}")
            return ["10:15"]

        async def try_booking(
            session,
            runtime_state,
            current_dates,
            current_appointments,
            timing,
        ):
            calls.append(f"book:{next(iter(current_appointments))}")
            return {"date": "2026-07-09", "time": "10:15", "method": "http"}

        self.monitor.PRIORITY_DATES = ["2026-07-09"]
        self.monitor.PREPARATION_DAYS = 1
        with (
            mock.patch.object(self.monitor.aiohttp, "ClientSession", FakeClientSession),
            mock.patch.object(self.monitor, "_read_state", return_value=state),
            mock.patch.object(self.monitor, "_write_state"),
            mock.patch.object(
                self.monitor,
                "_booking_allowed_from_date",
                return_value="2026-07-01",
            ),
            mock.patch.object(
                self.monitor,
                "fetch_dates_with_cookie_candidates",
                new=mock.AsyncMock(return_value=["2026-07-09", "2026-07-10"]),
            ),
            mock.patch.object(self.monitor, "fetch_available_times", new=fetch_times),
            mock.patch.object(
                self.monitor,
                "refresh_session_from_appointment_page",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch.object(self.monitor, "_try_autobook_priority_dates", new=try_booking),
        ):
            asyncio.run(self.monitor.job(force_run=True))

        self.assertEqual(calls, ["times:2026-07-09", "book:2026-07-09"])

    def test_booking_response_preview_redacts_session_values(self):
        state = self.monitor.MonitorState(
            cookie="session=private-cookie",
            csrf_token="private-csrf",
        )
        preview = self.monitor._safe_booking_response_preview(
            "<html>private-csrf private-cookie " + ("x" * 500) + "</html>",
            state,
        )

        self.assertNotIn("private-csrf", preview)
        self.assertNotIn("private-cookie", preview)
        self.assertLessEqual(len(preview), 300)

    def test_booking_post_exception_has_terminal_timing_event(self):
        class FailingSession:
            def post(self, *args, **kwargs):
                raise RuntimeError("network down")

        timing = {"run_started": self.monitor.time.perf_counter(), "slot_discovered": None}
        output = io.StringIO()

        with redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "network down"):
                asyncio.run(
                    self.monitor._submit_booking(
                        FailingSession(),
                        self.monitor.MonitorState(cookie="session=1", csrf_token="csrf"),
                        "134",
                        "2026-07-09",
                        "10:15",
                        timing,
                    )
                )

        self.assertIn("event=booking_post_error", output.getvalue())
        self.assertIn("error_type=RuntimeError", output.getvalue())


if __name__ == "__main__":
    unittest.main()
