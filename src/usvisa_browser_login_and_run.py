import asyncio
import os
import random
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import (
    BrowserContext,
    Page,
    Request,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from config_store import load_config_to_env
import usvisa_appointment_monitor as monitor

load_config_to_env(override=os.getenv("USVISA_PROFILE_CONFIG_ACTIVE") != "true")

SIGN_IN_URL = "https://ais.usvisa-info.com/ru-kz/niv/users/sign_in"
FACILITY_ID = "134"
LOGIN_EMAIL = os.getenv("USVISA_LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.getenv("USVISA_LOGIN_PASSWORD", "")
CREDENTIAL_SOURCE = os.getenv("USVISA_CREDENTIAL_SOURCE", "unknown")
HEADLESS = os.getenv("USVISA_BROWSER_HEADLESS", "true").lower() in {"1", "true", "yes"}
USVISA_VPN_ENABLED = os.getenv("USVISA_VPN_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
USVISA_VPN_PROXY_URL = os.getenv("USVISA_VPN_PROXY_URL", "http://127.0.0.1:2080").strip()
BROWSER_PROFILE_DIR = os.getenv(
    "USVISA_BROWSER_PROFILE_DIR",
    str(monitor.PROJECT_ROOT / "state/playwright-profile"),
)
BROWSER_ACTION_DELAY_MIN_SECONDS = float(os.getenv("USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS", "2"))
BROWSER_ACTION_DELAY_MAX_SECONDS = float(os.getenv("USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS", "3"))
BROWSER_KEEP_OPEN_SECONDS = int(os.getenv("USVISA_BROWSER_KEEP_OPEN_SECONDS", "0"))
BROWSER_LOGIN_MAX_ATTEMPTS = int(os.getenv("USVISA_BROWSER_LOGIN_MAX_ATTEMPTS", "2"))
BROWSER_LOGIN_RETRY_DELAY_SECONDS = int(os.getenv("USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS", "5"))
BROWSER_TRACE_USVISA_NETWORK = os.getenv(
    "USVISA_BROWSER_TRACE_USVISA_NETWORK",
    "false",
).lower() in {"1", "true", "yes"}
APPOINTMENT_FORM_SELECTOR = "#appointments_consulate_appointment_facility_id"
APPOINTMENT_DATE_SELECTORS = [
    "#appointments_consulate_appointment_date",
    'input[name="appointments[consulate_appointment][date]"]',
    "#appointments_consulate_appointment_date_input input",
    "#appointments_consulate_appointment_date_input",
]
APPOINTMENT_TIME_SELECTOR = "#appointments_consulate_appointment_time"
APPOINTMENT_SUBMIT_SELECTOR = "#appointments_submit"
DEBUG_DIR = monitor.PROJECT_ROOT / "state/browser-debug"


def _save_debug_artifacts(page: Page, label: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in label)
    base_path = DEBUG_DIR / f"{timestamp}-{safe_label}"
    try:
        page.screenshot(path=str(base_path.with_suffix(".png")), full_page=True)
    except Exception as exc:
        print(f"Debug screenshot failed: {type(exc).__name__}: {exc}")
    try:
        base_path.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"Debug HTML save failed: {type(exc).__name__}: {exc}")
    print(f"Browser debug artifacts saved with prefix: {base_path}")


def _browser_proxy_options() -> dict[str, str] | None:
    if not USVISA_VPN_ENABLED or not USVISA_VPN_PROXY_URL:
        return None
    return {"server": USVISA_VPN_PROXY_URL}


def _human_pause(page: Page) -> None:
    min_seconds = max(0.0, BROWSER_ACTION_DELAY_MIN_SECONDS)
    max_seconds = max(min_seconds, BROWSER_ACTION_DELAY_MAX_SECONDS)
    if max_seconds <= 0:
        return
    delay_ms = int(random.uniform(min_seconds, max_seconds) * 1000)
    page.wait_for_timeout(delay_ms)


def _require_password() -> str:
    if not LOGIN_PASSWORD:
        raise RuntimeError("Не задана переменная окружения USVISA_LOGIN_PASSWORD")
    return LOGIN_PASSWORD


def _password_mask(value: str) -> str:
    if not value:
        return "<empty>"
    return "<redacted>"


def _format_cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    )


def _extract_csrf_token(page: Page) -> str:
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    return token or ""


def _apply_response_headers_to_state(state: monitor.MonitorState, headers: dict[str, str]) -> None:
    state.session_id = headers.get("session-id") or headers.get("Session-Id") or state.session_id
    state.if_none_match = headers.get("etag") or headers.get("ETag") or state.if_none_match


def _response_header(headers: dict[str, str], name: str) -> str:
    return headers.get(name) or headers.get(name.lower()) or headers.get(name.title()) or ""


def _headers_from_response(response: Response) -> dict[str, str]:
    headers = dict(response.headers)
    set_cookies = _set_cookie_headers_from_response(response)
    if set_cookies:
        headers["set-cookie"] = "\n".join(set_cookies)
    return headers


def _set_cookie_headers_from_response(response: Response) -> list[str]:
    return response.header_values("set-cookie")


def _headers_from_request(request: Request) -> dict[str, str]:
    try:
        return dict(request.all_headers())
    except Exception:
        return dict(request.headers)


def _is_usvisa_url(url: str) -> bool:
    return "usvisa-info.com" in url


def _print_headers(headers: dict[str, str]) -> None:
    for name, value in sorted(headers.items()):
        print(f"{name}: {value}")


def _install_network_trace(context: BrowserContext) -> dict[str, Any]:
    network_capture: dict[str, Any] = {
        "last_set_cookie_url": "",
        "last_set_cookie_headers": [],
    }

    def on_request(request: Request) -> None:
        if not _is_usvisa_url(request.url):
            return

        if not BROWSER_TRACE_USVISA_NETWORK:
            return

        print("\n========== USVISA NETWORK REQUEST BEGIN ==========")
        print(f"method = {request.method}")
        print(f"url = {request.url}")
        print(f"resource_type = {request.resource_type}")
        post_data = request.post_data
        if post_data:
            print("\n--- request post data ---")
            print(post_data)
        print("\n--- request headers ---")
        _print_headers(_headers_from_request(request))
        print("========== USVISA NETWORK REQUEST END ==========\n")

    def on_response(response: Response) -> None:
        if not _is_usvisa_url(response.url):
            return

        set_cookies = _set_cookie_headers_from_response(response)
        if set_cookies:
            network_capture["last_set_cookie_url"] = response.url
            network_capture["last_set_cookie_headers"] = set_cookies

        if not BROWSER_TRACE_USVISA_NETWORK:
            return

        print("\n========== USVISA NETWORK RESPONSE BEGIN ==========")
        print(f"status = {response.status}")
        print(f"url = {response.url}")
        print("\n--- response headers ---")
        _print_headers(_headers_from_response(response))
        print("========== USVISA NETWORK RESPONSE END ==========\n")

    context.on("request", on_request)
    context.on("response", on_response)
    return network_capture


def _select_facility_and_wait_for_api_responses(
    page: Page,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    page.locator(APPOINTMENT_FORM_SELECTOR).wait_for(state="visible")
    _human_pause(page)

    with page.expect_response(
        lambda response: f"/appointment/address/{FACILITY_ID}" in response.url,
        timeout=30_000,
    ) as address_response_info, page.expect_response(
        lambda response: f"/appointment/days/{FACILITY_ID}.json" in response.url,
        timeout=30_000,
    ) as days_response_info:
        page.locator(APPOINTMENT_FORM_SELECTOR).select_option(FACILITY_ID)

    address_response = address_response_info.value
    days_response = days_response_info.value
    _human_pause(page)
    page.wait_for_load_state("networkidle")
    print(f"USVisa address response.status = {address_response.status}")
    print(f"USVisa browser days response.status = {days_response.status}")
    return (
        _headers_from_request(address_response.request),
        _headers_from_request(days_response.request),
        _headers_from_response(address_response),
        _headers_from_response(days_response),
    )


def _set_cookie_headers_from_headers(headers: dict[str, str]) -> list[str]:
    set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie")
    if not set_cookie:
        return []
    return set_cookie.splitlines()


def _cookie_with_set_cookie(base_cookie: str, set_cookie_headers: list[str]) -> str:
    if not base_cookie:
        return ""
    if not set_cookie_headers:
        return base_cookie
    return monitor._merge_set_cookies(base_cookie, set_cookie_headers)


def _type_slowly(page: Page, selector: str, value: str) -> None:
    locator = page.locator(selector)
    locator.click()
    _human_pause(page)
    locator.fill("")
    locator.type(value, delay=random.randint(10, 80))
    _human_pause(page)


def _wait_for_first_visible(page: Page, selectors: list[str], timeout_ms: int = 10_000):
    for selector in selectors:
        locator = page.locator(selector)
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
            return locator, selector
        except Exception:
            continue
    raise RuntimeError(f"Ни один селектор не стал видимым: {selectors}")


def _editable_locator(locator):
    tag_name = locator.evaluate("el => (el.tagName || '').toLowerCase()")
    if tag_name in {"input", "textarea", "select"}:
        return locator
    nested = locator.locator("input, textarea, select").first
    nested.wait_for(state="visible", timeout=5_000)
    return nested


def _parse_iso_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d")


def _try_pick_date_from_datepicker(page: Page, target_date: str) -> bool:
    target = _parse_iso_date(target_date)
    popup = page.locator("#ui-datepicker-div")
    try:
        popup.wait_for(state="visible", timeout=3_000)
    except Exception:
        return False

    next_button = popup.locator(".ui-datepicker-next, [data-handler='next']").first
    for _ in range(36):
        title = popup.locator(".ui-datepicker-title").first.inner_text().strip()
        try:
            current = datetime.strptime(title, "%B %Y")
        except ValueError:
            try:
                current = datetime.strptime(title, "%b %Y")
            except ValueError:
                current = None
        if current is not None and current.year == target.year and current.month == target.month:
            day_text = str(target.day)
            day_link = popup.locator(
                f"a.ui-state-default:text-is('{day_text}'), td[data-handler='selectDay'] a:text-is('{day_text}')"
            ).first
            day_link.wait_for(state="visible", timeout=3_000)
            day_link.click()
            _human_pause(page)
            return True
        if next_button.count() == 0:
            break
        next_button.click()
        page.wait_for_timeout(300)
    return False


def _set_appointment_date(page: Page, target_date: str) -> None:
    locator, selector = _wait_for_first_visible(page, APPOINTMENT_DATE_SELECTORS, timeout_ms=15_000)
    print(f"Setting appointment date via selector: {selector} -> {target_date}")
    clickable = locator
    editable = _editable_locator(locator)
    clickable.click()
    _human_pause(page)
    if _try_pick_date_from_datepicker(page, target_date):
        print(f"Date picked from datepicker: {target_date}")
        return
    editable.fill("")
    editable.fill(target_date)
    page.evaluate(
        """
        ([selectors, value]) => {
          for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (!el) continue;
            const input = el.matches('input, textarea, select') ? el : el.querySelector('input, textarea, select');
            if (!input) continue;
            input.value = value;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
          }
        }
        """,
        [APPOINTMENT_DATE_SELECTORS, target_date],
    )
    _human_pause(page)


def _select_appointment_time(page: Page, target_time: str) -> None:
    locator = page.locator(APPOINTMENT_TIME_SELECTOR)
    locator.wait_for(state="visible", timeout=15_000)
    locator = _editable_locator(locator)
    options = locator.locator("option").evaluate_all(
        """options => options.map(option => ({
            value: option.value || '',
            label: (option.textContent || '').trim()
        }))"""
    )
    normalized_target = target_time.strip()
    for option in options:
        value = str(option.get("value") or "").strip()
        label = str(option.get("label") or "").strip()
        if value == normalized_target:
            locator.select_option(value)
            print(f"Selected appointment time via {APPOINTMENT_TIME_SELECTOR}: {label or value}")
            _human_pause(page)
            return
        if label == normalized_target and value:
            locator.select_option(value)
            print(f"Selected appointment time via {APPOINTMENT_TIME_SELECTOR}: {label}")
            _human_pause(page)
            return
    raise RuntimeError(f"Не удалось найти время {target_time} среди options {options}")


def _prepare_appointment_form(page: Page, target_date: str, target_time: str) -> None:
    page.locator(APPOINTMENT_FORM_SELECTOR).wait_for(state="visible", timeout=45_000)
    current_facility = page.locator(APPOINTMENT_FORM_SELECTOR).input_value()
    if current_facility != FACILITY_ID:
        _select_facility_and_wait_for_api_responses(page)
    else:
        page.wait_for_load_state("networkidle")
    with page.expect_response(
        lambda response: (
            f"/appointment/times/{FACILITY_ID}.json" in response.url
            and f"date={target_date}" in response.url
        ),
        timeout=30_000,
    ) as times_response_info:
        _set_appointment_date(page, target_date)
    times_response = times_response_info.value
    print(f"USVisa booking times response.status = {times_response.status}")
    _human_pause(page)
    page.wait_for_load_state("networkidle")
    _select_appointment_time(page, target_time)


def _save_booking_debug_artifacts(page: Page, label: str) -> None:
    _save_debug_artifacts(page, f"booking-{label}")


def _open_appointment_page(page: Page) -> None:
    current_url = ""
    try:
        current_url = page.url
    except Exception:
        current_url = "<unavailable>"

    print(f"Before appointment goto: current_url = {current_url}")
    print(f"Opening appointment page directly: {monitor.APPOINTMENT_URL}")
    last_error = None
    for wait_until in ("domcontentloaded", "load", "networkidle"):
        try:
            page.goto(monitor.APPOINTMENT_URL, wait_until=wait_until)
            print(f"After appointment goto ({wait_until}): current_url = {page.url}")
            break
        except Exception as exc:
            last_error = exc
            try:
                failed_url = page.url
            except Exception:
                failed_url = "<unavailable>"
            print(
                "Appointment goto failed: "
                f"wait_until={wait_until}, error_type={type(exc).__name__}, "
                f"error={exc!r}, current_url={failed_url}"
            )
    else:
        _save_debug_artifacts(page, "appointment-goto-failed")
        raise last_error

    print(f"Waiting for appointment form: {APPOINTMENT_FORM_SELECTOR}")
    try:
        page.locator(APPOINTMENT_FORM_SELECTOR).wait_for(state="visible", timeout=45_000)
        print(f"Appointment form became visible on: {page.url}")
    except Exception as exc:
        try:
            failed_url = page.url
        except Exception:
            failed_url = "<unavailable>"
        print(
            "Appointment form wait failed: "
            f"error_type={type(exc).__name__}, error={exc!r}, current_url={failed_url}"
        )
        raise


def _collect_session_from_browser(
    context: BrowserContext,
    page: Page,
    network_capture: dict[str, Any],
    address_request_headers: dict[str, str],
    days_request_headers: dict[str, str],
    address_response_headers: dict[str, str],
    days_response_headers: dict[str, str],
) -> tuple[monitor.MonitorState, str]:
    state = monitor._read_state()
    state_cookie_source = "previous state"
    base_cookie = ""

    address_request_cookie = address_request_headers.get("cookie") or address_request_headers.get("Cookie")
    days_request_cookie = days_request_headers.get("cookie") or days_request_headers.get("Cookie")
    if days_request_cookie:
        base_cookie = days_request_cookie
        state_cookie_source = "days request headers cookie"
    else:
        browser_cookies = context.cookies("https://ais.usvisa-info.com")
        browser_cookie_header = _format_cookie_header(browser_cookies)
        if browser_cookie_header:
            base_cookie = browser_cookie_header
            state_cookie_source = "Playwright context.cookies fallback"

    if base_cookie:
        state.cookie = base_cookie

    state.browser_headers = monitor._parse_browser_headers(days_request_headers)

    days_set_cookie_headers = _set_cookie_headers_from_headers(days_response_headers)

    days_cookie = _cookie_with_set_cookie(
        days_request_cookie or base_cookie,
        days_set_cookie_headers,
    )
    csrf_token = _extract_csrf_token(page) or state.csrf_token
    if csrf_token:
        state.csrf_token = csrf_token

    state.cookie_candidates = []

    if days_cookie:
        state.cookie_candidates.append(
            {
                "label": "days Set-Cookie candidate",
                "cookie": days_cookie,
                "csrf_token": csrf_token,
                "session_id": _response_header(days_response_headers, "session-id") or state.session_id,
                "if_none_match": _response_header(days_response_headers, "etag") or state.if_none_match,
            }
        )

    if state.cookie_candidates:
        state.cookie = ""
        labels = ", ".join(candidate["label"] for candidate in state.cookie_candidates)
        state_cookie_source = f"{state_cookie_source} + candidates: {labels}"

    _apply_response_headers_to_state(state, address_response_headers)
    _apply_response_headers_to_state(state, days_response_headers)

    state.next_check_at = None
    return state, state_cookie_source


def _login_and_update_state_once() -> None:
    monitor.USVISA_PREFER_BOOTSTRAP_SESSION = False

    monitor.validate_astana_urls({
        "USVISA_APPOINTMENT_URL": monitor.APPOINTMENT_URL,
        "USVISA_DATES_API_URL": monitor.DATES_API_URL,
        "USVISA_TIMES_API_URL": monitor.TIMES_API_URL,
    }, required=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=HEADLESS,
            ignore_https_errors=True,
            proxy=_browser_proxy_options(),
        )
        try:
            network_capture = _install_network_trace(context)
            page = context.new_page()
            context.on("close", lambda: print("Playwright browser context closed"))
            page.on("close", lambda: print("Playwright page closed"))

            page.goto(SIGN_IN_URL, wait_until="domcontentloaded")
            print(f"Opened sign-in page: current_url = {page.url}")
            if page.locator("#user_email").count() > 0:
                password = _require_password()
                print("Signing in with locally configured credentials")
                _human_pause(page)
                _type_slowly(page, "#user_email", LOGIN_EMAIL)
                _type_slowly(page, "#user_password", password)
                _human_pause(page)
                page.locator("#policy_confirmed").check(force=True)
                _human_pause(page)
                page.locator('input[type="submit"][name="commit"]').click()
                page.wait_for_load_state("networkidle")
                print("Post-login stabilization: waiting 4 seconds after authorization")
                page.wait_for_timeout(4_000)
                page.wait_for_load_state("networkidle")
                print(f"After login submit: current_url = {page.url}")
            else:
                print(
                    "Login form was not shown: "
                    f"title={page.title()!r}, current_url={page.url}, "
                    f"body_preview={page.locator('body').inner_text(timeout=5_000)[:1000]!r}"
                )
                _save_debug_artifacts(page, "missing-login-form")
                print(
                    "Login form is unavailable; "
                    "trying to continue with the existing browser session."
                )

            _open_appointment_page(page)
            (
                address_request_headers,
                days_request_headers,
                address_response_headers,
                days_response_headers,
            ) = _select_facility_and_wait_for_api_responses(page)

            state, state_cookie_source = _collect_session_from_browser(
                context,
                page,
                network_capture,
                address_request_headers,
                days_request_headers,
                address_response_headers,
                days_response_headers,
            )
            monitor._write_state(state)
            print(f"Browser session saved to {monitor.STATE_FILE}")

            if BROWSER_KEEP_OPEN_SECONDS > 0:
                print(f"Keeping browser open for {BROWSER_KEEP_OPEN_SECONDS} seconds")
                page.wait_for_timeout(BROWSER_KEEP_OPEN_SECONDS * 1000)
        except Exception as exc:
            try:
                current_url = page.url
            except Exception:
                current_url = "<unavailable>"
            print(
                "Browser login flow failed: "
                f"error_type={type(exc).__name__}, error={exc!r}, current_url={current_url}"
            )
            raise
        finally:
            context.close()


def _open_booking_flow_once(target_date: str, target_time: str, *, submit: bool) -> bool:
    monitor.USVISA_PREFER_BOOTSTRAP_SESSION = False

    monitor.validate_astana_urls({
        "USVISA_APPOINTMENT_URL": monitor.APPOINTMENT_URL,
        "USVISA_DATES_API_URL": monitor.DATES_API_URL,
        "USVISA_TIMES_API_URL": monitor.TIMES_API_URL,
    }, required=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            headless=HEADLESS,
            ignore_https_errors=True,
            proxy=_browser_proxy_options(),
        )
        try:
            page = context.new_page()
            context.on("close", lambda: print("Playwright browser context closed"))
            page.on("close", lambda: print("Playwright page closed"))

            page.goto(SIGN_IN_URL, wait_until="domcontentloaded")
            print(f"Opened sign-in page: current_url = {page.url}")
            if page.locator("#user_email").count() > 0:
                password = _require_password()
                print("Signing in with locally configured credentials")
                _human_pause(page)
                _type_slowly(page, "#user_email", LOGIN_EMAIL)
                _type_slowly(page, "#user_password", password)
                _human_pause(page)
                page.locator("#policy_confirmed").check(force=True)
                _human_pause(page)
                page.locator('input[type="submit"][name="commit"]').click()
                page.wait_for_load_state("networkidle")
                print("Post-login stabilization: waiting 4 seconds after authorization")
                page.wait_for_timeout(4_000)
                page.wait_for_load_state("networkidle")
                print(f"After login submit: current_url = {page.url}")
            else:
                print(
                    "Login form was not shown: "
                    f"title={page.title()!r}, current_url={page.url}, "
                    f"body_preview={page.locator('body').inner_text(timeout=5_000)[:1000]!r}"
                )
                _save_debug_artifacts(page, "missing-login-form")
                print(
                    "Login form is unavailable; "
                    "trying to continue with the existing browser session."
                )

            _open_appointment_page(page)
            _prepare_appointment_form(page, target_date, target_time)
            print(f"Booking form prepared for {target_date} {target_time}")
            if not submit:
                _save_booking_debug_artifacts(page, f"dry-run-{target_date}-{target_time}")
                print("Dry-run mode: submit skipped before clicking appointments_submit")
                return True

            page.locator(APPOINTMENT_SUBMIT_SELECTOR).wait_for(state="visible", timeout=15_000)
            _human_pause(page)
            page.locator(APPOINTMENT_SUBMIT_SELECTOR).click()
            page.wait_for_load_state("networkidle")
            current_url = page.url
            print(f"After booking submit: current_url = {current_url}")
            booked = "/appointment/instructions" in current_url
            if not booked:
                _save_booking_debug_artifacts(page, f"submit-failed-{target_date}-{target_time}")
            return booked
        finally:
            context.close()


def open_booking_flow(target_date: str, target_time: str, *, submit: bool = False) -> bool:
    attempts = max(1, BROWSER_LOGIN_MAX_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            return _open_booking_flow_once(target_date, target_time, submit=submit)
        except PlaywrightTimeoutError as e:
            if attempt >= attempts:
                raise
            print(
                "USVisa browser booking flow timed out; "
                f"retrying ({attempt + 1}/{attempts}): {e}"
            )
            if BROWSER_LOGIN_RETRY_DELAY_SECONDS > 0:
                import time

                time.sleep(BROWSER_LOGIN_RETRY_DELAY_SECONDS)
    return False


def login_and_update_state() -> None:
    attempts = max(1, BROWSER_LOGIN_MAX_ATTEMPTS)
    for attempt in range(1, attempts + 1):
        try:
            _login_and_update_state_once()
            return
        except PlaywrightTimeoutError as e:
            if attempt >= attempts:
                raise
            print(
                "USVisa browser login timed out; "
                f"retrying authorization ({attempt + 1}/{attempts}): {e}"
            )
            if BROWSER_LOGIN_RETRY_DELAY_SECONDS > 0:
                import time

                time.sleep(BROWSER_LOGIN_RETRY_DELAY_SECONDS)


if __name__ == "__main__":
    target_date = os.getenv("USVISA_BOOK_DATE", "").strip()
    target_time = os.getenv("USVISA_BOOK_TIME", "").strip()
    if target_date and target_time:
        submit = os.getenv("USVISA_BOOK_SUBMIT", "false").lower() in {"1", "true", "yes"}
        open_booking_flow(target_date, target_time, submit=submit)
    else:
        login_and_update_state()
        monitor.USVISA_PREFER_BOOTSTRAP_SESSION = False
        asyncio.run(monitor.job(allow_browser_refresh=False, force_run=True))
