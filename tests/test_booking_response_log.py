import asyncio
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from test_support import TempProjectCaseMixin, reload_modules


class FakeHeaders(dict):
    def getall(self, key, default=None):
        value = self.get(key)
        if value is not None:
            return [value]
        return default if default is not None else []


class BookingResponseLogTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.monitor = reload_modules(
            "config_store",
            "monitor_store",
            "usvisa_appointment_monitor",
        )

    def test_response_preview_redacts_sensitive_values(self):
        state = self.monitor.MonitorState(
            cookie="session=private-cookie",
            csrf_token="private-csrf",
            session_id="private-session-id",
        )

        preview = self.monitor._safe_booking_response_preview(
            '<html>private-csrf private-cookie private-session-id '
            'user@example.com <input name="authenticity_token" '
            'value="private-form-token">Appointment is no longer available</html>',
            state,
        )

        self.assertIn("Appointment is no longer available", preview)
        for secret in (
            "private-csrf",
            "private-cookie",
            "private-session-id",
            "private-form-token",
            "user@example.com",
        ):
            self.assertNotIn(secret, preview)
        self.assertLessEqual(len(preview), 300)

    def test_booking_post_persists_sanitized_response(self):
        class FakeResponse:
            status = 422
            url = "https://ais.usvisa-info.com/ru-kz/niv/schedule/1/appointment?secret=query"
            headers = FakeHeaders(
                {
                    "Session-Id": "replacement-session-id",
                    "Set-Cookie": "session=replacement-cookie; Path=/",
                }
            )

            async def text(self):
                return (
                    '<html><input name="authenticity_token" value="private-form-token">'
                    "private-csrf private-cookie private-session-id user@example.com "
                    + ("x" * 350)
                    + "Appointment is no longer available</html>"
                )

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            def post(self, *args, **kwargs):
                return FakeResponse()

        log_file = self.temp_path / "booking-attempts.jsonl"
        state = self.monitor.MonitorState(
            cookie="session=private-cookie",
            csrf_token="private-csrf",
            session_id="private-session-id",
        )
        timing = {"run_started": self.monitor.time.perf_counter(), "slot_discovered": None}

        output = io.StringIO()
        with (
            mock.patch.object(self.monitor, "BOOKING_ATTEMPT_LOG_FILE", log_file),
            redirect_stdout(output),
        ):
            booked = asyncio.run(
                self.monitor._submit_booking(
                    FakeSession(),
                    state,
                    "134",
                    "2027-02-05",
                    "10:00",
                    timing,
                )
            )

        self.assertFalse(booked)
        record = json.loads(log_file.read_text(encoding="utf-8"))
        self.assertEqual(record["date"], "2027-02-05")
        self.assertEqual(record["time"], "10:00")
        self.assertEqual(record["status"], 422)
        self.assertEqual(record["final_path"], "/ru-kz/niv/schedule/1/appointment")
        self.assertFalse(record["confirmed"])
        self.assertGreater(record["body_length"], 0)
        self.assertEqual(len(record["body_sha256"]), 64)
        self.assertIn("Appointment is no longer available", record["response_body"])
        self.assertFalse(record["body_truncated"])
        serialized = json.dumps(record)
        for secret in (
            "private-form-token",
            "private-csrf",
            "private-cookie",
            "private-session-id",
            "user@example.com",
            "secret=query",
        ):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(os.stat(log_file).st_mode & 0o777, 0o600)

    def test_booking_attempt_log_rotates_to_recent_records(self):
        log_file = Path(self.temp_path) / "booking-attempts.jsonl"
        with (
            mock.patch.object(self.monitor, "BOOKING_ATTEMPT_LOG_FILE", log_file),
            mock.patch.object(self.monitor, "BOOKING_ATTEMPT_LOG_MAX_BYTES", 1),
            mock.patch.object(self.monitor, "BOOKING_ATTEMPT_LOG_RETAIN_LINES", 2),
        ):
            for index in range(3):
                self.monitor._persist_booking_attempt({"sequence": index})

        records = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records, [{"sequence": 1}, {"sequence": 2}])


if __name__ == "__main__":
    unittest.main()
