import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from test_support import TempProjectCaseMixin, reload_modules


class WebAppApiTests(TempProjectCaseMixin, AioHTTPTestCase):
    async def get_application(self):
        self.config_store, self.monitor_store, self.web_app = reload_modules(
            "config_store",
            "monitor_store",
            "web_app",
        )
        self.web_app.DEBUG_ARTIFACTS_DIR = self.temp_path / "browser-debug"
        self.web_app.VPN_REFRESH_REQUEST_FILE = self.temp_path / "vpn-refresh.request"
        return self.web_app.create_app()

    async def register_user(self, email="user@example.com", password="user-password", terms=True):
        return await self.client.post(
            "/api/auth/register",
            json={"email": email, "password": password, "termsAccepted": terms},
        )

    async def login_admin(self):
        response = await self.client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "admin-password"},
        )
        self.assertEqual(response.status, 200)
        return response

    async def test_auth_flow_register_login_logout_and_profile(self):
        response = await self.register_user()
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["user"]["email"], "user@example.com")
        self.assertEqual(payload["user"]["status"], "не оплачен")

        profile_response = await self.client.get("/api/profile")
        profile_payload = await profile_response.json()
        self.assertEqual(profile_payload["profile"]["city"], "astana")

        save_response = await self.client.post(
            "/api/profile",
            json={
                "embassyLogin": "embassy@example.com",
                "embassyPassword": "secret-pass",
                "city": "astana",
                "ignoredDates": ["2026-07-10", "invalid", "2026-07-10"],
            },
        )
        self.assertEqual(save_response.status, 200)
        saved_profile = await save_response.json()
        self.assertEqual(saved_profile["profile"]["city"], "astana")
        self.assertEqual(saved_profile["profile"]["ignoredDates"], ["2026-07-10"])

        logout_response = await self.client.post("/api/auth/logout")
        self.assertEqual(logout_response.status, 200)
        unauthorized = await self.client.get("/api/profile")
        self.assertEqual(unauthorized.status, 401)

    async def test_login_session_is_not_shadowed_by_a_legacy_cookie(self):
        response = await self.register_user(email="session-cookie@example.com")
        self.assertEqual(response.status, 200)
        self.assertIn("usvisa_session_v2", response.cookies)
        session_token = response.cookies["usvisa_session_v2"].value

        profile_response = await self.client.get(
            "/api/profile",
            headers={"Cookie": f"usvisa_session_v2={session_token}; usvisa_session=legacy-session"},
        )

        self.assertEqual(profile_response.status, 200)
        self.assertEqual((await profile_response.json())["profile"]["city"], "astana")

    async def test_register_validates_terms_and_duplicate_email(self):
        missing_terms = await self.register_user(terms=False)
        self.assertEqual(missing_terms.status, 400)
        self.assertIn("согласиться", (await missing_terms.json())["error"])

        created = await self.register_user()
        self.assertEqual(created.status, 200)
        duplicate = await self.register_user()
        self.assertEqual(duplicate.status, 409)

    async def test_astana_city_settings_are_saved_and_other_facilities_rejected(self):
        await self.login_admin()
        response = await self.client.get("/api/cities")
        cities = (await response.json())["cities"]
        self.assertEqual([city["code"] for city in cities], ["astana"])
        valid = await self.client.post("/api/cities", json={"cities": cities})
        self.assertEqual(valid.status, 200)
        cities[0]["facilityId"] = "125"
        rejected = await self.client.post("/api/cities", json={"cities": cities})
        self.assertEqual(rejected.status, 400)
        response = await self.client.get("/api/cities")
        self.assertEqual((await response.json())["cities"][0]["facilityId"], "134")

    async def test_registration_does_not_send_external_messages(self):
        with patch.object(self.web_app.asyncio, "to_thread", new_callable=AsyncMock) as to_thread:
            response = await self.register_user(email="new-user@example.com")
        self.assertEqual(response.status, 200)
        to_thread.assert_not_awaited()

    async def test_public_page_does_not_expose_owner_contacts(self):
        response = await self.client.get("/")
        page = await response.text()

        self.assertEqual(response.status, 200)
        self.assertNotIn("t.me/", page)
        self.assertNotIn("@personal-contact", page)

    async def test_admin_only_routes_and_status_payload(self):
        user_response = await self.register_user(email="plain@example.com")
        self.assertEqual(user_response.status, 200)
        forbidden = await self.client.get("/api/users")
        self.assertEqual(forbidden.status, 403)

        await self.client.post("/api/auth/logout")
        admin_login = await self.login_admin()
        admin_id = (await admin_login.json())["user"]["id"]

        admin_env = self.web_app._env_for_user_subprocess(admin_id)
        state_path = Path(admin_env["USVISA_STATE_FILE"])
        error_path = Path(admin_env["USVISA_ERROR_STATE_FILE"])
        event_at = datetime.now(timezone.utc).isoformat()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "appointments": {"2026-07-15": ["09:00", "10:00"]},
                    "latest_found_dates": ["2026-07-15", "2026-07-16"],
                    "latest_ignored_dates": ["2026-07-16"],
                    "latest_current_times": {"2026-07-15": ["09:00", "10:00"]},
                    "latest_booking": {"date": "2026-07-15", "time": "10:00", "method": "http"},
                    "monitor_events": [
                        {
                            "at": event_at,
                            "status": "success",
                            "found_dates": ["2026-07-15", "2026-07-16"],
                            "ignored_dates": ["2026-07-16"],
                            "active_appointments": {"2026-07-15": ["09:00", "10:00"]},
                        }
                    ],
                    "next_check_at": "2999-01-01T00:00:00+00:00",
                    "session": {
                        "cookie": "session=1",
                        "cookie_candidates": [{"label": "candidate", "cookie": "session=2"}],
                        "browser_headers": {"User-Agent": "Browser"},
                        "csrf_token": "csrf-1",
                        "session_id": "sid-1",
                        "if_none_match": '"etag-1"',
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.monitor_store.write_snapshot(
            json.loads(state_path.read_text(encoding="utf-8")),
            event_at,
        )
        error_path.write_text(
            json.dumps(
                {
                    "error_events": [{"at": event_at, "type": "RuntimeError", "message": "boom"}]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status_response = await self.client.get("/api/status?range_hours=24")
        self.assertEqual(status_response.status, 200)
        status_payload = await status_response.json()
        self.assertEqual(status_payload["user"]["role"], "admin")
        self.assertEqual(status_payload["summary"]["actualFreeDays"], 2)
        self.assertEqual(status_payload["state"]["session"]["cookieCandidates"], 1)
        self.assertTrue(status_payload["cities"])

        compact_response = await self.client.get("/api/status/compact?range_hours=24")
        compact_payload = await compact_response.json()
        self.assertEqual(compact_payload["state"]["latestBooking"]["date"], "2026-07-15")
        self.assertEqual(compact_payload["state"]["latestBooking"]["method"], "http")
        self.assertEqual(compact_payload["summary"]["foundFreeDays"], 2)

    async def test_admin_can_manage_users_settings_and_debug_artifacts(self):
        user_response = await self.client.post(
            "/api/auth/register",
            json={"email": "managed@example.com", "password": "user-password", "termsAccepted": True},
        )
        self.assertEqual(user_response.status, 200)
        await self.client.post("/api/auth/logout")
        await self.login_admin()
        users_response = await self.client.get("/api/users")
        users_payload = await users_response.json()
        managed_user = next(item for item in users_payload["users"] if item["email"] == "managed@example.com")

        update_status = await self.client.post(
            "/api/users/status",
            json={"userId": managed_user["id"], "status": "поиск включен"},
        )
        self.assertEqual(update_status.status, 200)

        save_cities = await self.client.post(
            "/api/cities",
            json={
                "cities": [
                    {
                        "code": "astana",
                        "label": "Астана",
                        "active": True,
                        "priority": 100,
                        "facilityId": "134",
                        "appointmentUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment",
                        "datesApiUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/days/134.json",
                        "timesApiUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/times/134.json",
                    },
                    {
                        "code": "krakow",
                        "label": "Краков",
                        "active": True,
                        "priority": 80,
                        "facilityId": "321",
                        "appointmentUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment",
                        "datesApiUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/days/321.json",
                        "timesApiUrl": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/times/321.json",
                    },
                ]
            },
        )
        self.assertEqual(save_cities.status, 400)
        cities_response = await self.client.get("/api/cities")
        self.assertEqual([city["code"] for city in (await cities_response.json())["cities"]], ["astana"])

        settings_response = await self.client.post(
            "/api/settings",
            json={
                "settings": {
                    "USVISA_VPN_SUBSCRIPTION_URL": "https://vpn.example.test/new",
                    "USVISA_VPN_ENABLED": True,
                }
            },
        )
        self.assertEqual(settings_response.status, 200)
        self.assertTrue(self.web_app.VPN_REFRESH_REQUEST_FILE.exists())

    async def test_admin_can_save_user_profile_proxy_and_user_runtime_isolated(self):
        user_response = await self.register_user(email="proxy-user@example.com")
        self.assertEqual(user_response.status, 200)
        user_id = (await user_response.json())["user"]["id"]

        await self.client.post("/api/auth/logout")
        await self.login_admin()
        save_response = await self.client.put(
            f"/api/users/{user_id}/profile",
            json={
                "embassyLogin": "proxy.embassy@example.com",
                "embassyPassword": "embassy-secret",
                "city": "astana",
                "vpnProxyUrl": "http://127.0.0.1:2081",
            },
        )

        self.assertEqual(save_response.status, 200)
        payload = await save_response.json()
        self.assertEqual(payload["profile"]["embassyLogin"], "proxy.embassy@example.com")
        self.assertEqual(payload["profile"]["vpnProxyUrl"], "http://127.0.0.1:2081")

        env = self.web_app._env_for_user_subprocess(user_id)
        self.assertEqual(env["USVISA_LOGIN_EMAIL"], "proxy.embassy@example.com")
        self.assertEqual(env["USVISA_VPN_ENABLED"], "true")
        self.assertEqual(env["USVISA_VPN_PROXY_URL"], "http://127.0.0.1:2081")
        self.assertEqual(env["USVISA_MONITOR_DB_SYNC"], "false")
        self.assertIn(f"state/users/{user_id}/usvisa_seen_dates.json", env["USVISA_STATE_FILE"])
        self.assertIn(f"state/users/{user_id}/playwright-profile", env["USVISA_BROWSER_PROFILE_DIR"])

    async def test_admin_can_open_and_save_full_user_settings(self):
        user_response = await self.register_user(email="settings-user@example.com")
        self.assertEqual(user_response.status, 200)
        user_id = (await user_response.json())["user"]["id"]

        await self.client.post("/api/auth/logout")
        await self.login_admin()
        save_response = await self.client.put(
            f"/api/users/{user_id}/settings",
            json={
                "embassyLogin": "settings.embassy@example.com",
                "embassyPassword": "embassy-secret",
                "city": "astana",
                "vpnProxyUrl": "http://127.0.0.1:2082",
                "appointmentUrl": "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment",
                "datesApiUrl": "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/days/134.json?appointments[expedite]=false",
                "timesApiUrl": "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/times/134.json?date=2026-08-01&appointments[expedite]=false",
                "ignoredDates": ["2026-07-10", "2026-07-11"],
                "ignoredPeriods": [{"start": "2026-07-10", "end": "2026-07-11"}],
                "priorityDates": ["2026-08-01"],
                "priorityPeriods": [{"start": "2026-08-01", "end": "2026-08-01"}],
                "preparationDays": 4,
            },
        )
        self.assertEqual(save_response.status, 200)
        saved_payload = await save_response.json()
        self.assertEqual(saved_payload["profile"]["vpnProxyUrl"], "http://127.0.0.1:2082")
        self.assertEqual(
            saved_payload["profile"]["appointmentUrl"],
            "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment",
        )
        self.assertEqual(
            saved_payload["profile"]["datesApiUrl"],
            "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/days/134.json?appointments[expedite]=false",
        )
        self.assertEqual(
            saved_payload["profile"]["timesApiUrl"],
            "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/times/134.json?date=2026-08-01&appointments[expedite]=false",
        )
        self.assertEqual(saved_payload["profile"]["ignoredPeriods"], [{"start": "2026-07-10", "end": "2026-07-11"}])
        self.assertEqual(saved_payload["profile"]["priorityDates"], ["2026-08-01"])
        self.assertEqual(saved_payload["profile"]["preparationDays"], 4)

        settings_response = await self.client.get(f"/api/users/{user_id}/settings")
        self.assertEqual(settings_response.status, 200)
        settings_payload = await settings_response.json()
        self.assertEqual(settings_payload["user"]["email"], "settings-user@example.com")
        self.assertEqual(settings_payload["profile"]["embassyLogin"], "settings.embassy@example.com")
        self.assertEqual(settings_payload["profile"]["ignoredDates"], ["2026-07-10", "2026-07-11"])
        self.assertEqual(settings_payload["profile"]["priorityPeriods"], [{"start": "2026-08-01", "end": "2026-08-01"}])

        env = self.web_app._env_for_user_subprocess(user_id)
        self.assertEqual(env["USVISA_APPOINTMENT_URL"], "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment")
        self.assertEqual(
            env["USVISA_DATES_API_URL"],
            "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/days/134.json?appointments[expedite]=false",
        )
        self.assertEqual(
            env["USVISA_TIMES_API_URL"],
            "https://ais.usvisa-info.com/en-kz/niv/schedule/123456/appointment/times/134.json?date=2026-08-01&appointments[expedite]=false",
        )

    async def test_monitor_user_rows_include_only_enabled_real_users(self):
        enabled_response = await self.register_user(email="enabled@example.com")
        disabled_response = await self.register_user(email="disabled@example.com")
        self.assertEqual(enabled_response.status, 200)
        self.assertEqual(disabled_response.status, 200)
        enabled_id = (await enabled_response.json())["user"]["id"]
        disabled_id = (await disabled_response.json())["user"]["id"]

        await self.client.post("/api/auth/logout")
        await self.login_admin()
        await self.client.put(
            f"/api/users/{enabled_id}/profile",
            json={
                "embassyLogin": "enabled.embassy@example.com",
                "embassyPassword": "embassy-password",
                "city": "astana",
                "vpnProxyUrl": "",
            },
        )
        await self.client.post("/api/users/status", json={"userId": enabled_id, "status": "поиск включен"})
        await self.client.post("/api/users/status", json={"userId": disabled_id, "status": "поиск выключен"})

        rows = self.web_app._monitor_user_rows()

        self.assertIn(enabled_id, [row["id"] for row in rows])
        self.assertNotIn(disabled_id, [row["id"] for row in rows])
        self.assertNotIn("demo@local", [row["email"] for row in rows])

    async def test_parallel_monitor_batch_runs_users_with_unique_proxies_concurrently(self):
        users = [
            {"id": 1, "email": "first@example.com"},
            {"id": 2, "email": "second@example.com"},
        ]
        envs = {
            1: {"USVISA_VPN_ENABLED": "true", "USVISA_VPN_PROXY_URL": "http://127.0.0.1:2081"},
            2: {"USVISA_VPN_ENABLED": "true", "USVISA_VPN_PROXY_URL": "http://127.0.0.1:2082"},
        }
        active = 0
        max_active = 0
        started: list[str] = []
        all_started = asyncio.Event()

        async def fake_run(command, *, user_email, env):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            started.append(user_email)
            if len(started) == 2:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            await asyncio.sleep(0)
            active -= 1
            return {"email": user_email, "returncode": 0, "env": env}

        with (
            patch.object(self.web_app, "_env_for_user_subprocess", side_effect=lambda user_id: envs[user_id]),
            patch.object(self.web_app, "_run_monitor_user_command", side_effect=fake_run),
        ):
            results = await self.web_app._run_parallel_monitor_batch(["monitor"], users)

        self.assertEqual([result["email"] for result in results], ["first@example.com", "second@example.com"])
        self.assertEqual(max_active, 2)

    async def test_parallel_monitor_batch_requires_unique_proxy_per_user(self):
        users = [
            {"id": 1, "email": "first@example.com"},
            {"id": 2, "email": "second@example.com"},
        ]
        same_proxy_env = {"USVISA_VPN_ENABLED": "true", "USVISA_VPN_PROXY_URL": "http://127.0.0.1:2081"}
        started: list[str] = []

        async def fake_run(command, *, user_email, env):
            started.append(user_email)
            return {"email": user_email, "returncode": 0, "env": env}

        with (
            patch.object(self.web_app, "_env_for_user_subprocess", return_value=same_proxy_env),
            patch.object(self.web_app, "_run_monitor_user_command", side_effect=fake_run),
        ):
            results = await self.web_app._run_parallel_monitor_batch(["monitor"], users)

        self.assertEqual([result["email"] for result in results], ["first@example.com"])
        self.assertEqual(started, ["first@example.com"])
        self.assertTrue(
            any("second@example.com" in line and "proxy уже используется" in line for line in self.web_app.PROCESS_STATE["logs"])
        )

    async def test_admin_deletes_user_and_revokes_their_sessions(self):
        user_response = await self.register_user(email="remove-me@example.com")
        self.assertEqual(user_response.status, 200)
        user_id = (await user_response.json())["user"]["id"]

        await self.client.post("/api/auth/logout")
        await self.login_admin()
        delete_response = await self.client.delete(f"/api/users/{user_id}")

        self.assertEqual(delete_response.status, 200)
        with self.web_app._db() as conn:
            self.assertIsNone(conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone())
            self.assertIsNone(conn.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone())
            self.assertIsNone(conn.execute("SELECT user_id FROM sessions WHERE user_id = ?", (user_id,)).fetchone())

    async def test_admin_cannot_delete_self_or_last_admin(self):
        await self.login_admin()
        with self.web_app._db() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE email = ?", ("admin@example.com",)).fetchone()["id"]

        delete_response = await self.client.delete(f"/api/users/{admin_id}")

        self.assertEqual(delete_response.status, 400)
        self.assertIn("последнего", (await delete_response.json())["error"])

        ignored_response = await self.client.post(
            "/api/ignored-dates",
            json={
                "dates": ["2026-07-21"],
                "periods": [{"start": "2026-07-21", "end": "2026-07-23"}],
                "priorityDates": ["2026-08-01"],
                "priorityPeriods": [{"start": "2026-08-01", "end": "2026-08-05"}],
                "preparationDays": 4,
            },
        )
        ignored_payload = await ignored_response.json()
        self.assertEqual(ignored_payload["profile"]["preparationDays"], 4)
        self.assertEqual(ignored_payload["profile"]["priorityDates"], ["2026-08-01"])

        self.web_app.DEBUG_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        artifact_base = self.web_app.DEBUG_ARTIFACTS_DIR / "20260701T120000Z-login-failure"
        artifact_base.with_suffix(".html").write_text("<html><title>Failure</title></html>", encoding="utf-8")
        artifact_base.with_suffix(".png").write_bytes(b"png")

        debug_response = await self.client.get("/api/debug-artifacts")
        debug_payload = await debug_response.json()
        self.assertEqual(len(debug_payload["artifacts"]), 1)

        html_response = await self.client.get(f"/api/debug-artifacts/file/{artifact_base.name}.html")
        self.assertEqual(html_response.status, 200)
        self.assertIn("Failure", await html_response.text())

        delete_one = await self.client.post("/api/debug-artifacts/delete", json={"artifactId": artifact_base.name})
        self.assertEqual((await delete_one.json())["deleted"], 2)

    async def test_demo_access_and_clear_errors(self):
        demo_response = await self.client.post("/api/auth/demo")
        self.assertEqual(demo_response.status, 200)

        status_response = await self.client.get("/api/status?range_hours=12")
        payload = await status_response.json()
        self.assertEqual(payload["user"]["role"], "demo")
        self.assertTrue(payload["state"]["currentActiveAppointments"])
        self.assertEqual(payload["summary"]["successfulChecks"], 44)

        await self.client.post("/api/auth/logout")
        await self.login_admin()
        Path(self.env["USVISA_ERROR_STATE_FILE"]).write_text(
            json.dumps(
                {
                    "last_error_at": "2026-07-15T09:00:00+00:00",
                    "last_error_digest_at": "2026-07-15T09:10:00+00:00",
                    "error_events": [{"at": "2026-07-15T09:10:00+00:00", "type": "RuntimeError", "message": "boom"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.monitor_store.append_error_event(
            {"at": "2026-07-15T09:10:00+00:00", "type": "RuntimeError", "message": "boom"}
        )

        cleared = await self.client.post("/api/errors/clear")
        self.assertEqual(cleared.status, 200)
        self.assertEqual(self.monitor_store.read_error_events(limit=10), [])


if __name__ == "__main__":
    unittest.main()
