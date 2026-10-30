import json
import sqlite3
import unittest
from pathlib import Path

from test_support import TempProjectCaseMixin, reload_modules


class ConfigStoreTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, = reload_modules("config_store")

    def test_write_and_read_db_settings_roundtrip(self):
        self.config_store.write_db_settings(
            {"PLAIN_KEY": "value", "SECRET_KEY": "secret"},
            secret_keys={"SECRET_KEY"},
        )

        values = self.config_store.read_db_settings()
        self.assertEqual(values["PLAIN_KEY"], "value")
        self.assertEqual(values["SECRET_KEY"], "secret")

        with sqlite3.connect(self.config_store.db_path()) as conn:
            row = conn.execute(
                "SELECT is_secret FROM app_settings WHERE key = ?",
                ("SECRET_KEY",),
            ).fetchone()
        self.assertEqual(row[0], 1)

    def test_load_config_to_env_prefers_admin_profile_and_rewrites_urls(self):
        db_path = self.config_store.db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '',
                    terms_accepted INTEGER NOT NULL DEFAULT 1,
                    terms_accepted_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE user_profiles (
                    user_id INTEGER PRIMARY KEY,
                    embassy_login TEXT NOT NULL DEFAULT '',
                    embassy_password TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT 'astana',
                    facility_id TEXT NOT NULL DEFAULT '777',
                    ignored_dates TEXT NOT NULL DEFAULT '',
                    ignored_periods TEXT NOT NULL DEFAULT '[]',
                    priority_dates TEXT NOT NULL DEFAULT '',
                    priority_periods TEXT NOT NULL DEFAULT '[]',
                    prep_days INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO users (email, password_hash, role, created_at, updated_at)
                VALUES ('admin@example.com', 'hash', 'admin', 'now', 'now')
                """
            )
            conn.execute(
                """
                INSERT INTO user_profiles (
                    user_id, embassy_login, embassy_password, facility_id, ignored_dates, priority_dates, prep_days, created_at, updated_at
                )
                VALUES (1, 'profile@example.com', 'profile-pass', '999', '2026-07-01', '2026-08-01', 5, 'now', 'now')
                """
            )

        self.config_store.write_db_settings(
            {
                "USVISA_DATES_API_URL": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/days/134.json?appointments[expedite]=false",
                "USVISA_TIMES_API_URL": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/times/134.json",
            }
        )
        self.config_store.load_config_to_env(override=True)

        self.assertEqual(self.config_store.read_admin_profile_settings()["USVISA_FACILITY_ID"], "134")
        self.assertEqual(self.config_store.os.environ["USVISA_LOGIN_EMAIL"], "profile@example.com")
        self.assertIn("/days/134.json", self.config_store.os.environ["USVISA_DATES_API_URL"])
        self.assertIn("/times/134.json", self.config_store.os.environ["USVISA_TIMES_API_URL"])

    def test_profile_setting_keys_from_app_settings_are_skipped_without_legacy_flag(self):
        self.config_store.os.environ.pop("USVISA_ALLOW_LEGACY_APP_SETTINGS_CREDENTIALS", None)
        self.config_store.write_db_settings({"USVISA_LOGIN_EMAIL": "from-db@example.com"})

        self.config_store.load_config_to_env(override=True)

        self.assertNotEqual(self.config_store.os.environ.get("USVISA_LOGIN_EMAIL"), "from-db@example.com")


class MonitorStoreTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config_store, self.monitor_store = reload_modules("config_store", "monitor_store")

    def test_snapshot_and_runs_roundtrip(self):
        payload = {"appointments": {"2026-07-01": ["09:00"]}, "next_check_at": "2026-07-01T10:00:00+00:00"}
        self.monitor_store.write_snapshot(payload, "2026-07-01T10:00:00+00:00")
        self.monitor_store.append_monitor_run({"at": "2026-07-01T09:00:00+00:00", "status": "success"})
        self.monitor_store.append_monitor_run({"at": "2026-07-01T09:05:00+00:00", "status": "error", "message": "boom"})

        self.assertEqual(self.monitor_store.read_snapshot(), payload)
        self.assertEqual(
            self.monitor_store.read_monitor_runs(limit=10),
            [
                {"at": "2026-07-01T09:00:00+00:00", "status": "success"},
                {"at": "2026-07-01T09:05:00+00:00", "status": "error", "message": "boom"},
            ],
        )

    def test_error_queue_filters_invalid_events_and_can_be_cleared(self):
        self.monitor_store.append_error_event({"at": "2026-07-01T09:00:00+00:00", "type": "RuntimeError", "message": "boom"})
        self.monitor_store.append_error_event({"at": "", "type": "ValueError", "message": "ignored"})

        self.assertEqual(
            self.monitor_store.read_error_events(limit=10),
            [{"at": "2026-07-01T09:00:00+00:00", "type": "RuntimeError", "message": "boom"}],
        )

        self.monitor_store.clear_error_events()
        self.assertEqual(self.monitor_store.read_error_events(limit=10), [])

    def test_sync_legacy_state_if_needed_imports_json_files(self):
        state_file = self.temp_path / "legacy-state.json"
        error_state_file = self.temp_path / "legacy-errors.json"
        state_file.write_text(
            json.dumps(
                {
                    "appointments": {"2026-07-02": ["10:00"]},
                    "next_check_at": "2026-07-02T11:00:00+00:00",
                    "monitor_events": [{"at": "2026-07-02T10:00:00+00:00", "status": "success"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        error_state_file.write_text(
            json.dumps(
                {
                    "error_events": [{"at": "2026-07-02T10:10:00+00:00", "type": "RuntimeError", "message": "oops"}]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.monitor_store.sync_legacy_state_if_needed(state_file, error_state_file)

        self.assertEqual(self.monitor_store.read_snapshot()["appointments"]["2026-07-02"], ["10:00"])
        self.assertEqual(len(self.monitor_store.read_monitor_runs(limit=10)), 1)
        self.assertEqual(len(self.monitor_store.read_error_events(limit=10)), 1)


if __name__ == "__main__":
    unittest.main()
