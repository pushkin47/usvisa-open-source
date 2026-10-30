import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from playwright.async_api import async_playwright

from test_support import TempProjectCaseMixin, reload_modules


class WebAppUiTests(TempProjectCaseMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        super().setUp()
        self.config_store, self.monitor_store, self.web_app = reload_modules(
            "config_store",
            "monitor_store",
            "web_app",
        )
        self.web_app.DEBUG_ARTIFACTS_DIR = self.temp_path / "browser-debug"
        self.web_app.VPN_REFRESH_REQUEST_FILE = self.temp_path / "vpn-refresh.request"
        self.app = self.web_app.create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        socket = next(iter(self.site._server.sockets))
        port = socket.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page()

    async def dismiss_tour_if_present(self):
        overlay = self.page.locator("#tourOverlay")
        if await overlay.is_visible():
            await self.page.click("#tourSkipBtn")
            await overlay.wait_for(state="hidden")

    async def asyncTearDown(self):
        await self.page.close()
        await self.browser.close()
        await self.playwright.stop()
        await self.runner.cleanup()
        super().tearDown()

    async def test_demo_access_renders_dashboard_and_tour(self):
        await self.page.goto(self.base_url, wait_until="networkidle")
        await self.page.click("#demoAccessBtn")
        await self.page.wait_for_selector("#appView:not([hidden])")
        await self.dismiss_tour_if_present()

        self.assertIn("демо", await self.page.locator("#userBadge").inner_text())
        self.assertEqual(await self.page.locator("#successfulChecks").inner_text(), "6")
        self.assertTrue(await self.page.locator("#resultsPanel").is_visible())

        await self.page.click('button[data-tab="instruction"]')
        await self.page.click("#instructionStartTourBtn")
        await self.page.wait_for_selector("#tourOverlay:not([hidden])")
        self.assertTrue(await self.page.locator("#tourTitle").inner_text())
        await self.page.click("#tourSkipBtn")
        await self.page.locator("#tourOverlay").wait_for(state="hidden")

    async def test_astana_settings_cannot_add_or_change_city(self):
        await self.page.goto(self.base_url, wait_until="networkidle")
        await self.page.fill("#authEmail", "admin@example.com")
        await self.page.fill("#authPassword", "admin-password")
        await self.page.click("#authSubmitBtn")
        await self.page.wait_for_selector("#appView:not([hidden])")
        await self.dismiss_tour_if_present()
        await self.page.click('button[data-tab="cities"]')
        await self.page.wait_for_selector('[data-city-key="facilityId"]')
        self.assertEqual(await self.page.locator('[data-city-row]').count(), 1)
        self.assertEqual(await self.page.locator('[data-city-key="facilityId"]').input_value(), "134")
        self.assertTrue(await self.page.locator('[data-city-key="facilityId"]').get_attribute("readonly") is not None)
        self.assertEqual(await self.page.locator('#addCityBtn').count(), 0)
        await self.page.reload(wait_until="networkidle")
        self.assertTrue(await self.page.locator('#appView').is_visible())

    async def test_register_login_save_profile_and_manage_ignore_list(self):
        today = datetime.now(timezone.utc).date()
        ignored_start = (today + timedelta(days=2)).isoformat()
        ignored_end = (today + timedelta(days=4)).isoformat()
        priority_start = (today + timedelta(days=10)).isoformat()
        priority_end = (today + timedelta(days=12)).isoformat()

        await self.page.goto(self.base_url, wait_until="networkidle")
        await self.page.click("#authModeBtn")
        await self.page.fill("#authEmail", "ui-user@example.com")
        await self.page.fill("#authPassword", "ui-password")
        await self.page.check("#termsAccepted")
        await self.page.click("#authSubmitBtn")
        await self.page.wait_for_selector("#appView:not([hidden])")
        await self.dismiss_tour_if_present()

        await self.page.click('button[data-tab="profile"]')
        await self.page.fill("#embassyLogin", "embassy-ui@example.com")
        await self.page.fill("#embassyPassword", "embassy-secret")
        await self.page.select_option("#profileCity", "astana")
        await self.page.click('#profileForm button[type="submit"]')
        await self.page.wait_for_function("() => document.querySelector('#profileStatus').textContent === 'Сохранено'")
        profile_response = await self.page.request.get(self.base_url + "/api/profile")
        saved_profile = (await profile_response.json())["profile"]
        self.assertEqual(saved_profile["embassyLogin"], "embassy-ui@example.com")
        self.assertEqual(saved_profile["city"], "astana")

        await self.page.click('button[data-tab="ignore"]')
        await self.page.fill("#ignorePeriodStart", ignored_start)
        await self.page.fill("#ignorePeriodEnd", ignored_end)
        await self.page.fill("#ignorePreparationDays", "5")
        await self.page.click("#ignorePeriodSubmitBtn")
        await self.page.wait_for_function(
            "(date) => document.querySelector('#ignoredDateList').textContent.includes(date)",
            arg=ignored_start,
        )

        await self.page.fill("#priorityPeriodStart", priority_start)
        await self.page.fill("#priorityPeriodEnd", priority_end)
        await self.page.click("#priorityPeriodSubmitBtn")
        await self.page.wait_for_function(
            "(date) => document.querySelector('#priorityDateList').textContent.includes(date)",
            arg=priority_start,
        )
        self.assertEqual(await self.page.input_value("#ignorePreparationDays"), "5")

    async def test_admin_can_delete_user_from_users_tab(self):
        await self.page.goto(self.base_url, wait_until="networkidle")
        await self.page.click("#authModeBtn")
        await self.page.fill("#authEmail", "delete-ui@example.com")
        await self.page.fill("#authPassword", "ui-password")
        await self.page.check("#termsAccepted")
        await self.page.click("#authSubmitBtn")
        await self.page.wait_for_selector("#appView:not([hidden])")
        await self.dismiss_tour_if_present()
        await self.page.click("#logoutBtn")

        await self.page.click("#authModeBtn")
        await self.page.fill("#authEmail", "admin@example.com")
        await self.page.fill("#authPassword", "admin-password")
        await self.page.click("#authSubmitBtn")
        await self.page.wait_for_selector("#appView:not([hidden])")
        await self.dismiss_tour_if_present()
        await self.page.click('button[data-tab="users"]')
        await self.page.wait_for_selector('[data-delete-user]')

        row = self.page.locator(".user-row", has_text="delete-ui@example.com")
        await row.locator("[data-open-user-settings]").click()
        await self.page.wait_for_selector("#userSettingsView:not([hidden])")
        await self.page.fill("#userSettingsEmbassyLogin", "delete.embassy@example.com")
        await self.page.fill("#userSettingsEmbassyPassword", "embassy-secret")
        await self.page.fill("#userSettingsVpnProxyUrl", "http://127.0.0.1:2081")
        await self.page.fill("#userSettingsIgnoreStart", "2026-07-10")
        await self.page.fill("#userSettingsIgnoreEnd", "2026-07-12")
        await self.page.click("#userSettingsAddIgnoreBtn")
        await self.page.fill("#userSettingsPriorityStart", "2026-08-01")
        await self.page.fill("#userSettingsPriorityEnd", "2026-08-01")
        await self.page.click("#userSettingsAddPriorityBtn")
        await self.page.fill("#userSettingsPreparationDays", "5")
        async with self.page.expect_response(lambda response: response.request.method == "PUT") as save_response_info:
            await self.page.click("#userSettingsSaveBtn")
        save_response = await save_response_info.value
        self.assertEqual(save_response.status, 200)
        await self.page.wait_for_function("() => document.querySelector('#userSettingsStatus').textContent.includes('Сохранено')")
        await self.page.click("#userSettingsBackBtn")
        await self.page.wait_for_selector("#usersList:not([hidden])")
        await self.page.wait_for_function("() => document.querySelector('#usersList').textContent.includes('http://127.0.0.1:2081')")
        row = self.page.locator(".user-row", has_text="delete-ui@example.com")
        await row.locator("[data-delete-user]").click()
        async with self.page.expect_response(lambda response: response.request.method == "DELETE") as response_info:
            await self.page.click("#confirmDialogAccept")
        response = await response_info.value
        self.assertEqual(response.status, 200)
        await row.wait_for(state="detached")


class WebStaticRoutesTests(unittest.TestCase):
    def test_assets_use_paths_rewritten_by_nginx(self):
        index_html = Path(__file__).resolve().parents[1] / "src" / "web_static" / "index.html"
        source = index_html.read_text()

        self.assertIn('href="/static/styles.css?', source)
        self.assertIn('src="/static/app.js?', source)

    def test_api_requests_are_rewritten_by_nginx_once(self):
        app_js = Path(__file__).resolve().parents[1] / "src" / "web_static" / "app.js"
        source = app_js.read_text()

        self.assertNotIn("const APP_BASE_PATH", source)
        self.assertNotIn("apiFetch(", source)
        self.assertIn('fetch("/api/auth/me")', source)
        self.assertIn('"/api/auth/login"', source)


if __name__ == "__main__":
    unittest.main()
