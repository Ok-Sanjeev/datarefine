"""
Production-Ready Persistent Session Store for DataRefine.

Provides:
1. Multi-worker safe, process-isolated session storage using SQLite in WAL mode.
2. Per-session on-disk filesystem isolation for DataFrames (compressed Parquet).
3. Server-restart resilience (active sessions survive uvicorn restarts / worker recycling).
4. Cryptographic session tokens and ownership validation.
5. Automated file and abandoned session cleanup with strict directory jail security.
6. Absolute filesystem path concealment from end-user APIs.
7. Seamless dict-like interface for 100% backward compatibility with existing routes and tests.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Union

import pandas as pd


# Configurable Storage and Retention Constants
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SESSIONS_DIR = (BASE_DIR / "data" / "sessions").resolve()
DEFAULT_UPLOADS_DIR = (BASE_DIR / "data" / "uploads").resolve()
DEFAULT_RUNS_DIR = (BASE_DIR / "runs").resolve()


def get_session_storage_dir() -> Path:
    raw = os.getenv("SESSION_STORAGE_DIR")
    path = Path(raw).resolve() if raw else DEFAULT_SESSIONS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_uploads_storage_dir() -> Path:
    raw = os.getenv("UPLOAD_STORAGE_DIR")
    path = Path(raw).resolve() if raw else DEFAULT_UPLOADS_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_session_max_age_seconds() -> int:
    return int(os.getenv("SESSION_MAX_AGE_SECONDS", str(4 * 3600)))  # 4 hours


def get_file_retention_hours() -> int:
    return int(os.getenv("FILE_RETENTION_HOURS", "24"))  # 24 hours


def conceal_absolute_path(path: Union[str, Path, None]) -> Optional[str]:
    """
    Conceal absolute server filesystem paths from client API responses.
    Converts /home/.../data/uploads/sample.csv or C:\\Users\\...\\data\\benchmark\\dirty.csv
    into a safe, portable relative identifier (e.g. 'uploads/sample.csv' or 'benchmark/dirty.csv').
    """
    if not path:
        return None
    try:
        resolved = Path(str(path)).resolve()
        # Check if inside project base directory
        try:
            rel = resolved.relative_to(BASE_DIR)
            return str(rel).replace("\\", "/")
        except ValueError:
            # Outside project directory; return basename only
            return resolved.name
    except Exception:
        return Path(str(path)).name


class PersistentSessionStore:
    """
    Production-grade session manager backed by SQLite (WAL mode) and on-disk Parquet files.
    Implements a dict-like interface so existing code using SESSIONS[session_id]
    functions seamlessly while gaining persistence across restarts and workers.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        storage_dir: Optional[Path] = None,
        uploads_dir: Optional[Path] = None,
    ):
        self.storage_dir = (storage_dir or get_session_storage_dir()).resolve()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir = (uploads_dir or get_uploads_storage_dir()).resolve()
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = (db_path or (self.storage_dir / "sessions_catalog.db")).resolve()
        self._lock = RLock()
        self._memory_cache: Dict[str, Dict[str, Any]] = {}

        self._init_sqlite()

    def _get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.row_factory = sqlite3.Row
        return con

    def _init_sqlite(self) -> None:
        with self._lock, self._get_connection() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    owner_id TEXT DEFAULT 'anonymous',
                    created_at REAL NOT NULL,
                    last_accessed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT DEFAULT 'active',
                    filepath TEXT,
                    dataset_name TEXT,
                    row_count INTEGER DEFAULT 0,
                    col_count INTEGER DEFAULT 0,
                    storage_path TEXT NOT NULL,
                    serialized_meta TEXT
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_last_accessed ON sessions(last_accessed_at);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id);")

            # Schema migration checks if table existed previously with session_token
            cur = con.execute("PRAGMA table_info(sessions);")
            cols = {row["name"] for row in cur.fetchall()}
            if "token_hash" not in cols:
                con.execute("ALTER TABLE sessions ADD COLUMN token_hash TEXT DEFAULT '';")
            if "version" not in cols:
                con.execute("ALTER TABLE sessions ADD COLUMN version INTEGER DEFAULT 1;")

    def _session_dir(self, session_id: str) -> Path:
        sdir = self.storage_dir / session_id
        sdir.mkdir(parents=True, exist_ok=True)
        return sdir

    # =========================================================================
    # CORE SESSION CRUD
    # =========================================================================

    @staticmethod
    def _is_safe_subpath(target_path: Union[str, Path], jail_dir: Union[str, Path]) -> bool:
        """Verify that target_path resolves strictly inside jail_dir and is not the jail itself."""
        try:
            resolved_target = Path(target_path).resolve()
            resolved_jail = Path(jail_dir).resolve()
            rel = resolved_target.relative_to(resolved_jail)
            return len(rel.parts) > 0 and not str(rel).startswith("..")
        except (ValueError, Exception):
            return False

    def create_session(
        self,
        session_id: str,
        df: pd.DataFrame,
        filepath: str,
        profile: Dict[str, Any],
        cleaning_session: Any = None,
        session_token: Optional[str] = None,
        owner_id: str = "anonymous",
        run_id: Optional[str] = None,
        run_dir: Optional[str] = None,
        max_age_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Initialize, persist to disk, and register a new session."""
        now = time.time()
        max_age = max_age_seconds if max_age_seconds is not None else get_session_max_age_seconds()
        expires_at = now + max_age
        token = session_token or secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        sdir = self._session_dir(session_id)

        # 1. Persist initial and current DataFrames as Parquet
        orig_pq = sdir / "original.parquet"
        curr_pq = sdir / "current.parquet"
        df.to_parquet(orig_pq, index=False)
        df.to_parquet(curr_pq, index=False)

        # 2. Persist cleaning session checkpoint state
        cp_dir = sdir / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        if cleaning_session is None:
            from session import CleaningSession
            cleaning_session = CleaningSession(df.copy(deep=True))
        if hasattr(cleaning_session, "save_to_directory"):
            cleaning_session.save_to_directory(sdir)

        # 3. Assemble metadata
        meta = {
            "run_id": run_id or f"run_{session_id[:8]}",
            "run_dir": run_dir or str(sdir),
            "filepath": filepath,
            "dataset_name": Path(filepath).name,
            "profile": profile,
            "plan": None,
            "executed_actions": [],
            "report": None,
        }

        # 4. Insert into SQLite catalog (storing SHA-256 token_hash, never plaintext)
        with self._lock, self._get_connection() as con:
            cur = con.execute("PRAGMA table_info(sessions);")
            cols = {r["name"] for r in cur.fetchall()}
            if "session_token" in cols:
                con.execute(
                    """
                    INSERT OR REPLACE INTO sessions (
                        session_id, session_token, token_hash, version, owner_id, created_at, last_accessed_at,
                        expires_at, status, filepath, dataset_name, row_count, col_count,
                        storage_path, serialized_meta
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        token_hash,  # store SHA-256 hash in legacy column too: zero plaintext on disk
                        token_hash,
                        1,
                        owner_id,
                        now,
                        now,
                        expires_at,
                        "active",
                        filepath,
                        Path(filepath).name,
                        len(df),
                        len(df.columns),
                        str(sdir),
                        json.dumps(meta, default=str),
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT OR REPLACE INTO sessions (
                        session_id, token_hash, version, owner_id, created_at, last_accessed_at,
                        expires_at, status, filepath, dataset_name, row_count, col_count,
                        storage_path, serialized_meta
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        token_hash,
                        1,
                        owner_id,
                        now,
                        now,
                        expires_at,
                        "active",
                        filepath,
                        Path(filepath).name,
                        len(df),
                        len(df.columns),
                        str(sdir),
                        json.dumps(meta, default=str),
                    ),
                )

        # 5. Populate in-memory representation
        session_dict = {
            "session_id": session_id,
            "session_token": token,  # available to the creating request only
            "token_hash": token_hash,
            "version": 1,
            "owner_id": owner_id,
            "created_timestamp": now,
            "last_accessed_timestamp": now,
            "expires_at": expires_at,
            "run_id": meta["run_id"],
            "run_dir": meta["run_dir"],
            "filepath": filepath,
            "dataset_name": meta["dataset_name"],
            "df_original": df.copy(deep=True),
            "df_current": df.copy(deep=True),
            "profile": profile,
            "cleaning_session": cleaning_session,
            "plan": None,
            "executed_actions": [],
            "report": None,
        }

        with self._lock:
            self._memory_cache[session_id] = session_dict

        return session_dict

    def get_session(
        self, session_id: str, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve session from in-memory cache or reload from SQLite and Parquet.
        Guarantees cross-worker cache consistency by verifying DB version.
        """
        if not session_id or not isinstance(session_id, str):
            return None

        now = time.time()

        # Query SQLite catalog to check version and expiration
        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            )
            row = cur.fetchone()

        if not row:
            with self._lock:
                self._memory_cache.pop(session_id, None)
            return None

        if row["expires_at"] < now:
            self.delete_session(session_id)
            return None

        row_dict = dict(row)
        db_version = row_dict.get("version", 1)

        # Check in-memory cache: if version matches db_version, memory is fresh!
        with self._lock:
            cached = self._memory_cache.get(session_id)
            if cached and cached.get("version", 1) == db_version:
                if touch:
                    cached["last_accessed_timestamp"] = now
                return cached

        # Cache miss or stale version: reload artifacts from disk
        sdir = Path(row["storage_path"])
        orig_pq = sdir / "original.parquet"
        curr_pq = sdir / "current.parquet"

        if not orig_pq.exists() or not curr_pq.exists():
            return None

        try:
            df_original = pd.read_parquet(orig_pq)
            df_current = pd.read_parquet(curr_pq)
        except Exception:
            return None

        from session import CleaningSession
        cleaning_session = CleaningSession(df_original, session_name=row["dataset_name"] or "default")
        if hasattr(cleaning_session, "load_from_directory"):
            cleaning_session.load_from_directory(sdir)
        else:
            cleaning_session.current_df = df_current.copy(deep=True)

        meta = {}
        if row["serialized_meta"]:
            try:
                meta = json.loads(row["serialized_meta"])
            except Exception:
                meta = {}

        plan_obj = None
        raw_plan = meta.get("plan")
        if raw_plan and isinstance(raw_plan, dict):
            try:
                from schemas import CleaningPlan
                plan_obj = CleaningPlan(**raw_plan)
            except Exception:
                plan_obj = None

        token_hash = row_dict.get("token_hash") or row_dict.get("session_token", "")

        session_dict = {
            "session_id": session_id,
            "token_hash": token_hash,
            "version": db_version,
            "owner_id": row["owner_id"],
            "created_timestamp": row["created_at"],
            "last_accessed_timestamp": now if touch else row["last_accessed_at"],
            "expires_at": row["expires_at"],
            "run_id": meta.get("run_id", f"run_{session_id[:8]}"),
            "run_dir": meta.get("run_dir", str(sdir)),
            "filepath": row["filepath"],
            "dataset_name": row["dataset_name"],
            "df_original": df_original,
            "df_current": df_current,
            "profile": meta.get("profile", {}),
            "cleaning_session": cleaning_session,
            "plan": plan_obj,
            "executed_actions": meta.get("executed_actions", []),
            "report": meta.get("report"),
        }

        with self._lock:
            self._memory_cache[session_id] = session_dict

        if touch:
            with self._lock, self._get_connection() as con:
                con.execute(
                    "UPDATE sessions SET last_accessed_at = ? WHERE session_id = ?",
                    (now, session_id),
                )

        return session_dict

    def sync_session_to_disk(self, session_id: str) -> None:
        """
        Synchronize in-memory changes to SQLite & Parquet and increment version counter.
        Uses atomic file replacement to prevent corrupt reads across workers.
        """
        with self._lock:
            cached = self._memory_cache.get(session_id)
            if not cached:
                return

        sdir = self._session_dir(session_id)
        df_current = cached.get("df_current")
        cleaning_session = cached.get("cleaning_session")
        if cleaning_session and isinstance(df_current, pd.DataFrame):
            cleaning_session.current_df = df_current.copy(deep=True)

        if cleaning_session and hasattr(cleaning_session, "save_to_directory"):
            cleaning_session.save_to_directory(sdir)
        elif isinstance(df_current, pd.DataFrame):
            tmp_curr = sdir / "current.parquet.tmp"
            final_curr = sdir / "current.parquet"
            df_current.to_parquet(tmp_curr, index=False)
            tmp_curr.replace(final_curr)

        new_version = cached.get("version", 1) + 1
        cached["version"] = new_version

        # Serialize metadata
        plan = cached.get("plan")
        plan_dict = plan.model_dump() if hasattr(plan, "model_dump") else (plan if isinstance(plan, dict) else None)
        meta = {
            "run_id": cached.get("run_id"),
            "run_dir": cached.get("run_dir"),
            "filepath": cached.get("filepath"),
            "dataset_name": cached.get("dataset_name"),
            "profile": cached.get("profile"),
            "plan": plan_dict,
            "executed_actions": cached.get("executed_actions", []),
            "report": cached.get("report"),
        }

        now = time.time()
        with self._lock, self._get_connection() as con:
            con.execute(
                """
                UPDATE sessions
                SET last_accessed_at = ?,
                    version = ?,
                    row_count = ?,
                    col_count = ?,
                    serialized_meta = ?
                WHERE session_id = ?
                """,
                (
                    now,
                    new_version,
                    len(df_current) if isinstance(df_current, pd.DataFrame) else 0,
                    len(df_current.columns) if isinstance(df_current, pd.DataFrame) else 0,
                    json.dumps(meta, default=str),
                    session_id,
                ),
            )

    def delete_session(self, session_id: str) -> bool:
        """Delete session from memory, SQLite, and securely unlink isolated storage."""
        with self._lock:
            self._memory_cache.pop(session_id, None)

        sdir = self.storage_dir / session_id

        # Secure deletion: verify directory jail confinement
        try:
            resolved_sdir = sdir.resolve()
            resolved_sdir.relative_to(self.storage_dir)
            if resolved_sdir.exists() and resolved_sdir.is_dir():
                shutil.rmtree(resolved_sdir, ignore_errors=True)
        except Exception:
            pass

        with self._lock, self._get_connection() as con:
            cur = con.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            return cur.rowcount > 0

    def verify_token(self, session_id: str, provided_token: Optional[str]) -> bool:
        """Constant-time token verification using SHA-256 hash comparison."""
        if not provided_token:
            return False

        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT token_hash FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            )
            row = cur.fetchone()

        if not row:
            return False

        row_dict = dict(row)
        stored_hash = row_dict.get("token_hash")
        if not stored_hash:
            return False

        provided_hash = hashlib.sha256(provided_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored_hash, provided_hash)

    def list_user_sessions(self, owner_id: str) -> List[Dict[str, Any]]:
        """List active sessions belonging strictly to a specific user (or anonymous demo)."""
        now = time.time()
        with self._lock, self._get_connection() as con:
            cur = con.execute(
                """
                SELECT session_id, dataset_name, filepath, row_count, col_count,
                       created_at, last_accessed_at, expires_at
                FROM sessions
                WHERE owner_id = ? AND status = 'active' AND expires_at > ?
                ORDER BY last_accessed_at DESC
                """,
                (owner_id, now),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows

    def reassign_session_owner(
        self,
        session_id: str,
        new_owner_id: str,
        session_token: Optional[str] = None,
    ) -> bool:
        """
        Safely reassign ownership of an anonymous demo session to an authenticated user account.
        Requires the valid session_token to prevent unauthorized session hijacking.
        Cannot overwrite or claim an existing user account's session.
        """
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT owner_id, token_hash FROM sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                return False
            # Only anonymous demo sessions can be adopted
            if row["owner_id"] != "anonymous" and not row["owner_id"].startswith("demo_"):
                return False
            # If token_hash was recorded, verify caller holds the original secret token
            if row["token_hash"]:
                if not session_token:
                    return False
                cand_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(row["token_hash"], cand_hash):
                    return False
            now = time.time()
            max_age = get_session_max_age_seconds()
            con.execute(
                "UPDATE sessions SET owner_id = ?, expires_at = ?, last_accessed_at = ? WHERE session_id = ?",
                (new_owner_id, now + max_age, now, session_id),
            )
        # Update in-memory session if loaded
        if session_id in self._memory_cache:
            self._memory_cache[session_id]["owner_id"] = new_owner_id
            self._memory_cache[session_id]["expires_at"] = now + max_age
        return True

    # =========================================================================
    # AUTOMATED CLEANUP & RETENTION ENGINE
    # =========================================================================

    def purge_expired_sessions(self, max_age_seconds: Optional[int] = None) -> int:
        """
        Purge expired sessions, associated isolated checkpoints, and linked uploads.
        Never deletes active files or sessions currently within lease.
        """
        now = time.time()
        age_limit = max_age_seconds if max_age_seconds is not None else get_session_max_age_seconds()
        cutoff_access = now - age_limit

        expired_ids: List[str] = []
        filepaths_to_check: List[str] = []

        with self._lock, self._get_connection() as con:
            cur = con.execute(
                """
                SELECT session_id, filepath, storage_path
                FROM sessions
                WHERE expires_at < ? OR last_accessed_at < ?
                """,
                (now, cutoff_access),
            )
            for row in cur.fetchall():
                expired_ids.append(row["session_id"])
                if row["filepath"]:
                    filepaths_to_check.append(row["filepath"])

        purged_count = 0
        for sid in expired_ids:
            if self.delete_session(sid):
                purged_count += 1

        # Query all active filepaths still within lease
        active_filepaths: Set[str] = set()
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT filepath FROM sessions WHERE status = 'active' AND expires_at >= ?", (now,))
            for r in cur.fetchall():
                if r["filepath"]:
                    try:
                        active_filepaths.add(str(Path(r["filepath"]).resolve()))
                    except Exception:
                        pass

        # Check and safely unlink abandoned uploaded files
        uploads_dir = self.uploads_dir
        lease_safety_cutoff = now - 3600  # 1 hour safety lease
        for fp in filepaths_to_check:
            try:
                p = Path(fp).resolve()
                p.relative_to(uploads_dir)  # Jail check: must reside inside data/uploads/
                # Safety: NEVER delete if referenced by any active session
                if str(p) in active_filepaths:
                    continue
                # Safety: NEVER delete if modified within recent lease window
                if p.is_file():
                    stat = p.stat()
                    if stat.st_mtime < lease_safety_cutoff:
                        p.unlink(missing_ok=True)
            except Exception:
                continue

        # Scan uploads directory for orphaned CSV files older than retention policy
        retention_cutoff = now - (get_file_retention_hours() * 3600)
        self._purge_orphaned_uploads(uploads_dir, retention_cutoff, active_filepaths)

        return purged_count

    def _purge_orphaned_uploads(
        self,
        uploads_dir: Path,
        cutoff_time: float,
        active_filepaths: Optional[Set[str]] = None,
    ) -> int:
        """Purge uploaded CSV files older than retention period not linked to active sessions."""
        if not uploads_dir.exists():
            return 0

        now = time.time()
        if active_filepaths is None:
            active_filepaths = set()
            with self._lock, self._get_connection() as con:
                cur = con.execute("SELECT filepath FROM sessions WHERE status = 'active' AND expires_at >= ?", (now,))
                for r in cur.fetchall():
                    if r["filepath"]:
                        try:
                            active_filepaths.add(str(Path(r["filepath"]).resolve()))
                        except Exception:
                            pass

        deleted_orphans = 0
        for item in uploads_dir.iterdir():
            if not item.is_file() or item.suffix.lower() != ".csv":
                continue
            try:
                resolved_item = item.resolve()
                resolved_item.relative_to(uploads_dir)  # Jail check
                if str(resolved_item) in active_filepaths:
                    continue  # Active session file: strictly protected

                stat = resolved_item.stat()
                if stat.st_mtime < cutoff_time:
                    resolved_item.unlink(missing_ok=True)
                    deleted_orphans += 1
            except Exception:
                continue
        return deleted_orphans

        deleted_orphans = 0
        for item in uploads_dir.iterdir():
            if not item.is_file() or item.suffix.lower() != ".csv":
                continue
            try:
                resolved_item = item.resolve()
                resolved_item.relative_to(uploads_dir)  # Jail check
                if str(resolved_item) in active_filepaths:
                    continue  # Active session file: strictly protected

                stat = resolved_item.stat()
                if stat.st_mtime < cutoff_time:
                    resolved_item.unlink(missing_ok=True)
                    deleted_orphans += 1
            except Exception:
                continue

        return deleted_orphans

    # =========================================================================
    # DICT-LIKE INTERFACE (100% Backward Compatibility)
    # =========================================================================

    def __getitem__(self, session_id: str) -> Dict[str, Any]:
        session = self.get_session(session_id, touch=True)
        if session is None:
            raise KeyError(f"Session not found or expired: {session_id}")
        return session

    def __setitem__(self, session_id: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._memory_cache[session_id] = value
        self.sync_session_to_disk(session_id)

    def __delitem__(self, session_id: str) -> None:
        if not self.delete_session(session_id):
            raise KeyError(f"Session not found: {session_id}")

    def __contains__(self, session_id: Any) -> bool:
        if not isinstance(session_id, str):
            return False
        return self.get_session(session_id, touch=False) is not None

    def __len__(self) -> int:
        now = time.time()
        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT COUNT(*) as cnt FROM sessions WHERE expires_at > ? AND status = 'active'",
                (now,),
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0

    def get(self, session_id: str, default: Any = None) -> Any:
        session = self.get_session(session_id, touch=True)
        return session if session is not None else default

    def pop(self, session_id: str, default: Any = None) -> Any:
        session = self.get_session(session_id, touch=False)
        if session is not None:
            self.delete_session(session_id)
            return session
        return default

    def items(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Return list of all active session items."""
        now = time.time()
        active_ids = []
        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT session_id FROM sessions WHERE expires_at > ? AND status = 'active'",
                (now,),
            )
            active_ids = [r["session_id"] for r in cur.fetchall()]

        result = []
        for sid in active_ids:
            s = self.get_session(sid, touch=False)
            if s:
                result.append((sid, s))
        return result

    def clear(self) -> None:
        """Clear all sessions (used for test isolation)."""
        now = time.time()
        with self._lock, self._get_connection() as con:
            con.execute("DELETE FROM sessions")
        with self._lock:
            self._memory_cache.clear()
        shutil.rmtree(self.storage_dir, ignore_errors=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
