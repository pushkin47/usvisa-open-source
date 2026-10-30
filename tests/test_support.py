import importlib
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


PROJECT_ENV_KEYS = [
    "USVISA_ADMIN_EMAIL",
    "USVISA_ADMIN_PASSWORD",
    "USVISA_ALLOW_LEGACY_APP_SETTINGS_CREDENTIALS",
    "USVISA_APPOINTMENT_URL",
    "USVISA_BOOTSTRAP_COOKIE",
    "USVISA_BOOTSTRAP_CSRF_TOKEN",
    "USVISA_BOOTSTRAP_IF_NONE_MATCH",
    "USVISA_BOOTSTRAP_SESSION_ID",
    "USVISA_BOOKING_ATTEMPT_LOG_FILE",
    "USVISA_BOOKING_ATTEMPT_LOG_MAX_BYTES",
    "USVISA_BOOKING_ATTEMPT_LOG_RETAIN_LINES",
    "USVISA_BOOKING_RESPONSE_BODY_MAX_CHARS",
    "USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS",
    "USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS",
    "USVISA_BROWSER_HEADLESS",
    "USVISA_BROWSER_KEEP_OPEN_SECONDS",
    "USVISA_BROWSER_LOGIN_MAX_ATTEMPTS",
    "USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS",
    "USVISA_BROWSER_PROFILE_DIR",
    "USVISA_BROWSER_TRACE_USVISA_NETWORK",
    "USVISA_CREDENTIAL_SOURCE",
    "USVISA_DATES_API_URL",
    "USVISA_ERROR_DIGEST_INTERVAL_MINUTES",
    "USVISA_ERROR_STATE_FILE",
    "USVISA_FACILITY_ID",
    "USVISA_HEALTHCHECK_INTERVAL_MINUTES",
    "USVISA_IGNORED_DATES",
    "USVISA_LOGIN_EMAIL",
    "USVISA_LOGIN_PASSWORD",
    "USVISA_MAX_CHECK_INTERVAL_SECONDS",
    "USVISA_MIN_CHECK_INTERVAL_SECONDS",
    "USVISA_MONITOR_AUTOSTART",
    "USVISA_MONITOR_DB_SYNC",
    "USVISA_MONITOR_LOOP_SECONDS",
    "USVISA_PREFER_BOOTSTRAP_SESSION",
    "USVISA_PREPARATION_DAYS",
    "USVISA_PRIORITY_DATES",
    "USVISA_PROFILE_CONFIG_ACTIVE",
    "USVISA_REQUEST_TIMEOUT_SECONDS",
    "USVISA_SSL_VERIFY",
    "USVISA_STATE_FILE",
    "USVISA_TIMES_API_URL",
    "USVISA_USER_AGENT",
    "USVISA_VPN_ENABLED",
    "USVISA_VPN_EXCLUDE_FILTER",
    "USVISA_VPN_HWID",
    "USVISA_VPN_LOCATION_FILTER",
    "USVISA_VPN_MAX_OUTBOUNDS",
    "USVISA_VPN_PROXY_COUNT",
    "USVISA_VPN_PROXY_URL",
    "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY",
    "USVISA_VPN_SUBSCRIPTION_URL",
    "USVISA_WEB_DB_FILE",
    "USVISA_WEB_PORT",
    "USVISA_WEB_PREVENT_SLEEP",
]


def base_env(tmpdir: str) -> dict[str, str]:
    temp_root = Path(tmpdir)
    return {
        "USVISA_ADMIN_EMAIL": "admin@example.com",
        "USVISA_ADMIN_PASSWORD": "admin-password",
        "USVISA_ALLOW_LEGACY_APP_SETTINGS_CREDENTIALS": "1",
        "USVISA_APPOINTMENT_URL": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment",
        "USVISA_BOOTSTRAP_COOKIE": "session=bootstrap",
        "USVISA_BOOTSTRAP_CSRF_TOKEN": "csrf-bootstrap",
        "USVISA_BOOTSTRAP_IF_NONE_MATCH": 'W/"bootstrap"',
        "USVISA_BOOTSTRAP_SESSION_ID": "session-bootstrap",
        "USVISA_BOOKING_ATTEMPT_LOG_FILE": str(temp_root / "booking-attempts.jsonl"),
        "USVISA_BOOKING_ATTEMPT_LOG_MAX_BYTES": str(512 * 1024),
        "USVISA_BOOKING_ATTEMPT_LOG_RETAIN_LINES": "200",
        "USVISA_BOOKING_RESPONSE_BODY_MAX_CHARS": str(32 * 1024),
        "USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS": "0",
        "USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS": "0",
        "USVISA_BROWSER_HEADLESS": "true",
        "USVISA_BROWSER_KEEP_OPEN_SECONDS": "0",
        "USVISA_BROWSER_LOGIN_MAX_ATTEMPTS": "1",
        "USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS": "0",
        "USVISA_BROWSER_PROFILE_DIR": str(temp_root / "playwright-profile"),
        "USVISA_BROWSER_TRACE_USVISA_NETWORK": "false",
        "USVISA_DATES_API_URL": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/days/134.json?appointments[expedite]=false",
        "USVISA_ERROR_DIGEST_INTERVAL_MINUTES": "180",
        "USVISA_ERROR_STATE_FILE": str(temp_root / "errors.json"),
        "USVISA_FACILITY_ID": "134",
        "USVISA_HEALTHCHECK_INTERVAL_MINUTES": "60",
        "USVISA_IGNORED_DATES": "",
        "USVISA_LOGIN_EMAIL": "embassy@example.com",
        "USVISA_LOGIN_PASSWORD": "embassy-password",
        "USVISA_MAX_CHECK_INTERVAL_SECONDS": "60",
        "USVISA_MIN_CHECK_INTERVAL_SECONDS": "60",
        "USVISA_MONITOR_AUTOSTART": "false",
        "USVISA_MONITOR_DB_SYNC": "true",
        "USVISA_MONITOR_LOOP_SECONDS": "30",
        "USVISA_PREFER_BOOTSTRAP_SESSION": "false",
        "USVISA_PREPARATION_DAYS": "2",
        "USVISA_PRIORITY_DATES": "",
        "USVISA_PROFILE_CONFIG_ACTIVE": "false",
        "USVISA_REQUEST_TIMEOUT_SECONDS": "5",
        "USVISA_SSL_VERIFY": "false",
        "USVISA_STATE_FILE": str(temp_root / "state.json"),
        "USVISA_TIMES_API_URL": "https://ais.usvisa-info.com/ru-kz/niv/schedule/123456/appointment/times/134.json",
        "USVISA_USER_AGENT": "TestAgent/1.0",
        "USVISA_VPN_ENABLED": "false",
        "USVISA_VPN_EXCLUDE_FILTER": "Россия",
        "USVISA_VPN_HWID": "test-hwid",
        "USVISA_VPN_LOCATION_FILTER": "",
        "USVISA_VPN_MAX_OUTBOUNDS": "4",
        "USVISA_VPN_PROXY_COUNT": "2",
        "USVISA_VPN_PROXY_URL": "http://127.0.0.1:2080",
        "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY": "false",
        "USVISA_VPN_SUBSCRIPTION_URL": "https://vpn.example.test/subscription",
        "USVISA_WEB_DB_FILE": str(temp_root / "web.sqlite3"),
        "USVISA_WEB_PREVENT_SLEEP": "false",
    }


@contextmanager
def patched_env(overrides: dict[str, str] | None = None):
    overrides = overrides or {}
    previous = {key: os.environ.get(key) for key in PROJECT_ENV_KEYS}
    for key in PROJECT_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key in PROJECT_ENV_KEYS:
            os.environ.pop(key, None)
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value


def reload_modules(*module_names: str):
    modules = []
    for name in module_names:
        module = importlib.import_module(name)
        modules.append(importlib.reload(module))
    return modules


class TempProjectCaseMixin:
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self._tmpdir.name)
        self.env = base_env(self._tmpdir.name)
        self._env_cm = patched_env(self.env)
        self._env_cm.__enter__()
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self._env_cm.__exit__(None, None, None)
        self._tmpdir.cleanup()
