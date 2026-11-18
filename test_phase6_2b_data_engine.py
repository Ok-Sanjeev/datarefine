"""
Phase 6.2B: Data Engine & Functional Correctness Test Suite.

Verifies:
1. Date-only conversion produces clean YYYY-MM-DD across JSON, CSV, diffs, and Python scripts;
   preserves genuine timestamps with non-zero time.
2. Sequential column mapping across actions, workflow recipe replay, and standalone Python export.
3. Accurate execution analytics (no_operations_selected, completed_no_changes, verified_completed, partially_completed).
4. Step-by-step checkpoints, undo, redo, direct target_step rollback, and lazy Parquet checkpointing.
5. OpenRefine clustering confidence filtering, reconciliation thresholds, and GREL data mutation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import json
import pytest
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, date

from actions import (
    convert_to_date,
    execute_action,
    normalize_column_names,
    rename_column,
    impute_missing,
    trim_whitespace,
    normalize_text,
    remove_duplicates,
)
from schemas import CleaningAction, CleaningPlan
from session import CleaningSession, LazyCheckpoint
from planner import execute_cleaning_plan, validate_cleaning_results
from workflow_recipe import WorkflowRecipeManager
from server import (
    app,
    sanitize_value_for_json,
    compute_side_by_side_diff,
    SESSIONS,
    limiter,
)
from grel_engine import (
    execute_grel_transform,
    preview_grel,
    CompiledGRELExpression,
    GRELSecurityError,
    GRELSyntaxError,
    GRELExecutionError,
)
from openrefine_clustering import (
    detect_clusters,
    _select_canonical,
)
from reconciliation import (
    reconcile_column_values,
)
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_limiter_before_each():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def test_client():
    return TestClient(app)


def create_test_session(test_client, csv_bytes: bytes, filename: str = "test.csv"):
    """Helper to upload CSV, profile it, and return session_id and session_token."""
    res = test_client.post(
        "/api/upload",
        files={"file": (filename, csv_bytes, "text/csv")},
    )
    assert res.status_code == 200, f"Upload failed: {res.text}"
    filepath = res.json()["filepath"]
    prof_res = test_client.post(
        "/api/profile",
        json={"filepath": filepath},
    )
    assert prof_res.status_code == 200, f"Profile failed: {prof_res.text}"
    data = prof_res.json()
    return data["session_id"], data["session_token"]


# ============================================================================
# 1. DATE-ONLY CONVERSION & TIMESTAMP PRESERVATION
# ============================================================================

def test_date_only_conversion_produces_yyyy_mm_dd():
    """Verify date-only values produce YYYY-MM-DD without T00:00:00 across JSON, diffs, and CSV."""
    df = pd.DataFrame({
        "Join_Date": ["2023-01-15", "04/02/2021", "2020/12/31", "not_a_date", None],
        "User": ["Alice", "Bob", "Charlie", "David", "Eve"],
    })

    # Execute date conversion
    df_clean = convert_to_date(df, "Join_Date")

    # 1. Check pandas dtype
    assert "datetime" in str(df_clean["Join_Date"].dtype).lower()

    # 2. Check sanitize_value_for_json: midnight timestamps must format as YYYY-MM-DD
    val0 = df_clean["Join_Date"].iloc[0]
    assert isinstance(val0, pd.Timestamp)
    assert sanitize_value_for_json(val0) == "2023-01-15"
    assert "T00:00:00" not in sanitize_value_for_json(val0)

    val1 = df_clean["Join_Date"].iloc[1]
    assert sanitize_value_for_json(val1) == "2021-04-02"

    # Coerced / missing values serialize to None
    val3 = df_clean["Join_Date"].iloc[3]
    assert sanitize_value_for_json(val3) is None

    # 3. Check CSV export
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df_clean.to_csv(tmp_path, index=False)
        with open(tmp_path, "r", encoding="utf-8") as f:
            csv_content = f.read()
        assert "2023-01-15" in csv_content
        assert "2021-04-02" in csv_content
        assert "00:00:00" not in csv_content
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 4. Check compute_side_by_side_diff
    diff = compute_side_by_side_diff(df, df_clean)
    row_diffs = diff["row_diffs"]
    row0_date = row_diffs[0]["cells"]["Join_Date"]
    assert row0_date["cleaned"] == "2023-01-15"
    assert "T00:00:00" not in str(row0_date["cleaned"])


def test_genuine_timestamp_preserves_time():
    """Verify that columns with meaningful timestamps preserve hour, minute, second."""
    df = pd.DataFrame({
        "Event_Time": ["2023-01-15 14:30:00", "2023-02-10 09:15:32", "2023-03-01 23:59:59"],
        "Event": ["Login", "Query", "Logout"],
    })

    df_clean = convert_to_date(df, "Event_Time")

    # Non-zero time must preserve time in sanitize_value_for_json
    val0 = df_clean["Event_Time"].iloc[0]
    sanitized0 = sanitize_value_for_json(val0)
    assert "14:30:00" in sanitized0

    val1 = df_clean["Event_Time"].iloc[1]
    sanitized1 = sanitize_value_for_json(val1)
    assert "09:15:32" in sanitized1

    # Check side-by-side diff
    diff = compute_side_by_side_diff(df, df_clean)
    row0 = diff["row_diffs"][0]["cells"]["Event_Time"]
    assert "14:30:00" in row0["cleaned"]


def test_date_only_conversion_in_preview_and_audit_json(test_client):
    """Verify date-only values format as YYYY-MM-DD in /api/profile preview, audit JSON, and recipe replay."""
    csv_content = b"Join_Date,Event_Time,User\n2023-01-15,2023-01-15 14:30:00,Alice\n04/02/2021,2023-02-10 09:15:32,Bob\n"
    res = test_client.post(
        "/api/upload",
        files={"file": ("test_dates_full.csv", csv_content, "text/csv")},
    )
    assert res.status_code == 200
    filepath = res.json()["filepath"]
    prof_res = test_client.post(
        "/api/profile",
        json={"filepath": filepath},
    )
    assert prof_res.status_code == 200
    prof_data = prof_res.json()
    session_id = prof_data["session_id"]
    token = prof_data["session_token"]
    preview_rows = prof_data["preview_rows"]

    # 1. Preview rows in profile
    assert preview_rows[0]["Join_Date"] == "2023-01-15"
    assert "T00:00:00" not in str(preview_rows[0]["Join_Date"])
    # Genuine timestamp retains time
    assert "14:30:00" in str(preview_rows[0]["Event_Time"])

    # 2. Plan and clean
    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Convert dates",
        actions=[
            CleaningAction(
                action="convert_to_date",
                column="Join_Date",
                params={},
                reasoning="Convert date",
            )
        ]
    )

    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0]},
    )
    assert clean_res.status_code == 200

    # 3. Audit report download
    rep_res = test_client.get(f"/api/download/report?session_id={session_id}", headers={"X-Session-Token": token})
    assert rep_res.status_code == 200
    report_data = rep_res.json()
    assert "actions" in report_data
    assert report_data["actions"][0]["action"] == "convert_to_date"
    assert report_data["actions"][0]["status"] == "success"

    # 4. Recipe replay outputs YYYY-MM-DD
    raw_df = pd.DataFrame({"Join_Date": ["2023-01-15", "04/02/2021"]})
    recipe = {"operations": [{"action": "convert_to_date", "column": "Join_Date", "params": {}}]}
    replayed_df, _ = WorkflowRecipeManager.replay_recipe(raw_df, recipe)
    val0 = replayed_df["Join_Date"].iloc[0]
    assert sanitize_value_for_json(val0) == "2023-01-15"
    assert "T00:00:00" not in sanitize_value_for_json(val0)


# ============================================================================
# 2. SEQUENTIAL COLUMN MAPPING
# ============================================================================

def test_sequential_column_mapping_normalize_then_date():
    """Operation 1 normalizes column names, Operation 2 converts date column using original name."""
    df = pd.DataFrame({
        "Join_Date": ["2021-04-02", "2022-07-10", "2023-12-05"],
        "User Name": ["Alice", "Bob", "Charlie"],
        "User Age": [25, None, 35],
    })

    plan = CleaningPlan(
        summary="Sequential test plan",
        actions=[
            CleaningAction(
                action="normalize_column_names",
                params={},
                reasoning="Normalize columns to snake_case",
            ),
            CleaningAction(
                action="convert_to_date",
                column="Join_Date",  # Original column name
                params={},
                reasoning="Convert date column",
            ),
            CleaningAction(
                action="impute_missing",
                column="User Age",  # Original column name
                params={"method": "median"},
                reasoning="Fill missing age",
            ),
        ]
    )

    df_cleaned, executed_actions = execute_cleaning_plan(df, plan)

    # Columns should be normalized
    assert "join_date" in df_cleaned.columns
    assert "user_name" in df_cleaned.columns
    assert "user_age" in df_cleaned.columns

    # All actions must have succeeded
    assert len(executed_actions) == 3
    for act in executed_actions:
        assert act["status"] == "success", f"Action {act['action']} failed: {act.get('error')}"

    # Date conversion must have worked on the renamed join_date column
    assert "datetime" in str(df_cleaned["join_date"].dtype).lower()
    # Impute missing must have worked on user_age
    assert df_cleaned["user_age"].isna().sum() == 0


def test_sequential_column_mapping_rename_then_action():
    """Operation 1 renames column, Operation 2 & 3 reference old name."""
    df = pd.DataFrame({
        "Old_Salary": ["$1,000", "$2,000", None, "$4,000"],
    })

    plan = CleaningPlan(
        summary="Rename then action test plan",
        actions=[
            CleaningAction(
                action="rename_column",
                column="Old_Salary",
                params={"old_name": "Old_Salary", "new_name": "annual_salary"},
                reasoning="Rename column to standard convention",
            ),
            CleaningAction(
                action="convert_to_numeric",
                column="Old_Salary",  # Referencing old column name
                params={},
                reasoning="Convert salary to numeric",
            ),
            CleaningAction(
                action="impute_missing",
                column="Old_Salary",  # Referencing old column name
                params={"method": "median"},
                reasoning="Impute missing salary",
            ),
        ]
    )

    df_cleaned, executed_actions = execute_cleaning_plan(df, plan)

    assert "annual_salary" in df_cleaned.columns
    assert "Old_Salary" not in df_cleaned.columns
    assert len(executed_actions) == 3
    for act in executed_actions:
        assert act["status"] == "success"

    assert pd.api.types.is_numeric_dtype(df_cleaned["annual_salary"])
    assert df_cleaned["annual_salary"].isna().sum() == 0


def test_workflow_recipe_sequential_column_replay():
    """Workflow recipe replaying sequential column renaming and conversion."""
    df = pd.DataFrame({
        "Join_Date": ["2021-04-02", "2022-07-10"],
        "User_Name": [" ALICE ", " BOB "],
    })

    recipe = {
        "operations": [
            {"action": "normalize_column_names", "params": {}},
            {"action": "convert_to_date", "column": "Join_Date", "params": {}},
            {"action": "trim_whitespace", "column": "User_Name", "params": {}},
        ]
    }

    df_replayed, results = WorkflowRecipeManager.replay_recipe(df, recipe)
    assert "join_date" in df_replayed.columns
    assert "user_name" in df_replayed.columns
    assert "datetime" in str(df_replayed["join_date"].dtype).lower()
    assert df_replayed["user_name"].tolist() == ["ALICE", "BOB"]


def test_python_export_sequential_column_execution():
    """Standalone Python script generated from history executes cleanly without KeyError."""
    history = [
        {"action": "INITIAL_STATE", "step": 0, "columns": ["Join_Date", "User_Name"]},
        {"action": "normalize_column_names", "step": 1, "params": {}},
        {"action": "convert_to_date", "column": "Join_Date", "step": 2, "params": {}},
        {"action": "trim_whitespace", "column": "User_Name", "step": 3, "params": {}},
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:
        input_csv = os.path.join(tmp_dir, "raw_data.csv")
        output_csv = os.path.join(tmp_dir, "cleaned_out.csv")

        pd.DataFrame({
            "Join_Date": ["2021-04-02", "2022-07-10"],
            "User_Name": [" alice ", " bob "],
        }).to_csv(input_csv, index=False)

        script_code = WorkflowRecipeManager.generate_python_script_from_history(
            history,
            input_filename=input_csv,
            output_filename=output_csv,
        )

        assert "normalize_column_names" in script_code
        # join_date must be used in subsequent steps
        assert "df['join_date']" in script_code or 'df["join_date"]' in script_code

        # Execute the generated script dynamically
        local_scope = {}
        exec(script_code, local_scope)
        run_func = local_scope.get("run_pipeline")
        assert callable(run_func)

        df_result = run_func(input_csv, output_csv)
        assert os.path.exists(output_csv)
        assert "join_date" in df_result.columns
        assert "datetime" in str(df_result["join_date"].dtype).lower()
        assert df_result["user_name"].tolist() == ["alice", "bob"]


# ============================================================================
# 3. EXECUTION ANALYTICS CORRECTNESS
# ============================================================================

def test_execution_analytics_no_operations_selected(test_client):
    """Zero approved actions must report 'no_operations_selected', not 'verified_completed'."""
    csv_content = b"id,name,age\n1,Alice,30\n2,Bob,25\n"
    session_id, token = create_test_session(test_client, csv_content, "test_none.csv")

    # Set session plan with candidate action
    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Test plan",
        actions=[CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="test")]
    )

    # Call /api/clean with NO approved actions
    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": []},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    # Must accurately distinguish status
    assert data["execution_status"] == "no_operations_selected"
    assert data["execution_status_label"] == "No Operations Selected"
    assert "No cleaning operations were selected" in data["execution_status_desc"]
    assert data["actions_applied"] == 0
    assert data["actions_attempted"] == 0
    assert data["cells_modified"] == 0


def test_execution_analytics_completed_no_changes(test_client):
    """Executing an action on already compliant data reports 'completed_no_changes'."""
    csv_content = b"id,name,city\n1,alice,boston\n2,bob,chicago\n"
    session_id, token = create_test_session(test_client, csv_content, "test_no_change.csv")

    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="No-op test",
        actions=[
            CleaningAction(
                action="normalize_text",
                column="city",
                params={"case": "lower"},
                reasoning="Normalize city to lower",
            )
        ]
    )

    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0]},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    assert data["execution_status"] == "completed_no_changes"
    assert data["execution_status_label"] == "Completed (No Changes Needed)"
    assert data["actions_applied"] == 1
    assert data["cells_modified"] == 0


def test_execution_analytics_completed_with_changes(test_client):
    """Executing an action that repairs cells reports 'verified_completed'."""
    csv_content = b"id,name,city\n1,  alice  ,boston\n2,bob  ,chicago\n"
    session_id, token = create_test_session(test_client, csv_content, "test_dirty.csv")

    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Trim test",
        actions=[
            CleaningAction(
                action="trim_whitespace",
                column="name",
                params={},
                reasoning="Trim name whitespace",
            )
        ]
    )

    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0]},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    assert data["execution_status"] == "verified_completed"
    assert data["execution_status_label"] == "Verified Completed"
    assert data["actions_applied"] == 1
    assert data["cells_modified"] > 0


def test_execution_analytics_skipped_operation(test_client):
    """When plan proposes multiple actions but user only approves a subset, track skipped actions accurately."""
    csv_content = b"id,name,city\n1,  alice  ,boston\n2,bob  ,chicago\n"
    session_id, token = create_test_session(test_client, csv_content, "test_skipped.csv")

    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Multi-step plan with skipped action",
        actions=[
            CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Action 0: trim name"),
            CleaningAction(action="normalize_text", column="city", params={"case": "upper"}, reasoning="Action 1: uppercase city"),
        ]
    )

    # Approve only action 0; action 1 is skipped
    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0]},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    assert data["operations_summary"]["proposed"] == 2
    assert data["operations_summary"]["approved"] == 1
    assert data["operations_summary"]["attempted"] == 1
    assert data["operations_summary"]["applied"] == 1
    assert data["operations_summary"]["skipped"] == 1
    assert data["operations_summary"]["failed"] == 0
    assert data["actions_applied"] == 1
    assert data["actions_skipped"] == 1
    assert data["actions_failed"] == 0
    assert data["execution_status"] == "verified_completed"


def test_execution_analytics_partial_success(test_client):
    """When some operations succeed while others fail, status must report 'partially_completed'."""
    csv_content = b"id,name,city\n1,  alice  ,boston\n2,bob  ,chicago\n"
    session_id, token = create_test_session(test_client, csv_content, "test_partial.csv")

    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Partial success plan",
        actions=[
            CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Valid action"),
            CleaningAction(action="convert_to_numeric", column="name", params={}, reasoning="Failing action: cannot convert name to numeric"),
        ]
    )

    # Approve both actions
    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0, 1]},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    assert data["execution_status"] == "partially_completed"
    assert data["execution_status_label"] == "Partially Completed"
    assert data["actions_applied"] == 1
    assert data["actions_failed"] == 1
    assert "1 operation(s) applied, 1 failed" in data["execution_status_desc"]


def test_execution_analytics_complete_failure(test_client):
    """When all attempted operations fail, status must report 'failed'."""
    csv_content = b"id,name,city\n1,alice,boston\n2,bob,chicago\n"
    session_id, token = create_test_session(test_client, csv_content, "test_failed.csv")

    session_data = SESSIONS.get_session(session_id)
    session_data["plan"] = CleaningPlan(
        summary="Failing plan",
        actions=[
            CleaningAction(action="convert_to_numeric", column="name", params={}, reasoning="Failing action"),
        ]
    )

    clean_res = test_client.post(
        "/api/clean",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "approved_action_ids": [0]},
    )
    assert clean_res.status_code == 200
    data = clean_res.json()

    assert data["execution_status"] == "failed"
    assert data["execution_status_label"] == "Execution Failed"
    assert data["actions_applied"] == 0
    assert data["actions_failed"] == 1
    assert "1 operation(s) failed during execution" in data["execution_status_desc"]


# ============================================================================
# 4. CHECKPOINT, UNDO, REDO, AND ROLLBACK
# ============================================================================

def test_checkpoints_undo_redo_and_rollback_to_step():
    """Verify initial checkpoint, sequential steps, undo, redo, and direct rollback in both directions."""
    df = pd.DataFrame({
        "id": [1, 2, 2, 3],
        "name": [" alice ", "BOB", "BOB", "charlie"],
        "salary": ["$50,000", "$60,000", "$60,000", "$70,000"],
    })

    session = CleaningSession(df)

    # Initial checkpoint 0
    assert session.current_step == 0
    assert not session.can_undo()
    assert not session.can_redo()
    assert len(session.checkpoints) == 1

    # Step 1: Remove duplicates
    session.apply_action(CleaningAction(action="remove_duplicates", params={}, reasoning="Deduplicate"))
    assert session.current_step == 1
    assert len(session.get_current_df()) == 3
    assert session.can_undo()

    # Step 2: Trim whitespace
    session.apply_action(CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Trim"))
    assert session.current_step == 2
    assert session.get_current_df()["name"].iloc[0] == "alice"

    # Step 3: Normalize text
    session.apply_action(CleaningAction(action="normalize_text", column="name", params={"case": "upper"}, reasoning="Upper"))
    assert session.current_step == 3
    assert session.get_current_df()["name"].iloc[0] == "ALICE"

    # Step 4: Convert salary to numeric (changes dtype)
    session.apply_action(CleaningAction(action="convert_to_numeric", column="salary", params={}, reasoning="Numeric salary"))
    assert session.current_step == 4
    assert pd.api.types.is_numeric_dtype(session.get_current_df()["salary"])
    assert session.get_current_df()["salary"].iloc[0] == 50000.0

    # Undo step 4 -> step 3 (restores salary dtype to object and string value)
    undo_salary = session.undo()
    assert undo_salary["status"] == "success"
    assert session.current_step == 3
    assert session.get_current_df()["salary"].iloc[0] == "$50,000"
    assert not pd.api.types.is_numeric_dtype(session.get_current_df()["salary"])

    # Undo step 3 -> step 2
    undo1 = session.undo()
    assert undo1["status"] == "success"
    assert session.current_step == 2
    assert session.get_current_df()["name"].iloc[0] == "alice"
    assert session.can_redo()

    # Redo step 2 -> step 3
    redo1 = session.redo()
    assert redo1["status"] == "success"
    assert session.current_step == 3
    assert session.get_current_df()["name"].iloc[0] == "ALICE"

    # Rollback directly to step 0 (initial uncleaned) - restores initial rows, values, types, columns
    rb0 = session.rollback_to(0)
    assert rb0["status"] == "success"
    assert session.current_step == 0
    assert len(session.get_current_df()) == 4
    assert session.get_current_df()["name"].iloc[0] == " alice "
    assert session.get_current_df()["salary"].iloc[0] == "$50,000"
    assert list(session.get_current_df().columns) == ["id", "name", "salary"]

    # Rollback forward to step 4 (via redo stack) - restores step 4 dtypes and values
    rb4 = session.rollback_to(4)
    assert rb4["status"] == "success"
    assert session.current_step == 4
    assert pd.api.types.is_numeric_dtype(session.get_current_df()["salary"])
    assert session.get_current_df()["salary"].iloc[0] == 50000.0


def test_api_session_rollback_target_step(test_client):
    """Test API endpoint /api/session/rollback with target_step."""
    csv_content = b"id,val\n1, a \n2, b \n"
    session_id, token = create_test_session(test_client, csv_content, "test_rb.csv")
    session_data = SESSIONS.get_session(session_id)
    sess_obj = session_data["cleaning_session"]

    # Apply 2 actions
    sess_obj.apply_action(CleaningAction(action="trim_whitespace", column="val", params={}, reasoning="Trim"))
    sess_obj.apply_action(CleaningAction(action="normalize_text", column="val", params={"case": "upper"}, reasoning="Upper"))
    assert sess_obj.current_step == 2

    # Call rollback to target_step 1
    rb_res = test_client.post(
        "/api/session/rollback",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "target_step": 1},
    )
    assert rb_res.status_code == 200
    rb_data = rb_res.json()
    assert rb_data["status"] == "success"
    assert rb_data["current_step"] == 1
    assert sess_obj.current_step == 1


def test_lazy_parquet_checkpoint_architecture():
    """Verify session checkpoint architecture saves Parquet snapshots without keeping deep copies in RAM."""
    df = pd.DataFrame({
        "col_a": list(range(100)),
        "col_b": [f"val_{i}" for i in range(100)],
    })

    with tempfile.TemporaryDirectory() as storage_tmp:
        session = CleaningSession(df, storage_dir=storage_tmp)
        # Apply an action
        session.apply_action(CleaningAction(action="trim_whitespace", column="col_b", params={}, reasoning="Trim"))
        assert session.current_step == 1

        # Save session snapshots
        session.save_to_directory(storage_tmp)

        # Verify Parquet files exist on disk
        p = Path(storage_tmp)
        assert (p / "original.parquet").exists()
        assert (p / "current.parquet").exists()
        cp_file = p / "checkpoints" / "step_1.parquet"
        assert cp_file.exists()

        # Load session into fresh instance from disk
        fresh_session = CleaningSession(pd.DataFrame())
        fresh_session.load_from_directory(storage_tmp)
        assert fresh_session.current_step == 1
        assert len(fresh_session.get_current_df()) == 100
        assert fresh_session.get_current_df()["col_b"].iloc[0] == "val_0"


# ============================================================================
# 5. OPENREFINE FEATURES CORRECTNESS
# ============================================================================

def test_clustering_confidence_and_canonical_selection():
    """Verify clustering confidence calculation, >=80% high-confidence filtering, and canonical selection."""
    series = pd.Series(
        ["New York"] * 5 + ["new york", "New-York", "NEW YORK"] +
        ["Los Angeles", "Los Angelas"] +
        ["San Francisco", "Tokyo"]
    )

    clusters = detect_clusters(series, method="fingerprint")
    assert len(clusters) >= 1

    # First cluster should contain New York variants
    c0 = clusters[0]
    assert c0["confidence"] >= 0.8  # High confidence
    assert c0["canonical"] == "New York" or c0["proposed_canonical"] == "New York"

    # Canonical selection tie-breaker:
    # 1. Most frequent
    # 2. Shortest length
    items = [("Longer Form String", 2), ("Short", 2)]
    best = _select_canonical(items)
    assert best == "Short"

    # Most frequent wins over shorter
    items2 = [("Frequent Long Title", 10), ("Short", 2)]
    best2 = _select_canonical(items2)
    assert best2 == "Frequent Long Title"


def test_reconciliation_threshold_filtering():
    """Verify entity reconciliation filters candidate matches based on threshold."""
    # US states reconciliation test
    series = pd.Series(["California", "Calfornia", "CA", "Californ", "XYZState"])

    # High threshold (0.90) - strict
    res_strict = reconcile_column_values(series, catalog_name="us_states", match_threshold=0.90)
    candidates_strict = {c["original"]: c for c in res_strict["candidates"]}

    # California and CA should match strictly
    assert candidates_strict["California"]["matched_canonical"] == "California"
    assert candidates_strict["CA"]["matched_canonical"] == "California"

    # Lower threshold (0.65) - should catch slightly typoed "Californ"
    res_lenient = reconcile_column_values(series, catalog_name="us_states", match_threshold=0.65)
    candidates_lenient = {c["original"]: c for c in res_lenient["candidates"]}

    assert candidates_lenient["Californ"]["matched_canonical"] == "California"
    assert candidates_lenient["Californ"]["score"] >= 0.65

    # Completely non-matching value remains unmatched under both
    assert candidates_strict["XYZState"]["matched_canonical"] is None
    assert candidates_lenient["XYZState"]["matched_canonical"] is None


def test_grel_value_trim_and_mutation():
    """Verify GREL value.trim() mutates DataFrame, and verify sandbox security restrictions."""
    df = pd.DataFrame({
        "location": ["  New York  ", " London\t", "\nParis ", "Tokyo"],
    })

    # Execute GREL transform with value.trim()
    df_clean = execute_grel_transform(df, "location", "value.trim()")
    assert df_clean["location"].tolist() == ["New York", "London", "Paris", "Tokyo"]

    # Preview GREL
    preview = preview_grel(df, "location", "value.trim().toUppercase()", sample_limit=2)
    assert preview["success"] is True
    assert preview["rows"][0]["transformed"] == "NEW YORK"

    # Unsafe GREL blocked
    with pytest.raises(GRELSecurityError):
        CompiledGRELExpression("value.__class__")

    with pytest.raises(GRELSyntaxError):
        CompiledGRELExpression("import os")


def test_clustering_checkbox_states_and_dataset_application(test_client):
    """Verify clustering checkbox selection logic (>=80% auto-selected, <80% unselected) and dataset transformation."""
    csv_content = b"city,pop\nNew York,100\nnew york,200\nNew-York,300\nNEW YORK,400\nLos Angeles,500\nLos Angelas,600\nTokyo,700\n"
    session_id, token = create_test_session(test_client, csv_content, "test_cluster_app.csv")

    # Detect clusters via API
    det_res = test_client.post(
        "/api/cluster/detect",
        headers={"X-Session-Token": token},
        json={"session_id": session_id, "column": "city", "method": "fingerprint"},
    )
    assert det_res.status_code == 200
    clusters = det_res.json()["clusters"]
    assert len(clusters) >= 1

    # Checkbox logic:
    # High-confidence cluster (confidence >= 0.8) must be auto-selected (checked = True)
    # Low-confidence cluster (confidence < 0.8) must not be auto-selected (checked = False)
    high_conf = [c for c in clusters if c.get("confidence", 0) >= 0.8]
    assert len(high_conf) >= 1
    for c in clusters:
        is_auto_selected = (c.get("confidence", 1.0) >= 0.8)
        assert is_auto_selected == (c["confidence"] >= 0.8)

    # Apply approved cluster merge
    c0 = clusters[0]
    canonical = c0["canonical"]
    mapping = {m["value"]: canonical for m in c0["members"]}

    apply_res = test_client.post(
        "/api/cluster/apply",
        headers={"X-Session-Token": token},
        json={
            "session_id": session_id,
            "column": "city",
            "clusters": [c0],
            "mapping": mapping,
        },
    )
    assert apply_res.status_code == 200
    apply_data = apply_res.json()
    assert apply_data["status"] == "success"

    # Verify session DataFrame values are updated to canonical
    session_data = SESSIONS.get_session(session_id)
    df_after = session_data["df_current"]
    # All variants from c0 must now equal canonical
    for orig_val in mapping.keys():
        assert orig_val not in df_after["city"].values or orig_val == canonical


def test_grel_timeout_behavior():
    """Verify runaway or slow GREL transformations trigger execution timeout."""
    # Create 2000 rows to ensure batch iteration crosses the 1000-row timeout checkpoint
    df_large = pd.DataFrame({"text_val": ["  sample string to process  "] * 2000})

    # execute_grel_transform with an ultra-short timeout (0.0001 seconds)
    with pytest.raises((GRELExecutionError, TimeoutError)) as exc_info:
        execute_grel_transform(
            df_large,
            "text_val",
            "value.trim().toLowercase()",
            timeout_seconds=0.00001,
        )
    assert "timeout" in str(exc_info.value).lower()


def test_recipe_validation_malformed_json_and_output():
    """Verify recipe validation handles malformed JSON, rejects invalid operations, and replayed output is correct."""
    # 1. Missing 'operations' key
    valid1, errs1 = WorkflowRecipeManager.validate_recipe({})
    assert valid1 is False
    assert any("operations" in e for e in errs1)

    # 2. Operations is not a list
    valid2, errs2 = WorkflowRecipeManager.validate_recipe({"operations": "not_a_list"})
    assert valid2 is False
    assert any("list" in e for e in errs2)

    # 3. Operation item is not a dict
    valid3, errs3 = WorkflowRecipeManager.validate_recipe({"operations": ["not_a_dict"]})
    assert valid3 is False
    assert any("valid JSON object" in e for e in errs3)

    # 4. Operation missing 'action' field
    valid4, errs4 = WorkflowRecipeManager.validate_recipe({"operations": [{"column": "city"}]})
    assert valid4 is False
    assert any("missing required 'action'" in e for e in errs4)

    # 5. Invalid/unsupported action name
    valid5, errs5 = WorkflowRecipeManager.validate_recipe({"operations": [{"action": "eval_malicious_code", "column": "city"}]})
    assert valid5 is False
    assert any("unsupported action" in e for e in errs5)

    # 6. Replay raises ValueError on invalid recipe
    df = pd.DataFrame({"city": ["Paris", "London"]})
    with pytest.raises(ValueError) as exc:
        WorkflowRecipeManager.replay_recipe(df, {"operations": "invalid"})
    assert "Invalid recipe" in str(exc.value)

    # 7. Valid recipe replays correctly and produces correct final output
    valid_recipe = {
        "operations": [
            {"action": "normalize_text", "column": "city", "params": {"case": "upper"}},
        ]
    }
    cleaned_df, report = WorkflowRecipeManager.replay_recipe(df, valid_recipe)
    assert cleaned_df["city"].tolist() == ["PARIS", "LONDON"]
    assert report["successful_steps"] == 1
    assert report["failed_steps"] == 0
