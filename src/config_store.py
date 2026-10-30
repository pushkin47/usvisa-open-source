import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_FILE = PROJECT_ROOT / "state/usvisa_web.sqlite3"
SETTINGS_TABLE = "app_settings"
PROFILE_SETTING_KEYS = {
    "USVISA_LOGIN_EMAIL",
    "USVISA_LOGIN_PASSWORD",
    "USVISA_FACILITY_ID",
    "USVISA_IGNORED_DATES",
    "USVISA_PRIORITY_DATES",
    "USVISA_PREPARATION_DAYS",
}


def db_path() -> Path:
    configured = os.getenv("USVISA_WEB_DB_FILE", str(DEFAULT_DB_FILE))
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def read_db_settings() -> dict[str, str]:
    path = db_path()
    if not path.exists():
        return {}

    try:
        with sqlite3.connect(path) as conn:
            rows = conn.execute(f"SELECT key, value FROM {SETTINGS_TABLE}").fetchall()
    except sqlite3.Error:
        return {}

    return {str(key): str(value or "") for key, value in rows}


def read_admin_profile_settings() -> dict[str, str]:
    path = db_path()
    if not path.exists():
        return {}

    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT profiles.embassy_login, profiles.embassy_password,
                       profiles.facility_id, profiles.ignored_dates, profiles.priority_dates, profiles.prep_days
                FROM users
                JOIN user_profiles profiles ON profiles.user_id = users.id
                WHERE users.role = 'admin'
                ORDER BY users.id
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error:
        return {}

    if row is None:
        return {}

    try:
        prep_days = max(1, int(row["prep_days"]))
    except (TypeError, ValueError):
        prep_days = 1

    values = {
        "USVISA_LOGIN_EMAIL": str(row["embassy_login"] or ""),
        "USVISA_LOGIN_PASSWORD": str(row["embassy_password"] or ""),
        "USVISA_FACILITY_ID": "134",
        "USVISA_IGNORED_DATES": str(row["ignored_dates"] or ""),
        "USVISA_PRIORITY_DATES": str(row["priority_dates"] or ""),
        "USVISA_PREPARATION_DAYS": str(prep_days),
        "USVISA_CREDENTIAL_SOURCE": "user_profiles.admin",
    }
    return {key: value for key, value in values.items() if value}


def write_db_settings(values: dict[str, str], secret_keys: set[str] | None = None) -> None:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret_keys = secret_keys or set()
    with sqlite3.connect(path) as conn:
        try:
            path.chmod(0o600)
        except OSError:
            pass
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SETTINGS_TABLE} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                is_secret INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        now = datetime.now(timezone.utc).isoformat()
        for key, value in values.items():
            conn.execute(
                f"""
                INSERT INTO {SETTINGS_TABLE} (key, value, is_secret, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    is_secret = excluded.is_secret,
                    updated_at = excluded.updated_at
                """,
                (key, value, 1 if key in secret_keys else 0, now, now),
            )


def load_config_to_env(override: bool = True) -> dict[str, str]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    settings = read_db_settings()
    for key, value in settings.items():
        if key in PROFILE_SETTING_KEYS and not os.getenv("USVISA_ALLOW_LEGACY_APP_SETTINGS_CREDENTIALS"):
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    if os.getenv("USVISA_PROFILE_CONFIG_ACTIVE") != "true":
        profile_settings = read_admin_profile_settings()
        for key, value in profile_settings.items():
            if override or key not in os.environ or key in PROFILE_SETTING_KEYS or key == "USVISA_CREDENTIAL_SOURCE":
                os.environ[key] = value
    os.environ["USVISA_FACILITY_ID"] = "134"
    return settings

def validate_astana_urls(values: dict[str, str], *, required: bool = False) -> None:
    patterns = {
        "USVISA_APPOINTMENT_URL": r"https://ais\.usvisa-info\.com/(?:ru|en)-kz/niv/schedule/(\d+)/appointment/?",
        "USVISA_DATES_API_URL": r"https://ais\.usvisa-info\.com/(?:ru|en)-kz/niv/schedule/(\d+)/appointment/days/134\.json(?:\?[^#]*)?",
        "USVISA_TIMES_API_URL": r"https://ais\.usvisa-info\.com/(?:ru|en)-kz/niv/schedule/(\d+)/appointment/times/134\.json(?:\?[^#]*)?",
    }
    schedule_ids = set()
    for key, pattern in patterns.items():
        value = values.get(key, "").strip()
        if not value and not required:
            continue
        match = re.fullmatch(pattern, value)
        if not match or match.group(1) == "00000000":
            raise ValueError(f"Set {key} to your own Astana appointment URL (facility 134)")
        schedule_ids.add(match.group(1))
    if len(schedule_ids) > 1:
        raise ValueError("All appointment URLs must refer to the same schedule")
