"""
Phase 3 Test Suite: Session Reliability and Data Privacy Hardening.

Covers:
1. Multi-worker safe SQLite WAL session persistence across server/store restarts.
2. Per-session Parquet filesystem isolation and checkpoint recovery.
3. Graceful handling of corrupted/missing checkpoints.
4. Session expiration TTL, abandoned session purging, and orphaned upload cleanup.
5. Directory traversal protection in storage cleanup.
6. Session token verification, anti-tampering, and cross-session protection.
7. Absolute filesystem path concealment from API responses.
8. Small dataset statistical leakage suppression (N < MIN_SAMPLE_FOR_STATS).
9. Sensitive column header sanitization (PII, medical, financial).
10. Outbound AI privacy firewall enforcement and round-trip token remapping.
11. Concurrent multi-thread access under SQLite WAL mode.
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from ai_privacy import (
    MIN_SAMPLE_FOR_STATS,
    PrivacyViolationError,
    build_ai_safe_profile,
    classify_and_sanitize_column_header,
    remap_plan_columns,
    validate_outbound_payload,
)
from server import SESSIONS, app, get_authenticated_session
from session import CleaningSession
from session_store import PersistentSessionStore, conceal_absolute_path


@pytest.fixture
def temp_storage():
    """Create isolated temporary directories for session and upload storage."""
    temp_dir = tempfile.mkdtemp(prefix="datarefine_test_storage_")
    sessions_dir = Path(temp_dir) / "sessions"
    uploads_dir = Path(temp_dir) / "uploads"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    store = PersistentSessionStore(
        db_path=sessions_dir / "test_catalog.db",
        storage_dir=sessions_dir,
    )
    yield {
        "root": Path(temp_dir),
        "sessions": sessions_dir,
        "uploads": uploads_dir,
        "store": store,
    }
    shutil.rmtree(temp_dir, ignore_errors=True)


# ==============================================================================
# 1. PERSISTENCE ACROSS RESTARTS & MULTI-WORKER ISOLATION
# ==============================================================================

def test_session_store_persistence_across_instances(temp_storage):
    """Sessions and DataFrames must survive store/process re-initialization."""
    store1 = temp_storage["store"]
    df = pd.DataFrame({
        "id": [1, 2, 3],
        "name": ["Alice", "Bob", "Charlie"],
        "score": [95.5, 88.0, 92.3],
    })
    session_id = "test-session-restart-001"
    session_token = "tok_sec_12345"

    cleaning_sess = CleaningSession(df.copy(), session_name="test_sess")
    store1.create_session(
        session_id=session_id,
        df=df,
        filepath="data/uploads/sample.csv",
        profile={"rows": 3, "columns": 3},
        cleaning_session=cleaning_sess,
        session_token=session_token,
        owner_id="owner_abc",
    )

    # Simulate fresh server start / second worker accessing the same shared storage
    store2 = PersistentSessionStore(
        db_path=temp_storage["sessions"] / "test_catalog.db",
        storage_dir=temp_storage["sessions"],
    )

    assert session_id in store2
    retrieved = store2.get_session(session_id)
    assert retrieved is not None
    import hashlib
    assert retrieved["token_hash"] == hashlib.sha256(session_token.encode()).hexdigest()
    assert store2.verify_token(session_id, session_token) is True
    assert retrieved["owner_id"] == "owner_abc"

    df_recovered = retrieved["df_current"]
    assert isinstance(df_recovered, pd.DataFrame)
    assert len(df_recovered) == 3
    assert list(df_recovered["name"]) == ["Alice", "Bob", "Charlie"]
    assert list(df_recovered["score"]) == [95.5, 88.0, 92.3]


def test_checkpoint_persistence_and_undo_redo_recovery(temp_storage):
    """CleaningSession checkpoints, undo, redo, and rollback survive reloads."""
    store = temp_storage["store"]
    df = pd.DataFrame({"col_a": ["old_val_1", "old_val_2"], "col_b": [10, 20]})
    session_id = "test-checkpoints-002"

    cleaning_sess = CleaningSession(df.copy(), session_name="chk_test")
    from schemas import CleaningAction
    act1 = CleaningAction(
        action="trim_whitespace",
        column="col_a",
        reasoning="Step 1: trim whitespace",
    )
    cleaning_sess.apply_action(act1)

    act2 = CleaningAction(
        action="normalize_whitespace",
        column="col_a",
        reasoning="Step 2: normalize whitespace",
    )
    cleaning_sess.apply_action(act2)

    assert cleaning_sess.current_step == 2
    assert len(cleaning_sess.get_history()) == 3

    # Save to session store
    store.create_session(
        session_id=session_id,
        df=cleaning_sess.get_current_df(),
        filepath="uploads/data.csv",
        profile={"rows": 2},
        cleaning_session=cleaning_sess,
        session_token="tok_chk_99",
    )

    # Reload in a separate instance
    store_fresh = PersistentSessionStore(
        db_path=temp_storage["sessions"] / "test_catalog.db",
        storage_dir=temp_storage["sessions"],
    )
    session_data = store_fresh.get_session(session_id)
    reloaded_sess = session_data["cleaning_session"]

    assert reloaded_sess.current_step == 2
    assert len(reloaded_sess.get_history()) == 3

    # Test Undo on reloaded session
    undo_res = reloaded_sess.undo()
    assert undo_res["status"] == "success"
    assert reloaded_sess.current_step == 1

    # Test Redo on reloaded session
    redo_res = reloaded_sess.redo()
    assert redo_res["status"] == "success"
    assert reloaded_sess.current_step == 2

    # Test Rollback on reloaded session
    roll_res = reloaded_sess.rollback_all()
    assert roll_res["status"] == "success"
    assert reloaded_sess.current_step == 0


def test_corrupted_checkpoint_recovery(temp_storage):
    """If a checkpoint file on disk is deleted or corrupted, session degrades gracefully."""
    sess_dir = temp_storage["sessions"] / "corrupt_session"
    sess_dir.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame({"x": [1, 2, 3]})
    cleaning_sess = CleaningSession(df, session_name="corrupt_test")
    from schemas import CleaningAction
    cleaning_sess.apply_action(CleaningAction(action="drop_missing_rows", reasoning="step 1"))
    cleaning_sess.save_to_directory(sess_dir)

    # Intentionally corrupt step 1 file
    step1_file = sess_dir / "checkpoints" / "step_1.parquet"
    assert step1_file.exists()
    step1_file.write_bytes(b"CORRUPTED_PARQUET_HEADER_INVALID_DATA")

    # Load from directory
    recovered = CleaningSession(df.copy(), session_name="recovered")
    recovered.load_from_directory(sess_dir)
    # Current df should fall back safely to available data
    current = recovered.get_current_df()
    assert isinstance(current, pd.DataFrame)
    assert len(current) == 3


# ==============================================================================
# 2. AUTOMATIC FILE & SESSION CLEANUP
# ==============================================================================

def test_session_expiration_and_cleanup(temp_storage):
    """Expired sessions past max_age must be pruned from SQLite and filesystem."""
    store = temp_storage["store"]
    df = pd.DataFrame({"val": [100, 200]})

    # Create active session
    store.create_session(
        session_id="active-session-123",
        df=df,
        filepath="uploads/active.csv",
        profile={},
        session_token="tok_active",
    )

    # Create expired session
    store.create_session(
        session_id="expired-session-456",
        df=df,
        filepath="uploads/expired.csv",
        profile={},
        session_token="tok_expired",
    )

    # Artificially age the expired session in SQLite (e.g. 5 hours ago)
    old_time = time.time() - (5 * 3600)
    with store._lock:
        con = store._get_connection()
        try:
            with con:
                con.execute(
                    "UPDATE sessions SET last_accessed_at = ?, created_at = ?, expires_at = ? WHERE session_id = ?",
                    (old_time, old_time, old_time, "expired-session-456"),
                )
        finally:
            con.close()

    # Run purge with max_age = 4 hours (14400s)
    purged_count = store.purge_expired_sessions(max_age_seconds=14400)
    assert purged_count >= 1

    assert "expired-session-456" not in store
    assert "active-session-123" in store
    assert not (temp_storage["sessions"] / "expired-session-456").exists()
    assert (temp_storage["sessions"] / "active-session-123").exists()


def test_orphaned_uploads_cleanup(temp_storage):
    """Old uploaded files with no active referencing session must be purged."""
    uploads_dir = temp_storage["uploads"]
    store = temp_storage["store"]

    # Active uploaded file
    active_file = uploads_dir / "active_file.csv"
    active_file.write_text("a,b\n1,2\n")

    # Orphaned uploaded file
    orphan_file = uploads_dir / "orphan_file.csv"
    orphan_file.write_text("x,y\n8,9\n")

    # Set mtime on orphan file to 48 hours ago
    old_mtime = time.time() - (48 * 3600)
    os.utime(str(orphan_file), (old_mtime, old_mtime))

    # Active file has an active session referencing it
    df = pd.DataFrame({"a": [1], "b": [2]})
    store.create_session(
        session_id="active-ref-session",
        df=df,
        filepath=str(active_file),
        profile={},
        session_token="tok_ref",
    )

    # Purge uploads older than 24 hours
    cutoff = time.time() - (24 * 3600)
    deleted = store._purge_orphaned_uploads(uploads_dir=uploads_dir, cutoff_time=cutoff)

    assert deleted >= 1
    assert active_file.exists()
    assert not orphan_file.exists()


def test_cleanup_directory_jail_security(temp_storage):
    """_is_safe_subpath must prevent directory traversal outside configured root."""
    store = temp_storage["store"]
    safe_root = temp_storage["sessions"]

    valid_subpath = safe_root / "session_1" / "file.csv"
    assert store._is_safe_subpath(valid_subpath, safe_root) is True

    # Traversal attempts
    traversal_path = safe_root / ".." / "system32"
    assert store._is_safe_subpath(traversal_path, safe_root) is False

    root_itself = safe_root
    assert store._is_safe_subpath(root_itself, safe_root) is False


# ==============================================================================
# 3. SESSION SECURITY & TOKEN VERIFICATION
# ==============================================================================

def test_session_token_authentication_and_tampering():
    """Endpoints require valid X-Session-Token and reject tampering with 403."""
    client = TestClient(app)

    # 1. Profile a dataset to establish a session
    res = client.post("/api/profile", json={"filepath": "data/benchmark/dirty_sample.csv"})
    assert res.status_code == 200
    data = res.json()
    session_id = data["session_id"]
    valid_token = data.get("session_token")
    assert valid_token is not None

    # 2. Access with valid token -> 200 OK
    res_valid = client.post(
        "/api/dataset/explore",
        json={"session_id": session_id, "limit": 5},
        headers={"X-Session-Token": valid_token},
    )
    assert res_valid.status_code == 200

    # 3. Access with tampered/invalid token -> 403 Forbidden
    res_tampered = client.post(
        "/api/dataset/explore",
        json={"session_id": session_id, "limit": 5},
        headers={"X-Session-Token": "invalid_tampered_token_999"},
    )
    assert res_tampered.status_code == 403
    assert "Invalid session access token" in res_tampered.json()["detail"]

    # 4. Access with non-existent session_id -> 404 Not Found
    res_unknown = client.post(
        "/api/dataset/explore",
        json={"session_id": "non-existent-session-id-000", "limit": 5},
        headers={"X-Session-Token": valid_token},
    )
    assert res_unknown.status_code == 404


def test_conceal_absolute_paths_in_api():
    """File paths returned in API responses must not leak absolute server directories."""
    client = TestClient(app)
    res = client.post("/api/profile", json={"filepath": "data/benchmark/dirty_sample.csv"})
    assert res.status_code == 200
    data = res.json()
    filepath = data["filepath"]

    # Must be relative or safe basename, never containing Windows drive root or full users directory
    assert "C:\\Users" not in filepath
    assert "/Users/" not in filepath
    assert "/home/" not in filepath
    assert "benchmark" in filepath or "dirty_sample.csv" in filepath


# ==============================================================================
# 4. PRIVACY FIREWALL: SMALL DATASET SUPPRESSION & SENSITIVE HEADERS
# ==============================================================================

def test_small_dataset_statistical_suppression():
    """Small datasets (N < MIN_SAMPLE_FOR_STATS) must suppress exact statistics."""
    # N = 3 rows (< 5)
    small_profile = {
        "rows": 3,
        "columns": 2,
        "duplicate_rows": 0,
        "column_profiles": {
            "amount": {
                "inferred_type": "numeric",
                "null_count": 0,
                "null_percentage": 0.0,
                "statistics": {
                    "min": 10.0,
                    "max": 50.0,
                    "mean": 30.0,
                    "median": 30.0,
                },
                "category_count": 3,
                "most_frequent_category_count": 1,
            },
            "status": {
                "inferred_type": "categorical",
                "null_count": 0,
                "null_percentage": 0.0,
                "category_count": 2,
            },
        },
    }

    safe_profile, token_to_col = build_ai_safe_profile(small_profile)

    # Validate statistics dictionary is suppressed (empty or absent)
    amount_meta = safe_profile["column_profiles"]["amount"]
    assert "statistics" not in amount_meta or len(amount_meta["statistics"]) == 0
    assert "category_count" not in amount_meta
    assert "Small dataset" in safe_profile["privacy_note"]
    assert f"rows=3 < {MIN_SAMPLE_FOR_STATS}" in safe_profile["privacy_note"]

    # Verify that outbound payload validation passes
    validate_outbound_payload(safe_profile)


def test_normal_dataset_statistical_retention():
    """Datasets with N >= MIN_SAMPLE_FOR_STATS retain aggregate structural statistics."""
    # N = 100 rows (>= 5)
    normal_profile = {
        "rows": 100,
        "columns": 1,
        "duplicate_rows": 2,
        "column_profiles": {
            "score": {
                "inferred_type": "numeric",
                "null_count": 5,
                "null_percentage": 5.0,
                "statistics": {
                    "min": 12.0,
                    "max": 98.0,
                    "mean": 64.5,
                    "median": 65.0,
                },
            },
        },
    }

    safe_profile, _ = build_ai_safe_profile(normal_profile)
    score_meta = safe_profile["column_profiles"]["score"]
    assert "statistics" in score_meta
    assert score_meta["statistics"]["min"] == 12.0
    assert score_meta["statistics"]["mean"] == 64.5


def test_sensitive_header_classification_and_sanitization():
    """Sensitive headers (names, emails, phones, medical, financial) are sanitized to tokens."""
    sensitive_headers = [
        ("patient_diagnosis_code", "medical"),
        ("credit_card_number", "financial"),
        ("user_ssn_id", "government_id"),
        ("customer_phone_number", "contact_phone"),
        ("personal_email_address", "contact_email"),
        ("full_name_of_patient", "name"),
    ]

    for header, expected_type in sensitive_headers:
        sanitized, was_sanitized = classify_and_sanitize_column_header(header, 1)
        assert was_sanitized is True
        assert header != sanitized
        assert expected_type in sanitized

    # Non-sensitive header remains intact
    safe_header, was_sanitized = classify_and_sanitize_column_header("order_status", 2)
    assert was_sanitized is False
    assert safe_header == "order_status"


def test_remap_plan_columns_round_trip():
    """Sanitized column tokens in AI plan actions are remapped back to original DataFrame columns."""
    token_to_col = {
        "col_0_medical": "Patient_Medical_Condition",
        "col_1_financial": "Account_Balance_USD",
        "city": "City",
    }

    ai_plan = {
        "summary": "Clean medical records",
        "actions": [
            {
                "action": "trim_whitespace",
                "column": "col_0_medical",
                "params": {},
                "reasoning": "Strip whitespace from condition",
            },
            {
                "action": "impute_missing",
                "column": "col_1_financial",
                "params": {},
                "reasoning": "Impute median account balance",
            },
            {
                "action": "convert_to_date",
                "column": "city",
                "params": {},
                "reasoning": "Standardize date",
            },
        ],
    }

    remapped = remap_plan_columns(ai_plan, token_to_col)
    actions = remapped["actions"]
    assert actions[0]["column"] == "Patient_Medical_Condition"
    assert actions[1]["column"] == "Account_Balance_USD"
    assert actions[2]["column"] == "City"


# ==============================================================================
# 5. CONCURRENT ACCESS UNDER SQLITE WAL MODE
# ==============================================================================

def test_concurrent_session_writes(temp_storage):
    """Multiple concurrent threads must read and write to the session store without locking errors."""
    import concurrent.futures

    store = temp_storage["store"]
    num_threads = 8

    def worker_task(thread_id):
        sess_id = f"worker-session-{thread_id}"
        df = pd.DataFrame({"worker": [thread_id] * 5, "val": range(5)})
        store.create_session(
            session_id=sess_id,
            df=df,
            filepath=f"uploads/w_{thread_id}.csv",
            profile={"rows": 5},
            session_token=f"tok_{thread_id}",
        )
        # Read back
        retrieved = store.get_session(sess_id)
        assert retrieved is not None
        assert store.verify_token(sess_id, f"tok_{thread_id}") is True
        assert len(retrieved["df_current"]) == 5
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker_task, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    assert all(results)
    assert len(store) >= num_threads
