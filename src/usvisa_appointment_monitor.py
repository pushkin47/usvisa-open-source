import asyncio
from copy import deepcopy
import fcntl
import hashlib
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv

from config_store import load_config_to_env, validate_astana_urls
import monitor_store
from local_events import record_event

load_config_to_env(override=os.getenv("USVISA_PROFILE_CONFIG_ACTIVE") != "true")
monitor_store.ensure_tables()

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

APPOINTMENT_URL = os.getenv(
    "USVISA_APPOINTMENT_URL",
    "",
)
DATES_API_URL = os.getenv(
    "USVISA_DATES_API_URL",
    "",
)
TIMES_API_URL = os.getenv(
    "USVISA_TIMES_API_URL",
    "",
)
ACCOUNT_LABEL = os.getenv("USVISA_LOGIN_EMAIL", "").strip() or "общий аккаунт"
CURRENT_FACILITY_ID_MATCH = re.search(r"/appointment/days/(\d+)\.json", DATES_API_URL)
CURRENT_FACILITY_ID = "134"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
configured_state_file = Path(
    os.getenv("USVISA_STATE_FILE", str(PROJECT_ROOT / "state/usvisa_seen_dates.json"))
)
STATE_FILE = configured_state_file if configured_state_file.is_absolute() else PROJECT_ROOT / configured_state_file
configured_booking_attempt_log_file = Path(
    os.getenv(
        "USVISA_BOOKING_ATTEMPT_LOG_FILE",
        str(STATE_FILE.with_name("usvisa_booking_attempts.jsonl")),
    )
)
BOOKING_ATTEMPT_LOG_FILE = (
    configured_booking_attempt_log_file
    if configured_booking_attempt_log_file.is_absolute()
    else PROJECT_ROOT / configured_booking_attempt_log_file
)
BOOKING_ATTEMPT_LOG_MAX_BYTES = max(
    4096,
    int(os.getenv("USVISA_BOOKING_ATTEMPT_LOG_MAX_BYTES", str(512 * 1024))),
)
BOOKING_ATTEMPT_LOG_RETAIN_LINES = max(
    1,
    int(os.getenv("USVISA_BOOKING_ATTEMPT_LOG_RETAIN_LINES", "200")),
)
BOOKING_RESPONSE_BODY_MAX_CHARS = max(
    1024,
    int(os.getenv("USVISA_BOOKING_RESPONSE_BODY_MAX_CHARS", str(32 * 1024))),
)
configured_error_state_file = Path(
    os.getenv("USVISA_ERROR_STATE_FILE", str(PROJECT_ROOT / "state/usvisa_errors.json"))
)
ERROR_STATE_FILE = (
    configured_error_state_file
    if configured_error_state_file.is_absolute()
    else PROJECT_ROOT / configured_error_state_file
)
REQUEST_TIMEOUT_SECONDS = int(os.getenv("USVISA_REQUEST_TIMEOUT_SECONDS", "30"))
HEALTHCHECK_INTERVAL_MINUTES = int(os.getenv("USVISA_HEALTHCHECK_INTERVAL_MINUTES", "60"))
ERROR_DIGEST_INTERVAL_MINUTES = int(os.getenv("USVISA_ERROR_DIGEST_INTERVAL_MINUTES", "180"))
MIN_ERROR_DIGEST_INTERVAL_MINUTES = 180
TRANSIENT_DATES_STATUS_RETRY_DELAYS_SECONDS = [15, 45]
TRANSIENT_DATES_STATUSES = {502, 503, 504}
MIN_CHECK_INTERVAL_SECONDS = int(os.getenv("USVISA_MIN_CHECK_INTERVAL_SECONDS", "60"))
MAX_CHECK_INTERVAL_SECONDS = int(os.getenv("USVISA_MAX_CHECK_INTERVAL_SECONDS", "300"))
USVISA_SSL_VERIFY = os.getenv("USVISA_SSL_VERIFY", "true").lower() not in {"0", "false", "no"}
USVISA_VPN_ENABLED = os.getenv("USVISA_VPN_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
USVISA_VPN_PROXY_URL = os.getenv("USVISA_VPN_PROXY_URL", "http://127.0.0.1:2080").strip()
USVISA_MONITOR_DB_SYNC = os.getenv("USVISA_MONITOR_DB_SYNC", "true").lower() not in {"0", "false", "no"}
USVISA_PREFER_BOOTSTRAP_SESSION = os.getenv(
    "USVISA_PREFER_BOOTSTRAP_SESSION",
    os.getenv("USVISA_PREFER_ENV_COOKIE", "false"),
).lower() in {"1", "true", "yes"}
IGNORED_DATES = {
    date.strip()
    for date in os.getenv("USVISA_IGNORED_DATES", "").split(",")
    if date.strip()
}

# Эти значения нужны только как bootstrap после ручного логина.
USVISA_BOOTSTRAP_COOKIE = os.getenv("USVISA_BOOTSTRAP_COOKIE", os.getenv("USVISA_COOKIE", ""))
USVISA_BOOTSTRAP_CSRF_TOKEN = os.getenv(
    "USVISA_BOOTSTRAP_CSRF_TOKEN",
    os.getenv("USVISA_X_CSRF_TOKEN", ""),
)
USVISA_BOOTSTRAP_SESSION_ID = os.getenv("USVISA_BOOTSTRAP_SESSION_ID", os.getenv("USVISA_SESSION_ID", ""))
USVISA_BOOTSTRAP_IF_NONE_MATCH = os.getenv(
    "USVISA_BOOTSTRAP_IF_NONE_MATCH",
    os.getenv("USVISA_IF_NONE_MATCH", ""),
)
USVISA_USER_AGENT = os.getenv(
    "USVISA_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PRIORITY_DATES = [
    date.strip()
    for date in os.getenv("USVISA_PRIORITY_DATES", "").split(",")
    if DATE_RE.match(date.strip())
]
try:
    PREPARATION_DAYS = max(1, int(os.getenv("USVISA_PREPARATION_DAYS", "1")))
except ValueError:
    PREPARATION_DAYS = 1


class DatesFetchError(RuntimeError):
    pass


class AuthRequiredError(DatesFetchError):
    pass


class EmptyDatesError(DatesFetchError):
    pass


class MonitorState:
    def __init__(
        self,
        appointments: dict[str, set[str]] | None = None,
        last_healthcheck_at: datetime | None = None,
        last_slot_notification_at: datetime | None = None,
        last_error_at: datetime | None = None,
        last_error_digest_at: datetime | None = None,
        error_events: list[dict[str, str]] | None = None,
        next_check_at: datetime | None = None,
        cookie: str = "",
        cookie_candidates: list[dict[str, str]] | None = None,
        browser_headers: dict[str, str] | None = None,
        csrf_token: str = "",
        session_id: str = "",
        if_none_match: str = "",
        monitor_events: list[dict[str, Any]] | None = None,
        latest_found_dates: list[str] | None = None,
        latest_ignored_dates: list[str] | None = None,
        latest_ignored_appointments: dict[str, set[str]] | None = None,
        latest_dates_payload: list[dict[str, Any]] | None = None,
        latest_dates_to_check: list[str] | None = None,
        latest_current_times: dict[str, set[str]] | None = None,
        latest_new_times: dict[str, set[str]] | None = None,
        latest_booking: dict[str, Any] | None = None,
    ) -> None:
        self.appointments = appointments or {}
        self.last_healthcheck_at = last_healthcheck_at
        self.last_slot_notification_at = last_slot_notification_at
        self.last_error_at = last_error_at
        self.last_error_digest_at = last_error_digest_at
        self.error_events = error_events or []
        self.next_check_at = next_check_at
        self.cookie = cookie
        self.cookie_candidates = cookie_candidates or []
        self.browser_headers = browser_headers or {}
        self.csrf_token = csrf_token
        self.session_id = session_id
        self.if_none_match = if_none_match
        self.monitor_events = monitor_events or []
        self.latest_found_dates = latest_found_dates or []
        self.latest_ignored_dates = latest_ignored_dates or []
        self.latest_ignored_appointments = latest_ignored_appointments or {}
        self.latest_dates_payload = latest_dates_payload or []
        self.latest_dates_to_check = latest_dates_to_check or []
        self.latest_current_times = latest_current_times or {}
        self.latest_new_times = latest_new_times or {}
        self.latest_booking = latest_booking or {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_timing_context() -> dict[str, float | None]:
    return {
        "run_started": time.perf_counter(),
        "slot_discovered": None,
    }


def _timing_log(
    event: str,
    *,
    timing: dict[str, float | None],
    action_started: float | None = None,
    **fields: Any,
) -> None:
    now = _utc_now()
    monotonic_now = time.perf_counter()
    parts = [
        "TIMING",
        f"utc={now.astimezone(timezone.utc).isoformat(timespec='milliseconds')}",
        f"msk={now.astimezone(MOSCOW_TZ).isoformat(timespec='milliseconds')}",
        f"event={event}",
        f"run_elapsed_ms={round((monotonic_now - float(timing['run_started'])) * 1000)}",
    ]
    slot_discovered = timing.get("slot_discovered")
    if slot_discovered is not None:
        parts.append(
            f"slot_elapsed_ms={round((monotonic_now - float(slot_discovered)) * 1000)}"
        )
    if action_started is not None:
        parts.append(f"action_elapsed_ms={round((monotonic_now - action_started) * 1000)}")
    for key, value in fields.items():
        safe_value = str(value).replace("\r", " ").replace("\n", " ")
        parts.append(f"{key}={safe_value}")
    print(" ".join(parts), flush=True)


def _sanitize_booking_response_body(
    body: str,
    state: MonitorState,
    extra_secrets: list[str] | None = None,
) -> str:
    sanitized = body
    secrets = [state.csrf_token, state.session_id]
    secrets.extend(_parse_cookie_header(state.cookie).values())
    secrets.extend(extra_secrets or [])
    for candidate in state.cookie_candidates:
        if not isinstance(candidate, dict):
            continue
        for key, value in candidate.items():
            if any(marker in str(key).lower() for marker in ("cookie", "csrf", "token", "session")):
                secrets.append(str(value))
    for key, value in state.browser_headers.items():
        if any(marker in str(key).lower() for marker in ("cookie", "csrf", "token", "session", "authorization")):
            secrets.append(str(value))
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    sanitized = re.sub(
        r'(?i)(authenticity_token.{0,100}?value=["\'])[^"\']+',
        r"\1<redacted>",
        sanitized,
    )
    sanitized = re.sub(
        r'(?i)(csrf-token.{0,100}?content=["\'])[^"\']+',
        r"\1<redacted>",
        sanitized,
    )
    return re.sub(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
        "<redacted-email>",
        sanitized,
    )


def _safe_booking_response_preview(
    body: str,
    state: MonitorState,
    extra_secrets: list[str] | None = None,
) -> str:
    sanitized = _sanitize_booking_response_body(body, state, extra_secrets)
    return re.sub(r"\s+", " ", sanitized[:4000]).strip()[:300]


def _persist_booking_attempt(record: dict[str, Any]) -> None:
    path = BOOKING_ATTEMPT_LOG_FILE
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600)
        os.chmod(path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        pending = memoryview(encoded)
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("Не удалось дописать booking attempt log")
            pending = pending[written:]

        if os.fstat(fd).st_size > BOOKING_ATTEMPT_LOG_MAX_BYTES:
            os.lseek(fd, 0, os.SEEK_SET)
            content = os.read(fd, os.fstat(fd).st_size)
            recent_lines = content.splitlines()[-BOOKING_ATTEMPT_LOG_RETAIN_LINES :]
            rotated = b"\n".join(recent_lines) + (b"\n" if recent_lines else b"")
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            pending = memoryview(rotated)
            while pending:
                written = os.write(fd, pending)
                if written <= 0:
                    raise OSError("Не удалось завершить ротацию booking attempt log")
                pending = pending[written:]

        os.fsync(fd)
    except Exception as exc:
        print(
            "⚠️ Не удалось сохранить ответ booking POST: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(fd)


def _persist_booking_response(
    state: MonitorState,
    *,
    date: str,
    time_value: str,
    status: int,
    final_url: str,
    body: str,
    confirmed: bool,
    request_secrets: list[str],
) -> None:
    record: dict[str, Any] = {
        "at": _utc_now().astimezone(timezone.utc).isoformat(),
        "date": date,
        "time": time_value,
        "status": status,
        "final_path": urlsplit(final_url).path,
        "confirmed": confirmed,
        "body_length": len(body.encode("utf-8")),
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }
    if not confirmed:
        sanitized_body = _sanitize_booking_response_body(
            body,
            state,
            extra_secrets=request_secrets,
        )
        record["response_body"] = sanitized_body[:BOOKING_RESPONSE_BODY_MAX_CHARS]
        record["body_truncated"] = len(sanitized_body) > BOOKING_RESPONSE_BODY_MAX_CHARS
        record["response_preview"] = _safe_booking_response_preview(
            body,
            state,
            extra_secrets=request_secrets,
        )
    _persist_booking_attempt(record)


def _parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bootstrap_state(appointments: dict[str, set[str]] | None = None) -> MonitorState:
    return MonitorState(
        appointments=appointments,
        cookie=USVISA_BOOTSTRAP_COOKIE,
        cookie_candidates=[],
        browser_headers={},
        csrf_token=USVISA_BOOTSTRAP_CSRF_TOKEN,
        session_id=USVISA_BOOTSTRAP_SESSION_ID,
        if_none_match=USVISA_BOOTSTRAP_IF_NONE_MATCH,
    )


def _state_payload(state: MonitorState) -> dict[str, Any]:
    return {
        "appointments": {
            date: sorted(times)
            for date, times in sorted(state.appointments.items())
        },
        "last_healthcheck_at": (
            state.last_healthcheck_at.astimezone(timezone.utc).isoformat()
            if state.last_healthcheck_at
            else None
        ),
        "last_slot_notification_at": (
            state.last_slot_notification_at.astimezone(timezone.utc).isoformat()
            if state.last_slot_notification_at
            else None
        ),
        "monitor_events": state.monitor_events[-1000:],
        "latest_found_dates": sorted(state.latest_found_dates),
        "latest_ignored_dates": sorted(state.latest_ignored_dates),
        "latest_ignored_appointments": {
            date: sorted(times)
            for date, times in sorted(state.latest_ignored_appointments.items())
        },
        "latest_dates_payload": state.latest_dates_payload,
        "latest_dates_to_check": sorted(state.latest_dates_to_check),
        "latest_current_times": {
            date: sorted(times)
            for date, times in sorted(state.latest_current_times.items())
        },
        "latest_new_times": {
            date: sorted(times)
            for date, times in sorted(state.latest_new_times.items())
        },
        "latest_booking": state.latest_booking,
        "next_check_at": (
            state.next_check_at.astimezone(timezone.utc).isoformat()
            if state.next_check_at
            else None
        ),
        "session": {
            "cookie": "" if state.cookie_candidates else state.cookie,
            "cookie_candidates": state.cookie_candidates,
            "browser_headers": state.browser_headers,
            "csrf_token": state.csrf_token,
            "session_id": state.session_id,
            "if_none_match": state.if_none_match,
        },
    }


def _random_check_interval() -> timedelta:
    min_seconds = max(0, MIN_CHECK_INTERVAL_SECONDS)
    max_seconds = max(min_seconds, MAX_CHECK_INTERVAL_SECONDS)
    return timedelta(seconds=random.randint(min_seconds, max_seconds))


def _schedule_next_check(state: MonitorState, now: datetime | None = None) -> None:
    now = now or _utc_now()
    state.next_check_at = now + _random_check_interval()
    print(f"Следующая проверка запланирована на {state.next_check_at.isoformat()}")


def _is_check_due(state: MonitorState, now: datetime) -> bool:
    if state.next_check_at is None:
        return True
    return now >= state.next_check_at


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in cookie_header.split(";"):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def _format_cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _merge_set_cookies(cookie_header: str, set_cookie_headers: list[str]) -> str:
    cookies = _parse_cookie_header(cookie_header)
    for set_cookie_header in set_cookie_headers:
        parsed_cookie = SimpleCookie()
        parsed_cookie.load(set_cookie_header)
        for name, morsel in parsed_cookie.items():
            cookies[name] = morsel.value
    return _format_cookie_header(cookies)


def _extract_set_cookie_names(set_cookie_headers: list[str]) -> list[str]:
    names: list[str] = []
    for set_cookie_header in set_cookie_headers:
        parsed_cookie = SimpleCookie()
        parsed_cookie.load(set_cookie_header)
        names.extend(parsed_cookie.keys())
    return sorted(set(names))


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        parts = name.split("-")
        normalized_name = "-".join(part[:1].upper() + part[1:].lower() for part in parts)
        normalized[normalized_name] = value
    return normalized


def _parse_browser_headers(raw_headers: Any) -> dict[str, str]:
    if not isinstance(raw_headers, dict):
        return {}

    parsed: dict[str, str] = {}
    blocked_headers = {
        "cookie",
        "host",
        "if-none-match",
        "x-csrf-token",
        "session-id",
    }
    for raw_name, raw_value in raw_headers.items():
        name = str(raw_name).strip()
        value = str(raw_value).strip()
        if not name or not value or name.lower() in blocked_headers:
            continue
        parsed[name] = value
    return _normalize_headers(parsed)


def _build_headers(state: MonitorState, include_if_none_match: bool = True) -> dict[str, str]:
    if not USVISA_BOOTSTRAP_COOKIE and not state.cookie:
        raise RuntimeError("Не задана bootstrap cookie для запроса US Visa")
    if not USVISA_BOOTSTRAP_CSRF_TOKEN and not state.csrf_token:
        raise RuntimeError("Не задан bootstrap CSRF token для запроса US Visa")
    if not state.cookie:
        raise RuntimeError("Не задана cookie для запроса US Visa")
    if not state.csrf_token:
        raise RuntimeError("Не задан CSRF token для запроса US Visa")

    headers = _normalize_headers(state.browser_headers)
    fallback_headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
        "Host": "ais.usvisa-info.com",
        "Referer": APPOINTMENT_URL,
        "Sec-Ch-Ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": USVISA_USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
    }
    for name, value in fallback_headers.items():
        headers.setdefault(name, value)

    headers["Cookie"] = state.cookie
    headers["X-Csrf-Token"] = state.csrf_token

    if state.session_id:
        headers["Session-Id"] = state.session_id

    if include_if_none_match and state.if_none_match:
        headers["If-None-Match"] = state.if_none_match

    return headers


def _extract_dates(payload: list[dict[str, Any]]) -> list[str]:
    dates: list[str] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        raw_date = item.get("date")
        if isinstance(raw_date, str) and raw_date.strip():
            dates.append(raw_date.strip())

    # Убираем дубли и сортируем.
    return sorted(set(dates))


def _extract_times(payload: dict[str, Any]) -> list[str]:
    raw_times = payload.get("business_times")
    if not isinstance(raw_times, list):
        raw_times = payload.get("available_times")
    if not isinstance(raw_times, list):
        return []

    times = [
        raw_time.strip()
        for raw_time in raw_times
        if isinstance(raw_time, str) and raw_time.strip()
    ]
    return sorted(set(times))


def _usvisa_proxy_url() -> str | None:
    if not USVISA_VPN_ENABLED:
        return None
    return USVISA_VPN_PROXY_URL or None


async def _submit_booking(
    session: aiohttp.ClientSession,
    state: MonitorState,
    facility_id: str,
    date: str,
    time_value: str,
    timing: dict[str, float | None],
) -> bool:
    started = time.perf_counter()
    _timing_log(
        "booking_post_start",
        timing=timing,
        date=date,
        time_value=time_value,
    )
    print(f"Submitting appointment via HTTP POST: {date} {time_value}")
    form_data = {
        "authenticity_token": state.csrf_token,
        "confirmed_limit_message": "1",
        "use_consulate_appointment_capacity": "true",
        "appointments[consulate_appointment][facility_id]": facility_id,
        "appointments[consulate_appointment][date]": date,
        "appointments[consulate_appointment][time]": time_value,
        "appointments[asc_appointment][facility_id]": "",
        "appointments[asc_appointment][date]": "",
        "appointments[asc_appointment][time]": "",
    }
    headers = _build_appointment_headers(state)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    request_secrets = [state.csrf_token, state.session_id]
    request_secrets.extend(_parse_cookie_header(state.cookie).values())
    for key, value in headers.items():
        if any(marker in str(key).lower() for marker in ("cookie", "csrf", "token", "session", "authorization")):
            request_secrets.append(str(value))
    try:
        async with session.post(
            APPOINTMENT_URL,
            data=form_data,
            headers=headers,
            allow_redirects=True,
            ssl=USVISA_SSL_VERIFY,
            proxy=_usvisa_proxy_url(),
        ) as response:
            _update_state_cookie_from_response(state, response)
            _update_state_headers_from_response(state, response, update_etag=False)
            body = await response.text()
            final_url = str(response.url)
            response_status = response.status
    except Exception as exc:
        _persist_booking_attempt(
            {
                "at": _utc_now().astimezone(timezone.utc).isoformat(),
                "date": date,
                "time": time_value,
                "status": None,
                "confirmed": False,
                "error_type": type(exc).__name__,
            }
        )
        _timing_log(
            "booking_post_error",
            timing=timing,
            action_started=started,
            date=date,
            time_value=time_value,
            error_type=type(exc).__name__,
            error=repr(exc),
        )
        raise
    booked = "/appointment/instructions" in final_url or "/appointment/instructions" in body
    _persist_booking_response(
        state,
        date=date,
        time_value=time_value,
        status=response_status,
        final_url=final_url,
        body=body,
        confirmed=booked,
        request_secrets=request_secrets,
    )
    log_fields = {
        "date": date,
        "time_value": time_value,
        "status": response_status,
        "final_path": urlsplit(final_url).path,
        "confirmed": booked,
    }
    if not booked:
        log_fields["response_preview"] = _safe_booking_response_preview(
            body,
            state,
            extra_secrets=request_secrets,
        )
    _timing_log(
        "booking_post_end",
        timing=timing,
        action_started=started,
        **log_fields,
    )
    return booked


def _record_booking_success(
    state: MonitorState,
    *,
    date: str,
    time_value: str,
    method: str,
) -> None:
    state.latest_booking = {
        "at": _utc_now().astimezone(timezone.utc).isoformat(),
        "date": date,
        "time": time_value,
        "method": method,
    }


def _preferred_time_for_date(available_times: list[str]) -> str:
    sorted_times = sorted(set(available_times))
    if len(sorted_times) >= 3:
        return sorted_times[len(sorted_times) // 2]
    return sorted_times[-1]


def _booking_allowed_from_date(preparation_days: int) -> str:
    return (datetime.now().astimezone().date() + timedelta(days=max(1, preparation_days))).isoformat()


def _priority_date_attempt_order(dates: list[str]) -> list[str]:
    if len(dates) <= 1:
        return dates[:]
    if len(dates) == 2:
        return [dates[1], dates[0]]

    middle = len(dates) // 2
    ordered_indexes = [middle]
    step = 1
    while len(ordered_indexes) < len(dates):
        right = middle + step
        left = middle - step
        if right < len(dates):
            ordered_indexes.append(right)
        if left >= 0:
            ordered_indexes.append(left)
        step += 1
    return [dates[index] for index in ordered_indexes]


def _build_priority_booking_candidates(
    matched_dates: list[str],
    current_appointments: dict[str, set[str]],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    earliest_allowed_date = _booking_allowed_from_date(PREPARATION_DAYS)
    for date in _priority_date_attempt_order(matched_dates):
        if date not in current_appointments:
            print(
                f"Автозапись пропущена: приоритетная дата {date} найдена, "
                "но находится в игнор-листе, поэтому времена для нее не проверялись"
            )
            continue
        if date < earliest_allowed_date:
            print(
                f"Автозапись пропущена: дата {date} слишком близкая. "
                f"Разрешено записывать не раньше {earliest_allowed_date} "
                f"(сегодня + {PREPARATION_DAYS} дн.)"
            )
            continue
        available_times = sorted(current_appointments.get(date, set()))
        if not available_times:
            print(
                f"Автозапись пропущена: приоритетная дата {date} проверена, "
                "но свободного времени для записи не найдено"
            )
            continue
        candidates.append((date, _preferred_time_for_date(available_times)))
    return candidates


async def notify_booking_success(date: str, time_value: str, method: str) -> bool:
    message = (
        "✅ Автозапись на приоритетный слот подтверждена.\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"Дата: {date}\n"
        f"Время: {time_value}\n"
        f"Метод: {method}\n"
        f"{APPOINTMENT_URL}"
    )
    return await record_event(message)


async def notify_booking_failure(candidates: list[tuple[str, str]]) -> bool:
    lines = [f"• {date} {time_value}" for date, time_value in candidates]
    message = (
        "❌ Автозапись на приоритетные слоты не удалась.\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"Пробовали слоты:\n{chr(10).join(lines)}\n"
        f"{APPOINTMENT_URL}"
    )
    return await record_event(message)


async def _refresh_priority_booking_candidates(
    session: aiohttp.ClientSession,
    state: MonitorState,
    timing: dict[str, float | None],
) -> list[tuple[str, str]]:
    started = time.perf_counter()
    _timing_log("availability_refresh_start", timing=timing)
    refreshed_dates = await fetch_dates_with_cookie_candidates(session, state)
    refreshed_dates_set = set(refreshed_dates)
    refreshed_dates_to_check = sorted(refreshed_dates_set - IGNORED_DATES)
    refreshed_appointments: dict[str, set[str]] = {}
    for date in refreshed_dates_to_check:
        refreshed_appointments[date] = set(await fetch_available_times(session, state, date))
    refreshed_matched_dates = [date for date in PRIORITY_DATES if date in refreshed_dates_set]
    refreshed_candidates = _build_priority_booking_candidates(refreshed_matched_dates, refreshed_appointments)
    print(
        "Актуализированные кандидаты перед browser fallback: "
        + (
            ", ".join(f"{date} {time_value}" for date, time_value in refreshed_candidates)
            if refreshed_candidates
            else "не найдены"
        )
    )
    _timing_log(
        "availability_refresh_end",
        timing=timing,
        action_started=started,
        candidate_count=len(refreshed_candidates),
    )
    return refreshed_candidates


async def _try_autobook_priority_dates(
    session: aiohttp.ClientSession,
    state: MonitorState,
    current_dates: list[str],
    current_appointments: dict[str, set[str]],
    timing: dict[str, float | None],
) -> dict[str, str] | None:
    if not PRIORITY_DATES:
        return None
    if not CURRENT_FACILITY_ID:
        print("Автозапись пропущена: не удалось определить facility_id для текущего консульства")
        return None

    found_dates = set(current_dates)
    matched_dates = [date for date in PRIORITY_DATES if date in found_dates]
    if not matched_dates:
        return None

    candidates = _build_priority_booking_candidates(matched_dates, current_appointments)
    if not candidates:
        print(
            "Стратегия автозаписи не нашла готовые кандидаты среди приоритетных дат. "
            f"matched_dates={matched_dates}"
        )
        return None

    print(
        "Кандидаты на автозапись по серединной стратегии: "
        + ", ".join(f"{date} {time_value}" for date, time_value in candidates)
    )

    for date, time_value in candidates:
        print(f"HTTP booking attempt for priority slot: {date} {time_value}")
        booked = await _submit_booking(
            session,
            state,
            CURRENT_FACILITY_ID,
            date,
            time_value,
            timing,
        )
        if booked:
            _record_booking_success(state, date=date, time_value=time_value, method="http")
            notification_started = time.perf_counter()
            _timing_log(
                "booking_success_notification_start",
                timing=timing,
                date=date,
                time_value=time_value,
            )
            notification_sent = await notify_booking_success(date, time_value, "http")
            _timing_log(
                "booking_success_notification_end",
                timing=timing,
                action_started=notification_started,
                sent=notification_sent,
            )
            if not notification_sent:
                print("⚠️ Не удалось отправить уведомление об успешной записи в локальный журнал")
            return {
                "date": date,
                "time": time_value,
                "method": "http",
            }

        print(f"HTTP-автозапись не подтвердилась: {date} {time_value}")

    print("HTTP-автозапись не сработала ни для одного приоритетного слота. Актуализируем список дат и пробуем browser fallback.")
    try:
        browser_candidates = await _refresh_priority_booking_candidates(session, state, timing)
    except Exception as exc:
        print(
            "Не удалось актуализировать кандидатов перед browser fallback, "
            f"используем последний известный список: {type(exc).__name__}: {exc}"
        )
        browser_candidates = candidates

    if not browser_candidates:
        notification_started = time.perf_counter()
        _timing_log("booking_failure_notification_start", timing=timing)
        notification_sent = await notify_booking_failure(candidates)
        _timing_log(
            "booking_failure_notification_end",
            timing=timing,
            action_started=notification_started,
            sent=notification_sent,
        )
        if not notification_sent:
            print("⚠️ Не удалось отправить уведомление о неуспешной записи в локальный журнал")
        return None

    for date, time_value in browser_candidates:
        browser_started = time.perf_counter()
        _timing_log(
            "browser_fallback_start",
            timing=timing,
            date=date,
            time_value=time_value,
        )
        try:
            browser_booked = await asyncio.to_thread(_book_priority_date_in_browser, date, time_value)
        except Exception as exc:
            _timing_log(
                "browser_fallback_error",
                timing=timing,
                action_started=browser_started,
                error_type=type(exc).__name__,
                error=repr(exc),
            )
            print(
                f"Browser fallback не удался для {date} {time_value}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue
        _timing_log(
            "browser_fallback_end",
            timing=timing,
            action_started=browser_started,
            confirmed=browser_booked,
        )
        if browser_booked:
            _record_booking_success(state, date=date, time_value=time_value, method="browser")
            notification_sent = await notify_booking_success(date, time_value, "browser")
            if not notification_sent:
                print("⚠️ Не удалось отправить уведомление об успешной записи в локальный журнал")
            return {
                "date": date,
                "time": time_value,
                "method": "browser",
            }
        print(f"Browser fallback не подтвердил запись: {date} {time_value}")

    notification_started = time.perf_counter()
    _timing_log("booking_failure_notification_start", timing=timing)
    notification_sent = await notify_booking_failure(browser_candidates)
    _timing_log(
        "booking_failure_notification_end",
        timing=timing,
        action_started=notification_started,
        sent=notification_sent,
    )
    if not notification_sent:
        print("⚠️ Не удалось отправить уведомление о неуспешной записи в локальный журнал")
    return None


def _book_priority_date_in_browser(date: str, time_value: str) -> bool:
    from usvisa_browser_login_and_run import open_booking_flow

    print(f"Запускаем browser fallback для {date} {time_value}")
    return bool(open_booking_flow(date, time_value, submit=True))


def _is_auth_required_payload(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("error") == "You need to sign in or sign up before continuing."
    )


def _parse_seen_appointments(content: dict[Any, Any]) -> dict[str, set[str]]:
    seen_appointments: dict[str, set[str]] = {}
    for raw_date, raw_times in content.items():
        date = str(raw_date)
        if isinstance(raw_times, list):
            seen_appointments[date] = {str(raw_time) for raw_time in raw_times}
        else:
            seen_appointments[date] = set()
    return seen_appointments


def _parse_error_events(raw_events: Any) -> list[dict[str, str]]:
    if not isinstance(raw_events, list):
        return []

    events: list[dict[str, str]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        occurred_at = str(raw_event.get("at") or "").strip()
        error_type = str(raw_event.get("type") or "").strip()
        message = str(raw_event.get("message") or "").strip()
        if occurred_at and error_type and message:
            events.append(
                {
                    "at": occurred_at,
                    "type": error_type,
                    "message": message,
                }
            )
    return events


def _read_error_state() -> dict[str, Any]:
    if not ERROR_STATE_FILE.exists():
        return {}
    try:
        content = json.loads(ERROR_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return content if isinstance(content, dict) else {}


def _write_error_state(state: MonitorState) -> None:
    ERROR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ERROR_STATE_FILE.write_text(
        json.dumps(
            {
                "last_error_at": (
                    state.last_error_at.astimezone(timezone.utc).isoformat()
                    if state.last_error_at
                    else None
                ),
                "last_error_digest_at": (
                    state.last_error_digest_at.astimezone(timezone.utc).isoformat()
                    if state.last_error_digest_at
                    else None
                ),
                "error_events": state.error_events,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _parse_monitor_events(raw_events: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_events, list):
        return []

    events: list[dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        occurred_at = str(raw_event.get("at") or "").strip()
        status = str(raw_event.get("status") or "").strip()
        if not occurred_at or status not in {"success", "error"}:
            continue
        event: dict[str, Any] = {
            "at": occurred_at,
            "status": status,
        }
        for key in (
            "error_category",
            "error_type",
            "message",
            "found_dates",
            "active_dates",
            "dates_without_times",
            "ignored_dates",
            "active_appointments",
            "ignored_appointments",
        ):
            value = raw_event.get(key)
            if value is not None:
                event[key] = value
        events.append(event)
    return events[-1000:]


def _normalize_date_list(raw_dates: Any) -> list[str]:
    if not isinstance(raw_dates, list):
        return []
    return sorted({str(date).strip() for date in raw_dates if str(date).strip()})


def _parse_cookie_candidates(raw_candidates: Any) -> list[dict[str, str]]:
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[dict[str, str]] = []
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            continue
        label = str(raw_candidate.get("label") or "").strip()
        cookie = str(raw_candidate.get("cookie") or "").strip()
        if cookie:
            candidate = {"label": label or "cookie candidate", "cookie": cookie}
            for key in ("csrf_token", "session_id", "if_none_match"):
                value = str(raw_candidate.get(key) or "").strip()
                if value:
                    candidate[key] = value
            candidates.append(candidate)
    return candidates


def _read_state() -> MonitorState:
    if not STATE_FILE.exists():
        snapshot = monitor_store.read_snapshot() if USVISA_MONITOR_DB_SYNC else {}
        if isinstance(snapshot, dict) and snapshot:
            content = snapshot
        else:
            return _bootstrap_state()
    else:
        try:
            content = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            snapshot = monitor_store.read_snapshot() if USVISA_MONITOR_DB_SYNC else {}
            if isinstance(snapshot, dict) and snapshot:
                content = snapshot
            else:
                return _bootstrap_state()

    try:
        if isinstance(content, list):
            return _bootstrap_state(appointments={str(item): set() for item in content})
        if not isinstance(content, dict):
            return _bootstrap_state()

        error_state = _read_error_state()
        appointments = content.get("appointments")
        session_state = content.get("session")
        if isinstance(appointments, dict):
            if isinstance(session_state, dict):
                state_cookie = session_state.get("cookie")
                state_cookie_candidates = session_state.get("cookie_candidates")
                state_browser_headers = session_state.get("browser_headers")
                state_csrf_token = session_state.get("csrf_token")
                state_session_id = session_state.get("session_id")
                state_if_none_match = session_state.get("if_none_match")
            else:
                state_cookie = content.get("cookie")
                state_cookie_candidates = content.get("cookie_candidates")
                state_browser_headers = content.get("browser_headers")
                state_csrf_token = content.get("csrf_token")
                state_session_id = content.get("session_id")
                state_if_none_match = content.get("if_none_match")
            cookie_candidates = _parse_cookie_candidates(state_cookie_candidates)
            browser_headers = _parse_browser_headers(state_browser_headers)

            cookie = (
                USVISA_BOOTSTRAP_COOKIE
                if USVISA_PREFER_BOOTSTRAP_SESSION
                else str(state_cookie or ("" if cookie_candidates else USVISA_BOOTSTRAP_COOKIE))
            )
            csrf_token = (
                USVISA_BOOTSTRAP_CSRF_TOKEN
                if USVISA_PREFER_BOOTSTRAP_SESSION
                else str(state_csrf_token or USVISA_BOOTSTRAP_CSRF_TOKEN)
            )
            session_id = (
                USVISA_BOOTSTRAP_SESSION_ID
                if USVISA_PREFER_BOOTSTRAP_SESSION
                else str(state_session_id or USVISA_BOOTSTRAP_SESSION_ID)
            )
            if_none_match = (
                USVISA_BOOTSTRAP_IF_NONE_MATCH
                if USVISA_PREFER_BOOTSTRAP_SESSION
                else str(state_if_none_match or USVISA_BOOTSTRAP_IF_NONE_MATCH)
            )
            return MonitorState(
                appointments=_parse_seen_appointments(appointments),
                last_healthcheck_at=_parse_utc_datetime(content.get("last_healthcheck_at")),
                last_slot_notification_at=_parse_utc_datetime(content.get("last_slot_notification_at")),
                last_error_at=_parse_utc_datetime(
                    error_state.get("last_error_at", content.get("last_error_at"))
                ),
                last_error_digest_at=_parse_utc_datetime(
                    error_state.get("last_error_digest_at", content.get("last_error_digest_at"))
                ),
                error_events=_parse_error_events(
                    error_state.get("error_events", content.get("error_events"))
                ),
                next_check_at=_parse_utc_datetime(content.get("next_check_at")),
                cookie=cookie,
                cookie_candidates=(
                    []
                    if USVISA_PREFER_BOOTSTRAP_SESSION
                    else cookie_candidates
                ),
                browser_headers=(
                    {}
                    if USVISA_PREFER_BOOTSTRAP_SESSION
                    else browser_headers
                ),
                csrf_token=csrf_token,
                session_id=session_id,
                if_none_match=if_none_match,
                monitor_events=_parse_monitor_events(content.get("monitor_events")),
                latest_found_dates=_normalize_date_list(content.get("latest_found_dates")),
                latest_ignored_dates=_normalize_date_list(content.get("latest_ignored_dates")),
                latest_ignored_appointments=_parse_seen_appointments(
                    content.get("latest_ignored_appointments")
                    if isinstance(content.get("latest_ignored_appointments"), dict)
                    else {}
                ),
                latest_dates_payload=(
                    [item for item in content.get("latest_dates_payload") if isinstance(item, dict)]
                    if isinstance(content.get("latest_dates_payload"), list)
                    else []
                ),
                latest_dates_to_check=_normalize_date_list(content.get("latest_dates_to_check")),
                latest_current_times=_parse_seen_appointments(
                    content.get("latest_current_times")
                    if isinstance(content.get("latest_current_times"), dict)
                    else {}
                ),
                latest_new_times=_parse_seen_appointments(
                    content.get("latest_new_times")
                    if isinstance(content.get("latest_new_times"), dict)
                    else {}
                ),
                latest_booking=(
                    content.get("latest_booking")
                    if isinstance(content.get("latest_booking"), dict)
                    else {}
                ),
            )

        return _bootstrap_state(appointments=_parse_seen_appointments(content))
    except Exception:
        return _bootstrap_state()


def _write_state(state: MonitorState) -> None:
    payload = _state_payload(state)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if USVISA_MONITOR_DB_SYNC:
        monitor_store.write_snapshot(payload, _utc_now().astimezone(timezone.utc).isoformat())
    _write_error_state(state)


def _build_times_url(date: str) -> str:
    query = urlencode(
        {
            "date": date,
            "appointments[expedite]": "false",
        },
        safe="[]",
    )
    return f"{TIMES_API_URL}?{query}"


def _update_state_cookie_from_response(state: MonitorState, response: aiohttp.ClientResponse) -> None:
    set_cookie_headers = response.headers.getall("Set-Cookie", [])
    if not set_cookie_headers:
        return

    updated_cookie = _merge_set_cookies(state.cookie, set_cookie_headers)
    if updated_cookie != state.cookie:
        state.cookie = updated_cookie


def _update_state_headers_from_response(
    state: MonitorState,
    response: aiohttp.ClientResponse,
    update_etag: bool = True,
) -> None:
    session_id = response.headers.get("Session-Id")
    if session_id and session_id != state.session_id:
        state.session_id = session_id
        print("USVisa Session-Id updated from response")

    etag = response.headers.get("ETag")
    if update_etag and etag and etag != state.if_none_match:
        state.if_none_match = etag
        print("USVisa If-None-Match updated from ETag")


def _extract_csrf_token_from_html(html: str) -> str:
    patterns = [
        r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']csrf-token["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _build_appointment_headers(state: MonitorState) -> dict[str, str]:
    headers = _build_headers(state, include_if_none_match=False)
    headers["Accept"] = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    )
    headers["Sec-Fetch-Dest"] = "document"
    headers["Sec-Fetch-Mode"] = "navigate"
    headers.pop("Session-Id", None)
    headers.pop("X-Csrf-Token", None)
    headers.pop("X-Requested-With", None)
    return headers


async def refresh_session_from_appointment_page(
    session: aiohttp.ClientSession,
    state: MonitorState,
) -> None:
    headers = _build_appointment_headers(state)

    async with session.get(
        APPOINTMENT_URL,
        headers=headers,
        ssl=USVISA_SSL_VERIFY,
        proxy=_usvisa_proxy_url(),
    ) as response:
        response_text = await response.text()
        print(f"USVisa appointment page response.status = {response.status}")
        print(f"USVisa appointment page response.url = {response.url}")

        if "/users/sign_in" in str(response.url):
            raise AuthRequiredError("US Visa перенаправил на страницу входа. Нужно обновить browser session.")

        if response.status == 401:
            raise AuthRequiredError("Страница appointment вернула 401. Похоже, сессия протухла.")

        if response.status == 403:
            raise AuthRequiredError("Страница appointment вернула 403. Похоже, сессия протухла.")

        if not (200 <= response.status < 300):
            raise DatesFetchError(
                f"Неожиданный статус страницы appointment: {response.status}. Тело: {response_text[:500]}"
            )

        if 'id="user_email"' in response_text or "user[email]" in response_text:
            raise AuthRequiredError("Страница appointment открыла форму входа. Нужно обновить browser session.")

        _update_state_cookie_from_response(state, response)
        _update_state_headers_from_response(state, response, update_etag=False)

        csrf_token = _extract_csrf_token_from_html(response_text)
        if csrf_token and csrf_token != state.csrf_token:
            state.csrf_token = csrf_token
            print("USVisa CSRF token updated from appointment page")


async def _request_text(
    session: aiohttp.ClientSession,
    state: MonitorState,
    url: str,
    include_if_none_match: bool,
) -> tuple[int, str]:
    headers = _build_headers(state, include_if_none_match=include_if_none_match)
    request_kind = "with If-None-Match" if include_if_none_match else "without If-None-Match"
    print(f"USVisa request start: {url} ({request_kind})")

    try:
        async with session.get(
            url,
            headers=headers,
            ssl=USVISA_SSL_VERIFY,
            proxy=_usvisa_proxy_url(),
        ) as response:
            _update_state_cookie_from_response(state, response)
            _update_state_headers_from_response(state, response)
            response_text = await response.text()
            return response.status, response_text
    except Exception as e:
        print(
            "USVisa request failed: "
            f"url={url}, kind={request_kind}, error_type={type(e).__name__}, error={e!r}"
        )
        raise


async def _request_json_text(
    session: aiohttp.ClientSession,
    state: MonitorState,
    url: str,
    label: str,
) -> tuple[int, str]:
    attempts = len(TRANSIENT_DATES_STATUS_RETRY_DELAYS_SECONDS) + 1
    last_error: BaseException | None = None
    status = 0
    response_text = ""
    for attempt_index in range(attempts):
        try:
            status, response_text = await _request_text(
                session,
                state,
                url,
                include_if_none_match=True,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            should_retry = attempt_index < attempts - 1
            if not should_retry:
                raise
            delay = TRANSIENT_DATES_STATUS_RETRY_DELAYS_SECONDS[attempt_index]
            print(
                f"{label} transient connection error; retrying in {delay}s: "
                f"{type(exc).__name__}: {exc}"
            )
            await asyncio.sleep(delay)
            continue

        if status not in TRANSIENT_DATES_STATUSES or attempt_index >= attempts - 1:
            break
        delay = TRANSIENT_DATES_STATUS_RETRY_DELAYS_SECONDS[attempt_index]
        print(f"{label} transient status {status}; retrying in {delay}s")
        await asyncio.sleep(delay)

    if last_error is not None and not response_text:
        raise last_error

    if response_text.strip() or not state.if_none_match:
        return status, response_text

    return await _request_text(
        session,
        state,
        url,
        include_if_none_match=False,
    )

async def fetch_available_dates(session: aiohttp.ClientSession, state: MonitorState) -> list[str]:
    response_status, response_text = await _request_json_text(
        session,
        state,
        DATES_API_URL,
        "USVisa dates",
    )
    if response_status == 401:
        raise AuthRequiredError("Сервис вернул 401. Похоже, сессия или cookie протухли.")

    if response_status == 403:
        raise AuthRequiredError("Сервис вернул 403. Похоже, CSRF token / cookie больше невалидны.")

    if response_status == 429:
        raise DatesFetchError("Сервис вернул 429 Too Many Requests. Вас могли временно ограничить.")

    if response_status != 304 and not (200 <= response_status < 300):
        raise DatesFetchError(
            f"Неожиданный статус ответа: {response_status}. Тело: {response_text[:500]}"
        )

    if not response_text.strip():
        raise DatesFetchError(
            "Сервис вернул успешный статус для списка дат, но тело ответа пустое даже после retry без If-None-Match."
        )

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise DatesFetchError(f"Ответ не является JSON: {e}") from e

    if _is_auth_required_payload(payload):
        raise AuthRequiredError("US Visa API вернул: You need to sign in or sign up before continuing.")

    if not isinstance(payload, list):
        raise DatesFetchError(f"Ожидался список дат, но получено: {type(payload).__name__}")

    dates = _extract_dates(payload)
    state.latest_dates_payload = [item for item in payload if isinstance(item, dict)]
    print(f"USVisa response.status = {response_status}")
    print(f"USVisa response.text = {response_text[:1000]}")
    return dates


def _cookie_attempts(state: MonitorState) -> list[dict[str, str]]:
    attempts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for candidate in state.cookie_candidates:
        cookie = str(candidate.get("cookie") or "").strip()
        if not cookie:
            continue
        csrf_token = str(candidate.get("csrf_token") or "").strip()
        session_id = str(candidate.get("session_id") or "").strip()
        if_none_match = str(candidate.get("if_none_match") or "").strip()
        identity = (cookie, csrf_token, session_id, if_none_match)
        if identity in seen:
            continue
        label = str(candidate.get("label") or "cookie candidate").strip()
        attempt = {"label": label or "cookie candidate", "cookie": cookie}
        if csrf_token:
            attempt["csrf_token"] = csrf_token
        if session_id:
            attempt["session_id"] = session_id
        if if_none_match:
            attempt["if_none_match"] = if_none_match
        attempts.append(attempt)
        seen.add(identity)

    if attempts:
        return attempts

    if state.cookie:
        attempts.append(
            {
                "label": "saved cookie",
                "cookie": state.cookie,
                "csrf_token": state.csrf_token,
                "session_id": state.session_id,
                "if_none_match": state.if_none_match,
            }
        )

    return attempts or [{"label": "saved cookie", "cookie": state.cookie}]


async def fetch_dates_with_cookie_candidates(
    session: aiohttp.ClientSession,
    state: MonitorState,
) -> list[str]:
    attempts = _cookie_attempts(state)
    errors: list[Exception] = []

    for attempt in attempts:
        original_cookie = attempt["cookie"]
        state.cookie = attempt["cookie"]
        state.csrf_token = attempt.get("csrf_token") or state.csrf_token
        state.session_id = attempt.get("session_id") or state.session_id
        state.if_none_match = attempt.get("if_none_match") or state.if_none_match
        # print(f"USVisa trying cookie candidate: {attempt['label']}")
        # print(f"USVisa candidate cookie: {state.cookie}")
        # print(f"USVisa candidate csrf_token: {state.csrf_token}")
        # print(f"USVisa candidate session_id: {state.session_id}")
        # print(f"USVisa candidate if_none_match: {state.if_none_match}")
        try:
            dates = await fetch_available_dates(session, state)
        except (AuthRequiredError, DatesFetchError) as e:
            errors.append(e)
            print(
                "USVisa cookie candidate failed: "
                f"label={attempt['label']}, error_type={type(e).__name__}, error={e!r}"
            )
            continue
        except Exception as e:
            print(
                "USVisa unexpected failure with cookie candidate: "
                f"label={attempt['label']}, error_type={type(e).__name__}, error={e!r}"
            )
            raise

        for candidate in state.cookie_candidates:
            if (
                candidate.get("label") == attempt["label"]
                and candidate.get("cookie") == original_cookie
            ):
                candidate["cookie"] = state.cookie
                candidate["csrf_token"] = state.csrf_token
                candidate["session_id"] = state.session_id
                candidate["if_none_match"] = state.if_none_match
                break
        return dates

    if errors:
        raise errors[-1]
    raise DatesFetchError("Не удалось проверить список дат ни с одним cookie candidate.")


async def fetch_available_times(
    session: aiohttp.ClientSession,
    state: MonitorState,
    date: str,
) -> list[str]:
    url = _build_times_url(date)
    response_status, response_text = await _request_json_text(
        session,
        state,
        url,
        f"USVisa times for {date}",
    )
    if response_status == 401:
        raise AuthRequiredError("Сервис вернул 401 при запросе времен. Похоже, сессия или cookie протухли.")

    if response_status == 403:
        raise AuthRequiredError("Сервис вернул 403 при запросе времен. CSRF token / cookie больше невалидны.")

    if response_status == 429:
        raise DatesFetchError("Сервис вернул 429 Too Many Requests при запросе времен.")

    if response_status != 304 and not (200 <= response_status < 300):
        raise DatesFetchError(
            f"Неожиданный статус ответа времен для {date}: {response_status}. Тело: {response_text[:500]}"
        )

    if not response_text.strip():
        raise DatesFetchError(
            f"Сервис вернул успешный статус для времен {date}, но тело ответа пустое даже после retry без If-None-Match."
        )

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise DatesFetchError(f"Ответ времен для {date} не является JSON: {e}") from e

    if _is_auth_required_payload(payload):
        raise AuthRequiredError("US Visa API времен вернул: You need to sign in or sign up before continuing.")

    if not isinstance(payload, dict):
        raise DatesFetchError(
            f"Ожидался объект времен для {date}, но получено: {type(payload).__name__}"
        )

    times = _extract_times(payload)
    return times


async def notify_no_dates() -> bool:
    message = (
        "❌ Ошибка мониторинга US Visa: запрос выполнен, но не вернул ни одной даты.\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"Проверьте вручную: {APPOINTMENT_URL}"
    )
    return await record_event(message)


async def notify_new_dates(
    new_dates: list[str],
    current_appointments: dict[str, set[str]] | None = None,
) -> bool:
    current_appointments = current_appointments or {}
    lines = []
    for date in sorted({str(date) for date in new_dates if str(date)}):
        times = sorted(str(time) for time in current_appointments.get(date, set()) if str(time))
        if times:
            lines.append(f"• {date}: {', '.join(times)}")
        else:
            lines.append(f"• {date}: нет доступного времени для автозаписи")
    dates_block = "\n".join(lines)
    message = (
        "🎯 В Астане появилась новая дата для записи:\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"{dates_block}\n"
        f"{APPOINTMENT_URL}"
    )

    return await record_event(message)


def _hourly_monitor_summary(
    state: MonitorState,
    now: datetime,
    window_minutes: int = 60,
) -> tuple[int, int, int]:
    cutoff = now - timedelta(minutes=max(1, window_minutes))
    successful_checks = 0
    error_count = 0
    found_dates: set[str] = set()

    for event in state.monitor_events:
        occurred_at = _parse_utc_datetime(event.get("at"))
        if occurred_at is None or occurred_at < cutoff:
            continue

        if event.get("status") == "success":
            successful_checks += 1
            ignored_dates = {str(date) for date in event.get("ignored_dates") or []}
            for date in event.get("found_dates") or []:
                date_value = str(date)
                if date_value and date_value not in ignored_dates:
                    found_dates.add(date_value)
        elif event.get("status") == "error":
            error_count += 1

    return successful_checks, error_count, len(found_dates)


def _healthcheck_period_label(interval_minutes: int) -> str:
    minutes = max(1, interval_minutes)
    if minutes == 60:
        return "последний час"
    if minutes % 60 == 0:
        hours = minutes // 60
        if hours == 1:
            return "последний час"
        if 2 <= hours <= 4:
            return f"последние {hours} часа"
        return f"последние {hours} часов"
    return f"последние {minutes} минут"


async def notify_healthcheck(state: MonitorState) -> bool:
    now = _utc_now()
    period_label = _healthcheck_period_label(HEALTHCHECK_INTERVAL_MINUTES)
    successful_checks, error_count, found_dates_count = _hourly_monitor_summary(
        state,
        now,
        HEALTHCHECK_INTERVAL_MINUTES,
    )
    summary_line = f"За {period_label} новых слотов и ошибок не было."
    if error_count > 0 and found_dates_count > 0:
        summary_line = f"За {period_label} были и ошибки, и найденные даты вне игнор-листа."
    elif error_count > 0:
        summary_line = f"За {period_label} были ошибки мониторинга."
    elif found_dates_count > 0:
        summary_line = f"За {period_label} монитор находил даты вне игнор-листа."

    message = (
        "✅ US Visa monitor работает штатно.\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"{summary_line}\n"
        f"Успешных проверок за {period_label}: {successful_checks}\n"
        f"Ошибок за {period_label}: {error_count}\n"
        f"Найдено дат вне игнор-листа за {period_label}: {found_dates_count}\n"
    )
    return await record_event(message)


def _should_send_healthcheck(last_healthcheck_at: datetime | None, now: datetime) -> bool:
    threshold = timedelta(minutes=HEALTHCHECK_INTERVAL_MINUTES)
    if HEALTHCHECK_INTERVAL_MINUTES <= 0:
        return False
    if last_healthcheck_at is not None and now - last_healthcheck_at < threshold:
        return False
    return True


async def notify_error(error: Exception) -> bool:
    message = (
        "❌ Ошибка мониторинга US Visa:\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"{type(error).__name__}: {error}\n"
    )
    return await record_event(message)


def _format_local_time(value: datetime) -> str:
    return value.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")


def _error_event(error: Exception, occurred_at: datetime) -> dict[str, str]:
    return {
        "at": occurred_at.astimezone(timezone.utc).isoformat(),
        "type": type(error).__name__,
        "message": str(error),
    }


def _error_category(error: Exception) -> str:
    if isinstance(error, AuthRequiredError):
        return "token"

    error_text = f"{type(error).__name__} {error}".lower()
    if any(marker in error_text for marker in ("timeout", "timed out", "playwright", "browser")):
        return "browser_timeout"

    return "other"


def _append_monitor_event(state: MonitorState, event: dict[str, Any]) -> None:
    state.monitor_events.append(event)
    state.monitor_events = state.monitor_events[-1000:]
    if USVISA_MONITOR_DB_SYNC:
        monitor_store.append_monitor_run(event)


def _success_monitor_event(
    occurred_at: datetime,
    found_dates: list[str],
    active_appointments: dict[str, set[str]],
    ignored_dates: list[str],
) -> dict[str, Any]:
    active_dates = sorted(date for date, times in active_appointments.items() if times)
    dates_without_times = sorted(
        date for date, times in active_appointments.items() if not times
    )
    return {
        "at": occurred_at.astimezone(timezone.utc).isoformat(),
        "status": "success",
        "found_dates": sorted(found_dates),
        "active_dates": active_dates,
        "dates_without_times": dates_without_times,
        "ignored_dates": sorted(ignored_dates),
        "active_appointments": {
            date: sorted(times)
            for date, times in sorted(active_appointments.items())
            if times
        },
    }


def _error_monitor_event(error: Exception, occurred_at: datetime) -> dict[str, Any]:
    return {
        "at": occurred_at.astimezone(timezone.utc).isoformat(),
        "status": "error",
        "error_category": _error_category(error),
        "error_type": type(error).__name__,
        "message": str(error),
    }


def _should_send_error_digest(state: MonitorState, now: datetime) -> bool:
    if ERROR_DIGEST_INTERVAL_MINUTES <= 0 or not state.error_events:
        return False
    threshold_minutes = max(MIN_ERROR_DIGEST_INTERVAL_MINUTES, ERROR_DIGEST_INTERVAL_MINUTES)
    state.error_events = _pending_error_events(
        state.error_events,
        now,
        threshold_minutes,
        state.last_error_digest_at,
    )
    if not state.error_events:
        return False
    if state.last_error_digest_at is None:
        return True
    return now - state.last_error_digest_at >= timedelta(minutes=threshold_minutes)


def _pending_error_events(
    error_events: list[dict[str, str]],
    now: datetime,
    window_minutes: int,
    last_error_digest_at: datetime | None = None,
) -> list[dict[str, str]]:
    cutoff = now - timedelta(minutes=max(1, window_minutes))
    if last_error_digest_at is not None and last_error_digest_at > cutoff:
        cutoff = last_error_digest_at
    fresh_events: list[dict[str, str]] = []
    for event in error_events:
        occurred_at = _parse_utc_datetime(event.get("at"))
        if occurred_at is not None and occurred_at > cutoff:
            fresh_events.append(event)
    return fresh_events


def _compact_error_message(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message)).strip()
    title_match = re.search(r"<title>\s*(.*?)\s*</title>", text, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        prefix = text.split("Тело:", 1)[0].strip()
        title = re.sub(r"\s+", " ", title_match.group(1)).strip()
        if prefix:
            return f"{prefix} Ответ: {title}"
        return f"HTML-ответ: {title}"
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 220:
        return text[:217].rstrip() + "..."
    return text


def _error_digest_lines(error_events: list[dict[str, str]]) -> list[str]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for event in error_events:
        error_type = str(event.get("type") or "Error")
        message = _compact_error_message(str(event.get("message") or ""))
        key = (error_type, message)
        occurred_at = _parse_utc_datetime(event.get("at"))
        group = groups.setdefault(
            key,
            {
                "count": 0,
                "first_at": occurred_at,
                "last_at": occurred_at,
                "type": error_type,
                "message": message,
            },
        )
        group["count"] += 1
        if occurred_at is not None:
            first_at = group["first_at"]
            last_at = group["last_at"]
            group["first_at"] = occurred_at if first_at is None else min(first_at, occurred_at)
            group["last_at"] = occurred_at if last_at is None else max(last_at, occurred_at)

    sorted_groups = sorted(
        groups.values(),
        key=lambda group: group["last_at"] or datetime.min.replace(tzinfo=timezone.utc),
    )
    lines: list[str] = []
    for group in sorted_groups[-8:]:
        count_suffix = f" ×{group['count']}" if group["count"] > 1 else ""
        first_at = group["first_at"]
        last_at = group["last_at"]
        if first_at is not None and last_at is not None and first_at != last_at:
            first_text = first_at.astimezone(MOSCOW_TZ).strftime("%H:%M:%S")
            last_text = _format_local_time(last_at)
            time_text = f"{first_text}–{last_text}"
        elif last_at is not None:
            time_text = _format_local_time(last_at)
        else:
            time_text = ""
        lines.append(f"• {time_text}: {group['type']}{count_suffix}: {group['message']}")

    hidden_count = len(sorted_groups) - len(lines)
    if hidden_count > 0:
        hidden_events = sum(group["count"] for group in sorted_groups[:hidden_count])
        lines.append(f"• ... еще групп ошибок: {hidden_count}, событий: {hidden_events}")
    return lines


async def notify_error_digest(error_events: list[dict[str, str]]) -> bool:
    period_label = _healthcheck_period_label(
        max(MIN_ERROR_DIGEST_INTERVAL_MINUTES, ERROR_DIGEST_INTERVAL_MINUTES)
    )
    message = (
        f"❌ Ошибки мониторинга US Visa за {period_label}:\n"
        f"Аккаунт: {ACCOUNT_LABEL}\n"
        f"Всего событий: {len(error_events)}.\n"
        + "\n".join(_error_digest_lines(error_events))
    )
    return await record_event(message)


async def _record_error_and_maybe_notify(state: MonitorState, error: Exception) -> None:
    now = _utc_now()
    state.last_error_at = now
    error_event = _error_event(error, now)
    state.error_events.append(error_event)
    if USVISA_MONITOR_DB_SYNC:
        monitor_store.append_error_event(error_event)
    _append_monitor_event(state, _error_monitor_event(error, now))
    if not _should_send_error_digest(state, now):
        return

    notification_sent = await notify_error_digest(state.error_events)
    if not notification_sent:
        print("⚠️ Не удалось отправить сводку ошибок в локальный журнал")
        return

    state.last_error_digest_at = now
    state.error_events = []


def _refresh_session_with_browser() -> None:
    from usvisa_browser_login_and_run import login_and_update_state

    print("USVisa session expired; starting browser login refresh")
    login_and_update_state()
    # раскомментить, когда нужно будет обновлять куки через браузер
    global USVISA_PREFER_BOOTSTRAP_SESSION
    USVISA_PREFER_BOOTSTRAP_SESSION = False


async def job(allow_browser_refresh: bool = True, force_run: bool = False) -> None:
    validate_astana_urls({
        "USVISA_APPOINTMENT_URL": APPOINTMENT_URL,
        "USVISA_DATES_API_URL": DATES_API_URL,
        "USVISA_TIMES_API_URL": TIMES_API_URL,
    }, required=True)
    timing = _new_timing_context()
    _timing_log("monitor_start", timing=timing, force_run=force_run)
    print("⏰ Старт проверки доступных дат US Visa")

    state = _read_state()
    now = _utc_now()

    if not force_run and not _is_check_due(state, now):
        print(f"Проверка еще не нужна. Следующая проверка: {state.next_check_at.isoformat()}")
        return

    try:
        runtime_state = deepcopy(state)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            dates_started = time.perf_counter()
            _timing_log("dates_request_start", timing=timing)
            current_dates = await fetch_dates_with_cookie_candidates(session, runtime_state)
            _timing_log(
                "dates_request_end",
                timing=timing,
                action_started=dates_started,
                date_count=len(current_dates),
            )

            if not current_dates:
                print("ℹ️ Свободных дат сейчас нет")

            current_dates_set = set(current_dates)
            dates_to_check = sorted(current_dates_set - IGNORED_DATES)
            ignored_current_dates = sorted(current_dates_set & IGNORED_DATES)
            current_appointments: dict[str, set[str]] = {}
            new_appointments: dict[str, list[str]] = {}
            previous_appointments = runtime_state.appointments
            previous_found_dates = {str(date) for date in runtime_state.latest_found_dates}
            new_dates = sorted(date for date in dates_to_check if date not in previous_found_dates)

            print(f"Текущие даты: {current_dates}")
            print(f"Даты из списка исключений: {ignored_current_dates} \n")
            print(f"Даты для проверки времен: {dates_to_check}")

            matched_priority_dates = [
                date for date in PRIORITY_DATES if date in dates_to_check
            ]
            earliest_allowed_date = _booking_allowed_from_date(PREPARATION_DAYS)
            priority_dates_to_check = _priority_date_attempt_order(
                [date for date in matched_priority_dates if date >= earliest_allowed_date]
            )
            booked_priority_slot = None
            if priority_dates_to_check:
                _timing_log(
                    "priority_dates_matched",
                    timing=timing,
                    dates=",".join(priority_dates_to_check),
                )
                refresh_started = time.perf_counter()
                _timing_log("session_refresh_start", timing=timing)
                await refresh_session_from_appointment_page(session, runtime_state)
                _timing_log(
                    "session_refresh_end",
                    timing=timing,
                    action_started=refresh_started,
                )
            for date in priority_dates_to_check:
                times_started = time.perf_counter()
                _timing_log("priority_times_request_start", timing=timing, date=date)
                times = set(await fetch_available_times(session, runtime_state, date))
                _timing_log(
                    "priority_times_request_end",
                    timing=timing,
                    action_started=times_started,
                    date=date,
                    time_count=len(times),
                )
                current_appointments[date] = times

                seen_times = previous_appointments.get(date, set())
                new_times = sorted(times - seen_times)
                if new_times:
                    new_appointments[date] = new_times

                if not times:
                    continue

                timing["slot_discovered"] = time.perf_counter()
                preferred_time = _preferred_time_for_date(sorted(times))
                _timing_log(
                    "priority_slot_discovered",
                    timing=timing,
                    date=date,
                    time_value=preferred_time,
                )
                booked_priority_slot = await _try_autobook_priority_dates(
                    session,
                    runtime_state,
                    [date],
                    {date: times},
                    timing,
                )
                if booked_priority_slot:
                    break

            if booked_priority_slot:
                print(
                    "✅ Автозапись выполнена: "
                    f"{booked_priority_slot['date']} {booked_priority_slot['time']} "
                    f"(method={booked_priority_slot.get('method', 'unknown')})"
                )
                runtime_state.appointments = current_appointments
                runtime_state.latest_found_dates = current_dates
                runtime_state.latest_ignored_dates = ignored_current_dates
                runtime_state.latest_ignored_appointments = {}
                runtime_state.latest_dates_to_check = dates_to_check
                runtime_state.latest_current_times = current_appointments
                runtime_state.latest_new_times = {
                    date: set(times) for date, times in new_appointments.items()
                }
                _append_monitor_event(
                    runtime_state,
                    _success_monitor_event(
                        _utc_now(),
                        current_dates,
                        current_appointments,
                        ignored_current_dates,
                    ),
                )
                _write_state(runtime_state)
                _timing_log(
                    "booking_confirmed",
                    timing=timing,
                    date=booked_priority_slot["date"],
                    time_value=booked_priority_slot["time"],
                    method=booked_priority_slot.get("method", "unknown"),
                )
                print("🛑 Мониторинг остановлен после успешной записи")
                return

            for date in dates_to_check:
                if date in current_appointments:
                    continue
                times_started = time.perf_counter()
                _timing_log("times_request_start", timing=timing, date=date)
                times = set(await fetch_available_times(session, runtime_state, date))
                _timing_log(
                    "times_request_end",
                    timing=timing,
                    action_started=times_started,
                    date=date,
                    time_count=len(times),
                )
                current_appointments[date] = times

                seen_times = previous_appointments.get(date, set())
                new_times = sorted(times - seen_times)
                if new_times:
                    new_appointments[date] = new_times

            print(
                "Текущие времена: "
                f"{ {date: sorted(times) for date, times in current_appointments.items()} }"
            )
            print(f"Новые даты вне игнор-листа: {new_dates}")
            print(f"Новые времена: {new_appointments}")

            if new_dates:
                notification_sent = await notify_new_dates(new_dates, current_appointments)
                if not notification_sent:
                    print("⚠️ Не удалось отправить уведомление о новых датах в локальный журнал")
                else:
                    runtime_state.last_slot_notification_at = _utc_now()
            else:
                now = _utc_now()
                if _should_send_healthcheck(runtime_state.last_healthcheck_at, now):
                    notification_sent = await notify_healthcheck(runtime_state)
                    if not notification_sent:
                        print("⚠️ Не удалось отправить heartbeat-уведомление в локальный журнал")
                    else:
                        runtime_state.last_healthcheck_at = now

            # State хранит снимок последнего успешного запуска.
            # Если дата или время исчезли и потом вернулись, они снова считаются новыми.
            runtime_state.appointments = current_appointments
            runtime_state.latest_found_dates = current_dates
            runtime_state.latest_ignored_dates = ignored_current_dates
            runtime_state.latest_ignored_appointments = {}
            runtime_state.latest_dates_to_check = dates_to_check
            runtime_state.latest_current_times = current_appointments
            runtime_state.latest_new_times = {
                date: set(times) for date, times in new_appointments.items()
            }
            _append_monitor_event(
                runtime_state,
                _success_monitor_event(
                    _utc_now(),
                    current_dates,
                    current_appointments,
                    ignored_current_dates,
                ),
            )
            _schedule_next_check(runtime_state)
            _write_state(runtime_state)
            _timing_log("monitor_complete", timing=timing, booked=False)

    except AuthRequiredError as e:
        if allow_browser_refresh:
            try:
                print(f"Требуется обновление авторизации: {e}")
                token_error_state = _read_state()
                _append_monitor_event(token_error_state, _error_monitor_event(e, _utc_now()))
                _write_state(token_error_state)
                await asyncio.to_thread(_refresh_session_with_browser)
                await job(allow_browser_refresh=False, force_run=True)
                return
            except Exception as refresh_error:
                e = refresh_error

        state = _read_state()
        await _record_error_and_maybe_notify(state, e)
        _schedule_next_check(state)
        _write_state(state)
        print(f"❌ Ошибка мониторинга: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        raise

    except Exception as e:
        state = _read_state()
        await _record_error_and_maybe_notify(state, e)
        _schedule_next_check(state)
        _write_state(state)
        print(f"❌ Ошибка мониторинга: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        raise


if __name__ == "__main__":
    try:
        asyncio.run(job())
    except Exception:
        sys.exit(1)
