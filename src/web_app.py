import asyncio
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shlex
import signal
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from aiohttp import web
from dotenv import dotenv_values

from config_store import load_config_to_env, read_db_settings, validate_astana_urls
import monitor_store
from local_events import record_event


load_config_to_env(override=True)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
STATIC_DIR = SRC_DIR / "web_static"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"
LEGACY_ENV_EXAMPLE_FILE = PROJECT_ROOT / ".example"
DEFAULT_STATE_FILE = PROJECT_ROOT / "state/usvisa_seen_dates.json"
DEFAULT_ERROR_STATE_FILE = PROJECT_ROOT / "state/usvisa_errors.json"
DEFAULT_DB_FILE = PROJECT_ROOT / "state/usvisa_web.sqlite3"
USER_STATE_ROOT = PROJECT_ROOT / "state/users"
DEBUG_ARTIFACTS_DIR = PROJECT_ROOT / "state/browser-debug"
VPN_REFRESH_REQUEST_FILE = PROJECT_ROOT / "state/vpn_refresh.request"
MAX_LOG_LINES = 500
IGNORED_DATES_ENV_KEY = "USVISA_IGNORED_DATES"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SESSION_COOKIE = "usvisa_session_v2"
PASSWORD_ITERATIONS = 260_000
MONITOR_ERROR_WINDOW_SECONDS = 10 * 60
MONITOR_ERROR_PAUSE_STEPS_SECONDS = (2 * 60, 5 * 60, 10 * 60)
MONITOR_ERROR_RECOVERY_SECONDS = 30 * 60
MONITOR_ERROR_CYCLE_NOTIFICATION_INTERVAL_MINUTES = 180
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ASTANA_FACILITY_ID = "134"
VPN_SETTING_KEYS = {
    "USVISA_VPN_SUBSCRIPTION_URL",
    "USVISA_VPN_HWID",
    "USVISA_VPN_LOCATION_FILTER",
    "USVISA_VPN_EXCLUDE_FILTER",
    "USVISA_VPN_MAX_OUTBOUNDS",
    "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY",
}
USER_STATUS_UNPAID = "не оплачен"
USER_STATUS_SEARCH_ENABLED = "поиск включен"
USER_STATUS_SEARCH_DISABLED = "поиск выключен"
USER_STATUS_BOOKED = "записан на слот"
USER_STATUS_BLOCKED = "заблокирован"
USER_STATUS_DELETED = "удален"
USER_STATUS_VALUES = {
    USER_STATUS_UNPAID,
    USER_STATUS_SEARCH_ENABLED,
    USER_STATUS_SEARCH_DISABLED,
    USER_STATUS_BOOKED,
    USER_STATUS_BLOCKED,
    USER_STATUS_DELETED,
}
USER_STATUS_SQL_CHECK = (
    f"'{USER_STATUS_UNPAID}', "
    f"'{USER_STATUS_SEARCH_ENABLED}', "
    f"'{USER_STATUS_SEARCH_DISABLED}', "
    f"'{USER_STATUS_BOOKED}', "
    f"'{USER_STATUS_BLOCKED}', "
    f"'{USER_STATUS_DELETED}'"
)
USER_BLOCKED_LOGIN_ERRORS = {
    USER_STATUS_DELETED: "Пользователь удалён",
    USER_STATUS_BLOCKED: "Пользователь заблокирован",
}
PUBLIC_ROUTES = {
    "/",
    "/api/auth/me",
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/demo",
}
ADMIN_API_ROUTES = {
    "/api/settings",
    "/api/users/status",
    "/api/run-check",
    "/api/run-login",
    "/api/stop-process",
    "/api/monitor/start",
    "/api/monitor/stop",
}
DEMO_FORBIDDEN_ROUTES = set()

BOOLEAN_KEYS = {
    "USVISA_BROWSER_HEADLESS",
    "USVISA_BROWSER_TRACE_USVISA_NETWORK",
    "USVISA_SSL_VERIFY",
    "USVISA_VPN_ENABLED",
    "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY",
    "USVISA_PREFER_BOOTSTRAP_SESSION",
    "USVISA_MONITOR_AUTOSTART",
    "USVISA_WEB_PREVENT_SLEEP",
}

SECRET_KEYS = {
    "USVISA_BOOTSTRAP_COOKIE",
    "USVISA_BOOTSTRAP_CSRF_TOKEN",
    "USVISA_BOOTSTRAP_SESSION_ID",
    "USVISA_BOOTSTRAP_IF_NONE_MATCH",
    "USVISA_LOGIN_PASSWORD",
    "USVISA_VPN_SUBSCRIPTION_URL",
}

SETTING_GROUPS = [
    {
        "title": "Мониторинг",
        "description": "Основные параметры, которые обычно меняют чаще всего",
        "keys": [
            "USVISA_MONITOR_LOOP_SECONDS",
            "USVISA_MONITOR_AUTOSTART",
            "USVISA_WEB_PREVENT_SLEEP",
            "USVISA_REQUEST_TIMEOUT_SECONDS",
            "USVISA_MIN_CHECK_INTERVAL_SECONDS",
            "USVISA_MAX_CHECK_INTERVAL_SECONDS",
            "USVISA_HEALTHCHECK_INTERVAL_MINUTES",
            "USVISA_ERROR_DIGEST_INTERVAL_MINUTES",
            "USVISA_SSL_VERIFY",
            "USVISA_VPN_ENABLED",
            "USVISA_VPN_PROXY_URL",
            "USVISA_VPN_SUBSCRIPTION_URL",
            "USVISA_VPN_HWID",
            "USVISA_VPN_LOCATION_FILTER",
            "USVISA_VPN_EXCLUDE_FILTER",
            "USVISA_VPN_MAX_OUTBOUNDS",
            "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY",
        ],
    },
    {
        "title": "Браузерный логин",
        "description": "Настройки Playwright для ручного логина и обновления сессии",
        "keys": [
            "USVISA_BROWSER_HEADLESS",
            "USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS",
            "USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS",
            "USVISA_BROWSER_LOGIN_MAX_ATTEMPTS",
            "USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS",
            "USVISA_BROWSER_KEEP_OPEN_SECONDS",
            "USVISA_BROWSER_TRACE_USVISA_NETWORK",
        ],
    },
    {
        "title": "Авторизация",
        "description": "Редко меняемые данные аккаунта и bootstrap-сессии US Visa",
        "keys": [
            "USVISA_LOGIN_EMAIL",
            "USVISA_LOGIN_PASSWORD",
            "USVISA_BROWSER_PROFILE_DIR",
            "USVISA_BOOTSTRAP_COOKIE",
            "USVISA_BOOTSTRAP_CSRF_TOKEN",
            "USVISA_BOOTSTRAP_SESSION_ID",
            "USVISA_BOOTSTRAP_IF_NONE_MATCH",
            "USVISA_PREFER_BOOTSTRAP_SESSION",
        ],
    },
    {
        "title": "Технические параметры US Visa",
        "description": "Редко меняемые служебные параметры монитора",
        "keys": [
            "USVISA_STATE_FILE",
        ],
    },
    {
        "title": "Веб-панель",
        "description": "Параметры самого локального интерфейса",
        "keys": [
            "USVISA_WEB_PORT",
        ],
    },
]

SETTING_LABELS = {
    "USVISA_BOOTSTRAP_COOKIE": "Стартовая cookie сессии",
    "USVISA_BOOTSTRAP_CSRF_TOKEN": "Стартовый CSRF-токен",
    "USVISA_BOOTSTRAP_SESSION_ID": "Стартовый Session-Id",
    "USVISA_BOOTSTRAP_IF_NONE_MATCH": "Стартовый If-None-Match",
    "USVISA_FACILITY_ID": "ID консульства",
    "USVISA_LOGIN_EMAIL": "Email",
    "USVISA_LOGIN_PASSWORD": "Пароль",
    "USVISA_BROWSER_HEADLESS": "Headless-режим браузера",
    "USVISA_BROWSER_PROFILE_DIR": "Папка профиля браузера",
    "USVISA_BROWSER_KEEP_OPEN_SECONDS": "Держать окно открытым после логина, сек.",
    "USVISA_APPOINTMENT_URL": "Страница записи",
    "USVISA_DATES_API_URL": "API дат",
    "USVISA_TIMES_API_URL": "API времени",
    "USVISA_STATE_FILE": "Файл состояния",
    "USVISA_REQUEST_TIMEOUT_SECONDS": "Сколько ждать ответ от US Visa, сек.",
    "USVISA_HEALTHCHECK_INTERVAL_MINUTES": "Интервал записи рабочего статуса в локальный журнал, мин.",
    "USVISA_ERROR_DIGEST_INTERVAL_MINUTES": "Интервал записи сводки ошибок в локальный журнал, мин. Минимум 180.",
    "USVISA_MIN_CHECK_INTERVAL_SECONDS": "Минимальная пауза между проверками, сек.",
    "USVISA_MAX_CHECK_INTERVAL_SECONDS": "Максимальная пауза между проверками, сек.",
    "USVISA_SSL_VERIFY": "Проверять SSL-сертификат US Visa",
    "USVISA_VPN_ENABLED": "Использовать VPN для US Visa",
    "USVISA_VPN_PROXY_URL": "Адрес локального VPN proxy",
    "USVISA_VPN_SUBSCRIPTION_URL": "Ссылка подписки VPN",
    "USVISA_VPN_HWID": "HWID VPS для VPN",
    "USVISA_VPN_LOCATION_FILTER": "Фильтр стран VPN",
    "USVISA_VPN_EXCLUDE_FILTER": "Исключить страны VPN",
    "USVISA_VPN_MAX_OUTBOUNDS": "Максимум VPN-нод",
    "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY": "Проверять SSL подписки VPN",
    "USVISA_PREFER_BOOTSTRAP_SESSION": "Брать стартовую сессию из .env",
    "USVISA_IGNORED_DATES": "Игнорируемые даты",
    "USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS": "Минимальная пауза между действиями браузера, сек.",
    "USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS": "Максимальная пауза между действиями браузера, сек.",
    "USVISA_BROWSER_LOGIN_MAX_ATTEMPTS": "Сколько раз пробовать логин",
    "USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS": "Пауза перед новой попыткой логина, сек.",
    "USVISA_BROWSER_TRACE_USVISA_NETWORK": "Показывать подробные сетевые логи US Visa",
    "USVISA_WEB_PORT": "Порт веб-панели",
    "USVISA_MONITOR_LOOP_SECONDS": "Как часто запускать фоновую проверку, сек.",
    "USVISA_MONITOR_AUTOSTART": "Стартовать мониторинг вместе с веб-панелью",
    "USVISA_WEB_PREVENT_SLEEP": "Не давать Mac заснуть во время мониторинга",
}

SETTING_HELP = {
    "USVISA_MONITOR_LOOP_SECONDS": "Как часто веб-панель запускает новый фоновый прогон монитора.",
    "USVISA_MONITOR_AUTOSTART": "Если включено, мониторинг начнется сам сразу после старта веб-сервера.",
    "USVISA_WEB_PREVENT_SLEEP": "Полезно для MacBook: пока мониторинг работает, система не уйдет в сон.",
    "USVISA_REQUEST_TIMEOUT_SECONDS": "Если US Visa отвечает дольше этого времени, запрос считается неудачным.",
    "USVISA_MIN_CHECK_INTERVAL_SECONDS": "Самая короткая пауза между двумя проверками. Слишком маленькое значение может повысить риск блокировки.",
    "USVISA_MAX_CHECK_INTERVAL_SECONDS": "Самая длинная пауза между двумя проверками. Вместе с минимальной паузой задает разброс по времени.",
    "USVISA_HEALTHCHECK_INTERVAL_MINUTES": "Как часто записывать в локальный журнал рабочий статус монитора.",
    "USVISA_ERROR_DIGEST_INTERVAL_MINUTES": "Как часто собирать ошибки в одну сводку в локальном журнале. Значения меньше 180 минут будут ограничены до 3 часов.",
    "USVISA_SSL_VERIFY": "Обычно лучше держать включенным. Выключают только если есть проблемы именно с SSL-проверкой.",
    "USVISA_BROWSER_HEADLESS": "Выключите для видимого окна браузера; включите для сервера без рабочего стола.",
    "USVISA_VPN_ENABLED": "Если включено, HTTP-запросы к US Visa и браузерный логин идут через локальный proxy.",
    "USVISA_VPN_PROXY_URL": "Адрес вашего proxy-клиента, если он используется.",
    "USVISA_VPN_SUBSCRIPTION_URL": "Секретная ссылка подписки, из которой VPS обновляет список доступных VPN-нод.",
    "USVISA_VPN_HWID": "Идентификатор устройства, если он требуется вашим провайдером.",
    "USVISA_VPN_LOCATION_FILTER": "Необязательный список стран или фрагментов названий через запятую. Если пусто, используются все подходящие ноды.",
    "USVISA_VPN_EXCLUDE_FILTER": "Список стран или фрагментов названий через запятую, которые не попадут в авто-выбор.",
    "USVISA_VPN_MAX_OUTBOUNDS": "Сколько нод максимум включать в sing-box auto-select config.",
    "USVISA_VPN_SUBSCRIPTION_SSL_VERIFY": "Обычно лучше держать включенным. Можно выключить, если сервер не может проверить сертификат подписки.",
    "USVISA_BROWSER_ACTION_DELAY_MIN_SECONDS": "Минимальная задержка между кликами и переходами в браузере во время логина.",
    "USVISA_BROWSER_ACTION_DELAY_MAX_SECONDS": "Максимальная задержка между действиями в браузере. Можно использовать, чтобы логин шел чуть спокойнее.",
    "USVISA_BROWSER_LOGIN_MAX_ATTEMPTS": "Сколько раз подряд пробовать браузерный логин, если сайт ответил ошибкой или завис.",
    "USVISA_BROWSER_LOGIN_RETRY_DELAY_SECONDS": "Сколько ждать перед повторной попыткой браузерного логина.",
    "USVISA_BROWSER_KEEP_OPEN_SECONDS": "Сколько секунд оставить окно браузера открытым после успешного логина для ручной проверки.",
    "USVISA_BROWSER_TRACE_USVISA_NETWORK": "Включает очень подробные сетевые логи Playwright. Нужен только для глубокой отладки.",
    "USVISA_LOGIN_EMAIL": "Логин аккаунта US Visa для браузерного входа.",
    "USVISA_LOGIN_PASSWORD": "Пароль этого аккаунта.",
    "USVISA_BROWSER_PROFILE_DIR": "Папка, где браузер хранит cookies и локальную сессию между запусками.",
    "USVISA_BOOTSTRAP_COOKIE": "Резервная стартовая cookie для первых запросов, если рабочая сессия еще не загружена.",
    "USVISA_BOOTSTRAP_CSRF_TOKEN": "Резервный CSRF-токен для стартовых запросов.",
    "USVISA_BOOTSTRAP_SESSION_ID": "Резервный Session-Id, если сайт требует его в запросах.",
    "USVISA_BOOTSTRAP_IF_NONE_MATCH": "Технический заголовок для кэширующих запросов. Обычно менять его не нужно.",
    "USVISA_PREFER_BOOTSTRAP_SESSION": "Если включено, при старте брать сессию из .env, а не последнюю сохраненную рабочую сессию.",
    "USVISA_FACILITY_ID": "Идентификатор консульства или визового центра, по которому идет поиск дат.",
    "USVISA_APPOINTMENT_URL": "Основная страница записи для твоего schedule id.",
    "USVISA_DATES_API_URL": "Технический адрес, откуда монитор читает список доступных дат.",
    "USVISA_TIMES_API_URL": "Технический адрес, откуда монитор читает доступное время для выбранной даты.",
    "USVISA_STATE_FILE": "JSON-файл, где хранятся текущее состояние монитора, история событий и рабочая сессия.",
    "USVISA_WEB_PORT": "Порт, на котором открывается локальная веб-панель.",
}

RUN_LOCK = asyncio.Lock()
PROCESS_STATE: dict[str, Any] = {
    "kind": None,
    "running": False,
    "returncode": None,
    "started_at": None,
    "finished_at": None,
    "logs": [],
}
CURRENT_SUBPROCESS: asyncio.subprocess.Process | None = None
MONITOR_TASK: asyncio.Task[None] | None = None
CAFFEINATE_PROCESS: asyncio.subprocess.Process | None = None
MONITOR_SUBPROCESSES: set[asyncio.subprocess.Process] = set()
MONITOR_STATE: dict[str, Any] = {
    "active": False,
    "auto_paused": False,
    "paused_until": None,
    "interval_seconds": 30,
    "started_at": None,
    "stopped_at": None,
    "last_iteration_started_at": None,
    "last_iteration_finished_at": None,
    "iterations": 0,
    "last_returncode": None,
    "preventing_sleep": False,
    "error_recovery": False,
    "error_cycle_count": 0,
    "last_error_cycle_notification_at": None,
    "handled_bookings": [],
}


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now_dt().isoformat()


def _db_path() -> Path:
    configured = os.getenv("USVISA_WEB_DB_FILE", str(DEFAULT_DB_FILE))
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _db() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _file_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (LEGACY_ENV_EXAMPLE_FILE, ENV_EXAMPLE_FILE, ENV_FILE):
        if path.exists():
            values.update({key: str(value or "") for key, value in dotenv_values(path).items()})
    return values


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(email: Any) -> str:
    return str(email or "").strip().lower()


def _default_admin_email(values: dict[str, str] | None = None) -> str:
    return _normalize_email(os.getenv("USVISA_ADMIN_EMAIL", ""))


def _default_admin_password(values: dict[str, str] | None = None) -> str:
    password = os.getenv("USVISA_ADMIN_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError("Set USVISA_ADMIN_PASSWORD to at least 12 characters in .env")
    return password


def _init_db() -> None:
    file_values = _file_env_values()
    monitor_store.ensure_tables()
    with _db() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                is_secret INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user', 'demo')),
                priority INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'не оплачен' CHECK (status IN ({USER_STATUS_SQL_CHECK})),
                terms_accepted INTEGER NOT NULL DEFAULT 0,
                terms_accepted_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                embassy_login TEXT NOT NULL DEFAULT '',
                embassy_password TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT 'astana',
                facility_id TEXT NOT NULL DEFAULT '134',
                ignored_dates TEXT NOT NULL DEFAULT '',
                ignored_periods TEXT NOT NULL DEFAULT '[]',
                priority_dates TEXT NOT NULL DEFAULT '',
                priority_periods TEXT NOT NULL DEFAULT '[]',
                prep_days INTEGER NOT NULL DEFAULT 3,
                vpn_proxy_url TEXT NOT NULL DEFAULT '',
                appointment_url TEXT NOT NULL DEFAULT '',
                dates_api_url TEXT NOT NULL DEFAULT '',
                times_api_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cities (
                code TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 0,
                facility_id TEXT NOT NULL DEFAULT '',
                appointment_url TEXT NOT NULL DEFAULT '',
                dates_api_url TEXT NOT NULL DEFAULT '',
                times_api_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        _migrate_file_env_to_db(conn, file_values)
        _migrate_demo_role(conn)
        _migrate_users_priority_column(conn)
        _migrate_users_status_column(conn)
        _migrate_users_terms_columns(conn)
        _migrate_user_profiles_priority_dates(conn)
        _migrate_user_profiles_period_columns(conn)
        _migrate_user_profiles_prep_days(conn)
        _migrate_user_profiles_vpn_proxy_url(conn)
        _migrate_user_profiles_schedule_urls(conn)
        _seed_cities(conn, file_values)
        values = _public_env_values()
        admin_email = _default_admin_email(values)
        admin_row = conn.execute("SELECT id, role FROM users WHERE email = ?", (admin_email,)).fetchone()
        admin_exists = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if admin_exists is None and (not admin_email or "@" not in admin_email):
            raise RuntimeError("Set USVISA_ADMIN_EMAIL in .env before first startup")
        if admin_row is not None:
            updates = ["role = 'admin'", "updated_at = ?"]
            params: list[Any] = [_utc_now_iso()]
            if os.getenv("USVISA_ADMIN_PASSWORD"):
                updates.append("password_hash = ?")
                params.append(_hash_password(_default_admin_password(values)))
            params.append(admin_row["id"])
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            _ensure_profile(conn, int(admin_row["id"]), values)
        elif admin_email and admin_exists is None:
            now = _utc_now_iso()
            cursor = conn.execute(
                """
                INSERT INTO users (email, password_hash, role, status, terms_accepted, terms_accepted_at, created_at, updated_at)
                VALUES (?, ?, 'admin', ?, ?, ?, ?, ?)
                """,
                (admin_email, _hash_password(_default_admin_password(values)), USER_STATUS_SEARCH_ENABLED, 1, now, now, now),
            )
            _ensure_profile(conn, int(cursor.lastrowid), values)
        _ensure_demo_user(conn)
    values = _public_env_values()
    monitor_store.sync_legacy_state_if_needed(
        _state_file_from_env(values),
        _error_state_file_from_env(values),
    )


def _migrate_file_env_to_db(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    now = _utc_now_iso()
    for key in sorted(values):
        conn.execute(
            """
            INSERT OR IGNORE INTO app_settings (key, value, is_secret, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, values.get(key, ""), 1 if _is_secret_key(key) else 0, now, now),
        )


def _is_secret_key(key: str) -> bool:
    return key in SECRET_KEYS or any(part in key for part in ("PASSWORD", "TOKEN", "COOKIE", "CSRF", "CHAT_ID"))


def _migrate_demo_role(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    sql = row["sql"] if row else ""
    if "'demo'" in sql:
        return
    conn.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'user', 'demo')),
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'не оплачен' CHECK (status IN ({USER_STATUS_SQL_CHECK})),
            terms_accepted INTEGER NOT NULL DEFAULT 0,
            terms_accepted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO users_new (id, email, password_hash, role, priority, status, terms_accepted, terms_accepted_at, created_at, updated_at)
        SELECT id, email, password_hash, role, 0, 'не оплачен', 1, created_at, created_at, updated_at FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
        PRAGMA foreign_keys = ON;
        """
    )


def _migrate_users_priority_column(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)")
    }
    if "priority" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")


def _migrate_users_status_column(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)")
    }
    added = False
    if "status" not in columns:
        conn.execute(f"ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT '{USER_STATUS_UNPAID}'")
        added = True

    if added:
        now = _utc_now_iso()
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE role = 'admin'",
            (USER_STATUS_SEARCH_ENABLED, now),
        )
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE role != 'admin'",
            (USER_STATUS_SEARCH_DISABLED, now),
        )

    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    sql = row["sql"] if row else ""
    normalized_sql = " ".join(str(sql).upper().split())
    if "CHECK (STATUS IN (" in normalized_sql:
        return

    conn.executescript(
        f"""
        PRAGMA foreign_keys = OFF;
        CREATE TABLE users_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'user', 'demo')),
            priority INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'не оплачен' CHECK (status IN ({USER_STATUS_SQL_CHECK})),
            terms_accepted INTEGER NOT NULL DEFAULT 0,
            terms_accepted_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO users_new (id, email, password_hash, role, priority, status, terms_accepted, terms_accepted_at, created_at, updated_at)
        SELECT
            id,
            email,
            password_hash,
            role,
            COALESCE(priority, 0),
            CASE
                WHEN status IN ({USER_STATUS_SQL_CHECK}) THEN status
                WHEN role = 'admin' THEN '{USER_STATUS_SEARCH_ENABLED}'
                ELSE '{USER_STATUS_SEARCH_DISABLED}'
            END,
            1,
            created_at,
            created_at,
            updated_at
        FROM users;
        DROP TABLE users;
        ALTER TABLE users_new RENAME TO users;
        PRAGMA foreign_keys = ON;
        """
    )


def _migrate_users_terms_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(users)")
    }
    if "terms_accepted" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN terms_accepted INTEGER NOT NULL DEFAULT 1")
    if "terms_accepted_at" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN terms_accepted_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE users SET terms_accepted = 1 WHERE terms_accepted IS NULL OR terms_accepted = 0"
    )
    conn.execute(
        "UPDATE users SET terms_accepted_at = created_at WHERE terms_accepted_at = ''"
    )


def _migrate_user_profiles_prep_days(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    if "prep_days" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN prep_days INTEGER NOT NULL DEFAULT 3")
    conn.execute("UPDATE user_profiles SET prep_days = 1 WHERE prep_days IS NULL OR prep_days < 1")


def _migrate_user_profiles_vpn_proxy_url(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    if "vpn_proxy_url" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN vpn_proxy_url TEXT NOT NULL DEFAULT ''")


def _migrate_user_profiles_schedule_urls(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    for column in ("appointment_url", "dates_api_url", "times_api_url"):
        if column not in columns:
            conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")


def _migrate_user_profiles_priority_dates(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    if "priority_dates" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN priority_dates TEXT NOT NULL DEFAULT ''")


def _migrate_user_profiles_period_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_profiles)")}
    if "ignored_periods" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN ignored_periods TEXT NOT NULL DEFAULT '[]'")
    if "priority_periods" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN priority_periods TEXT NOT NULL DEFAULT '[]'")
    rows = conn.execute(
        """
        SELECT user_id, ignored_dates, ignored_periods, priority_dates, priority_periods
        FROM user_profiles
        """
    ).fetchall()
    for row in rows:
        ignored_dates = [
            date.strip()
            for date in str(row["ignored_dates"] or "").split(",")
            if DATE_RE.match(date.strip())
        ]
        priority_dates = [
            date.strip()
            for date in str(row["priority_dates"] or "").split(",")
            if DATE_RE.match(date.strip())
        ]
        ignored_periods = _parse_periods_json(row["ignored_periods"])
        priority_periods = _parse_periods_json(row["priority_periods"])
        restored_ignored_periods = ignored_periods or _dates_to_periods(ignored_dates)
        restored_priority_periods = priority_periods or _dates_to_periods(priority_dates)
        if restored_ignored_periods != ignored_periods or restored_priority_periods != priority_periods:
            conn.execute(
                """
                UPDATE user_profiles
                SET ignored_periods = ?, priority_periods = ?
                WHERE user_id = ?
                """,
                (
                    _serialize_periods_json(restored_ignored_periods),
                    _serialize_periods_json(restored_priority_periods),
                    row["user_id"],
                ),
            )


def _seed_cities(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    now = _utc_now_iso()
    astana_appointment = values.get("USVISA_APPOINTMENT_URL", "")
    astana_dates = values.get("USVISA_DATES_API_URL", "")
    astana_times = values.get("USVISA_TIMES_API_URL", "")
    seeds = [
        {
            "code": "astana",
            "label": "Астана",
            "active": 1,
            "priority": 100,
            "facility_id": ASTANA_FACILITY_ID,
            "appointment_url": astana_appointment,
            "dates_api_url": astana_dates,
            "times_api_url": astana_times,
        },
    ]
    for city in seeds:
        conn.execute(
            """
            INSERT INTO cities (
                code, label, active, priority, facility_id, appointment_url, dates_api_url, times_api_url,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO NOTHING
            """,
            (
                city["code"],
                city["label"],
                city["active"],
                city["priority"],
                city["facility_id"],
                city["appointment_url"],
                city["dates_api_url"],
                city["times_api_url"],
                now,
                now,
            ),
        )


def _ensure_demo_user(conn: sqlite3.Connection) -> sqlite3.Row:
    email = "demo@local"
    now = _utc_now_iso()
    row = conn.execute("SELECT id, email, role, priority, status FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO users (email, password_hash, role, status, terms_accepted, terms_accepted_at, created_at, updated_at)
            VALUES (?, ?, 'demo', ?, ?, ?, ?, ?)
            """,
            (email, _hash_password(secrets.token_urlsafe(24)), USER_STATUS_SEARCH_DISABLED, 1, now, now, now),
        )
    else:
        conn.execute(
            "UPDATE users SET role = 'demo', status = ?, updated_at = ? WHERE id = ?",
            (USER_STATUS_SEARCH_DISABLED, now, row["id"]),
        )
    row = conn.execute("SELECT id, email, role, priority, status FROM users WHERE email = ?", (email,)).fetchone()
    assert row is not None
    conn.execute(
        """
        INSERT INTO user_profiles (
            user_id, embassy_login, embassy_password, city, facility_id, ignored_dates, ignored_periods, priority_dates, priority_periods, prep_days, vpn_proxy_url, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            embassy_login = excluded.embassy_login,
            embassy_password = excluded.embassy_password,
            city = excluded.city,
            facility_id = excluded.facility_id,
            ignored_dates = excluded.ignored_dates,
            ignored_periods = excluded.ignored_periods,
            priority_dates = excluded.priority_dates,
            priority_periods = excluded.priority_periods,
            prep_days = excluded.prep_days,
            vpn_proxy_url = excluded.vpn_proxy_url,
            updated_at = excluded.updated_at
        """,
        (
            row["id"],
            "demo.embassy@example.com",
            "demo-password",
            "astana",
            ASTANA_FACILITY_ID,
            "2026-05-16,2026-05-23,2026-06-12,2026-06-13",
            json.dumps(
                [
                    {"start": "2026-05-16", "end": "2026-05-16"},
                    {"start": "2026-05-23", "end": "2026-05-23"},
                    {"start": "2026-06-12", "end": "2026-06-13"},
                ],
                ensure_ascii=False,
            ),
            "2026-05-20,2026-05-28,2026-06-18",
            json.dumps(
                [
                    {"start": "2026-05-20", "end": "2026-05-20"},
                    {"start": "2026-05-28", "end": "2026-05-28"},
                    {"start": "2026-06-18", "end": "2026-06-18"},
                ],
                ensure_ascii=False,
            ),
            3,
            "",
            now,
            now,
        ),
    )
    return row


def _ensure_profile(
    conn: sqlite3.Connection,
    user_id: int,
    values: dict[str, str] | None = None,
) -> None:
    exists = conn.execute("SELECT user_id FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    if exists:
        return
    values = _public_env_values() if values is None else values
    now = _utc_now_iso()
    facility_id = ASTANA_FACILITY_ID
    city_options = _city_options(values, include_inactive=True)
    city = "astana"
    conn.execute(
        """
        INSERT INTO user_profiles (
            user_id, embassy_login, embassy_password, city, facility_id, ignored_dates, ignored_periods, priority_dates, priority_periods, prep_days, vpn_proxy_url, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            values.get("USVISA_LOGIN_EMAIL", ""),
            values.get("USVISA_LOGIN_PASSWORD", ""),
            city,
            facility_id,
            values.get(IGNORED_DATES_ENV_KEY, ""),
            "[]",
            "",
            "[]",
            3,
            values.get("USVISA_VPN_PROXY_URL", "") if _parse_bool(values.get("USVISA_VPN_ENABLED", "false")) else "",
            now,
            now,
        ),
    )


def _parse_periods_json(value: Any) -> list[dict[str, str]]:
    try:
        payload = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    periods: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        if DATE_RE.match(start) and DATE_RE.match(end) and start <= end:
            periods.append({"start": start, "end": end})
    return periods


def _serialize_periods_json(periods: list[dict[str, str]]) -> str:
    normalized: list[dict[str, str]] = []
    for item in periods:
        if not isinstance(item, dict):
            continue
        start = str(item.get("start") or "").strip()
        end = str(item.get("end") or "").strip()
        if DATE_RE.match(start) and DATE_RE.match(end) and start <= end:
            normalized.append({"start": start, "end": end})
    return json.dumps(normalized, ensure_ascii=False)


def _dates_to_periods(dates: list[str]) -> list[dict[str, str]]:
    normalized = sorted({date for date in dates if DATE_RE.match(date)})
    if not normalized:
        return []

    periods: list[dict[str, str]] = []
    start = normalized[0]
    end = normalized[0]

    for date in normalized[1:]:
        previous = datetime.fromisoformat(end).date()
        current = datetime.fromisoformat(date).date()
        if current == previous + timedelta(days=1):
            end = date
        else:
            periods.append({"start": start, "end": end})
            start = date
            end = date

    periods.append({"start": start, "end": end})
    return periods


def _safe_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _profile_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    city_options = _city_options(include_inactive=True)
    city = row["city"] if row["city"] in city_options else "astana"
    city_option = city_options[city]
    ignored_dates = [
        date.strip()
        for date in str(row["ignored_dates"] or "").split(",")
        if DATE_RE.match(date.strip())
    ]
    priority_dates = [
        date.strip()
        for date in str(row["priority_dates"] or "").split(",")
        if DATE_RE.match(date.strip())
    ] if "priority_dates" in keys else []
    ignored_periods = _parse_periods_json(row["ignored_periods"] if "ignored_periods" in keys else "[]")
    priority_periods = _parse_periods_json(row["priority_periods"] if "priority_periods" in keys else "[]")
    if not ignored_periods and ignored_dates:
        ignored_periods = _dates_to_periods(ignored_dates)
    if not priority_periods and priority_dates:
        priority_periods = _dates_to_periods(priority_dates)
    return {
        "embassyLogin": row["embassy_login"] or "",
        "embassyPassword": row["embassy_password"] or "",
        "city": city,
        "cityLabel": city_option["label"] + (" (неактивна)" if not city_option.get("active") else ""),
        "facilityId": row["facility_id"] or city_option["facilityId"],
        "vpnProxyUrl": str(row["vpn_proxy_url"] or "") if "vpn_proxy_url" in keys else "",
        "appointmentUrl": str(row["appointment_url"] or "") if "appointment_url" in keys else "",
        "datesApiUrl": str(row["dates_api_url"] or "") if "dates_api_url" in keys else "",
        "timesApiUrl": str(row["times_api_url"] or "") if "times_api_url" in keys else "",
        "ignoredDates": sorted(set(ignored_dates)),
        "ignoredPeriods": ignored_periods,
        "priorityDates": sorted(set(priority_dates)),
        "priorityPeriods": priority_periods,
        "preparationDays": max(1, _safe_non_negative_int(row["prep_days"] if "prep_days" in keys else 1, default=1)),
    }


def _user_payload(user: sqlite3.Row, profile: sqlite3.Row | None = None) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "role": user["role"],
        "priority": int(user["priority"] or 0),
        "status": user["status"] if "status" in user.keys() else USER_STATUS_UNPAID,
        "profile": _profile_from_row(profile),
    }


def _current_user(request: web.Request) -> dict[str, Any] | None:
    cached = request.get("current_user")
    if cached is not None:
        return cached
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            """
            SELECT users.id, users.email, users.role, profiles.embassy_login, profiles.embassy_password,
                   users.priority, users.status, profiles.city, profiles.facility_id, profiles.ignored_dates,
                   profiles.ignored_periods, profiles.priority_dates, profiles.priority_periods,
                   profiles.prep_days, profiles.vpn_proxy_url
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            LEFT JOIN user_profiles profiles ON profiles.user_id = users.id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (_hash_token(token), _utc_now_iso()),
        ).fetchone()
        if row is None:
            return None
        if row["status"] in {USER_STATUS_BLOCKED, USER_STATUS_DELETED}:
            return None
        user = {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"],
            "priority": int(row["priority"] or 0),
            "status": row["status"] or USER_STATUS_UNPAID,
            "profile": _profile_from_row(row),
        }
        request["current_user"] = user
        return user


def _require_user(request: web.Request) -> dict[str, Any]:
    user = _current_user(request)
    if user is None:
        raise web.HTTPUnauthorized(text="auth required")
    return user


def _require_admin(request: web.Request) -> dict[str, Any]:
    user = _require_user(request)
    if user["role"] != "admin":
        raise web.HTTPForbidden(text="admin required")
    return user


def _require_run_observer(request: web.Request) -> dict[str, Any]:
    user = _require_user(request)
    if user["role"] not in {"admin", "demo"}:
        raise web.HTTPForbidden(text="run observer required")
    return user


def _session_response(user: dict[str, Any], token: str) -> web.Response:
    response = web.json_response({"ok": True, "user": user, "cityOptions": _city_options(include_inactive=True)})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="Lax",
        max_age=60 * 60 * 24 * 30,
        secure=False,
    )
    return response


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _public_env_values() -> dict[str, str]:
    values = _file_env_values()
    values.update(read_db_settings())
    values.setdefault("USVISA_MONITOR_LOOP_SECONDS", "30")
    values.setdefault("USVISA_MONITOR_AUTOSTART", "false")
    values.setdefault("USVISA_WEB_PREVENT_SLEEP", "true")
    values.setdefault("USVISA_BROWSER_HEADLESS", "true")
    values.setdefault("USVISA_VPN_ENABLED", "false")
    values.setdefault("USVISA_VPN_PROXY_URL", "http://127.0.0.1:2080")
    values.setdefault("USVISA_VPN_HWID", "")
    values.setdefault("USVISA_VPN_LOCATION_FILTER", "")
    values.setdefault("USVISA_VPN_EXCLUDE_FILTER", "Россия")
    values.setdefault("USVISA_VPN_MAX_OUTBOUNDS", "12")
    values.setdefault("USVISA_VPN_SUBSCRIPTION_SSL_VERIFY", "true")
    return values


def _all_city_rows() -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute(
            """
            SELECT code, label, active, priority, facility_id, appointment_url, dates_api_url, times_api_url,
                   created_at, updated_at
            FROM cities
            WHERE code = 'astana' AND facility_id = '134'
            ORDER BY priority DESC, label ASC
            """
        ).fetchall()


def _serialize_city_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "code": row["code"],
        "label": row["label"],
        "active": bool(row["active"]),
        "priority": int(row["priority"] or 0),
        "facilityId": row["facility_id"] or "",
        "appointmentUrl": row["appointment_url"] or "",
        "datesApiUrl": row["dates_api_url"] or "",
        "timesApiUrl": row["times_api_url"] or "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _city_options(values: dict[str, str] | None = None, include_inactive: bool = False) -> dict[str, dict[str, Any]]:
    rows = _all_city_rows()
    if not rows:
        values = values or _public_env_values()
        options: dict[str, dict[str, Any]] = {
            "astana": {"label": "Астана", "facilityId": ASTANA_FACILITY_ID, "active": True},
        }
    else:
        options = {
            row["code"]: {
                "label": row["label"],
                "facilityId": row["facility_id"] or "",
                "active": bool(row["active"]),
                "priority": int(row["priority"] or 0),
                "appointmentUrl": row["appointment_url"] or "",
                "datesApiUrl": row["dates_api_url"] or "",
                "timesApiUrl": row["times_api_url"] or "",
            }
            for row in rows
        }
    if include_inactive:
        return options
    return {key: option for key, option in options.items() if option.get("active")}


def _state_file_from_env(values: dict[str, str]) -> Path:
    configured = values.get("USVISA_STATE_FILE") or str(DEFAULT_STATE_FILE)
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _error_state_file_from_env(values: dict[str, str]) -> Path:
    configured = values.get("USVISA_ERROR_STATE_FILE") or str(DEFAULT_ERROR_STATE_FILE)
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _ignored_dates_from_env(values: dict[str, str]) -> list[str]:
    return sorted(
        {
            date.strip()
            for date in values.get(IGNORED_DATES_ENV_KEY, "").split(",")
            if DATE_RE.match(date.strip())
        }
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {"_read_error": str(exc)}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _seconds_until(value: Any) -> int | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0, round((parsed - datetime.now(timezone.utc)).total_seconds()))


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


def _recent_error_count(error_state: Any, window_seconds: int) -> int:
    if not isinstance(error_state, dict):
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - window_seconds
    count = 0
    for event in _parse_error_events(error_state.get("error_events")):
        occurred_at = _parse_dt(event.get("at"))
        if occurred_at is not None and occurred_at.timestamp() >= cutoff:
            count += 1
    return count


def _recent_consecutive_error_count(raw_state: Any) -> int:
    if not isinstance(raw_state, dict):
        return 0

    raw_monitor_events = raw_state.get("monitor_events") if isinstance(raw_state.get("monitor_events"), list) else []
    db_monitor_events = monitor_store.read_monitor_runs(limit=100)
    monitor_events = _merge_monitor_events(raw_monitor_events, db_monitor_events)
    count = 0
    for event in reversed(monitor_events):
        status = str(event.get("status") or "").strip()
        if status == "error":
            count += 1
            continue
        if status == "success":
            break
    return count


def _events_in_range(events: list[dict[str, Any]], hours: int | None) -> list[dict[str, Any]]:
    if not hours:
        return list(events)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    filtered: list[dict[str, Any]] = []
    for event in events:
        occurred_at = _parse_dt(event.get("at"))
        if occurred_at is not None and occurred_at >= cutoff:
            filtered.append(event)
    return filtered


def _merge_monitor_events(*event_lists: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_events in event_lists:
        if not isinstance(raw_events, list):
            continue
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            event_key = json.dumps(event, sort_keys=True, ensure_ascii=False)
            if event_key in seen:
                continue
            seen.add(event_key)
            merged.append(event)

    merged.sort(key=lambda item: (_parse_dt(item.get("at")) or datetime.min.replace(tzinfo=timezone.utc), json.dumps(item, sort_keys=True, ensure_ascii=False)))
    return merged[-5000:]


def _metrics_summary(state: dict[str, Any], hours: int | None) -> dict[str, int]:
    events = _events_in_range(state.get("monitorEvents") or [], hours)
    ignored = {str(date) for date in state.get("ignoredDates") or []}
    found_dates: set[str] = set()
    for event in events:
        for date in event.get("found_dates") or []:
            date_value = str(date).strip()
            if date_value and date_value not in ignored:
                found_dates.add(date_value)

    return {
        "successfulChecks": sum(1 for event in events if event.get("status") == "success"),
        "tokenErrors": sum(1 for event in events if event.get("error_category") == "token"),
        "browserErrors": sum(1 for event in events if event.get("error_category") == "browser_timeout"),
        "foundFreeDays": len(found_dates),
        "actualFreeDays": len(state.get("currentActiveAppointments") or []),
    }


def _period_found_dates(state: dict[str, Any], hours: int | None) -> list[str]:
    events = _events_in_range(state.get("monitorEvents") or [], hours)
    ignored = {str(date) for date in state.get("ignoredDates") or []}
    found_dates: set[str] = set()
    for event in events:
        for date in event.get("found_dates") or []:
            date_value = str(date).strip()
            if date_value and date_value not in ignored:
                found_dates.add(date_value)
    return sorted(found_dates)


def _summarize_state(
    raw_state: Any,
    state_file: Path,
    ignored_dates: list[str] | None = None,
    error_state: Any | None = None,
    use_db_snapshot: bool = True,
) -> dict[str, Any]:
    ignored_dates = ignored_dates or []
    error_state = error_state if isinstance(error_state, dict) else {}
    db_snapshot = monitor_store.read_snapshot() if use_db_snapshot else {}
    if isinstance(db_snapshot, dict) and db_snapshot:
        raw_state = db_snapshot
    db_error_events = monitor_store.read_error_events(limit=20) if use_db_snapshot else []
    db_monitor_events = monitor_store.read_monitor_runs(limit=1000) if use_db_snapshot else []
    if not isinstance(raw_state, dict):
        return {
            "exists": state_file.exists(),
            "path": str(state_file),
            "appointments": [],
            "ignoredDates": ignored_dates,
            "totalDates": 0,
            "totalTimes": 0,
            "errorEvents": db_error_events or _parse_error_events(error_state.get("error_events"))[-20:],
            "monitorEvents": db_monitor_events,
            "lastErrorAt": error_state.get("last_error_at"),
            "lastErrorDigestAt": error_state.get("last_error_digest_at"),
            "recentErrorCount10m": _recent_error_count(error_state, MONITOR_ERROR_WINDOW_SECONDS),
        }

    appointments_raw = raw_state.get("appointments") if isinstance(raw_state.get("appointments"), dict) else {}
    appointments = [
        {"date": str(date), "times": sorted(str(time) for time in times if str(time))}
        for date, times in sorted(appointments_raw.items())
        if isinstance(times, list)
    ]
    session = raw_state.get("session") if isinstance(raw_state.get("session"), dict) else {}
    cookie_candidates = session.get("cookie_candidates") if isinstance(session.get("cookie_candidates"), list) else []
    error_events = db_error_events or (
        error_state.get("error_events")
        if isinstance(error_state.get("error_events"), list)
        else raw_state.get("error_events")
        if isinstance(raw_state.get("error_events"), list)
        else []
    )
    raw_monitor_events = raw_state.get("monitor_events") if isinstance(raw_state.get("monitor_events"), list) else []
    monitor_events = _merge_monitor_events(raw_monitor_events, db_monitor_events)
    latest_found_dates = raw_state.get("latest_found_dates") if isinstance(raw_state.get("latest_found_dates"), list) else []
    latest_dates_payload = raw_state.get("latest_dates_payload") if isinstance(raw_state.get("latest_dates_payload"), list) else []
    latest_dates_to_check = raw_state.get("latest_dates_to_check") if isinstance(raw_state.get("latest_dates_to_check"), list) else []
    latest_current_times = raw_state.get("latest_current_times") if isinstance(raw_state.get("latest_current_times"), dict) else {}
    latest_new_times = raw_state.get("latest_new_times") if isinstance(raw_state.get("latest_new_times"), dict) else {}
    latest_booking = raw_state.get("latest_booking") if isinstance(raw_state.get("latest_booking"), dict) else {}
    latest_found_dates_normalized = sorted({str(date) for date in latest_found_dates if str(date)})
    effective_ignored_dates = sorted(date for date in latest_found_dates_normalized if date in set(ignored_dates))
    current_ignored_days = [{"date": date, "times": []} for date in effective_ignored_dates]
    current_active_appointments = []
    for date in latest_found_dates_normalized:
        if date in set(ignored_dates):
            continue
        times = latest_current_times.get(date)
        if not isinstance(times, list):
            times = appointments_raw.get(date) if isinstance(appointments_raw.get(date), list) else []
        current_active_appointments.append(
            {
                "date": date,
                "times": sorted(str(time) for time in times if str(time)),
            }
        )

    return {
        "exists": state_file.exists(),
        "path": str(state_file),
        "appointments": appointments,
        "currentActiveAppointments": current_active_appointments,
        "ignoredDates": ignored_dates,
        "latestFoundDates": latest_found_dates_normalized,
        "latestIgnoredDates": effective_ignored_dates,
        "latestDatesPayload": [
            {
                "date": str(item.get("date", "")),
                "business_day": bool(item.get("business_day")),
            }
            for item in latest_dates_payload
            if isinstance(item, dict) and item.get("date")
        ],
        "latestDatesToCheck": [str(date) for date in latest_dates_to_check],
        "latestCurrentTimes": {
            str(date): sorted(str(time) for time in times if str(time))
            for date, times in latest_current_times.items()
            if isinstance(times, list)
        },
        "latestNewTimes": {
            str(date): sorted(str(time) for time in times if str(time))
            for date, times in latest_new_times.items()
            if isinstance(times, list)
        },
        "latestBooking": latest_booking,
        "currentIgnoredAppointments": current_ignored_days,
        "totalDates": len(appointments),
        "totalTimes": sum(len(item["times"]) for item in appointments),
        "lastHealthcheckAt": raw_state.get("last_healthcheck_at"),
        "lastSlotNotificationAt": raw_state.get("last_slot_notification_at"),
        "lastErrorAt": error_state.get("last_error_at", raw_state.get("last_error_at")),
        "lastErrorDigestAt": error_state.get("last_error_digest_at", raw_state.get("last_error_digest_at")),
        "recentErrorCount10m": _recent_error_count(error_state, MONITOR_ERROR_WINDOW_SECONDS),
        "nextCheckAt": raw_state.get("next_check_at"),
        "secondsUntilNextCheck": _seconds_until(raw_state.get("next_check_at")),
        "errorEvents": error_events[-20:] if isinstance(error_events, list) else [],
        "monitorEvents": monitor_events[-1000:] if isinstance(monitor_events, list) else [],
        "session": {
            "hasCookie": bool(session.get("cookie")),
            "cookieCandidates": len(cookie_candidates),
            "hasCsrfToken": bool(session.get("csrf_token")),
            "hasSessionId": bool(session.get("session_id")),
            "hasIfNoneMatch": bool(session.get("if_none_match")),
            "browserHeaderCount": len(session.get("browser_headers") or {}),
        },
        "readError": raw_state.get("_read_error"),
    }


def _setting_schema(values: dict[str, str]) -> list[dict[str, Any]]:
    groups = []
    for group in SETTING_GROUPS:
        fields = []
        for key in group["keys"]:
            value = values.get(key, "")
            fields.append(
                {
                    "key": key,
                    "label": SETTING_LABELS.get(key, key),
                    "help": SETTING_HELP.get(key, ""),
                    "value": value,
                    "type": "checkbox" if key in BOOLEAN_KEYS else "password" if key in SECRET_KEYS else "text",
                    "multiline": key.endswith("_COOKIE") or key.endswith("_URL") or key == "USVISA_IGNORED_DATES",
                }
            )
        groups.append({**group, "fields": fields})
    return groups


def _quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if "\n" in value or value[0].isspace() or value[-1].isspace() or "#" in value:
        return shlex.quote(value)
    return value


def _write_settings(values: dict[str, str]) -> None:
    now = _utc_now_iso()
    with _db() as conn:
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, is_secret, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = excluded.updated_at
                """,
                (key, value, 1 if _is_secret_key(key) else 0, now, now),
            )


def _write_env(values: dict[str, str]) -> None:
    existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pending = dict(values)
    output: list[str] = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in pending:
            output.append(f"{key}={_quote_env_value(pending.pop(key))}")
        else:
            output.append(line)

    if pending and output and output[-1].strip():
        output.append("")
    for key in _ordered_setting_keys():
        if key in pending:
            output.append(f"{key}={_quote_env_value(pending.pop(key))}")
    for key in sorted(pending):
        output.append(f"{key}={_quote_env_value(pending[key])}")

    ENV_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")


def _env_for_subprocess() -> dict[str, str]:
    env = os.environ.copy()
    env.update(_public_env_values())
    env["PYTHONPATH"] = str(SRC_DIR)
    return env


def _prepare_browser_env(env: dict[str, str] | None = None) -> dict[str, str]:
    prepared = dict(env or _env_for_subprocess())
    is_headless = _parse_bool(str(prepared.get("USVISA_BROWSER_HEADLESS", "true")))
    if platform.system() == "Linux" and not is_headless and not prepared.get("DISPLAY"):
        prepared["DISPLAY"] = ":99"
    return prepared


def _display_command(command: list[str], env: dict[str, str]) -> str:
    prefixes: list[str] = []
    if env.get("DISPLAY"):
        prefixes.append(f"DISPLAY={env['DISPLAY']}")
    if "USVISA_BROWSER_HEADLESS" in env:
        prefixes.append(f"USVISA_BROWSER_HEADLESS={env['USVISA_BROWSER_HEADLESS']}")
    return " ".join(prefixes + [shlex.join(command)])


def _rewrite_facility_url(value: str, facility_id: str) -> str:
    if not value or not facility_id:
        return value
    return re.sub(r"(/appointment/(?:days|times)/)\d+(\.json)", rf"\g<1>{facility_id}\2", value)


def _user_runtime_dir(user_id: int) -> Path:
    return USER_STATE_ROOT / str(int(user_id))


def _user_state_file(user_id: int) -> Path:
    return _user_runtime_dir(user_id) / "usvisa_seen_dates.json"


def _user_error_state_file(user_id: int) -> Path:
    return _user_runtime_dir(user_id) / "usvisa_errors.json"


def _user_browser_profile_dir(user_id: int) -> Path:
    return _user_runtime_dir(user_id) / "playwright-profile"


def _profile_env_values(user_id: int) -> dict[str, str]:
    with _db() as conn:
        row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    profile = _profile_from_row(row)
    if not profile:
        return {}

    values = _public_env_values()
    city_options = _city_options(include_inactive=True)
    city_config = city_options.get(str(profile.get("city") or "astana"), {})
    facility_id = ASTANA_FACILITY_ID
    ignored_dates = ",".join(profile.get("ignoredDates") or [])
    overrides = {
        "USVISA_LOGIN_EMAIL": str(profile.get("embassyLogin") or ""),
        "USVISA_LOGIN_PASSWORD": str(profile.get("embassyPassword") or ""),
        "USVISA_FACILITY_ID": facility_id,
        "USVISA_IGNORED_DATES": ignored_dates,
        "USVISA_PROFILE_CONFIG_ACTIVE": "true",
        "USVISA_CREDENTIAL_SOURCE": f"user_profiles.user_id:{user_id}",
        "USVISA_STATE_FILE": str(_user_state_file(user_id)),
        "USVISA_ERROR_STATE_FILE": str(_user_error_state_file(user_id)),
        "USVISA_BROWSER_PROFILE_DIR": str(_user_browser_profile_dir(user_id)),
        "USVISA_MONITOR_DB_SYNC": "false",
    }
    vpn_proxy_url = str(profile.get("vpnProxyUrl") or "").strip()
    if vpn_proxy_url:
        overrides["USVISA_VPN_ENABLED"] = "true"
        overrides["USVISA_VPN_PROXY_URL"] = vpn_proxy_url
    appointment_url = str(profile.get("appointmentUrl") or city_config.get("appointmentUrl") or values.get("USVISA_APPOINTMENT_URL") or "")
    dates_api_url = str(profile.get("datesApiUrl") or city_config.get("datesApiUrl") or values.get("USVISA_DATES_API_URL") or "")
    times_api_url = str(profile.get("timesApiUrl") or city_config.get("timesApiUrl") or values.get("USVISA_TIMES_API_URL") or "")
    if appointment_url:
        overrides["USVISA_APPOINTMENT_URL"] = appointment_url
    if dates_api_url:
        overrides["USVISA_DATES_API_URL"] = dates_api_url
    if times_api_url:
        overrides["USVISA_TIMES_API_URL"] = times_api_url
    return overrides


def _admin_profile_env_values() -> dict[str, str]:
    with _db() as conn:
        row = conn.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
    return _profile_env_values(int(row["id"])) if row else {}


def _env_for_user_subprocess(user_id: int) -> dict[str, str]:
    env = _env_for_subprocess()
    env.update(_profile_env_values(user_id))
    return env


def _env_for_admin_subprocess() -> dict[str, str]:
    env = _env_for_subprocess()
    env.update(_admin_profile_env_values())
    return env


def _ordered_setting_keys() -> list[str]:
    keys: list[str] = []
    for group in SETTING_GROUPS:
        keys.extend(group["keys"])
    return keys


def _debug_timestamp_to_iso(value: str) -> str | None:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return parsed.isoformat()


def _debug_html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _debug_artifacts_payload() -> dict[str, Any]:
    if not DEBUG_ARTIFACTS_DIR.exists():
        return {"exists": False, "path": str(DEBUG_ARTIFACTS_DIR), "artifacts": []}

    grouped: dict[str, dict[str, Any]] = {}
    for path in sorted(DEBUG_ARTIFACTS_DIR.iterdir(), reverse=True):
        if not path.is_file() or path.suffix.lower() not in {".png", ".html"}:
            continue
        bucket = grouped.setdefault(path.stem, {"stem": path.stem})
        bucket[path.suffix.lower().lstrip(".")] = path

    artifacts: list[dict[str, Any]] = []
    for stem, bundle in sorted(grouped.items(), reverse=True):
        prefix, _, label = stem.partition("-")
        created_at = _debug_timestamp_to_iso(prefix)
        screenshot = bundle.get("png")
        html_file = bundle.get("html")
        html_preview = ""
        html_title = ""
        html_url = None
        if isinstance(html_file, Path):
            try:
                html_text = html_file.read_text(encoding="utf-8")
            except Exception as exc:
                html_preview = f"Не удалось прочитать HTML: {type(exc).__name__}: {exc}"
            else:
                html_preview = html_text[:8000]
                html_title = _debug_html_title(html_text)
            html_url = f"/api/debug-artifacts/file/{html_file.name}"

        artifacts.append(
            {
                "id": stem,
                "label": label or stem,
                "createdAt": created_at,
                "screenshotUrl": f"/api/debug-artifacts/file/{screenshot.name}" if isinstance(screenshot, Path) else None,
                "screenshotName": screenshot.name if isinstance(screenshot, Path) else "",
                "screenshotSize": screenshot.stat().st_size if isinstance(screenshot, Path) else 0,
                "htmlUrl": html_url,
                "htmlName": html_file.name if isinstance(html_file, Path) else "",
                "htmlSize": html_file.stat().st_size if isinstance(html_file, Path) else 0,
                "htmlPreview": html_preview,
                "htmlTitle": html_title,
            }
        )

    return {"exists": True, "path": str(DEBUG_ARTIFACTS_DIR), "artifacts": artifacts}


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    if request.path.startswith("/static/") or request.path in PUBLIC_ROUTES:
        return await handler(request)
    user = _current_user(request)
    if user is None:
        return web.json_response({"ok": False, "error": "Требуется вход"}, status=401)
    if request.path in ADMIN_API_ROUTES and user["role"] != "admin":
        return web.json_response({"ok": False, "error": "Недостаточно прав"}, status=403)
    if request.path in DEMO_FORBIDDEN_ROUTES and request.method != "GET" and user["role"] == "demo":
        return web.json_response({"ok": False, "error": "Демо-доступ работает только на просмотр"}, status=403)
    return await handler(request)


async def api_auth_me(request: web.Request) -> web.Response:
    user = _current_user(request)
    return web.json_response({"user": user, "cityOptions": _city_options(include_inactive=True)})


async def api_auth_register(request: web.Request) -> web.Response:
    payload = await request.json()
    email = _normalize_email(payload.get("email"))
    password = str(payload.get("password") or "")
    terms_accepted = bool(payload.get("termsAccepted"))
    if not email or "@" not in email:
        raise web.HTTPBadRequest(text="email is invalid")
    if len(password) < 8:
        raise web.HTTPBadRequest(text="password is too short")
    if not terms_accepted:
        return web.json_response({"ok": False, "error": "Для регистрации нужно согласиться с правилами использования сервиса"}, status=400)

    now = _utc_now_iso()
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with _db() as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (email, password_hash, role, status, terms_accepted, terms_accepted_at, created_at, updated_at)
                VALUES (?, ?, 'user', ?, ?, ?, ?, ?)
                """,
                (email, _hash_password(password), USER_STATUS_UNPAID, 1, now, now, now),
            )
        except sqlite3.IntegrityError:
            return web.json_response({"ok": False, "error": "Пользователь уже существует"}, status=409)
        user_id = int(cursor.lastrowid)
        _ensure_profile(conn, user_id, {})
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_id, _hash_token(token), now, expires_at),
        )
        user_row = conn.execute("SELECT id, email, role, priority, status FROM users WHERE id = ?", (user_id,)).fetchone()
        profile_row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        assert user_row is not None
    return _session_response(_user_payload(user_row, profile_row), token)





async def api_auth_login(request: web.Request) -> web.Response:
    payload = await request.json()
    email = _normalize_email(payload.get("email"))
    password = str(payload.get("password") or "")
    with _db() as conn:
        user_row = conn.execute("SELECT id, email, role, priority, status, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if user_row is None or not _verify_password(password, user_row["password_hash"]):
            return web.json_response({"ok": False, "error": "Неверный email или пароль"}, status=401)
        blocked_reason = USER_BLOCKED_LOGIN_ERRORS.get(user_row["status"])
        if blocked_reason:
            return web.json_response({"ok": False, "error": blocked_reason}, status=403)
        _ensure_profile(conn, int(user_row["id"]))
        now = _utc_now_iso()
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_row["id"], _hash_token(token), now, expires_at),
        )
        profile_row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_row["id"],)).fetchone()
    return _session_response(_user_payload(user_row, profile_row), token)


async def api_auth_demo(_: web.Request) -> web.Response:
    now = _utc_now_iso()
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with _db() as conn:
        user_row = _ensure_demo_user(conn)
        profile_row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_row["id"],)).fetchone()
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (user_row["id"], _hash_token(token), now, expires_at),
        )
    return _session_response(_user_payload(user_row, profile_row), token)


async def api_auth_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with _db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE)
    return response


async def api_profile(request: web.Request) -> web.Response:
    user = _require_user(request)
    with _db() as conn:
        profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    return web.json_response({"profile": _profile_from_row(profile), "cityOptions": _city_options(include_inactive=True)})


async def api_save_profile(request: web.Request) -> web.Response:
    user = _require_user(request)
    payload = await request.json()
    city = str(payload.get("city") or "astana").strip().lower()
    city_options = _city_options()
    if city not in city_options:
        raise web.HTTPBadRequest(text="city is invalid")
    raw_dates = payload.get("ignoredDates", [])
    if not isinstance(raw_dates, list):
        raise web.HTTPBadRequest(text="ignoredDates must be a list")

    ignored_dates = sorted({str(date).strip() for date in raw_dates if DATE_RE.match(str(date).strip())})
    now = _utc_now_iso()
    values = (
        str(payload.get("embassyLogin") or "").strip(),
        str(payload.get("embassyPassword") or ""),
        city,
        city_options[city]["facilityId"],
        ",".join(ignored_dates),
        now,
        user["id"],
    )
    with _db() as conn:
        _ensure_profile(conn, int(user["id"]))
        conn.execute(
            """
            UPDATE user_profiles
            SET embassy_login = ?, embassy_password = ?, city = ?, facility_id = ?,
                ignored_dates = ?, updated_at = ?
            WHERE user_id = ?
            """,
            values,
        )
        profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    return web.json_response({"ok": True, "profile": _profile_from_row(profile)})


async def api_users(request: web.Request) -> web.Response:
    _require_admin(request)
    city_options = _city_options(include_inactive=True)
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT users.id, users.email, users.role, users.priority, users.created_at,
                   users.status, profiles.city, profiles.facility_id, profiles.ignored_dates,
                   profiles.embassy_login, profiles.vpn_proxy_url
            FROM users
            LEFT JOIN user_profiles profiles ON profiles.user_id = users.id
            ORDER BY
                CASE WHEN users.status IN (?, ?) THEN 1 ELSE 0 END ASC,
                users.priority DESC,
                users.created_at DESC
            """,
            (USER_STATUS_BLOCKED, USER_STATUS_DELETED),
        ).fetchall()
    users = [
        {
            "id": row["id"],
            "email": row["email"],
            "role": row["role"],
            "priority": int(row["priority"] or 0),
            "status": row["status"] or USER_STATUS_UNPAID,
            "createdAt": row["created_at"],
            "city": city_options.get(row["city"] or "astana", city_options["astana"])["label"],
            "cityCode": row["city"] or "astana",
            "facilityId": row["facility_id"] or "",
            "embassyLogin": row["embassy_login"] or "",
            "vpnProxyUrl": row["vpn_proxy_url"] or "",
            "ignoredDatesCount": len([date for date in str(row["ignored_dates"] or "").split(",") if date]),
        }
        for row in rows
    ]
    return web.json_response({"users": users})


def _user_id_from_request(request: web.Request) -> int:
    try:
        return int(request.match_info["user_id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text="userId is invalid")


def _target_user_and_profile(conn: sqlite3.Connection, user_id: int) -> tuple[sqlite3.Row, sqlite3.Row]:
    target = conn.execute(
        "SELECT id, email, role, priority, status, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if target is None:
        raise web.HTTPNotFound(text="user not found")
    if target["role"] == "demo":
        raise web.HTTPBadRequest(text="demo profile cannot be changed")
    _ensure_profile(conn, user_id)
    profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    assert profile is not None
    return target, profile


def _validated_user_settings_payload(payload: dict[str, Any], existing_profile: sqlite3.Row) -> dict[str, Any]:
    city = str(payload.get("city") or "astana").strip().lower()
    city_options = _city_options()
    if city not in city_options:
        raise web.HTTPBadRequest(text="city is invalid")
    vpn_proxy_url = str(payload.get("vpnProxyUrl") or "").strip()
    if vpn_proxy_url and not re.match(r"^(https?|socks5?|socks)://", vpn_proxy_url):
        raise web.HTTPBadRequest(text="vpnProxyUrl is invalid")
    appointment_url = str(payload.get("appointmentUrl") or "").strip()
    dates_api_url = str(payload.get("datesApiUrl") or "").strip()
    times_api_url = str(payload.get("timesApiUrl") or "").strip()
    for key, url in (
        ("appointmentUrl", appointment_url),
        ("datesApiUrl", dates_api_url),
        ("timesApiUrl", times_api_url),
    ):
        if url and not url.startswith("https://ais.usvisa-info.com/"):
            raise web.HTTPBadRequest(text=f"{key} is invalid")
    try:
        validate_astana_urls({
            "USVISA_APPOINTMENT_URL": appointment_url,
            "USVISA_DATES_API_URL": dates_api_url,
            "USVISA_TIMES_API_URL": times_api_url,
        })
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))
    for key in ("ignoredDates", "ignoredPeriods", "priorityDates", "priorityPeriods"):
        if key in payload and not isinstance(payload.get(key), list):
            raise web.HTTPBadRequest(text=f"{key} must be a list")
    try:
        preparation_days = max(0, int(payload.get("preparationDays", existing_profile["prep_days"] or 3)))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="preparationDays must be a non-negative integer")

    existing_ignored_dates = [
        date.strip()
        for date in str(existing_profile["ignored_dates"] or "").split(",")
        if DATE_RE.match(date.strip())
    ]
    existing_priority_dates = [
        date.strip()
        for date in str(existing_profile["priority_dates"] or "").split(",")
        if DATE_RE.match(date.strip())
    ]
    raw_ignored_periods = payload.get("ignoredPeriods", _parse_periods_json(existing_profile["ignored_periods"]))
    raw_priority_periods = payload.get("priorityPeriods", _parse_periods_json(existing_profile["priority_periods"]))
    ignored_periods = _parse_periods_json(json.dumps(raw_ignored_periods, ensure_ascii=False))
    priority_periods = _parse_periods_json(json.dumps(raw_priority_periods, ensure_ascii=False))
    ignored_dates = sorted(
        {str(date).strip() for date in payload.get("ignoredDates", existing_ignored_dates) if DATE_RE.match(str(date).strip())}
    )
    priority_dates = sorted(
        {str(date).strip() for date in payload.get("priorityDates", existing_priority_dates) if DATE_RE.match(str(date).strip())}
    )
    existing_password = str(existing_profile["embassy_password"] or "")
    incoming_password = str(payload.get("embassyPassword") or "")
    return {
        "embassy_login": str(payload.get("embassyLogin") or "").strip(),
        "embassy_password": incoming_password if incoming_password else existing_password,
        "city": city,
        "facility_id": city_options[city]["facilityId"],
        "vpn_proxy_url": vpn_proxy_url,
        "appointment_url": appointment_url,
        "dates_api_url": dates_api_url,
        "times_api_url": times_api_url,
        "ignored_dates": ",".join(ignored_dates),
        "ignored_periods": _serialize_periods_json(ignored_periods),
        "priority_dates": ",".join(priority_dates),
        "priority_periods": _serialize_periods_json(priority_periods),
        "prep_days": preparation_days,
    }


def expand_period_dates(periods: list[dict[str, str]]) -> list[str]:
    dates: list[str] = []
    for period in periods:
        start = period.get("start", "")
        end = period.get("end", "")
        if not DATE_RE.match(start) or not DATE_RE.match(end) or start > end:
            continue
        cursor = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
        while cursor <= end_date:
            dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
    return dates


async def api_user_settings(request: web.Request) -> web.Response:
    _require_admin(request)
    user_id = _user_id_from_request(request)
    with _db() as conn:
        target, profile = _target_user_and_profile(conn, user_id)
    return web.json_response(
        {
            "user": {
                "id": target["id"],
                "email": target["email"],
                "role": target["role"],
                "priority": int(target["priority"] or 0),
                "status": target["status"] or USER_STATUS_UNPAID,
                "createdAt": target["created_at"],
            },
            "profile": _profile_from_row(profile),
            "cityOptions": _city_options(include_inactive=True),
        }
    )


async def api_save_user_settings(request: web.Request) -> web.Response:
    _require_admin(request)
    user_id = _user_id_from_request(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="settings must be an object")

    with _db() as conn:
        target, existing_profile = _target_user_and_profile(conn, user_id)
        values = _validated_user_settings_payload(payload, existing_profile)
        conn.execute(
            """
            UPDATE user_profiles
            SET embassy_login = ?, embassy_password = ?, city = ?, facility_id = ?,
                ignored_dates = ?, ignored_periods = ?, priority_dates = ?, priority_periods = ?,
                prep_days = ?, vpn_proxy_url = ?, appointment_url = ?, dates_api_url = ?,
                times_api_url = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                values["embassy_login"],
                values["embassy_password"],
                values["city"],
                values["facility_id"],
                values["ignored_dates"],
                values["ignored_periods"],
                values["priority_dates"],
                values["priority_periods"],
                values["prep_days"],
                values["vpn_proxy_url"],
                values["appointment_url"],
                values["dates_api_url"],
                values["times_api_url"],
                _utc_now_iso(),
                user_id,
            ),
        )
        profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()

    return web.json_response(
        {
            "ok": True,
            "user": {
                "id": target["id"],
                "email": target["email"],
                "role": target["role"],
                "priority": int(target["priority"] or 0),
                "status": target["status"] or USER_STATUS_UNPAID,
                "createdAt": target["created_at"],
            },
            "profile": _profile_from_row(profile),
            "cityOptions": _city_options(include_inactive=True),
        }
    )


async def api_save_user_profile(request: web.Request) -> web.Response:
    return await api_save_user_settings(request)


async def api_update_user_status(request: web.Request) -> web.Response:
    _require_admin(request)
    payload = await request.json()
    try:
        user_id = int(payload.get("userId"))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="userId is invalid")

    status = str(payload.get("status") or "").strip()
    if status not in USER_STATUS_VALUES:
        raise web.HTTPBadRequest(text="status is invalid")

    with _db() as conn:
        row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(text="user not found")
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (status, _utc_now_iso(), user_id),
        )

    return web.json_response({"ok": True})


async def api_delete_user(request: web.Request) -> web.Response:
    admin = _require_admin(request)
    try:
        user_id = int(request.match_info["user_id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text="userId is invalid")

    with _db() as conn:
        user = conn.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            raise web.HTTPNotFound(text="user not found")
        if user_id == int(admin["id"]):
            return web.json_response(
                {"ok": False, "error": "Нельзя удалить текущего или последнего администратора"},
                status=400,
            )
        if user["role"] == "admin":
            admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
            if admin_count <= 1:
                return web.json_response(
                    {"ok": False, "error": "Нельзя удалить текущего или последнего администратора"},
                    status=400,
                )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    return web.json_response({"ok": True})


async def api_cities(request: web.Request) -> web.Response:
    _require_admin(request)
    return web.json_response({"cities": [_serialize_city_row(row) for row in _all_city_rows()]})


async def api_save_cities(request: web.Request) -> web.Response:
    _require_admin(request)
    payload = await request.json()
    raw_cities = payload.get("cities")
    if not isinstance(raw_cities, list):
        raise web.HTTPBadRequest(text="cities must be a list")

    if len(raw_cities) != 1 or not isinstance(raw_cities[0], dict):
        raise web.HTTPBadRequest(text="Only Astana is supported")
    city = raw_cities[0]
    if city.get("code") != "astana" or str(city.get("facilityId")) != ASTANA_FACILITY_ID:
        raise web.HTTPBadRequest(text="Only Astana (134) is supported")
    if city.get("label") != "Астана" or not city.get("active"):
        raise web.HTTPBadRequest(text="Astana must remain active")
    try:
        validate_astana_urls({
            "USVISA_APPOINTMENT_URL": str(city.get("appointmentUrl") or ""),
            "USVISA_DATES_API_URL": str(city.get("datesApiUrl") or ""),
            "USVISA_TIMES_API_URL": str(city.get("timesApiUrl") or ""),
        })
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error))

    now = _utc_now_iso()
    with _db() as conn:
        existing_codes = {
            row["code"] for row in conn.execute("SELECT code FROM cities").fetchall()
        }
        submitted_codes: set[str] = set()
        for raw_city in raw_cities:
            if not isinstance(raw_city, dict):
                continue
            code = re.sub(r"[^a-z0-9_-]", "", str(raw_city.get("code") or "").strip().lower())
            label = str(raw_city.get("label") or "").strip()
            if not code or not label:
                continue
            submitted_codes.add(code)
            conn.execute(
                """
                INSERT INTO cities (
                    code, label, active, priority, facility_id, appointment_url, dates_api_url, times_api_url,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    label = excluded.label,
                    active = excluded.active,
                    priority = excluded.priority,
                    facility_id = excluded.facility_id,
                    appointment_url = excluded.appointment_url,
                    dates_api_url = excluded.dates_api_url,
                    times_api_url = excluded.times_api_url,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    label,
                    1 if bool(raw_city.get("active")) else 0,
                    int(raw_city.get("priority") or 0),
                    str(raw_city.get("facilityId") or "").strip(),
                    str(raw_city.get("appointmentUrl") or "").strip(),
                    str(raw_city.get("datesApiUrl") or "").strip(),
                    str(raw_city.get("timesApiUrl") or "").strip(),
                    now,
                    now,
                ),
            )
        for code in existing_codes - submitted_codes:
            conn.execute("DELETE FROM cities WHERE code = ?", (code,))

    return web.json_response({"ok": True, "cities": [_serialize_city_row(row) for row in _all_city_rows()]})


async def api_status(request: web.Request) -> web.Response:
    user = _require_user(request)
    values = _public_env_values()
    effective_ignored_dates = user.get("profile", {}).get("ignoredDates")
    if user["role"] == "admin" and effective_ignored_dates is None:
        effective_ignored_dates = _ignored_dates_from_env(values)
    range_hours_raw = str(request.query.get("range_hours") or "").strip().lower()
    try:
        range_hours = None if not range_hours_raw or range_hours_raw == "all" else max(1, int(range_hours_raw))
    except ValueError:
        range_hours = 24
    is_admin = user["role"] == "admin"
    if user["role"] == "demo":
        state = _demo_state(effective_ignored_dates, range_hours)
        return web.json_response(
            {
                "user": user,
                "cityOptions": _city_options(values, include_inactive=True),
                "settings": [],
                "cities": [],
                "state": state,
                "summary": _demo_summary(range_hours),
                "process": {"running": False, "logs": []},
                "monitor": _demo_monitor(values),
                "projectRoot": "",
                "envFile": "",
            }
        )

    state_values = _env_for_user_subprocess(int(user["id"]))
    state_file = _state_file_from_env(state_values)
    error_state_file = _error_state_file_from_env(state_values)
    state = _summarize_state(
        _read_json(state_file),
        state_file,
        effective_ignored_dates,
        _read_json(error_state_file),
        use_db_snapshot=_parse_bool(state_values.get("USVISA_MONITOR_DB_SYNC", "true")),
    )
    can_observe_runs = is_admin
    response_state = dict(state)
    response_state.pop("monitorEvents", None)
    response_state["periodFoundDates"] = _period_found_dates(state, range_hours)
    return web.json_response(
        {
            "user": user,
            "cityOptions": _city_options(values, include_inactive=True),
            "settings": _setting_schema(values) if is_admin else [],
            "cities": [_serialize_city_row(row) for row in _all_city_rows()] if is_admin else [],
            "state": response_state,
            "summary": _metrics_summary(state, range_hours),
            "process": PROCESS_STATE if can_observe_runs else {"running": False, "logs": []},
            "monitor": _public_monitor_state(values) if can_observe_runs else {"active": False},
            "projectRoot": str(PROJECT_ROOT) if is_admin else "",
            "envFile": str(ENV_FILE) if is_admin else "",
        }
    )


async def api_status_compact(request: web.Request) -> web.Response:
    user = _require_user(request)
    values = _public_env_values()
    effective_ignored_dates = user.get("profile", {}).get("ignoredDates")
    if user["role"] == "admin" and effective_ignored_dates is None:
        effective_ignored_dates = _ignored_dates_from_env(values)
    range_hours_raw = str(request.query.get("range_hours") or "").strip().lower()
    try:
        range_hours = None if not range_hours_raw or range_hours_raw == "all" else max(1, int(range_hours_raw))
    except ValueError:
        range_hours = 24
    if user["role"] == "demo":
        state = _demo_state(effective_ignored_dates, range_hours)
        return web.json_response(
            {
                "state": state,
                "summary": _demo_summary(range_hours),
            }
        )

    state_values = _env_for_user_subprocess(int(user["id"]))
    state_file = _state_file_from_env(state_values)
    error_state_file = _error_state_file_from_env(state_values)
    state = _summarize_state(
        _read_json(state_file),
        state_file,
        effective_ignored_dates,
        _read_json(error_state_file),
        use_db_snapshot=_parse_bool(state_values.get("USVISA_MONITOR_DB_SYNC", "true")),
    )

    compact_state = {
        "currentActiveAppointments": state.get("currentActiveAppointments") or [],
        "ignoredDates": state.get("ignoredDates") or [],
        "nextCheckAt": state.get("nextCheckAt"),
        "secondsUntilNextCheck": state.get("secondsUntilNextCheck"),
        "periodFoundDates": _period_found_dates(state, range_hours),
        "latestBooking": state.get("latestBooking") if isinstance(state.get("latestBooking"), dict) else {},
    }
    return web.json_response(
        {
            "state": compact_state,
            "summary": _metrics_summary(state, range_hours),
        }
    )


async def api_process_status(request: web.Request) -> web.Response:
    user = _require_user(request)
    if user["role"] != "admin":
        raise web.HTTPForbidden(text="Недостаточно прав")
    values = _public_env_values()
    return web.json_response(
        {
            "process": PROCESS_STATE,
            "monitor": _public_monitor_state(values),
        }
    )


async def api_debug_artifacts(request: web.Request) -> web.Response:
    _require_admin(request)
    return web.json_response(_debug_artifacts_payload())


async def api_debug_artifact_file(request: web.Request) -> web.StreamResponse:
    _require_admin(request)
    name = request.match_info.get("name", "")
    if not name or "/" in name or "\\" in name:
        raise web.HTTPBadRequest(text="invalid artifact name")

    path = (DEBUG_ARTIFACTS_DIR / name).resolve()
    try:
        path.relative_to(DEBUG_ARTIFACTS_DIR.resolve())
    except ValueError:
        raise web.HTTPForbidden(text="invalid artifact path") from None

    if not path.exists() or not path.is_file():
        raise web.HTTPNotFound(text="artifact not found")

    if path.suffix.lower() == ".html":
        return web.Response(text=path.read_text(encoding="utf-8"), content_type="text/plain")
    return web.FileResponse(path)


async def api_clear_errors(request: web.Request) -> web.Response:
    _require_admin(request)
    values = _public_env_values()
    error_state_file = _error_state_file_from_env(values)
    current = _read_json(error_state_file)
    payload = current if isinstance(current, dict) else {}
    payload["error_events"] = []
    payload["last_error_at"] = None
    payload["last_error_digest_at"] = None
    _write_json(error_state_file, payload)
    monitor_store.clear_error_events()
    return web.json_response({"ok": True})


async def api_delete_debug_artifact(request: web.Request) -> web.Response:
    _require_admin(request)
    payload = await request.json()
    artifact_id = str(payload.get("artifactId") or "").strip()
    if not artifact_id:
        raise web.HTTPBadRequest(text="artifactId is required")

    deleted = 0
    for suffix in (".png", ".html"):
        path = (DEBUG_ARTIFACTS_DIR / f"{artifact_id}{suffix}").resolve()
        try:
            path.relative_to(DEBUG_ARTIFACTS_DIR.resolve())
        except ValueError:
            raise web.HTTPForbidden(text="invalid artifact path") from None
        if path.exists() and path.is_file():
            path.unlink()
            deleted += 1

    if deleted == 0:
        raise web.HTTPNotFound(text="artifact not found")
    return web.json_response({"ok": True, "deleted": deleted})


async def api_delete_all_debug_artifacts(request: web.Request) -> web.Response:
    _require_admin(request)
    deleted = 0
    if DEBUG_ARTIFACTS_DIR.exists():
        for path in DEBUG_ARTIFACTS_DIR.iterdir():
            if path.is_file() and path.suffix.lower() in {".png", ".html"}:
                path.unlink()
                deleted += 1
    return web.json_response({"ok": True, "deleted": deleted})


async def api_save_settings(request: web.Request) -> web.Response:
    payload = await request.json()
    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise web.HTTPBadRequest(text="settings must be an object")

    current = _public_env_values()
    previous = dict(current)
    for key in _ordered_setting_keys():
        if key not in raw_settings:
            continue
        value = raw_settings[key]
        if key in BOOLEAN_KEYS:
            current[key] = "true" if bool(value) else "false"
        else:
            current[key] = str(value or "")

    _write_settings({key: current.get(key, "") for key in _ordered_setting_keys()})
    if any(previous.get(key, "") != current.get(key, "") for key in VPN_SETTING_KEYS):
        VPN_REFRESH_REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        VPN_REFRESH_REQUEST_FILE.write_text(_utc_now_iso(), encoding="utf-8")
    return web.json_response({"ok": True})


async def api_save_ignored_dates(request: web.Request) -> web.Response:
    user = _require_user(request)
    payload = await request.json()
    raw_dates = payload.get("dates")
    raw_periods = payload.get("periods", [])
    raw_priority_dates = payload.get("priorityDates", [])
    raw_priority_periods = payload.get("priorityPeriods", [])
    if not isinstance(raw_dates, list):
        raise web.HTTPBadRequest(text="dates must be a list")
    if not isinstance(raw_periods, list):
        raise web.HTTPBadRequest(text="periods must be a list")
    if not isinstance(raw_priority_dates, list):
        raise web.HTTPBadRequest(text="priorityDates must be a list")
    if not isinstance(raw_priority_periods, list):
        raise web.HTTPBadRequest(text="priorityPeriods must be a list")
    try:
        preparation_days = max(0, int(payload.get("preparationDays", 3)))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="preparationDays must be a non-negative integer")

    dates = sorted({str(date).strip() for date in raw_dates if DATE_RE.match(str(date).strip())})
    periods = _parse_periods_json(json.dumps(raw_periods, ensure_ascii=False))
    priority_dates = sorted(
        {str(date).strip() for date in raw_priority_dates if DATE_RE.match(str(date).strip())}
    )
    priority_periods = _parse_periods_json(json.dumps(raw_priority_periods, ensure_ascii=False))
    if user["role"] != "admin":
        with _db() as conn:
            _ensure_profile(conn, int(user["id"]))
            conn.execute(
                """
                UPDATE user_profiles
                SET ignored_dates = ?, ignored_periods = ?, priority_dates = ?, priority_periods = ?, prep_days = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    ",".join(dates),
                    _serialize_periods_json(periods),
                    ",".join(priority_dates),
                    _serialize_periods_json(priority_periods),
                    preparation_days,
                    _utc_now_iso(),
                    user["id"],
                ),
            )
            profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
        return web.json_response({"ok": True, "dates": dates, "profile": _profile_from_row(profile)})

    now = _utc_now_iso()
    serialized_dates = ",".join(dates)
    serialized_periods = _serialize_periods_json(periods)
    serialized_priority_dates = ",".join(priority_dates)
    serialized_priority_periods = _serialize_periods_json(priority_periods)
    with _db() as conn:
        _ensure_profile(conn, int(user["id"]))
        conn.execute(
            """
            UPDATE user_profiles
            SET ignored_dates = ?, ignored_periods = ?, priority_dates = ?, priority_periods = ?, prep_days = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                serialized_dates,
                serialized_periods,
                serialized_priority_dates,
                serialized_priority_periods,
                preparation_days,
                now,
                user["id"],
            ),
        )
        profile = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
    _write_settings({IGNORED_DATES_ENV_KEY: serialized_dates})
    return web.json_response({"ok": True, "dates": dates, "profile": _profile_from_row(profile)})


async def _append_log(line: str) -> None:
    PROCESS_STATE["logs"].append(line.rstrip("\n"))
    if len(PROCESS_STATE["logs"]) > MAX_LOG_LINES:
        PROCESS_STATE["logs"] = PROCESS_STATE["logs"][-MAX_LOG_LINES:]


def _monitor_interval(values: dict[str, str] | None = None) -> int:
    values = values or _public_env_values()
    try:
        return max(5, int(values.get("USVISA_MONITOR_LOOP_SECONDS") or "30"))
    except ValueError:
        return 30


def _monitor_error_pause_seconds(consecutive_errors: int) -> int | None:
    if consecutive_errors <= 0:
        return None
    if consecutive_errors > len(MONITOR_ERROR_PAUSE_STEPS_SECONDS):
        return None
    return MONITOR_ERROR_PAUSE_STEPS_SECONDS[consecutive_errors - 1]


def _format_moscow_time(value: datetime) -> str:
    return value.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")


async def _notify_monitor_recovery_due_to_errors(consecutive_errors: int) -> bool:
    message = (
        "❌ 24/7 мониторинг US Visa приостановлен из-за повторяющихся ошибок.\n"
        f"Подряд ошибочных прогонов: {consecutive_errors}.\n"
        "Автопаузы 2, 5 и 10 минут не помогли. "
        "Следующая автоматическая попытка будет через 30 минут."
    )
    return await record_event(message)


async def _notify_monitor_pause(
    *,
    consecutive_errors: int,
    pause_seconds: int,
    paused_until: datetime,
    recovery_mode: bool,
) -> bool:
    mode_label = "recovery-режим" if recovery_mode else "обычный режим"
    message = (
        "Пауза 24/7 мониторинга US Visa после ошибки.\n"
        f"Режим: {mode_label}.\n"
        f"Ошибок подряд: {consecutive_errors}.\n"
        f"Длительность паузы: {pause_seconds // 60} мин.\n"
        f"Следующая попытка: {_format_moscow_time(paused_until)}."
    )
    return await record_event(message)


async def _notify_monitor_resume(*, recovery_mode: bool) -> bool:
    mode_label = "recovery-режима" if recovery_mode else "автопаузы"
    message = (
        "Возобновление 24/7 мониторинга US Visa.\n"
        f"Причина: истекла пауза {mode_label}.\n"
        "Запускаю очередную проверку."
    )
    return await record_event(message)


async def _notify_monitor_recovered(interval_seconds: int) -> bool:
    message = (
        "24/7 мониторинг US Visa восстановился после ошибок.\n"
        "Последний прогон прошел успешно.\n"
        f"Возвращаюсь к обычному интервалу: {interval_seconds} сек."
    )
    return await record_event(message)


def _should_send_monitor_error_cycle_notification(now: datetime) -> bool:
    last_sent_at = _parse_dt(MONITOR_STATE.get("last_error_cycle_notification_at"))
    if last_sent_at is None:
        return True
    threshold = timedelta(minutes=MONITOR_ERROR_CYCLE_NOTIFICATION_INTERVAL_MINUTES)
    return now - last_sent_at >= threshold


async def _send_monitor_error_cycle_notification(notification) -> bool:
    await _append_log("24/7 monitor events are stored in the local log")
    return False


def _public_monitor_state(values: dict[str, str] | None = None) -> dict[str, Any]:
    state = dict(MONITOR_STATE)
    state["interval_seconds"] = _monitor_interval(values)
    state["task_alive"] = MONITOR_TASK is not None and not MONITOR_TASK.done()
    state["preventing_sleep"] = CAFFEINATE_PROCESS is not None and CAFFEINATE_PROCESS.returncode is None
    return state


def _monitor_user_rows() -> list[sqlite3.Row]:
    with _db() as conn:
        return conn.execute(
            """
            SELECT users.id, users.email, users.role, users.priority, users.status
            FROM users
            JOIN user_profiles profiles ON profiles.user_id = users.id
            WHERE users.status = ?
              AND users.role != 'demo'
              AND profiles.embassy_login != ''
              AND profiles.embassy_password != ''
            ORDER BY users.priority DESC, users.created_at ASC, users.id ASC
            """,
            (USER_STATUS_SEARCH_ENABLED,),
        ).fetchall()


def _latest_booking_from_state(values: dict[str, str] | None = None) -> dict[str, Any]:
    values = values or _public_env_values()
    if _parse_bool(values.get("USVISA_MONITOR_DB_SYNC", "true")):
        snapshot = monitor_store.read_snapshot()
        latest_booking = snapshot.get("latest_booking") if isinstance(snapshot, dict) else None
        if isinstance(latest_booking, dict) and latest_booking:
            return latest_booking
    raw_state = _read_json(_state_file_from_env(values))
    latest_booking = raw_state.get("latest_booking") if isinstance(raw_state, dict) else None
    return latest_booking if isinstance(latest_booking, dict) else {}


def _demo_summary(hours: int | None) -> dict[str, int]:
    if hours == 1:
        return {
            "successfulChecks": 6,
            "tokenErrors": 0,
            "browserErrors": 0,
            "foundFreeDays": 10,
            "actualFreeDays": 10,
        }
    if hours == 12:
        return {
            "successfulChecks": 44,
            "tokenErrors": 1,
            "browserErrors": 0,
            "foundFreeDays": 18,
            "actualFreeDays": 10,
        }
    if hours == 24:
        return {
            "successfulChecks": 83,
            "tokenErrors": 1,
            "browserErrors": 1,
            "foundFreeDays": 22,
            "actualFreeDays": 10,
        }
    if hours == 168:
        return {
            "successfulChecks": 412,
            "tokenErrors": 4,
            "browserErrors": 2,
            "foundFreeDays": 31,
            "actualFreeDays": 10,
        }
    return {
        "successfulChecks": 1284,
        "tokenErrors": 9,
        "browserErrors": 4,
        "foundFreeDays": 48,
        "actualFreeDays": 10,
    }


def _demo_state(ignored_dates: list[str] | None = None, hours: int | None = None) -> dict[str, Any]:
    ignored_dates = sorted(set(ignored_dates or []))
    now = datetime.now(timezone.utc)
    next_check_at = now + timedelta(seconds=27)
    active_times = {
        "2026-05-14": ["09:30", "10:00"],
        "2026-05-15": ["08:45"],
        "2026-05-20": ["09:15", "11:30"],
        "2026-05-21": [],
        "2026-05-22": ["10:45"],
        "2026-05-28": ["08:30", "09:00", "09:45"],
        "2026-06-02": [],
        "2026-06-03": ["10:30"],
        "2026-06-10": ["08:00", "08:15"],
        "2026-06-18": ["09:00"],
        "2026-06-19": ["09:45", "10:15"],
        "2026-06-24": [],
    }
    active_dates = list(active_times.keys())
    current_active = []
    for date in active_dates:
        if date in set(ignored_dates):
            continue
        current_active.append({"date": date, "times": active_times.get(date, [])})
    if hours == 1:
        period_found_dates = [date for date in active_dates if date not in set(ignored_dates)]
    elif hours == 12:
        period_found_dates = [date for date in (active_dates + ["2026-06-25", "2026-06-26"]) if date not in set(ignored_dates)]
    elif hours == 24:
        period_found_dates = [date for date in (active_dates + ["2026-06-25", "2026-06-26", "2026-07-02"]) if date not in set(ignored_dates)]
    elif hours == 168:
        period_found_dates = [date for date in (active_dates + ["2026-06-25", "2026-06-26", "2026-07-02", "2026-07-09", "2026-07-16"]) if date not in set(ignored_dates)]
    else:
        period_found_dates = [date for date in (active_dates + ["2026-06-25", "2026-06-26", "2026-07-02", "2026-07-09", "2026-07-16", "2026-08-03"]) if date not in set(ignored_dates)]

    latest_current_times = {
        date: times for date, times in active_times.items() if times and date not in set(ignored_dates)
    }
    latest_new_times = {
        "2026-05-20": ["11:30"],
        "2026-05-28": ["09:45"],
        "2026-06-18": ["09:00"],
    }

    return {
        "exists": True,
        "path": "demo://monitor-state",
        "appointments": [],
        "currentActiveAppointments": current_active,
        "ignoredDates": ignored_dates,
        "latestFoundDates": active_dates,
        "latestIgnoredDates": [date for date in active_dates if date in set(ignored_dates)],
        "latestDatesPayload": [{"date": date, "business_day": True} for date in active_dates],
        "latestDatesToCheck": [date for date in active_dates if date not in set(ignored_dates)],
        "latestCurrentTimes": latest_current_times,
        "latestNewTimes": latest_new_times,
        "currentIgnoredAppointments": [{"date": date, "times": []} for date in active_dates if date in set(ignored_dates)],
        "periodFoundDates": period_found_dates,
        "totalDates": len(active_dates),
        "totalTimes": sum(len(times) for times in latest_current_times.values()),
        "lastHealthcheckAt": (now - timedelta(minutes=8)).isoformat(),
        "lastSlotNotificationAt": (now - timedelta(minutes=24)).isoformat(),
        "lastErrorAt": None,
        "lastErrorDigestAt": None,
        "recentErrorCount10m": 0,
        "nextCheckAt": next_check_at.isoformat(),
        "secondsUntilNextCheck": 27,
        "errorEvents": [],
        "session": {
            "hasCookie": True,
            "cookieCandidates": 2,
            "hasCsrfToken": True,
            "hasSessionId": True,
            "hasIfNoneMatch": True,
            "browserHeaderCount": 6,
        },
        "readError": None,
    }


def _demo_process() -> dict[str, Any]:
    return {
        "kind": "check",
        "running": False,
        "returncode": 0,
        "started_at": "2026-05-04T09:58:12+00:00",
        "finished_at": "2026-05-04T09:58:21+00:00",
        "logs": [
            "$ DISPLAY=:99 USVISA_BROWSER_HEADLESS=false /opt/usvisa_monitor/.venv/bin/python /opt/usvisa_monitor/src/usvisa_appointment_monitor.py",
            "⏰ Старт проверки доступных дат US Visa",
            "USVisa request start: https://ais.usvisa-info.com/ru-kz/niv/schedule/00000000/appointment/days/134.json?appointments[expedite]=false",
            "Текущие даты: ['2026-05-14', '2026-05-15', '2026-05-20', '2026-05-21', '2026-05-22', '2026-05-28', '2026-06-02', '2026-06-03', '2026-06-10', '2026-06-18', '2026-06-19', '2026-06-24']",
            "Даты из списка исключений: ['2026-05-16', '2026-05-23', '2026-06-12', '2026-06-13']",
            "Даты для проверки времен: ['2026-05-14', '2026-05-15', '2026-05-20', '2026-05-21', '2026-05-22', '2026-05-28', '2026-06-02', '2026-06-03', '2026-06-10', '2026-06-18', '2026-06-19', '2026-06-24']",
            "Текущие времена: {'2026-05-14': ['09:30', '10:00'], '2026-05-15': ['08:45'], '2026-05-20': ['09:15', '11:30'], '2026-05-22': ['10:45'], '2026-05-28': ['08:30', '09:00', '09:45'], '2026-06-03': ['10:30'], '2026-06-10': ['08:00', '08:15'], '2026-06-18': ['09:00'], '2026-06-19': ['09:45', '10:15']}",
            "Новые времена: {'2026-05-20': ['11:30'], '2026-05-28': ['09:45'], '2026-06-18': ['09:00']}",
            "Следующая проверка запланирована на 2026-05-04T10:06:41+00:00",
        ],
    }


def _demo_monitor(values: dict[str, str] | None = None) -> dict[str, Any]:
    interval_seconds = _monitor_interval(values)
    return {
        "active": True,
        "task_alive": True,
        "preventing_sleep": False,
        "interval_seconds": interval_seconds,
        "iterations": 148,
        "auto_paused": False,
        "paused_until": None,
    }


async def _start_caffeinate_if_needed() -> None:
    global CAFFEINATE_PROCESS
    values = _public_env_values()
    if not _parse_bool(values.get("USVISA_WEB_PREVENT_SLEEP", "true")):
        return
    if platform.system() != "Darwin":
        return
    if CAFFEINATE_PROCESS is not None and CAFFEINATE_PROCESS.returncode is None:
        return

    try:
        CAFFEINATE_PROCESS = await asyncio.create_subprocess_exec(
            "caffeinate",
            "-ims",
            "-w",
            str(os.getpid()),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await _append_log("macOS sleep prevention enabled with caffeinate")
    except FileNotFoundError:
        await _append_log("caffeinate not found; sleep prevention is unavailable")


async def _stop_caffeinate() -> None:
    global CAFFEINATE_PROCESS
    if CAFFEINATE_PROCESS is None:
        return
    if CAFFEINATE_PROCESS.returncode is None:
        try:
            CAFFEINATE_PROCESS.send_signal(signal.SIGTERM)
            await asyncio.wait_for(CAFFEINATE_PROCESS.wait(), timeout=3)
        except Exception:
            CAFFEINATE_PROCESS.kill()
            await CAFFEINATE_PROCESS.wait()
    CAFFEINATE_PROCESS = None


async def _run_command(
    kind: str,
    command: list[str],
    reset_logs: bool = True,
    env: dict[str, str] | None = None,
) -> None:
    global CURRENT_SUBPROCESS
    async with RUN_LOCK:
        prepared_env = _prepare_browser_env(env)
        logs = [] if reset_logs else PROCESS_STATE["logs"]
        PROCESS_STATE.update(
            {
                "kind": kind,
                "running": True,
                "returncode": None,
                "started_at": _utc_now_iso(),
                "finished_at": None,
                "logs": logs,
            }
        )
        await _append_log(f"$ {_display_command(command, prepared_env)}")

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=prepared_env,
        )
        CURRENT_SUBPROCESS = process

        try:
            assert process.stdout is not None
            async for raw_line in process.stdout:
                await _append_log(raw_line.decode("utf-8", errors="replace"))

            PROCESS_STATE["returncode"] = await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            PROCESS_STATE["returncode"] = process.returncode
            raise
        finally:
            if CURRENT_SUBPROCESS is process:
                CURRENT_SUBPROCESS = None
            PROCESS_STATE["running"] = False
            PROCESS_STATE["finished_at"] = _utc_now_iso()


def _proxy_label_for_env(env: dict[str, str]) -> str:
    if _parse_bool(env.get("USVISA_VPN_ENABLED", "false")):
        proxy_url = env.get("USVISA_VPN_PROXY_URL", "").strip()
        if proxy_url:
            return proxy_url
    return "direct"


def _parallel_monitor_plan(
    monitor_users: list[sqlite3.Row],
) -> tuple[list[tuple[sqlite3.Row, dict[str, str], str]], list[tuple[str, str]]]:
    planned: list[tuple[sqlite3.Row, dict[str, str], str]] = []
    skipped: list[tuple[str, str]] = []
    seen_proxies: set[str] = set()
    require_proxy = len(monitor_users) > 1

    for monitor_user in monitor_users:
        user_email = str(monitor_user["email"])
        user_env = _env_for_user_subprocess(int(monitor_user["id"]))
        proxy_label = _proxy_label_for_env(user_env)
        if require_proxy and proxy_label == "direct":
            skipped.append((user_email, "для параллельного batch нужен отдельный proxy"))
            continue
        if proxy_label in seen_proxies:
            skipped.append((user_email, f"proxy уже используется в этом batch: {proxy_label}"))
            continue
        seen_proxies.add(proxy_label)
        planned.append((monitor_user, user_env, proxy_label))

    return planned, skipped


async def _run_monitor_user_command(
    command: list[str],
    *,
    user_email: str,
    env: dict[str, str],
) -> dict[str, Any]:
    prepared_env = _prepare_browser_env(env)
    proxy_label = _proxy_label_for_env(prepared_env)
    await _append_log(f"24/7 monitor user start: {user_email} proxy={proxy_label}")
    await _append_log(f"[{user_email}] $ {_display_command(command, prepared_env)}")

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=prepared_env,
    )
    MONITOR_SUBPROCESSES.add(process)

    try:
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            await _append_log(f"[{user_email}] {line}")
        returncode = await process.wait()
        return {"email": user_email, "returncode": returncode, "env": env}
    except asyncio.CancelledError:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        MONITOR_SUBPROCESSES.discard(process)


async def _run_parallel_monitor_batch(
    command: list[str],
    monitor_users: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    planned, skipped = _parallel_monitor_plan(monitor_users)
    for user_email, reason in skipped:
        await _append_log(f"24/7 monitor user skipped: {user_email} ({reason})")
    if not planned:
        return []

    PROCESS_STATE.update(
        {
            "kind": "monitor",
            "running": True,
            "returncode": None,
            "started_at": _utc_now_iso(),
            "finished_at": None,
        }
    )
    await _append_log(f"24/7 monitor parallel batch start: users={len(planned)}")
    tasks = [
        asyncio.create_task(
            _run_monitor_user_command(
                command,
                user_email=str(monitor_user["email"]),
                env=user_env,
            )
        )
        for monitor_user, user_env, _proxy_label in planned
    ]
    try:
        return await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        PROCESS_STATE["running"] = False
        PROCESS_STATE["finished_at"] = _utc_now_iso()


async def _start_process(kind: str, command: list[str], env: dict[str, str] | None = None) -> web.Response:
    if RUN_LOCK.locked() or PROCESS_STATE.get("running"):
        return web.json_response({"ok": False, "error": "Уже выполняется другая операция"}, status=409)
    asyncio.create_task(_run_command(kind, command, env=env))
    return web.json_response({"ok": True})


async def api_run_check(request: web.Request) -> web.Response:
    user = _require_admin(request)
    return await _start_process(
        "check",
        [sys.executable, str(SRC_DIR / "usvisa_appointment_monitor.py")],
        env=_env_for_user_subprocess(int(user["id"])),
    )


async def api_run_login(request: web.Request) -> web.Response:
    user = _require_admin(request)
    if MONITOR_TASK is not None and not MONITOR_TASK.done():
        await _append_log("Stopping 24/7 monitor before browser login")
        await _stop_monitor_task()
    return await _start_process(
        "login",
        [sys.executable, str(SRC_DIR / "usvisa_browser_login_and_run.py")],
        env=_env_for_user_subprocess(int(user["id"])),
    )


async def api_reset_next_check(request: web.Request) -> web.Response:
    user = _require_admin(request)
    values = _env_for_user_subprocess(int(user["id"]))
    state_file = _state_file_from_env(values)
    raw_state = _read_json(state_file)
    payload = raw_state if isinstance(raw_state, dict) else {}
    payload["next_check_at"] = None
    _write_json(state_file, payload)
    snapshot = monitor_store.read_snapshot() if _parse_bool(values.get("USVISA_MONITOR_DB_SYNC", "true")) else {}
    if isinstance(snapshot, dict) and snapshot:
        snapshot["next_check_at"] = None
        monitor_store.write_snapshot(snapshot, _utc_now_iso())
    await _append_log("Reset next_check_at by user request")
    return web.json_response({"ok": True})


async def api_stop_process(_: web.Request) -> web.Response:
    process = CURRENT_SUBPROCESS
    if process is None or process.returncode is not None or not PROCESS_STATE.get("running"):
        if MONITOR_TASK is not None and not MONITOR_TASK.done():
            await _append_log("Stopping 24/7 monitor loop by user request")
            await _stop_monitor_task()
            PROCESS_STATE["running"] = False
            PROCESS_STATE["kind"] = None
            PROCESS_STATE["finished_at"] = _utc_now_iso()
            return web.json_response({"ok": True, "stopped": "monitor"})
        return web.json_response({"ok": False, "error": "Нет активного процесса"}, status=409)

    await _append_log("Stopping current process by user request")
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        await _append_log("Process did not stop after SIGTERM; killing")
        process.kill()
        await process.wait()

    return web.json_response({"ok": True})


async def _monitor_loop() -> None:
    command = [sys.executable, str(SRC_DIR / "usvisa_appointment_monitor.py")]
    MONITOR_STATE.update(
        {
            "active": True,
            "auto_paused": False,
            "paused_until": None,
            "started_at": _utc_now_iso(),
            "stopped_at": None,
            "iterations": 0,
            "last_returncode": None,
            "error_recovery": False,
            "error_cycle_count": 0,
        }
    )
    await _start_caffeinate_if_needed()
    try:
        while True:
            values = _public_env_values()
            interval = _monitor_interval(values)
            MONITOR_STATE["interval_seconds"] = interval
            paused_until = _parse_dt(MONITOR_STATE.get("paused_until"))
            resumed_from_recovery = False
            if MONITOR_STATE.get("auto_paused") and paused_until is not None:
                now = datetime.now(timezone.utc)
                if paused_until > now:
                    MONITOR_STATE["active"] = False
                    sleep_seconds = max(1, min(30, round((paused_until - now).total_seconds())))
                    await asyncio.sleep(sleep_seconds)
                    continue
                MONITOR_STATE["auto_paused"] = False
                MONITOR_STATE["paused_until"] = None
                MONITOR_STATE["active"] = True
                recovery_mode = bool(MONITOR_STATE.get("error_recovery"))
                if recovery_mode:
                    resumed_from_recovery = True
                    MONITOR_STATE["error_cycle_count"] = 0
                    MONITOR_STATE["error_recovery"] = False
                await _append_log("Auto-paused 24/7 monitor resumed after cooldown")
                notification_sent = await _send_monitor_error_cycle_notification(
                    lambda: _notify_monitor_resume(recovery_mode=recovery_mode)
                )
                await _append_log(
                    "24/7 monitor resume notification sent: "
                    f"{'yes' if notification_sent else 'no'}"
                )

            MONITOR_STATE["active"] = True
            MONITOR_STATE["last_iteration_started_at"] = _utc_now_iso()
            monitor_users = _monitor_user_rows()
            if not monitor_users:
                await _append_log("24/7 monitor skipped: no users with enabled search and embassy credentials")
                PROCESS_STATE["returncode"] = 0
            else:
                iteration_failed = False
                results = await _run_parallel_monitor_batch(command, monitor_users)
                if not results:
                    iteration_failed = True
                for result in results:
                    if result.get("returncode") not in {0, None}:
                        iteration_failed = True
                    user_email = str(result.get("email") or "-")
                    latest_booking = _latest_booking_from_state(result.get("env"))
                    if latest_booking:
                        booking_key = "|".join(
                            [
                                user_email,
                                str(latest_booking.get("at") or ""),
                                str(latest_booking.get("date") or ""),
                                str(latest_booking.get("time") or ""),
                                str(latest_booking.get("method") or ""),
                            ]
                        )
                        handled_bookings = list(MONITOR_STATE.get("handled_bookings") or [])
                        if booking_key in handled_bookings:
                            continue
                        handled_bookings.append(booking_key)
                        MONITOR_STATE["handled_bookings"] = handled_bookings[-100:]
                        await _append_log(
                            "Booking completed successfully; continuing 24/7 monitor for other users: "
                            f"{user_email} {latest_booking.get('date', '-') } {latest_booking.get('time', '-') } "
                            f"(method={latest_booking.get('method', 'unknown')})"
                        )
                PROCESS_STATE["returncode"] = 1 if iteration_failed else 0
            MONITOR_STATE["last_iteration_finished_at"] = _utc_now_iso()
            MONITOR_STATE["last_returncode"] = PROCESS_STATE.get("returncode")
            MONITOR_STATE["iterations"] = int(MONITOR_STATE.get("iterations") or 0) + 1

            run_failed = PROCESS_STATE.get("returncode") not in {0, None}
            if run_failed:
                MONITOR_STATE["error_cycle_count"] = int(MONITOR_STATE.get("error_cycle_count") or 0) + 1
            else:
                MONITOR_STATE["error_cycle_count"] = 0
            consecutive_errors = int(MONITOR_STATE.get("error_cycle_count") or 0)
            pause_seconds = _monitor_error_pause_seconds(consecutive_errors)
            recovery_mode = bool(MONITOR_STATE.get("error_recovery"))
            if not run_failed and resumed_from_recovery:
                await _append_log("24/7 monitor recovered after 30m recovery retry; returning to normal schedule")
                notification_sent = await _send_monitor_error_cycle_notification(
                    lambda: _notify_monitor_recovered(interval)
                )
                await _append_log(
                    "24/7 monitor recovery notification sent: "
                    f"{'yes' if notification_sent else 'no'}"
                )
                await asyncio.sleep(interval)
                continue

            if pause_seconds is not None:
                paused_until_dt = (
                    datetime.now(timezone.utc) + timedelta(seconds=pause_seconds)
                ).replace(microsecond=0)
                MONITOR_STATE["auto_paused"] = True
                MONITOR_STATE["paused_until"] = paused_until_dt.isoformat()
                MONITOR_STATE["active"] = False
                await _append_log(
                    "24/7 monitor auto-paused: "
                    f"{consecutive_errors} consecutive errors detected. "
                    f"Cooldown {pause_seconds // 60}m. "
                    f"Will retry at {paused_until_dt.isoformat()}"
                )
                notification_sent = await _send_monitor_error_cycle_notification(
                    lambda: _notify_monitor_pause(
                        consecutive_errors=consecutive_errors,
                        pause_seconds=pause_seconds,
                        paused_until=paused_until_dt,
                        recovery_mode=False,
                    )
                )
                await _append_log(
                    "24/7 monitor pause notification sent: "
                    f"{'yes' if notification_sent else 'no'}"
                )
                continue

            if consecutive_errors > len(MONITOR_ERROR_PAUSE_STEPS_SECONDS):
                if not recovery_mode:
                    notification_sent = await _send_monitor_error_cycle_notification(
                        lambda: _notify_monitor_recovery_due_to_errors(consecutive_errors)
                    )
                    await _append_log(
                        "24/7 monitor entered 30m recovery mode after repeated errors: "
                        f"{consecutive_errors} consecutive errors detected after 2m/5m/10m cooldowns. "
                        f"локальный журнал notified: {'yes' if notification_sent else 'no'}"
                    )

                paused_until_dt = (
                    datetime.now(timezone.utc) + timedelta(seconds=MONITOR_ERROR_RECOVERY_SECONDS)
                ).replace(microsecond=0)
                MONITOR_STATE["active"] = False
                MONITOR_STATE["auto_paused"] = True
                MONITOR_STATE["paused_until"] = paused_until_dt.isoformat()
                MONITOR_STATE["error_recovery"] = True
                await _append_log(
                    "24/7 monitor recovery retry scheduled: "
                    f"{consecutive_errors} consecutive errors still detected. "
                    f"Will retry at {paused_until_dt.isoformat()}"
                )
                notification_sent = await _send_monitor_error_cycle_notification(
                    lambda: _notify_monitor_pause(
                        consecutive_errors=consecutive_errors,
                        pause_seconds=MONITOR_ERROR_RECOVERY_SECONDS,
                        paused_until=paused_until_dt,
                        recovery_mode=True,
                    )
                )
                await _append_log(
                    "24/7 monitor recovery pause notification sent: "
                    f"{'yes' if notification_sent else 'no'}"
                )
                continue

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    finally:
        MONITOR_STATE["active"] = False
        MONITOR_STATE["auto_paused"] = False
        MONITOR_STATE["paused_until"] = None
        MONITOR_STATE["error_recovery"] = False
        MONITOR_STATE["error_cycle_count"] = 0
        MONITOR_STATE["stopped_at"] = _utc_now_iso()
        await _stop_caffeinate()


async def api_monitor_start(_: web.Request) -> web.Response:
    global MONITOR_TASK
    if MONITOR_TASK is not None and not MONITOR_TASK.done():
        return web.json_response({"ok": True})
    MONITOR_TASK = asyncio.create_task(_monitor_loop())
    return web.json_response({"ok": True})


async def _stop_monitor_task() -> None:
    global MONITOR_TASK
    if MONITOR_TASK is None:
        await _stop_caffeinate()
        return
    if not MONITOR_TASK.done():
        MONITOR_TASK.cancel()
        try:
            await MONITOR_TASK
        except asyncio.CancelledError:
            pass
    MONITOR_TASK = None


async def api_monitor_stop(_: web.Request) -> web.Response:
    await _stop_monitor_task()
    return web.json_response({"ok": True})


async def _on_startup(_: web.Application) -> None:
    _init_db()
    values = _public_env_values()
    if _parse_bool(values.get("USVISA_MONITOR_AUTOSTART", "false")):
        global MONITOR_TASK
        MONITOR_TASK = asyncio.create_task(_monitor_loop())


async def _on_cleanup(_: web.Application) -> None:
    await _stop_monitor_task()


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/api/auth/me", api_auth_me)
    app.router.add_post("/api/auth/register", api_auth_register)
    app.router.add_post("/api/auth/login", api_auth_login)
    app.router.add_post("/api/auth/demo", api_auth_demo)
    app.router.add_post("/api/auth/logout", api_auth_logout)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/status/compact", api_status_compact)
    app.router.add_get("/api/process-status", api_process_status)
    app.router.add_post("/api/errors/clear", api_clear_errors)
    app.router.add_get("/api/debug-artifacts", api_debug_artifacts)
    app.router.add_get("/api/debug-artifacts/file/{name}", api_debug_artifact_file)
    app.router.add_post("/api/debug-artifacts/delete", api_delete_debug_artifact)
    app.router.add_post("/api/debug-artifacts/delete-all", api_delete_all_debug_artifacts)
    app.router.add_get("/api/profile", api_profile)
    app.router.add_post("/api/profile", api_save_profile)
    app.router.add_get("/api/users", api_users)
    app.router.add_get("/api/users/{user_id}/settings", api_user_settings)
    app.router.add_put("/api/users/{user_id}/settings", api_save_user_settings)
    app.router.add_put("/api/users/{user_id}/profile", api_save_user_profile)
    app.router.add_post("/api/users/status", api_update_user_status)
    app.router.add_delete("/api/users/{user_id}", api_delete_user)
    app.router.add_get("/api/cities", api_cities)
    app.router.add_post("/api/cities", api_save_cities)
    app.router.add_post("/api/settings", api_save_settings)
    app.router.add_post("/api/ignored-dates", api_save_ignored_dates)
    app.router.add_post("/api/run-check", api_run_check)
    app.router.add_post("/api/run-login", api_run_login)
    app.router.add_post("/api/monitor/reset-next-check", api_reset_next_check)
    app.router.add_post("/api/stop-process", api_stop_process)
    app.router.add_post("/api/monitor/start", api_monitor_start)
    app.router.add_post("/api/monitor/stop", api_monitor_stop)
    app.router.add_static("/static", STATIC_DIR)
    return app


if __name__ == "__main__":
    port = int(os.getenv("USVISA_WEB_PORT", "8765"))
    web.run_app(create_app(), host="127.0.0.1", port=port)
