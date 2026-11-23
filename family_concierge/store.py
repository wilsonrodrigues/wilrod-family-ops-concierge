"""Shared SQLite persistence layer for Family Ops Concierge household, members, calendar, and lists."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from typing import Any, Iterator, Optional
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "family_ops.db"
SEED_DATA_PATH = Path(__file__).resolve().parent / "seed_data.json"

_BACKUP_LOCK = threading.Lock()
_RESTORED_RUNTIME_MAP: dict[str, str] = {}
_INITIALIZED_RUNTIME_PATHS: set[str] = set()


def get_db_path() -> str:
    """Returns the configured SQLite file path for shared household state."""
    return os.environ.get("FAMILY_OPS_DB_PATH", str(DEFAULT_DB_PATH))


def resolve_sqlite_runtime_path(configured_path: str) -> str:
    """Maps GCSFUSE SQLite paths onto fast local `/tmp` storage on Cloud Run (`K_SERVICE`), restoring any backup once."""
    if not configured_path or configured_path == ":memory:":
        return configured_path

    target = Path(configured_path)
    tmp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target

    # Outside Cloud Run (or if already under /tmp), operate directly on configured_path
    if not os.environ.get("K_SERVICE") or str(resolved_target).startswith(str(tmp_root) + os.sep):
        return str(target)

    with _BACKUP_LOCK:
        cached = _RESTORED_RUNTIME_MAP.get(str(target))
        if cached and Path(cached).exists():
            return cached

        runtime_dir = tmp_root / "family_ops_runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(target).encode("utf-8")).hexdigest()[:10]
        runtime_path = runtime_dir / f"{digest}_{target.name}"

        if not runtime_path.exists():
            try:
                if target.is_file() and target.stat().st_size > 0:
                    shutil.copyfile(str(target), str(runtime_path))
            except OSError as exc:
                logger.warning("Could not restore SQLite backup from %s: %s", target, exc)

        _RESTORED_RUNTIME_MAP[str(target)] = str(runtime_path)
        return str(runtime_path)


def sync_sqlite_backup(runtime_path: str, configured_path: str) -> None:
    """Exports a consistent single-file SQLite snapshot from `runtime_path` to `configured_path` (e.g. GCSFUSE)."""
    if (
        not runtime_path
        or not configured_path
        or runtime_path == configured_path
        or runtime_path == ":memory:"
    ):
        return

    runtime_file = Path(runtime_path)
    target_file = Path(configured_path)
    if not runtime_file.is_file():
        return

    with _BACKUP_LOCK:
        snap_tmp = runtime_file.with_name(f"{runtime_file.name}.snap.tmp")
        try:
            src_conn = sqlite3.connect(str(runtime_file), timeout=10.0)
            dst_conn = sqlite3.connect(str(snap_tmp), timeout=10.0)
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(snap_tmp), str(target_file))
        except OSError as exc:
            logger.warning("Could not sync SQLite backup to %s: %s", target_file, exc)
        except sqlite3.Error as exc:
            logger.warning("SQLite snapshot backup error for %s: %s", target_file, exc)
        finally:
            try:
                snap_tmp.unlink(missing_ok=True)
            except OSError:
                pass


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    configured_path = get_db_path()
    runtime_path = resolve_sqlite_runtime_path(configured_path)
    db_path = Path(runtime_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Local /tmp supports fast POSIX WAL; non-staged custom paths still honor FAMILY_SQLITE_JOURNAL_MODE
    if runtime_path != configured_path:
        journal_mode = "WAL"
    else:
        journal_mode = os.environ.get("FAMILY_SQLITE_JOURNAL_MODE", "WAL").strip().upper()
        if journal_mode not in ("WAL", "DELETE", "TRUNCATE", "MEMORY"):
            journal_mode = "WAL"
    conn.execute(f"PRAGMA journal_mode={journal_mode};")
    conn.execute("PRAGMA foreign_keys=ON;")

    changes_before = conn.total_changes
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        mutated = conn.total_changes > changes_before
        conn.close()
        if mutated and runtime_path != configured_path:
            sync_sqlite_backup(runtime_path, configured_path)


def hash_passcode(passcode: str) -> str:
    """Returns SHA-256 hex digest for a household passcode (empty string if unset)."""
    cleaned = passcode.strip()
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


SCHEMA_CLEAN_START_VERSION = 2


def _load_seed_fixture() -> Optional[dict[str, Any]]:
    """Loads synthetic evaluation seed data ONLY when `FAMILY_OPS_LOAD_SEED=1` (e.g. unit tests)."""
    if os.environ.get("FAMILY_OPS_SKIP_SEED") == "1":
        return None
    if os.environ.get("FAMILY_OPS_LOAD_SEED") != "1":
        return None
    if not SEED_DATA_PATH.exists():
        return None
    try:
        return json.loads(SEED_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _serialize_event_row(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalizes a `calendar_events` SQLite row into our clean category-free event record."""
    d = dict(raw)
    d.pop("category", None)
    d["is_all_day"] = bool(d.get("is_all_day", 0))
    return d


def init_db() -> None:
    """Creates household tables if missing, applies column migrations, and loads synthetic seed data on fresh DBs."""
    configured_path = get_db_path()
    runtime_path = resolve_sqlite_runtime_path(configured_path)
    if runtime_path in _INITIALIZED_RUNTIME_PATHS and Path(runtime_path).is_file():
        return

    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS household_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                household_name TEXT NOT NULL DEFAULT 'Foobar Family',
                timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
                preferred_language TEXT NOT NULL DEFAULT 'en',
                passcode_hash TEXT NOT NULL DEFAULT '',
                sports_music_buffer_min INTEGER NOT NULL DEFAULT 30,
                is_configured INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS family_members (
                member_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'Parent',
                avatar TEXT NOT NULL DEFAULT '🙂',
                color TEXT NOT NULL DEFAULT '#1d6f5e',
                email TEXT NOT NULL DEFAULT '',
                routine_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS calendar_events (
                event_id TEXT PRIMARY KEY,
                series_id TEXT NOT NULL DEFAULT '',
                recurrence TEXT NOT NULL DEFAULT 'NONE',
                title TEXT NOT NULL,
                start_iso TEXT NOT NULL,
                end_iso TEXT NOT NULL,
                buffer_start_iso TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                participants TEXT NOT NULL DEFAULT 'All',
                location TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT 'Family',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_items (
                item_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                item_type TEXT NOT NULL CHECK (item_type IN ('NEED_TO_DO', 'WANT_TO_DO')),
                deadline_or_closing_date TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'DONE')),
                assigned_to TEXT NOT NULL DEFAULT 'Anyone',
                created_by TEXT NOT NULL DEFAULT 'Family',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS household_memories (
                memory_id TEXT PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'preference',
                member_name TEXT NOT NULL DEFAULT 'All',
                memory_text TEXT NOT NULL,
                source_session_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Chat',
                created_by TEXT NOT NULL DEFAULT 'Family',
                status TEXT NOT NULL DEFAULT 'idle',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES chat_sessions(session_id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                speaker_name TEXT NOT NULL DEFAULT '',
                speaker_avatar TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                models_json TEXT NOT NULL DEFAULT '[]',
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                pending_confirmations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gcal_integration (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                calendar_link_input TEXT NOT NULL DEFAULT '',
                calendar_id TEXT NOT NULL DEFAULT '',
                calendar_summary TEXT NOT NULL DEFAULT '',
                connected_email TEXT NOT NULL DEFAULT '',
                oauth_client_id TEXT NOT NULL DEFAULT '',
                oauth_client_secret TEXT NOT NULL DEFAULT '',
                oauth_tokens_json TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gcal_oauth_states (
                state_token TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL DEFAULT '',
                redirect_uri TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        # Safe in-place migrations for existing household databases
        existing_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(calendar_events)").fetchall()
        }
        if "location" not in existing_cols:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN location TEXT NOT NULL DEFAULT ''")
        if "series_id" not in existing_cols:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN series_id TEXT NOT NULL DEFAULT ''")
        if "recurrence" not in existing_cols:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'NONE'")
        if "is_all_day" not in existing_cols:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN is_all_day INTEGER NOT NULL DEFAULT 0")

        now_iso = datetime.now(timezone.utc).isoformat()
        seed = _load_seed_fixture()

        if seed is None:
            user_ver_row = conn.execute("PRAGMA user_version").fetchone()
            user_ver = int(user_ver_row[0]) if user_ver_row else 0
            if user_ver < SCHEMA_CLEAN_START_VERSION:
                # One-time clean reset from pre-v2 seeded Foobar Family state to brand-new unconfigured onboarding
                conn.execute("DELETE FROM calendar_events")
                conn.execute("DELETE FROM action_items")
                conn.execute("DELETE FROM household_memories")
                conn.execute("DELETE FROM chat_messages")
                conn.execute("DELETE FROM chat_sessions")
                conn.execute("DELETE FROM family_members")
                conn.execute("DELETE FROM gcal_oauth_states")
                conn.execute("DELETE FROM gcal_integration")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO household_config (
                        id, household_name, timezone, preferred_language, passcode_hash,
                        sports_music_buffer_min, is_configured, updated_at
                    ) VALUES (1, '', 'America/Sao_Paulo', 'en', '', 30, 0, ?)
                    """,
                    (now_iso,),
                )
                conn.execute(f"PRAGMA user_version = {SCHEMA_CLEAN_START_VERSION}")
                _INITIALIZED_RUNTIME_PATHS.add(runtime_path)
                return

        row = conn.execute("SELECT id FROM household_config WHERE id = 1").fetchone()
        if row is not None:
            _INITIALIZED_RUNTIME_PATHS.add(runtime_path)
            return

        if seed:
            hh = seed.get("household", {})
            conn.execute(
                """
                INSERT INTO household_config (
                    id, household_name, timezone, preferred_language, passcode_hash,
                    sports_music_buffer_min, is_configured, updated_at
                ) VALUES (1, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    hh.get("household_name", "Foobar Family"),
                    hh.get("timezone", "America/Sao_Paulo"),
                    hh.get("preferred_language", "en"),
                    int(hh.get("sports_music_buffer_min", 30)),
                    1 if hh.get("is_configured", True) else 0,
                    hh.get("updated_at", now_iso),
                ),
            )
            for m in seed.get("members", []):
                conn.execute(
                    """
                    INSERT INTO family_members (
                        member_id, name, role, avatar, color, email, routine_notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m["member_id"],
                        m["name"],
                        m.get("role", "Parent"),
                        m.get("avatar", "🙂"),
                        m.get("color", "#1b6b5a"),
                        m.get("email", ""),
                        m.get("routine_notes", ""),
                        m.get("created_at", now_iso),
                    ),
                )
            for ev in seed.get("events", []):
                conn.execute(
                    """
                    INSERT INTO calendar_events (
                        event_id, series_id, recurrence, title, start_iso, end_iso, buffer_start_iso,
                        is_all_day, category, participants, location, created_by, notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev["event_id"],
                        ev.get("series_id", ""),
                        ev.get("recurrence", "NONE"),
                        ev["title"],
                        ev["start_iso"],
                        ev["end_iso"],
                        ev["buffer_start_iso"],
                        1 if ev.get("is_all_day") else 0,
                        "",
                        ev.get("participants", "All"),
                        ev.get("location", ""),
                        ev.get("created_by", "Family"),
                        ev.get("notes", ""),
                        ev.get("created_at", now_iso),
                    ),
                )
            for item in seed.get("items", []):
                conn.execute(
                    """
                    INSERT INTO action_items (
                        item_id, title, item_type, deadline_or_closing_date, status, assigned_to, created_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["item_id"],
                        item["title"],
                        item["item_type"],
                        item.get("deadline_or_closing_date"),
                        item.get("status", "OPEN"),
                        item.get("assigned_to", "Anyone"),
                        item.get("created_by", "Family"),
                        item.get("created_at", now_iso),
                    ),
                )
            for mem in seed.get("memories", []):
                conn.execute(
                    """
                    INSERT INTO household_memories (
                        memory_id, category, member_name, memory_text, source_session_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mem["memory_id"],
                        mem.get("category", "routine"),
                        mem.get("member_name", "All"),
                        mem["memory_text"],
                        mem.get("source_session_id", "seed"),
                        mem.get("updated_at", now_iso),
                    ),
                )
            _INITIALIZED_RUNTIME_PATHS.add(runtime_path)
            return

        # Clean empty bootstrap for brand-new households (no seed data)
        conn.execute(
            """
            INSERT INTO household_config (
                id, household_name, timezone, preferred_language, passcode_hash,
                sports_music_buffer_min, is_configured, updated_at
            ) VALUES (1, '', 'America/Sao_Paulo', 'en', '', 30, 0, ?)
            """,
            (now_iso,),
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_CLEAN_START_VERSION}")
        _INITIALIZED_RUNTIME_PATHS.add(runtime_path)


def reset_household_to_new_user() -> dict[str, Any]:
    """Wipes household data back to a clean first-time onboarding state."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM calendar_events")
        conn.execute("DELETE FROM action_items")
        conn.execute("DELETE FROM household_memories")
        conn.execute("DELETE FROM chat_messages")
        conn.execute("DELETE FROM chat_sessions")
        conn.execute("DELETE FROM family_members")
        conn.execute("DELETE FROM gcal_oauth_states")
        conn.execute("DELETE FROM gcal_integration")
        conn.execute(
            """
            UPDATE household_config
            SET household_name = '',
                timezone = 'America/Sao_Paulo',
                preferred_language = 'en',
                passcode_hash = '',
                sports_music_buffer_min = 30,
                is_configured = 0,
                updated_at = ?
            WHERE id = 1
            """,
            (now_iso,),
        )
    return get_household_bundle()


def get_household_bundle() -> dict[str, Any]:
    """Returns household settings (without passcode_hash) and ordered family members."""
    init_db()
    with _connect() as conn:
        cfg = dict(conn.execute("SELECT * FROM household_config WHERE id = 1").fetchone())
        has_passcode = bool(cfg.pop("passcode_hash", ""))
        cfg["has_passcode"] = has_passcode
        cfg["is_configured"] = bool(cfg["is_configured"])
        members = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM family_members ORDER BY CASE role WHEN 'Parent' THEN 1 WHEN 'Teen' THEN 2 WHEN 'Child' THEN 3 ELSE 4 END, created_at ASC"
            ).fetchall()
        ]
    return {"household": cfg, "members": members}


def verify_household_passcode(candidate: str) -> bool:
    """Checks candidate passcode against household_config (returns True if no passcode is set)."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT passcode_hash FROM household_config WHERE id = 1").fetchone()
        stored = row["passcode_hash"] if row else ""
    if not stored:
        return True
    return hash_passcode(candidate or "") == stored


def save_household_setup(
    household_name: str,
    timezone_str: str,
    preferred_language: str,
    sports_music_buffer_min: int,
    members: list[dict[str, Any]],
    new_passcode: Optional[str] = None,
    clear_passcode: bool = False,
) -> dict[str, Any]:
    """Persists complete household configuration and replaces/updates member roster."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        existing = conn.execute("SELECT passcode_hash FROM household_config WHERE id = 1").fetchone()
        passcode_hash = existing["passcode_hash"] if existing else ""
        if clear_passcode:
            passcode_hash = ""
        elif new_passcode is not None and new_passcode.strip():
            passcode_hash = hash_passcode(new_passcode)

        conn.execute(
            """
            UPDATE household_config
            SET household_name = ?,
                timezone = ?,
                preferred_language = ?,
                passcode_hash = ?,
                sports_music_buffer_min = ?,
                is_configured = 1,
                updated_at = ?
            WHERE id = 1
            """,
            (
                household_name.strip(),
                timezone_str.strip() or "America/Sao_Paulo",
                preferred_language.strip() or "en",
                passcode_hash,
                int(sports_music_buffer_min),
                now_iso,
            ),
        )

        if members:
            conn.execute("DELETE FROM family_members")
            for m in members:
                m_id = (m.get("member_id") or "").strip() or f"mem-{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO family_members (
                        member_id, name, role, avatar, color, email, routine_notes, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m_id,
                        m["name"].strip(),
                        (m.get("role") or "Parent").strip(),
                        (m.get("avatar") or "🙂").strip(),
                        (m.get("color") or "#1b6b5a").strip(),
                        (m.get("email") or "").strip(),
                        (m.get("routine_notes") or "").strip(),
                        now_iso,
                    ),
                )
    return get_household_bundle()


def upsert_member_row(
    name: str,
    role: str = "Parent",
    avatar: str = "🙂",
    color: str = "#1b6b5a",
    email: str = "",
    routine_notes: str = "",
    member_id: Optional[str] = None,
) -> dict[str, Any]:
    """Inserts or updates a single family member record."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        target = None
        if member_id:
            target = conn.execute("SELECT * FROM family_members WHERE member_id = ?", (member_id,)).fetchone()
        if target is None:
            target = conn.execute("SELECT * FROM family_members WHERE LOWER(name) = LOWER(?)", (name.strip(),)).fetchone()

        if target is not None:
            m_id = target["member_id"]
            conn.execute(
                """
                UPDATE family_members
                SET name = ?, role = ?, avatar = ?, color = ?, email = ?, routine_notes = ?
                WHERE member_id = ?
                """,
                (name.strip(), role, avatar, color, email.strip(), routine_notes.strip(), m_id),
            )
        else:
            m_id = f"mem-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO family_members (
                    member_id, name, role, avatar, color, email, routine_notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (m_id, name.strip(), role, avatar, color, email.strip(), routine_notes.strip(), now_iso),
            )
        conn.execute("UPDATE household_config SET is_configured = 1, updated_at = ? WHERE id = 1", (now_iso,))
        saved = dict(conn.execute("SELECT * FROM family_members WHERE member_id = ?", (m_id,)).fetchone())
    return saved


def query_events(
    date_filter: Optional[str] = None,
    participant_filter: Optional[str] = None,
    include_past: bool = False,
) -> list[dict[str, Any]]:
    """Queries calendar events ordered chronologically (upcoming from today onwards unless `date_filter` or `include_past` is set)."""
    init_db()
    with _connect() as conn:
        tz_row = conn.execute("SELECT timezone FROM household_config WHERE id = 1").fetchone()
        tz_name = str(tz_row["timezone"]) if tz_row and tz_row["timezone"] else "America/Sao_Paulo"
        try:
            today_str = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
        except ZoneInfoNotFoundError:
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        sql = "SELECT * FROM calendar_events WHERE 1=1"
        params: list[Any] = []
        if date_filter:
            sql += " AND start_iso LIKE ?"
            params.append(f"{date_filter}%")
        elif not include_past:
            sql += " AND SUBSTR(end_iso, 1, 10) >= ?"
            params.append(today_str)
        if participant_filter and participant_filter.lower() not in ("all", "whole family"):
            sql += " AND (LOWER(participants) LIKE ? OR LOWER(participants) IN ('all', 'whole family'))"
            params.append(f"%{participant_filter.lower()}%")
        sql += " ORDER BY start_iso ASC"
        return [_serialize_event_row(dict(r)) for r in conn.execute(sql, params).fetchall()]


def insert_events_batch(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Inserts one or more validated calendar events atomically in a single SQLite transaction."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for ev in events:
        records.append(
            {
                "event_id": ev.get("event_id") or f"evt-{uuid.uuid4().hex[:8]}",
                "series_id": ev.get("series_id", ""),
                "recurrence": ev.get("recurrence", "NONE"),
                "title": ev["title"],
                "start_iso": ev["start_iso"],
                "end_iso": ev["end_iso"],
                "buffer_start_iso": ev["buffer_start_iso"],
                "is_all_day": 1 if ev.get("is_all_day") else 0,
                "category": "",
                "participants": ev.get("participants", "All"),
                "location": ev.get("location", ""),
                "created_by": ev.get("created_by", "Family"),
                "notes": ev.get("notes", ""),
                "created_at": now_iso,
            }
        )
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO calendar_events (
                event_id, series_id, recurrence, title, start_iso, end_iso, buffer_start_iso,
                is_all_day, category, participants, location, created_by, notes, created_at
            ) VALUES (
                :event_id, :series_id, :recurrence, :title, :start_iso, :end_iso, :buffer_start_iso,
                :is_all_day, :category, :participants, :location, :created_by, :notes, :created_at
            )
            """,
            records,
        )
    return [_serialize_event_row(r) for r in records]


def delete_event_by_id(event_id: str, delete_series: bool = False) -> Optional[dict[str, Any]]:
    """Deletes a single calendar event or its entire recurring series if `delete_series=True`."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM calendar_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return None
        target = _serialize_event_row(dict(row))
        if delete_series and target.get("series_id"):
            cur = conn.execute("DELETE FROM calendar_events WHERE series_id = ?", (target["series_id"],))
            target["deleted_count"] = cur.rowcount
        else:
            conn.execute("DELETE FROM calendar_events WHERE event_id = ?", (event_id,))
            target["deleted_count"] = 1
        return target


def query_action_items(item_type: Optional[str] = None, include_completed: bool = False) -> list[dict[str, Any]]:
    """Queries Need-To-Do errands and Want-To-Do wish-list items ordered by deadline."""
    init_db()
    sql = "SELECT * FROM action_items WHERE 1=1"
    params: list[Any] = []
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)
    if not include_completed:
        sql += " AND status = 'OPEN'"
    sql += " ORDER BY COALESCE(NULLIF(deadline_or_closing_date, ''), '9999-12-31') ASC, created_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def upsert_action_item_row(
    title: str,
    item_type: str,
    deadline_or_closing_date: Optional[str] = None,
    status: str = "OPEN",
    assigned_to: str = "Anyone",
    created_by: str = "Family",
    item_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Inserts or updates an action item in SQLite."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        if item_id:
            existing = conn.execute("SELECT * FROM action_items WHERE item_id = ?", (item_id,)).fetchone()
            if existing is None:
                raise KeyError(item_id)
            conn.execute(
                """
                UPDATE action_items
                SET title = ?, item_type = ?, deadline_or_closing_date = ?, status = ?, assigned_to = ?
                WHERE item_id = ?
                """,
                (title, item_type, deadline_or_closing_date, status, assigned_to, item_id),
            )
            saved = dict(conn.execute("SELECT * FROM action_items WHERE item_id = ?", (item_id,)).fetchone())
            return "updated", saved

        new_id = f"act-{uuid.uuid4().hex[:8]}"
        conn.execute(
            """
            INSERT INTO action_items (
                item_id, title, item_type, deadline_or_closing_date, status, assigned_to, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id, title, item_type, deadline_or_closing_date, status, assigned_to, created_by, now_iso),
        )
        saved = dict(conn.execute("SELECT * FROM action_items WHERE item_id = ?", (new_id,)).fetchone())
        return "created", saved


def toggle_or_delete_action_item(item_id: str, delete: bool = False, status: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Updates status or deletes an action item by ID."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM action_items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        if delete:
            conn.execute("DELETE FROM action_items WHERE item_id = ?", (item_id,))
            return dict(row)
        next_status = status or ("DONE" if row["status"] == "OPEN" else "OPEN")
        conn.execute("UPDATE action_items SET status = ? WHERE item_id = ?", (next_status, item_id))
        return dict(conn.execute("SELECT * FROM action_items WHERE item_id = ?", (item_id,)).fetchone())


def upsert_household_memory(
    memory_text: str,
    category: str = "preference",
    member_name: str = "All",
    source_session_id: str = "",
) -> dict[str, Any]:
    """Scrubs PII and inserts or refreshes a distilled long-term household memory fact in SQLite."""
    from .pii_redactor import default_pii_redactor

    init_db()
    cleaned_text = default_pii_redactor.scrub_text(" ".join(memory_text.strip().split()))
    if not cleaned_text:
        raise ValueError("memory_text must not be empty.")
    cleaned_member = default_pii_redactor.scrub_text((member_name or "All").strip()) or "All"
    cleaned_cat = (category or "preference").strip().lower() or "preference"
    now_iso = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT * FROM household_memories
            WHERE LOWER(member_name) = LOWER(?) AND LOWER(memory_text) = LOWER(?)
            """,
            (cleaned_member, cleaned_text),
        ).fetchone()
        if existing is not None:
            mem_id = existing["memory_id"]
            conn.execute(
                """
                UPDATE household_memories
                SET category = ?, source_session_id = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (cleaned_cat, source_session_id, now_iso, mem_id),
            )
        else:
            mem_id = f"memfact-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO household_memories (
                    memory_id, category, member_name, memory_text, source_session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mem_id, cleaned_cat, cleaned_member, cleaned_text, source_session_id, now_iso),
            )
        saved = dict(
            conn.execute(
                "SELECT * FROM household_memories WHERE memory_id = ?",
                (mem_id,),
            ).fetchone()
        )
    return saved


def query_household_memories(
    query: Optional[str] = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Queries distilled long-term household memories ordered by recency."""
    init_db()
    safe_limit = max(1, min(int(limit), 50))
    with _connect() as conn:
        if query and query.strip():
            tokens = [tok.lower() for tok in query.strip().split() if len(tok) >= 2]
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM household_memories ORDER BY updated_at DESC LIMIT 100"
                ).fetchall()
            ]
            if not tokens:
                return rows[:safe_limit]
            scored: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                haystack = f"{row['member_name']} {row['category']} {row['memory_text']}".lower()
                hits = sum(1 for tok in tokens if tok in haystack)
                if hits > 0:
                    scored.append((hits, row))
            scored.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
            return [r for _, r in scored[:safe_limit]]

        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM household_memories ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        ]


def delete_household_memory(memory_id: str) -> bool:
    """Deletes a single household memory entry by ID."""
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM household_memories WHERE memory_id = ?",
            (memory_id,),
        )
        return cur.rowcount > 0


def _derive_session_title(raw_text: str) -> str:
    """Builds a concise human-readable chat session title from the first prompt."""
    cleaned = " ".join((raw_text or "").strip().split())
    if not cleaned:
        return "New Chat"
    return cleaned if len(cleaned) <= 46 else f"{cleaned[:43].rstrip()}..."


def ensure_chat_session(
    session_id: str,
    *,
    first_prompt: Optional[str] = None,
    created_by: str = "Family",
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Creates a `chat_sessions` row if missing or updates its title/status/timestamp."""
    init_db()
    sid = session_id.strip()
    if not sid:
        raise ValueError("session_id must not be empty.")
    now_iso = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ?",
            (sid,),
        ).fetchone()

        if existing is None:
            initial_title = _derive_session_title(first_prompt or "")
            conn.execute(
                """
                INSERT INTO chat_sessions (session_id, title, created_by, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    initial_title,
                    (created_by or "Family").strip() or "Family",
                    status or "idle",
                    now_iso,
                    now_iso,
                ),
            )
        else:
            next_title = existing["title"]
            if next_title == "New Chat" and first_prompt and first_prompt.strip():
                next_title = _derive_session_title(first_prompt)
            next_status = status if status is not None else existing["status"]
            conn.execute(
                """
                UPDATE chat_sessions
                SET title = ?, status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (next_title, next_status, now_iso, sid),
            )

        saved = dict(
            conn.execute("SELECT * FROM chat_sessions WHERE session_id = ?", (sid,)).fetchone()
        )
    return saved


def set_chat_session_status(session_id: str, status: str) -> None:
    """Updates `status` ('idle', 'running', or 'awaiting_approval') on a chat session."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE chat_sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (status, now_iso, session_id),
        )


def append_chat_message(
    session_id: str,
    *,
    role: str,
    content: str,
    speaker_name: str = "",
    speaker_avatar: str = "",
    attachments_meta: Optional[list[dict[str, Any]]] = None,
    models_used: Optional[list[str]] = None,
    tool_calls: Optional[list[str]] = None,
    pending_confirmations: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Appends a user or assistant message to `chat_messages` and bumps session `updated_at`."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    msg_id = f"cmsg-{uuid.uuid4().hex[:10]}"
    ensure_chat_session(
        session_id,
        first_prompt=content if role == "user" else None,
        created_by=speaker_name or "Family",
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO chat_messages (
                message_id, session_id, role, speaker_name, speaker_avatar, content,
                attachments_json, models_json, tool_calls_json, pending_confirmations_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                session_id,
                role,
                speaker_name,
                speaker_avatar,
                content,
                json.dumps(attachments_meta or [], ensure_ascii=False),
                json.dumps(models_used or [], ensure_ascii=False),
                json.dumps(tool_calls or [], ensure_ascii=False),
                json.dumps(pending_confirmations or [], ensure_ascii=False),
                now_iso,
            ),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
            (now_iso, session_id),
        )
        row = dict(
            conn.execute("SELECT * FROM chat_messages WHERE message_id = ?", (msg_id,)).fetchone()
        )
    return _deserialize_chat_message_row(row)


def resolve_message_confirmation(
    session_id: str,
    confirmation_call_id: str,
    resolution_label: str,
) -> None:
    """Marks a pending HITL confirmation card inside `chat_messages` as resolved."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT message_id, pending_confirmations_json FROM chat_messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ).fetchall()
        for r in rows:
            try:
                confs = json.loads(r["pending_confirmations_json"] or "[]")
            except json.JSONDecodeError:
                confs = []
            changed = False
            for c in confs:
                if c.get("confirmation_call_id") == confirmation_call_id:
                    c["resolved"] = True
                    c["resolution_label"] = resolution_label
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE chat_messages SET pending_confirmations_json = ? WHERE message_id = ?",
                    (json.dumps(confs, ensure_ascii=False), r["message_id"]),
                )


def _deserialize_chat_message_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "speaker_name": row["speaker_name"],
        "speaker_avatar": row["speaker_avatar"],
        "content": row["content"],
        "attachments": json.loads(row.get("attachments_json") or "[]"),
        "models_used": json.loads(row.get("models_json") or "[]"),
        "tool_calls": json.loads(row.get("tool_calls_json") or "[]"),
        "pending_confirmations": json.loads(row.get("pending_confirmations_json") or "[]"),
        "created_at": row["created_at"],
    }


def list_chat_sessions(limit: int = 40) -> list[dict[str, Any]]:
    """Lists chat sessions ordered by most recent activity with message counts and previews."""
    init_db()
    safe_limit = max(1, min(int(limit), 100))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                s.session_id,
                s.title,
                s.created_by,
                s.status,
                s.created_at,
                s.updated_at,
                COUNT(m.message_id) AS message_count,
                COALESCE(
                    (
                        SELECT m2.content
                        FROM chat_messages m2
                        WHERE m2.session_id = s.session_id
                        ORDER BY m2.created_at DESC, m2.rowid DESC
                        LIMIT 1
                    ),
                    ''
                ) AS last_preview
            FROM chat_sessions s
            LEFT JOIN chat_messages m ON m.session_id = s.session_id
            GROUP BY s.session_id
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        preview = " ".join((d.get("last_preview") or "").split())
        d["last_preview"] = preview if len(preview) <= 72 else f"{preview[:69]}..."
        result.append(d)
    return result


def get_chat_session_detail(session_id: str) -> Optional[dict[str, Any]]:
    """Returns session metadata and full chronological message transcript for `session_id`."""
    init_db()
    with _connect() as conn:
        s_row = conn.execute(
            "SELECT * FROM chat_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if s_row is None:
            return None
        m_rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        ).fetchall()
    return {
        "session": dict(s_row),
        "messages": [_deserialize_chat_message_row(dict(r)) for r in m_rows],
    }


def delete_chat_session_record(session_id: str) -> bool:
    """Deletes a chat session and all its messages from SQLite."""
    init_db()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0


def get_gcal_integration_row() -> dict[str, Any]:
    """Returns internal Google Calendar integration config row (contains secrets - never expose raw)."""
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM gcal_integration WHERE id = 1").fetchone()
        if row is None:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO gcal_integration (id, enabled, updated_at) VALUES (1, 0, ?)",
                (now_iso,),
            )
            row = conn.execute("SELECT * FROM gcal_integration WHERE id = 1").fetchone()
        return dict(row)  # type: ignore[arg-type]


def save_gcal_integration_row(**updates: Any) -> dict[str, Any]:
    """Updates one or more columns on `gcal_integration` (`id = 1`) and returns the updated row."""
    current = get_gcal_integration_row()
    merged = {**current, **updates, "updated_at": datetime.now(timezone.utc).isoformat()}
    with _connect() as conn:
        conn.execute(
            """
            UPDATE gcal_integration
            SET enabled = ?,
                calendar_link_input = ?,
                calendar_id = ?,
                calendar_summary = ?,
                connected_email = ?,
                oauth_client_id = ?,
                oauth_client_secret = ?,
                oauth_tokens_json = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                1 if merged.get("enabled") else 0,
                str(merged.get("calendar_link_input") or ""),
                str(merged.get("calendar_id") or ""),
                str(merged.get("calendar_summary") or ""),
                str(merged.get("connected_email") or ""),
                str(merged.get("oauth_client_id") or ""),
                str(merged.get("oauth_client_secret") or ""),
                str(merged.get("oauth_tokens_json") or ""),
                merged["updated_at"],
            ),
        )
    return get_gcal_integration_row()


def save_oauth_state(state_token: str, code_verifier: str, redirect_uri: str) -> None:
    """Stores a one-time OAuth 2.0 CSRF `state_token` and PKCE `code_verifier`."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        # Keep table bounded by pruning older state rows beyond the 20 most recent
        conn.execute(
            """
            DELETE FROM gcal_oauth_states
            WHERE state_token NOT IN (
                SELECT state_token FROM gcal_oauth_states ORDER BY created_at DESC LIMIT 20
            )
            """
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO gcal_oauth_states (state_token, code_verifier, redirect_uri, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (state_token, code_verifier, redirect_uri, now_iso),
        )


def pop_oauth_state(state_token: str) -> Optional[dict[str, str]]:
    """Atomically retrieves and deletes a stored OAuth 2.0 CSRF + PKCE state row."""
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT state_token, code_verifier, redirect_uri FROM gcal_oauth_states WHERE state_token = ?",
            (state_token,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM gcal_oauth_states WHERE state_token = ?", (state_token,))
        return dict(row)



