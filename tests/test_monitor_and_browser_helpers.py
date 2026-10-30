import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from test_support import TempProjectCaseMixin, reload_modules


class MonitorHelperTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.monitor = reload_modules(
            "config_store",
            "monitor_store",
            "usvisa_appointment_monitor",
        )

    def test_cookie_merge_and_browser_headers_normalization(self):
        merged = self.monitor._merge_set_cookies(
            "foo=1; bar=2",
            ["bar=updated; Path=/", "baz=3; HttpOnly"],
        )
        self.assertEqual(
            self.monitor._parse_cookie_header(merged),
            {"foo": "1", "bar": "updated", "baz": "3"},
        )
        self.assertEqual(
            self.monitor._parse_browser_headers(
                {
                    "user-agent": "Browser/1.0",
                    "cookie": "skip-me",
                    "x-extra-header": "value",
                    "session-id": "skip-me-too",
                }
            ),
            {"User-Agent": "Browser/1.0", "X-Extra-Header": "value"},
        )

    def test_build_headers_and_appointment_headers(self):
        state = self.monitor.MonitorState(
            cookie="session=1",
            csrf_token="csrf-1",
            session_id="sid-1",
            if_none_match='"etag-1"',
            browser_headers={"accept-language": "en-US"},
        )

        headers = self.monitor._build_headers(state)
        appointment_headers = self.monitor._build_appointment_headers(state)

        self.assertEqual(headers["Cookie"], "session=1")
        self.assertEqual(headers["X-Csrf-Token"], "csrf-1")
        self.assertEqual(headers["Session-Id"], "sid-1")
        self.assertEqual(headers["If-None-Match"], '"etag-1"')
        self.assertEqual(headers["Accept-Language"], "en-US")
        self.assertNotIn("X-Csrf-Token", appointment_headers)
        self.assertNotIn("Session-Id", appointment_headers)
        self.assertEqual(appointment_headers["Sec-Fetch-Mode"], "navigate")

    def test_priority_candidate_selection_and_summary(self):
        self.monitor.PREPARATION_DAYS = 2
        self.monitor.PRIORITY_DATES = ["2026-07-05", "2026-07-10", "2026-07-20"]
        with mock.patch.object(
            self.monitor,
            "datetime",
            wraps=self.monitor.datetime,
        ) as fake_datetime:
            fake_datetime.now.return_value = datetime(2026, 7, 1, tzinfo=timezone.utc)
            fake_datetime.strptime = self.monitor.datetime.strptime
            fake_datetime.fromisoformat = self.monitor.datetime.fromisoformat
            candidates = self.monitor._build_priority_booking_candidates(
                ["2026-07-05", "2026-07-10", "2026-07-20"],
                {
                    "2026-07-05": {"09:00"},
                    "2026-07-10": {"09:00", "10:00", "11:00"},
                    "2026-07-20": set(),
                },
            )

        self.assertEqual(candidates, [("2026-07-10", "10:00"), ("2026-07-05", "09:00")])

        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        state = self.monitor.MonitorState(
            monitor_events=[
                {
                    "at": (now - timedelta(minutes=10)).isoformat(),
                    "status": "success",
                    "found_dates": ["2026-07-10", "2026-07-11"],
                    "ignored_dates": ["2026-07-11"],
                },
                {
                    "at": (now - timedelta(minutes=5)).isoformat(),
                    "status": "error",
                },
            ]
        )
        self.assertEqual(self.monitor._hourly_monitor_summary(state, now), (1, 1, 1))

    def test_healthcheck_summary_uses_configured_interval(self):
        self.monitor.HEALTHCHECK_INTERVAL_MINUTES = 180
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        state = self.monitor.MonitorState(
            monitor_events=[
                {
                    "at": (now - timedelta(hours=2)).isoformat(),
                    "status": "success",
                    "found_dates": ["2026-07-10"],
                    "ignored_dates": [],
                },
                {
                    "at": (now - timedelta(hours=4)).isoformat(),
                    "status": "error",
                },
            ]
        )
        captured = {}

        async def fake_send(message):
            captured["message"] = message
            return True

        with (
            mock.patch.object(self.monitor, "_utc_now", return_value=now),
            mock.patch.object(self.monitor, "record_event", fake_send),
        ):
            sent = asyncio.run(self.monitor.notify_healthcheck(state))

        self.assertTrue(sent)
        self.assertIn("За последние 3 часа монитор находил даты вне игнор-листа.", captured["message"])
        self.assertIn("Успешных проверок за последние 3 часа: 1", captured["message"])
        self.assertIn("Ошибок за последние 3 часа: 0", captured["message"])
        self.assertIn("Найдено дат вне игнор-листа за последние 3 часа: 1", captured["message"])

    def test_error_digest_is_sent_no_more_than_every_three_hours(self):
        self.assertEqual(self.monitor.ERROR_DIGEST_INTERVAL_MINUTES, 180)
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        state = self.monitor.MonitorState()
        captured_messages = []

        async def fake_send(message):
            captured_messages.append(message)
            return True

        with (
            mock.patch.object(self.monitor, "_utc_now", return_value=now),
            mock.patch.object(self.monitor, "record_event", fake_send),
        ):
            asyncio.run(self.monitor._record_error_and_maybe_notify(state, RuntimeError("first")))

        self.assertEqual(len(captured_messages), 1)
        self.assertEqual(state.last_error_digest_at, now)
        self.assertEqual(state.error_events, [])
        self.assertIn("за последние 3 часа", captured_messages[0])
        self.assertIn("RuntimeError: first", captured_messages[0])

        with (
            mock.patch.object(self.monitor, "_utc_now", return_value=now + timedelta(hours=2, minutes=59)),
            mock.patch.object(self.monitor, "record_event", fake_send),
        ):
            asyncio.run(self.monitor._record_error_and_maybe_notify(state, RuntimeError("second")))

        self.assertEqual(len(captured_messages), 1)
        self.assertEqual(len(state.error_events), 1)

        with (
            mock.patch.object(self.monitor, "_utc_now", return_value=now + timedelta(hours=3)),
            mock.patch.object(self.monitor, "record_event", fake_send),
        ):
            asyncio.run(self.monitor._record_error_and_maybe_notify(state, RuntimeError("third")))

        self.assertEqual(len(captured_messages), 2)
        self.assertEqual(state.last_error_digest_at, now + timedelta(hours=3))
        self.assertEqual(state.error_events, [])
        self.assertIn("RuntimeError: second", captured_messages[1])
        self.assertIn("RuntimeError: third", captured_messages[1])

    def test_error_digest_excludes_stale_queued_errors(self):
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        state = self.monitor.MonitorState(
            error_events=[
                {
                    "at": (now - timedelta(days=1)).isoformat(),
                    "type": "RuntimeError",
                    "message": "stale",
                }
            ]
        )
        captured_messages = []

        async def fake_send(message):
            captured_messages.append(message)
            return True

        with (
            mock.patch.object(self.monitor, "_utc_now", return_value=now),
            mock.patch.object(self.monitor, "record_event", fake_send),
        ):
            asyncio.run(self.monitor._record_error_and_maybe_notify(state, RuntimeError("fresh")))

        self.assertEqual(len(captured_messages), 1)
        self.assertNotIn("stale", captured_messages[0])
        self.assertIn("fresh", captured_messages[0])
        self.assertEqual(state.error_events, [])

    def test_error_digest_groups_repeated_html_errors(self):
        now = datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc)
        html_error = """Неожиданный статус ответа: 502. Тело: <!DOCTYPE html>
<html>
<head>
  <title>Under construction (503)</title>
  <link rel="stylesheet" type="text/css" href="/css/error-page.css">
</head>
<body><div class="error">maintenance</div></body>
</html>"""
        events = [
            {
                "at": now.isoformat(),
                "type": "DatesFetchError",
                "message": html_error,
            },
            {
                "at": (now + timedelta(minutes=5)).isoformat(),
                "type": "DatesFetchError",
                "message": html_error,
            },
        ]

        captured = {}

        async def fake_send(message):
            captured["message"] = message
            return True

        with mock.patch.object(self.monitor, "record_event", fake_send):
            sent = asyncio.run(self.monitor.notify_error_digest(events))

        self.assertTrue(sent)
        self.assertIn("Всего событий: 2", captured["message"])
        self.assertIn("DatesFetchError ×2", captured["message"])
        self.assertIn("Неожиданный статус ответа: 502", captured["message"])
        self.assertIn("Ответ: Under construction (503)", captured["message"])
        self.assertNotIn("<html", captured["message"])
        self.assertNotIn("<head", captured["message"])
        self.assertNotIn("stylesheet", captured["message"])
        self.assertLess(len(captured["message"]), 700)

    def test_local_events_include_account_label(self):
        captured = {}

        async def fake_send(message):
            captured["message"] = message
            return True

        with mock.patch.object(self.monitor, "record_event", fake_send):
            result = asyncio.run(self.monitor.notify_booking_success("2026-07-10", "10:00", "http"))

        self.assertTrue(result)
        self.assertIn("Аккаунт: embassy@example.com", captured["message"])
        self.assertIn("Дата: 2026-07-10", captured["message"])

    def test_state_read_write_roundtrip_and_snapshot_fallback(self):
        state = self.monitor.MonitorState(
            appointments={"2026-07-10": {"09:00", "10:00"}},
            cookie="session=live",
            browser_headers={"User-Agent": "Browser/1.0"},
            csrf_token="csrf-live",
            session_id="sid-live",
            if_none_match='"etag-live"',
            latest_found_dates=["2026-07-10"],
            latest_dates_payload=[{"date": "2026-07-10", "business_day": True}],
            latest_current_times={"2026-07-10": {"10:00"}},
            latest_new_times={"2026-07-10": {"10:00"}},
        )
        self.monitor._write_state(state)

        restored = self.monitor._read_state()
        self.assertEqual(restored.cookie, "session=live")
        self.assertEqual(restored.latest_found_dates, ["2026-07-10"])
        self.assertEqual(restored.latest_current_times["2026-07-10"], {"10:00"})

        self.monitor.STATE_FILE.unlink()
        snapshot = self.monitor_store.read_snapshot()
        self.assertEqual(snapshot["session"]["csrf_token"], "csrf-live")
        restored_from_snapshot = self.monitor._read_state()
        self.assertEqual(restored_from_snapshot.cookie, "session=live")

    def test_fetch_dates_retries_without_if_none_match_on_empty_304_style_body(self):
        calls = []

        async def fake_request_text(session, state, url, include_if_none_match):
            calls.append(include_if_none_match)
            if include_if_none_match:
                return 200, ""
            return 200, json.dumps([{"date": "2026-07-12"}])

        state = self.monitor.MonitorState(cookie="session=1", csrf_token="csrf=1", if_none_match='"etag"')
        with mock.patch.object(self.monitor, "_request_text", fake_request_text):
            dates = asyncio.run(self.monitor.fetch_available_dates(None, state))

        self.assertEqual(dates, ["2026-07-12"])
        self.assertEqual(calls, [True, False])

    def test_fetch_dates_retries_transient_usvisa_502(self):
        calls = []

        async def fake_request_text(session, state, url, include_if_none_match):
            calls.append(include_if_none_match)
            if len(calls) == 1:
                return 502, "<html><head><title>Under construction (503)</title></head></html>"
            return 200, json.dumps([{"date": "2026-07-12"}])

        state = self.monitor.MonitorState(cookie="session=1", csrf_token="csrf=1")
        with (
            mock.patch.object(self.monitor, "_request_text", fake_request_text),
            mock.patch.object(self.monitor.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            dates = asyncio.run(self.monitor.fetch_available_dates(None, state))

        self.assertEqual(dates, ["2026-07-12"])
        self.assertEqual(calls, [True, True])

    def test_fetch_dates_retries_transient_connection_error(self):
        calls = 0

        async def fake_request_text(session, state, url, include_if_none_match):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise self.monitor.aiohttp.ClientConnectionError("temporary connection drop")
            return 200, json.dumps([{"date": "2026-07-12"}])

        state = self.monitor.MonitorState(cookie="session=1", csrf_token="csrf=1")
        with (
            mock.patch.object(self.monitor, "_request_text", fake_request_text),
            mock.patch.object(self.monitor.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            dates = asyncio.run(self.monitor.fetch_available_dates(None, state))

        self.assertEqual(dates, ["2026-07-12"])
        self.assertEqual(calls, 2)

    def test_notify_new_dates_marks_dates_without_times(self):
        with mock.patch.object(
            self.monitor,
            "record_event",
            new=mock.AsyncMock(return_value=True),
        ) as send_message:
            sent = asyncio.run(
                self.monitor.notify_new_dates(
                    ["2027-02-08", "2027-02-10"],
                    {"2027-02-08": set(), "2027-02-10": {"09:00"}},
                )
            )

        self.assertTrue(sent)
        message = send_message.await_args.args[0]
        self.assertTrue(message.startswith("🎯 В Астане появилась новая дата для записи:"))
        self.assertIn("2027-02-08: нет доступного времени для автозаписи", message)
        self.assertIn("2027-02-10: 09:00", message)

    def test_success_monitor_event_tracks_dates_without_times(self):
        event = self.monitor._success_monitor_event(
            datetime(2026, 6, 16, 3, 20, tzinfo=timezone.utc),
            ["2027-02-08", "2027-02-10"],
            {"2027-02-08": set(), "2027-02-10": {"09:00"}},
            [],
        )

        self.assertEqual(event["active_dates"], ["2027-02-10"])
        self.assertEqual(event["dates_without_times"], ["2027-02-08"])

    def test_monitor_db_sync_can_be_disabled_for_user_processes(self):
        self.monitor.os.environ["USVISA_MONITOR_DB_SYNC"] = "false"
        self.config_store, self.monitor_store, self.monitor = reload_modules(
            "config_store",
            "monitor_store",
            "usvisa_appointment_monitor",
        )
        self.monitor_store.write_snapshot(
            {"appointments": {"2099-01-01": ["09:00"]}},
            "2099-01-01T09:00:00+00:00",
        )
        self.monitor.STATE_FILE.write_text(
            json.dumps({"appointments": {"2026-07-01": ["10:00"]}}, ensure_ascii=False),
            encoding="utf-8",
        )

        state = self.monitor._read_state()
        self.assertEqual(state.appointments, {"2026-07-01": {"10:00"}})
        state.appointments["2026-07-02"] = {"11:00"}
        self.monitor._write_state(state)

        self.assertEqual(
            self.monitor_store.read_snapshot(),
            {"appointments": {"2099-01-01": ["09:00"]}},
        )

    def test_job_treats_empty_dates_as_success_without_error_events(self):
        class FakeClientSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with (
            mock.patch.object(
                self.monitor.aiohttp,
                "ClientSession",
                return_value=FakeClientSession(),
            ),
            mock.patch.object(
                self.monitor,
                "fetch_dates_with_cookie_candidates",
                new=mock.AsyncMock(return_value=[]),
            ),
            mock.patch.object(
                self.monitor,
                "notify_healthcheck",
                new=mock.AsyncMock(return_value=True),
            ) as notify_healthcheck,
        ):
            asyncio.run(self.monitor.job(force_run=True))

        state = self.monitor._read_state()
        self.assertEqual(state.error_events, [])
        self.assertEqual(state.latest_found_dates, [])
        self.assertEqual(state.monitor_events[-1]["status"], "success")
        self.assertEqual(state.monitor_events[-1]["found_dates"], [])
        notify_healthcheck.assert_awaited_once()


class BrowserHelperTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.monitor, self.browser = reload_modules(
            "config_store",
            "monitor_store",
            "usvisa_appointment_monitor",
            "usvisa_browser_login_and_run",
        )

    def test_basic_browser_helpers(self):
        self.assertEqual(self.browser._password_mask(""), "<empty>")
        self.assertEqual(self.browser._password_mask("abcdef"), "<redacted>")
        self.assertEqual(
            self.browser._format_cookie_header(
                [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
            ),
            "a=1; b=2",
        )
        self.assertTrue(self.browser._is_usvisa_url("https://ais.usvisa-info.com/test"))
        self.assertFalse(self.browser._is_usvisa_url("https://example.test"))

    def test_cookie_and_header_helpers_bridge_monitor_state(self):
        state = self.monitor.MonitorState(session_id="old", if_none_match="old-etag")
        self.browser._apply_response_headers_to_state(state, {"session-id": "new", "etag": "new-etag"})
        self.assertEqual(state.session_id, "new")
        self.assertEqual(state.if_none_match, "new-etag")
        self.assertEqual(
            self.browser._cookie_with_set_cookie("a=1", ["b=2; Path=/"]),
            "a=1; b=2",
        )


class WebMonitorNotificationTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.web_app = reload_modules(
            "config_store",
            "monitor_store",
            "web_app",
        )

    def test_24_7_error_cycle_notifications_are_not_sent_to_chat(self):
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        sent = []

        async def fake_notification():
            sent.append("sent")
            return True

        with mock.patch.object(self.web_app, "_utc_now_dt", return_value=now):
            self.assertFalse(asyncio.run(self.web_app._send_monitor_error_cycle_notification(fake_notification)))

        self.assertEqual(sent, [])
        self.assertIsNone(self.web_app.MONITOR_STATE["last_error_cycle_notification_at"])


if __name__ == "__main__":
    unittest.main()
