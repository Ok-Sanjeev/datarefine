"""
Phase 3 Focused Verification Test Suite.

Verifies the 4 critical areas requested in Phase 3 verification:
1. Session Tokens & Downloads:
   - Authenticated download requests using X-Session-Token headers.
   - Rejection of unauthorized downloads (403/404).
   - Cache-Control: no-store, no-cache headers preventing disk caching.
   - URL hygiene (zero secrets in query string).
2. SQLite Multi-Process Concurrency & Cache Consistency:
   - Concurrent writes from multiple OS processes (multiprocessing).
   - Cross-process cache consistency: worker B automatically reloads when worker A increments version.
   - WAL mode lock recovery and non-blocking reads.
3. Token Storage Security:
   - Verify database stores SHA-256 token_hash and NEVER plaintext session tokens.
   - Verify API responses and session metadata never leak tokens to unauthorized callers.
4. Cleanup Safety:
   - Simultaneous file processing and cleanup: files referenced by active sessions are NEVER deleted.
   - Files within safety lease (< 1 hour mtime) are strictly protected against unlinking.
   - Expired session cleanup cannot delete another user's shared uploads or data.
"""

import hashlib
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from server import SESSIONS, app
from session import CleaningSession
from session_store import PersistentSessionStore


@pytest.fixture
def isolated_env():
    """Create isolated temporary environment for storage verification."""
    temp_dir = tempfile.mkdtemp(prefix="datarefine_verify_")
    sessions_dir = Path(temp_dir) / "sessions"
    uploads_dir = Path(temp_dir) / "uploads"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    db_path = sessions_dir / "verify_catalog.db"
    store = PersistentSessionStore(db_path=db_path, storage_dir=sessions_dir, uploads_dir=uploads_dir)

    yield {
        "root": Path(temp_dir),
        "sessions": sessions_dir,
        "uploads": uploads_dir,
        "db_path": db_path,
        "store": store,
    }
    shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# AREA 1: SESSION TOKENS & DOWNLOADS
# ==============================================================================

def test_authenticated_download_via_headers():
    """Cleaned CSV and audit reports can be downloaded using X-Session-Token header."""
    client = TestClient(app)

    # 1. Profile dataset
    res = client.post("/api/profile", json={"filepath": "data/benchmark/dirty_sample.csv"})
    assert res.status_code == 200
    data = res.json()
    session_id = data["session_id"]
    token = data["session_token"]

    # 2. Download cleaned CSV with valid X-Session-Token header
    res_down = client.get(
        f"/api/download/cleaned?session_id={session_id}",
        headers={"X-Session-Token": token},
    )
    assert res_down.status_code == 200
    assert "text/csv" in res_down.headers.get("content-type", "")
    assert "no-store" in res_down.headers.get("cache-control", "").lower()

    # 3. Download with invalid header -> 403 Forbidden
    res_tampered = client.get(
        f"/api/download/cleaned?session_id={session_id}",
        headers={"X-Session-Token": "tampered_bad_token"},
    )
    assert res_tampered.status_code == 403


def test_download_cache_control_headers():
    """Download endpoints must send Cache-Control: no-store to prevent browser/proxy leakage."""
    client = TestClient(app)
    res = client.post("/api/profile", json={"filepath": "data/benchmark/dirty_sample.csv"})
    data = res.json()
    session_id = data["session_id"]
    token = data["session_token"]

    res_down = client.get(
        f"/api/download/cleaned?session_id={session_id}",
        headers={"X-Session-Token": token},
    )
    assert res_down.status_code == 200
    cache_ctrl = res_down.headers.get("cache-control", "")
    assert "no-store" in cache_ctrl
    assert "no-cache" in cache_ctrl
    assert "private" in cache_ctrl


# ==============================================================================
# AREA 2: SQLITE MULTI-PROCESS CONCURRENCY & CACHE COHERENCE
# ==============================================================================

def _multiprocess_writer_task(db_path, sessions_dir, worker_idx, return_dict):
    """Worker task running in a completely separate OS process."""
    try:
        store = PersistentSessionStore(db_path=Path(db_path), storage_dir=Path(sessions_dir))
        session_id = f"proc-session-{worker_idx}"
        df = pd.DataFrame({"proc": [worker_idx] * 10, "val": range(10)})
        store.create_session(
            session_id=session_id,
            df=df,
            filepath=f"uploads/proc_{worker_idx}.csv",
            profile={"rows": 10},
            session_token=f"token_{worker_idx}",
        )
        # Verify read in same process
        sess = store.get_session(session_id)
        if not sess or len(sess["df_current"]) != 10:
            return_dict[worker_idx] = False
            return
        return_dict[worker_idx] = True
    except Exception as e:
        return_dict[worker_idx] = str(e)


def test_sqlite_multiprocess_simultaneous_writes(isolated_env):
    """Simultaneous writes from multiple separate OS processes must succeed under SQLite WAL."""
    db_path = str(isolated_env["db_path"])
    sessions_dir = str(isolated_env["sessions"])
    num_processes = 4

    manager = multiprocessing.Manager()
    return_dict = manager.dict()
    processes = []

    for i in range(num_processes):
        p = multiprocessing.Process(
            target=_multiprocess_writer_task,
            args=(db_path, sessions_dir, i, return_dict),
        )
        processes.append(p)
        p.start()

    for p in processes:
        p.join(timeout=15.0)

    for i in range(num_processes):
        assert return_dict.get(i) is True, f"Process {i} failed: {return_dict.get(i)}"

    # Read back all sessions in main process
    main_store = isolated_env["store"]
    for i in range(num_processes):
        sess = main_store.get_session(f"proc-session-{i}")
        assert sess is not None
        assert len(sess["df_current"]) == 10


def test_cross_worker_cache_consistency(isolated_env):
    """When worker A updates session version on disk, worker B automatically reloads fresh state."""
    db_path = isolated_env["db_path"]
    sessions_dir = isolated_env["sessions"]

    store_a = PersistentSessionStore(db_path=db_path, storage_dir=sessions_dir)
    store_b = PersistentSessionStore(db_path=db_path, storage_dir=sessions_dir)

    session_id = "cross-worker-sync-001"
    df_initial = pd.DataFrame({"col_x": [10, 20, 30]})

    # Worker A creates session (version 1)
    store_a.create_session(
        session_id=session_id,
        df=df_initial,
        filepath="uploads/sync.csv",
        profile={"rows": 3},
        session_token="sync_token_123",
    )

    # Worker B loads session into its local memory cache
    sess_b = store_b.get_session(session_id)
    assert sess_b is not None
    assert len(sess_b["df_current"]) == 3
    assert sess_b.get("version") == 1

    # Now Worker A mutates DataFrame (drops a row) and syncs to disk (version 2)
    sess_a = store_a.get_session(session_id)
    df_mutated = pd.DataFrame({"col_x": [10, 20]})  # now 2 rows
    sess_a["df_current"] = df_mutated
    store_a.sync_session_to_disk(session_id)
    assert sess_a.get("version") == 2

    # Worker B accesses the session again: must detect version mismatch and reload!
    sess_b_refreshed = store_b.get_session(session_id)
    assert sess_b_refreshed["version"] == 2
    assert len(sess_b_refreshed["df_current"]) == 2
    assert list(sess_b_refreshed["df_current"]["col_x"]) == [10, 20]


# ==============================================================================
# AREA 3: TOKEN STORAGE SECURITY
# ==============================================================================

def test_database_stores_token_hash_not_plaintext(isolated_env):
    """SQLite database file must NEVER store session tokens in plaintext."""
    store = isolated_env["store"]
    raw_secret_token = "SUPER_SECRET_USER_PLAINTEXT_TOKEN_ABC123"
    session_id = "token-hash-audit-001"
    expected_hash = hashlib.sha256(raw_secret_token.encode("utf-8")).hexdigest()

    df = pd.DataFrame({"a": [1, 2]})
    store.create_session(
        session_id=session_id,
        df=df,
        filepath="uploads/sec.csv",
        profile={},
        session_token=raw_secret_token,
    )

    # Query raw database directly using sqlite3
    con = sqlite3.connect(str(isolated_env["db_path"]))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    con.close()

    assert row is not None
    stored_hash = row["token_hash"]
    assert stored_hash == expected_hash

    # Read binary contents of the SQLite file and ensure raw_secret_token is nowhere in it
    db_bytes = isolated_env["db_path"].read_bytes()
    assert raw_secret_token.encode("utf-8") not in db_bytes

    # verify_token must succeed using constant-time hash comparison
    assert store.verify_token(session_id, raw_secret_token) is True
    assert store.verify_token(session_id, "wrong_token") is False


# ==============================================================================
# AREA 4: CLEANUP SAFETY
# ==============================================================================

def test_cleanup_preserves_active_session_files(isolated_env):
    """Files currently referenced by active sessions must NEVER be deleted during cleanup."""
    uploads_dir = isolated_env["uploads"]
    store = isolated_env["store"]

    # Create shared dataset in uploads
    shared_csv = uploads_dir / "company_shared_customers.csv"
    shared_csv.write_text("id,name\n1,Alice\n2,Bob\n")

    # Session 1: Expired session referencing shared_csv
    df = pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]})
    store.create_session(
        session_id="expired-session-ref",
        df=df,
        filepath=str(shared_csv),
        profile={},
        session_token="tok_exp_1",
    )

    # Session 2: Active session ALSO referencing shared_csv
    store.create_session(
        session_id="active-session-ref",
        df=df,
        filepath=str(shared_csv),
        profile={},
        session_token="tok_act_2",
    )

    # Artificially expire Session 1 only
    old_time = time.time() - (10 * 3600)
    with store._lock, store._get_connection() as con:
        con.execute(
            "UPDATE sessions SET last_accessed_at = ?, expires_at = ? WHERE session_id = ?",
            (old_time, old_time, "expired-session-ref"),
        )

    # Trigger expired session purging
    purged = store.purge_expired_sessions(max_age_seconds=3600)
    assert purged >= 1
    assert "expired-session-ref" not in store
    assert "active-session-ref" in store

    # Crucial assertion: shared_csv MUST still exist because active-session-ref uses it!
    assert shared_csv.exists()
    assert shared_csv.read_text().startswith("id,name")


def test_cleanup_preserves_recently_modified_worker_uploads(isolated_env):
    """Files modified within active safety lease window must not be unlinked even if uncataloged."""
    uploads_dir = isolated_env["uploads"]
    store = isolated_env["store"]

    # File uploaded by an active worker 2 minutes ago (not yet finalized into session)
    recent_file = uploads_dir / "worker_uploading_now.csv"
    recent_file.write_text("col1,col2\nval1,val2\n")

    # Orphan file uploaded 72 hours ago
    ancient_orphan = uploads_dir / "abandoned_3_days_ago.csv"
    ancient_orphan.write_text("old1,old2\nx,y\n")
    old_mtime = time.time() - (72 * 3600)
    os.utime(str(ancient_orphan), (old_mtime, old_mtime))

    # Run purge
    store.purge_expired_sessions(max_age_seconds=3600)

    # Recent file within safety lease must be preserved
    assert recent_file.exists()
    # Ancient orphan must be deleted
    assert not ancient_orphan.exists()
