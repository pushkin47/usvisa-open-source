import json
import sqlite3
from pathlib import Path
from typing import Any

from config_store import db_path


MONITOR_STATE_KEY = "latest"
MAX_MONITOR_RUN_ROWS = 5000
MAX_ERROR_QUEUE_ROWS = 1000


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_tables() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitor_state (
                key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitor_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error_category TEXT NOT NULL DEFAULT '',
                error_type TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitor_error_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                error_type TEXT NOT NULL,
                message TEXT NOT NULL,
                event_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _json_load(value: str, fallback: Any) -> Any:
    try:
        payload = json.loads(value)
    except Exception:
        return fallback
    return payload if isinstance(payload, type(fallback)) else fallback


def write_snapshot(payload: dict[str, Any], updated_at: str) -> None:
    ensure_tables()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_state (key, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (MONITOR_STATE_KEY, _json_dump(payload), updated_at),
        )


def read_snapshot() -> dict[str, Any]:
    ensure_tables()
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM monitor_state WHERE key = ?",
            (MONITOR_STATE_KEY,),
        ).fetchone()
    if row is None:
        return {}
    return _json_load(str(row["payload_json"] or "{}"), {})


def append_monitor_run(event: dict[str, Any]) -> None:
    ensure_tables()
    occurred_at = str(event.get("at") or "")
    status = str(event.get("status") or "")
    error_category = str(event.get("error_category") or "")
    error_type = str(event.get("error_type") or "")
    message = str(event.get("message") or "")
    if not occurred_at or not status:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_runs (
                occurred_at, status, error_category, error_type, message, event_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (occurred_at, status, error_category, error_type, message, _json_dump(event), occurred_at),
        )
        conn.execute(
            f"""
            DELETE FROM monitor_runs
            WHERE id NOT IN (
                SELECT id FROM monitor_runs
                ORDER BY occurred_at DESC, id DESC
                LIMIT {MAX_MONITOR_RUN_ROWS}
            )
            """
        )


def read_monitor_runs(limit: int = 1000) -> list[dict[str, Any]]:
    ensure_tables()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT event_json
            FROM monitor_runs
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    events = [_json_load(str(row["event_json"] or "{}"), {}) for row in rows]
    events = [event for event in events if isinstance(event, dict)]
    events.reverse()
    return events


def append_error_event(event: dict[str, str]) -> None:
    ensure_tables()
    occurred_at = str(event.get("at") or "")
    error_type = str(event.get("type") or "")
    message = str(event.get("message") or "")
    if not occurred_at or not error_type or not message:
        return
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO monitor_error_queue (
                occurred_at, error_type, message, event_json, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (occurred_at, error_type, message, _json_dump(event), occurred_at),
        )
        conn.execute(
            f"""
            DELETE FROM monitor_error_queue
            WHERE id NOT IN (
                SELECT id FROM monitor_error_queue
                ORDER BY occurred_at DESC, id DESC
                LIMIT {MAX_ERROR_QUEUE_ROWS}
            )
            """
        )


def read_error_events(limit: int = 20) -> list[dict[str, str]]:
    ensure_tables()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT event_json
            FROM monitor_error_queue
            ORDER BY occurred_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    events = [_json_load(str(row["event_json"] or "{}"), {}) for row in rows]
    events = [event for event in events if isinstance(event, dict)]
    events.reverse()
    return events


def clear_error_events() -> None:
    ensure_tables()
    with _connect() as conn:
        conn.execute("DELETE FROM monitor_error_queue")


def sync_legacy_state_if_needed(state_file: Path, error_state_file: Path) -> None:
    ensure_tables()
    with _connect() as conn:
        snapshot_exists = conn.execute(
            "SELECT 1 FROM monitor_state WHERE key = ? LIMIT 1",
            (MONITOR_STATE_KEY,),
        ).fetchone() is not None
        runs_exist = conn.execute("SELECT 1 FROM monitor_runs LIMIT 1").fetchone() is not None
        error_queue_exists = conn.execute("SELECT 1 FROM monitor_error_queue LIMIT 1").fetchone() is not None

    raw_state: dict[str, Any] = {}
    raw_error_state: dict[str, Any] = {}

    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            raw_state = loaded if isinstance(loaded, dict) else {}
        except Exception:
            raw_state = {}

    if error_state_file.exists():
        try:
            loaded = json.loads(error_state_file.read_text(encoding="utf-8"))
            raw_error_state = loaded if isinstance(loaded, dict) else {}
        except Exception:
            raw_error_state = {}

    if not snapshot_exists and raw_state:
        updated_at = str(raw_state.get("next_check_at") or raw_state.get("last_slot_notification_at") or raw_state.get("last_healthcheck_at") or "")
        write_snapshot(raw_state, updated_at or "")

    if not runs_exist:
        for event in raw_state.get("monitor_events") or []:
            if isinstance(event, dict):
                append_monitor_run(event)

    if not error_queue_exists:
        raw_error_events = raw_error_state.get("error_events", raw_state.get("error_events"))
        for event in raw_error_events or []:
            if isinstance(event, dict):
                append_error_event(event)
