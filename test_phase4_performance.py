"""
Unit & Integration Regression Test Suite for Phase 4:
Memory Optimization, Performance, and Resource Protection.
"""

import os
import shutil
import tempfile
import time
from pathlib import Path
import pandas as pd
import pytest
from fastapi import HTTPException

from grel_engine import (
    GRELExecutionError,
    GRELSecurityError,
    _validate_regex_safety,
    execute_grel_transform,
    preview_grel,
)
from openrefine_clustering import (
    ClusterList,
    MAX_UNIQUE_VALUES_DISTANCE,
    cluster_by_distance,
    detect_clusters,
)
from profiler import (
    MAX_DATASET_ROWS,
    MAX_UPLOAD_SIZE_BYTES,
    profile_dataset,
)
from schemas import CleaningAction
from server import heavy_operation_slot
from session import CleaningSession, LazyCheckpoint


@pytest.fixture
def sample_benchmark_df():
    """Generates a clean tabular dataset for performance testing."""
    return pd.DataFrame({
        "id": range(1, 101),
        "name": [f" User_{i} " for i in range(1, 101)],
        "city": ["New York", "new york", "San Francisco", "London"] * 25,
        "salary": [f"${40000 + i * 500}" for i in range(1, 101)],
        "notes": [f"Note {i}" for i in range(1, 101)],
    })


def test_checkpoint_lazy_eviction_and_on_demand_loading(sample_benchmark_df):
    """Verify older checkpoints have in-memory DataFrames evicted to Parquet and reloaded on demand."""
    temp_dir = Path(tempfile.mkdtemp(prefix="datarefine_test_cp_"))
    try:
        session = CleaningSession(
            sample_benchmark_df,
            session_name="test_lazy",
            storage_dir=temp_dir,
            max_in_memory_checkpoints=2,
        )

        # Apply 5 actions to exceed max_in_memory_checkpoints (2)
        session.apply_action(CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Trim 1"))
        session.apply_action(CleaningAction(action="normalize_whitespace", column="city", params={}, reasoning="Norm 2"))
        session.apply_action(CleaningAction(action="remove_column", column="notes", params={}, reasoning="Drop 3"))
        session.apply_action(CleaningAction(action="normalize_text", column="city", params={"case": "upper"}, reasoning="Upper 4"))
        session.apply_action(CleaningAction(action="trim_whitespace", column="salary", params={}, reasoning="Trim 5"))

        assert session.current_step == 5
        assert len(session.checkpoints) == 6  # step 0 + 5 actions

        # Checkpoints older than step 4 should have their in-memory DataFrame evicted
        step1_cp = session.checkpoints[1]
        assert isinstance(step1_cp, LazyCheckpoint)
        assert step1_cp._cached_df is None, "Historical checkpoint must evict in-memory DataFrame"

        # On-demand access to step 1 must transparently reload DataFrame from disk
        df_step1 = step1_cp.get_df()
        assert isinstance(df_step1, pd.DataFrame)
        assert len(df_step1) == 100
        assert "notes" in df_step1.columns  # notes was deleted in step 3, must still exist in step 1!

        # Checkpoint dictionary access via ['df'] must also work seamlessly
        df_dict_access = step1_cp["df"]
        assert isinstance(df_dict_access, pd.DataFrame)
        assert "notes" in df_dict_access.columns
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_undo_redo_preserves_data_integrity_after_eviction(sample_benchmark_df):
    """Verify undo and redo cycle functions identically even after intermediate checkpoints are evicted to disk."""
    temp_dir = Path(tempfile.mkdtemp(prefix="datarefine_test_integrity_"))
    try:
        session = CleaningSession(
            sample_benchmark_df,
            session_name="test_integrity",
            storage_dir=temp_dir,
            max_in_memory_checkpoints=2,
        )

        session.apply_action(CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Trim"))
        session.apply_action(CleaningAction(action="remove_column", column="notes", params={}, reasoning="Drop notes"))
        session.apply_action(CleaningAction(action="normalize_text", column="city", params={"case": "title"}, reasoning="Title city"))

        assert "notes" not in session.get_current_df().columns
        assert session.current_step == 3

        # Undo 2 operations back to step 1 (before notes removal)
        res_undo1 = session.undo()
        assert res_undo1["status"] == "success"
        res_undo2 = session.undo()
        assert res_undo2["status"] == "success"

        assert session.current_step == 1
        assert "notes" in session.get_current_df().columns
        assert session.get_current_df()["name"].iloc[0] == "User_1"  # Trimmed

        # Redo back forward
        res_redo = session.redo()
        assert res_redo["status"] == "success"
        assert session.current_step == 2
        assert "notes" not in session.get_current_df().columns
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_corrupted_checkpoint_recovery_safety(sample_benchmark_df):
    """Verify that a corrupted Parquet snapshot file on disk fails safely without crashing."""
    temp_dir = Path(tempfile.mkdtemp(prefix="datarefine_test_corrupt_"))
    try:
        session = CleaningSession(
            sample_benchmark_df,
            session_name="test_corrupt",
            storage_dir=temp_dir,
            max_in_memory_checkpoints=1,
        )

        session.apply_action(CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Trim"))
        session.apply_action(CleaningAction(action="normalize_whitespace", column="city", params={}, reasoning="Norm"))

        # Corrupt step 1's parquet file
        step1_pq = temp_dir / "checkpoints" / "step_1.parquet"
        assert step1_pq.exists()
        with open(step1_pq, "wb") as f:
            f.write(b"CORRUPTED_PARQUET_FILE_GARBAGE_BYTES")

        # Accessing corrupted checkpoint must not raise an unhandled crash
        cp1 = session.checkpoints[1]
        recovered_df = cp1.get_df()
        assert isinstance(recovered_df, pd.DataFrame)
        assert len(recovered_df) > 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_profiler_file_size_and_row_limits():
    """Verify that profiler rejects files exceeding maximum size and row boundaries."""
    temp_dir = Path(tempfile.mkdtemp(prefix="datarefine_limits_"))
    try:
        # 1. Test empty file rejection
        empty_csv = temp_dir / "empty.csv"
        empty_csv.write_text("")
        with pytest.raises(ValueError, match="empty"):
            profile_dataset(str(empty_csv))

        # 2. Test row limit rejection
        small_csv = temp_dir / "oversized_rows.csv"
        df_rows = pd.DataFrame({"col": range(100)})
        df_rows.to_csv(small_csv, index=False)

        # Temporarily set MAX_DATASET_ROWS lower for testing
        import profiler
        orig_max_rows = profiler.MAX_DATASET_ROWS
        try:
            profiler.MAX_DATASET_ROWS = 50
            with pytest.raises(ValueError, match="exceeding maximum supported limit"):
                profile_dataset(str(small_csv))
        finally:
            profiler.MAX_DATASET_ROWS = orig_max_rows
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_clustering_cardinality_capping_and_warning():
    """Verify that clustering columns with excessive distinct values caps evaluation and returns warnings."""
    # Generate series with 3,500 distinct values
    values = [f"Item_{i:04d}" for i in range(3500)]
    series = pd.Series(values)

    clusters = detect_clusters(series, method="levenshtein")
    assert isinstance(clusters, ClusterList)
    assert clusters.total_unique == 3500
    assert clusters.evaluated_unique <= MAX_UNIQUE_VALUES_DISTANCE
    assert clusters.truncated is True
    assert clusters.warning is not None
    assert "capped" in clusters.warning.lower()


def test_clustering_timeout_protection():
    """Verify that clustering enforces timeout limits and returns partial results safely."""
    # Generate series of varied strings
    values = [f"TextVariant_{i % 50}_{i}" for i in range(1000)]
    series = pd.Series(values)

    # Invoke with an ultra-short timeout (0.0001s)
    clusters = detect_clusters(series, method="levenshtein", params={"timeout_seconds": 0.0001})
    assert isinstance(clusters, ClusterList)
    assert clusters.timeout is True
    assert clusters.truncated is True
    assert "timeout" in clusters.warning.lower()


def test_grel_redos_protection():
    """Verify that nested quantifiers and ReDoS regex patterns are blocked by GREL engine."""
    # Dangerous nested quantifiers
    dangerous_patterns = [
        r"(a+)+$",
        r"([a-zA-Z]+)+",
        r"(\d+)+",
        r"(.*a)+",
        r"(a+){2,}",
        r"(foo|foo)+",
    ]
    for pattern in dangerous_patterns:
        with pytest.raises(GRELSecurityError, match="ReDoS risk|nested quantifier|alternation"):
            _validate_regex_safety(pattern)


def test_grel_execution_timeout_protection():
    """Verify that long-running GREL transformations abort with GRELExecutionError when timeout is reached."""
    # 2,000 rows
    df = pd.DataFrame({"text": [f"Sample text row {i}" for i in range(2000)]})

    # With timeout_seconds=0.00001, processing 2,000 rows must hit the timeout check
    with pytest.raises(GRELExecutionError, match="exceeded maximum execution timeout"):
        execute_grel_transform(
            df,
            column="text",
            expression="value.trim().toUppercase()",
            timeout_seconds=0.00001,
        )


def test_concurrency_semaphore_saturation():
    """Verify that heavy operations semaphore rejects requests with HTTP 503 when saturated."""
    import server

    # Save original semaphore and replace with single-capacity semaphore for unit testing
    orig_sem = server.heavy_ops_semaphore
    try:
        import threading
        test_sem = threading.BoundedSemaphore(1)
        server.heavy_ops_semaphore = test_sem

        # Acquire the single available slot
        with heavy_operation_slot():
            # A second attempt with a 0.05s timeout must be rejected with 503
            with pytest.raises(HTTPException) as exc_info:
                with heavy_operation_slot(timeout=0.05):
                    pass
            assert exc_info.value.status_code == 503
            assert "concurrency limit reached" in exc_info.value.detail.lower()
    finally:
        server.heavy_ops_semaphore = orig_sem
