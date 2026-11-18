"""
Cleaning Session Manager for DataRefine.

Provides step-by-step undo, redo, checkpointing, and rollback capabilities
for interactive cleaning sessions.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
import pandas as pd

from actions import execute_action
from schemas import CleaningAction


class LazyCheckpoint(dict):
    """
    Memory-efficient checkpoint dictionary that supports lazy DataFrame loading
    from on-disk Parquet snapshots, avoiding keeping full DataFrame deep copies in RAM.
    Inherits from dict for 100% backward compatibility with dictionary operations,
    JSON serialization, and dict-like access.
    """

    def __init__(
        self,
        *args,
        session: Optional["CleaningSession"] = None,
        parquet_path: Optional[Union[str, Path]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._session = session
        self._parquet_path = Path(parquet_path).resolve() if parquet_path else None
        # In-memory dataframe reference (can be evicted to save RAM)
        self._cached_df: Optional[pd.DataFrame] = kwargs.get("df")

    @property
    def is_cached_in_memory(self) -> bool:
        return self._cached_df is not None

    def get_df(self) -> Optional[pd.DataFrame]:
        # 1. Fast path: in-memory cache hit
        if self._cached_df is not None:
            return self._cached_df

        step = super().get("step", 0)

        # 2. Check if this step is pristine initial state
        if step == 0 and self._session is not None and self._session.initial_df is not None:
            return self._session.initial_df.copy(deep=True)

        # 3. Resolve Parquet file path on disk
        pq_path = self._parquet_path
        if (pq_path is None or not pq_path.exists()) and self._session:
            storage_dir = self._session.storage_dir or getattr(self._session, "_temp_storage_dir", None)
            if storage_dir:
                pq_path = Path(storage_dir) / "checkpoints" / f"step_{step}.parquet"

        # 4. Load from Parquet snapshot
        if pq_path and pq_path.exists():
            try:
                loaded = pd.read_parquet(pq_path)
                return loaded
            except Exception:
                # Corrupted checkpoint fallback: fail safely
                if self._session and self._session.initial_df is not None:
                    return self._session.initial_df.copy(deep=True)

        # 5. Dict fallback
        val = super().get("df")
        if isinstance(val, pd.DataFrame):
            return val

        if self._session and self._session.initial_df is not None:
            return self._session.initial_df.copy(deep=True)
        return None

    def __getitem__(self, key: str) -> Any:
        if key == "df":
            return self.get_df()
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "df":
            val = self.get_df()
            return val if val is not None else default
        return super().get(key, default)

    def evict_df(self) -> None:
        """Evict the in-memory DataFrame copy to free RAM."""
        self._cached_df = None
        dict.__setitem__(self, "df", None)

    def persist_to_disk(self, target_path: Path) -> bool:
        """Persist DataFrame to Parquet before eviction."""
        if self._cached_df is not None:
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                self._cached_df.to_parquet(target_path, index=False)
                self._parquet_path = target_path
                return True
            except Exception:
                return False
        return False


class CheckpointList(list):
    """
    List of checkpoints that automatically wraps dictionary entries in LazyCheckpoint
    and triggers eviction of excess in-memory DataFrames to prevent RAM bloat.
    """

    def __init__(self, iterable=None, session=None):
        super().__init__()
        self.session = session
        if iterable:
            for item in iterable:
                if isinstance(item, dict) and not isinstance(item, LazyCheckpoint):
                    item = LazyCheckpoint(item, session=self.session, df=item.get("df"))
                elif isinstance(item, LazyCheckpoint):
                    item._session = self.session
                super().append(item)

    def append(self, item: Any) -> None:
        if isinstance(item, dict) and not isinstance(item, LazyCheckpoint):
            item = LazyCheckpoint(item, session=self.session, df=item.get("df"))
        elif isinstance(item, LazyCheckpoint):
            item._session = self.session
        super().append(item)
        if self.session and hasattr(self.session, "checkpoints"):
            self.session._evict_excess_checkpoints()

    def insert(self, index: int, item: Any) -> None:
        if isinstance(item, dict) and not isinstance(item, LazyCheckpoint):
            item = LazyCheckpoint(item, session=self.session, df=item.get("df"))
        elif isinstance(item, LazyCheckpoint):
            item._session = self.session
        super().insert(index, item)
        if self.session and hasattr(self.session, "checkpoints"):
            self.session._evict_excess_checkpoints()


class CleaningSession:
    """
    Maintains a full checkpoint history of DataFrame states, allowing step-by-step
    undo/redo, jump-to-step rollback, and delta inspections with memory-efficient
    lazy on-disk Parquet snapshots.
    """

    def __init__(
        self,
        df: pd.DataFrame,
        session_name: str = "default",
        storage_dir: Optional[Union[str, Path]] = None,
        max_in_memory_checkpoints: int = 2,
    ):
        self.session_name = session_name
        self.storage_dir = Path(storage_dir).resolve() if storage_dir else None
        self._temp_storage_dir: Optional[Path] = None
        self.max_in_memory_checkpoints = max(1, int(max_in_memory_checkpoints))
        self.initial_df = df.copy(deep=True)
        self.current_df = df.copy(deep=True)

        # Step 0 is the initial pristine state
        init_cp = LazyCheckpoint(
            {
                "step": 0,
                "action": None,
                "action_name": "INITIAL_STATE",
                "column": None,
                "params": {},
                "df": self.initial_df,
                "timestamp": datetime.now().isoformat(),
                "rows": len(df),
                "columns": list(df.columns),
                "description": "Initial dataset before any cleaning operations",
            },
            session=self,
            df=self.initial_df,
        )

        self.checkpoints: CheckpointList = CheckpointList([init_cp], session=self)

        # Stack for redoing undone steps
        self.redo_stack: CheckpointList = CheckpointList(session=self)

        # Audit log of all actions applied
        self.executed_history: List[Dict[str, Any]] = []

    def _get_or_create_temp_dir(self) -> Path:
        """Create a session-scoped temporary directory for lazy Parquet snapshots if no storage_dir is set."""
        if self._temp_storage_dir is None:
            self._temp_storage_dir = Path(tempfile.mkdtemp(prefix="datarefine_session_")).resolve()
        return self._temp_storage_dir

    def _evict_excess_checkpoints(self) -> None:
        """
        Evict DataFrame copies from older checkpoints when number of in-memory
        checkpoints exceeds max_in_memory_checkpoints (default: 2).
        Saves evicted steps to Parquet on disk before eviction to preserve data integrity.
        """
        if not hasattr(self, "checkpoints") or len(self.checkpoints) <= self.max_in_memory_checkpoints:
            return

        storage_dir = self.storage_dir or self._get_or_create_temp_dir()
        cp_dir = storage_dir / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)

        cutoff = max(1, len(self.checkpoints) - self.max_in_memory_checkpoints)

        for i in range(cutoff):
            cp = self.checkpoints[i]
            if isinstance(cp, LazyCheckpoint):
                step = cp.get("step", i)
                if step > 0 and cp.is_cached_in_memory:
                    pq_path = cp_dir / f"step_{step}.parquet"
                    if not pq_path.exists():
                        cp.persist_to_disk(pq_path)
                    cp.evict_df()
            elif isinstance(cp, dict) and cp.get("df") is not None and i > 0:
                step = cp.get("step", i)
                pq_path = cp_dir / f"step_{step}.parquet"
                if not pq_path.exists() and isinstance(cp["df"], pd.DataFrame):
                    try:
                        cp["df"].to_parquet(pq_path, index=False)
                    except Exception:
                        pass
                lazy_cp = LazyCheckpoint(cp, session=self, parquet_path=pq_path, df=cp.get("df"))
                lazy_cp.evict_df()
                self.checkpoints[i] = lazy_cp

    def __del__(self):
        """Clean up temporary snapshot directory if created."""
        if self._temp_storage_dir and self._temp_storage_dir.exists():
            try:
                shutil.rmtree(self._temp_storage_dir, ignore_errors=True)
            except Exception:
                pass

    @property
    def current_step(self) -> int:
        return len(self.checkpoints) - 1

    def can_undo(self) -> bool:
        return len(self.checkpoints) > 1

    def can_redo(self) -> bool:
        return len(self.redo_stack) > 0

    def get_current_df(self) -> pd.DataFrame:
        return self.current_df

    def get_checkpoint_df(self, step: int) -> pd.DataFrame:
        """Retrieve the DataFrame for a given checkpoint step, loading from disk if evicted."""
        if step < 0 or step >= len(self.checkpoints):
            raise IndexError(f"Step {step} out of bounds (0 to {len(self.checkpoints) - 1}).")

        cp = self.checkpoints[step]
        if isinstance(cp, LazyCheckpoint):
            df = cp.get_df()
            if df is not None:
                return df

        val = cp.get("df")
        if isinstance(val, pd.DataFrame):
            return val

        if step == 0 and self.initial_df is not None:
            return self.initial_df.copy(deep=True)
        return self.current_df.copy(deep=True)

    def apply_action(self, action: Any) -> Dict[str, Any]:
        """Execute a cleaning action, record before/after states, and push a checkpoint."""
        if isinstance(action, dict):
            action_obj = CleaningAction(**action)
        else:
            action_obj = action

        before_rows = len(self.current_df)
        before_cols = list(self.current_df.columns)
        before_missing = int(self.current_df.isna().sum().sum())

        # Execute deterministic action
        transformed_df = execute_action(self.current_df, action_obj)

        after_rows = len(transformed_df)
        after_cols = list(transformed_df.columns)
        after_missing = int(transformed_df.isna().sum().sum())

        new_step = self.current_step + 1
        action_name = getattr(action_obj, "action", str(action_obj))
        target_col = getattr(action_obj, "column", None)
        col_desc = f" on '{target_col}'" if target_col else ""
        formatted_name = action_name.replace("_", " ").title()

        checkpoint = {
            "step": new_step,
            "action": action_obj.model_dump() if hasattr(action_obj, "model_dump") else action_obj,
            "action_name": action_name,
            "column": target_col,
            "params": getattr(action_obj, "params", {}),
            "df": transformed_df.copy(deep=True),
            "timestamp": datetime.now().isoformat(),
            "rows": after_rows,
            "columns": after_cols,
            "description": f"{formatted_name}{col_desc}",
            "diff": {
                "rows_delta": after_rows - before_rows,
                "cols_delta": len(after_cols) - len(before_cols),
                "missing_delta": after_missing - before_missing,
            },
        }

        self.current_df = transformed_df
        self.checkpoints.append(checkpoint)
        self.redo_stack.clear()

        success_record = {
            "step": new_step,
            "action": action_name,
            "action_name": formatted_name,
            "column": target_col,
            "params": getattr(action_obj, "params", {}),
            "reasoning": getattr(action_obj, "reasoning", ""),
            "status": "success",
            "rows_before": before_rows,
            "rows_after": after_rows,
            "timestamp": checkpoint["timestamp"],
            "description": checkpoint["description"],
        }
        self.executed_history.append(success_record)

        return success_record

    def undo(self) -> Dict[str, Any]:
        """Undo the most recent cleaning operation."""
        if not self.can_undo():
            return {
                "status": "noop",
                "message": "Cannot undo: already at initial dataset state.",
                "current_step": self.current_step,
            }

        last_checkpoint = self.checkpoints.pop()
        self.redo_stack.append(last_checkpoint)

        # Restore previous DataFrame state
        previous_checkpoint = self.checkpoints[-1]
        self.current_df = previous_checkpoint["df"].copy(deep=True)

        return {
            "status": "success",
            "undone_step": last_checkpoint["step"],
            "undone_action": last_checkpoint["action_name"],
            "undone_column": last_checkpoint["column"],
            "current_step": previous_checkpoint["step"],
            "remaining_checkpoints": len(self.checkpoints),
            "message": f"Undid step {last_checkpoint['step']}: {last_checkpoint['action_name']} on '{last_checkpoint['column']}'.",
            "rows": len(self.current_df),
            "columns": list(self.current_df.columns),
        }

    def redo(self) -> Dict[str, Any]:
        """Redo the most recently undone cleaning operation."""
        if not self.can_redo():
            return {
                "status": "noop",
                "message": "Cannot redo: redo stack is empty.",
                "current_step": self.current_step,
            }

        checkpoint_to_restore = self.redo_stack.pop()
        self.checkpoints.append(checkpoint_to_restore)
        self.current_df = checkpoint_to_restore["df"].copy(deep=True)

        return {
            "status": "success",
            "redone_step": checkpoint_to_restore["step"],
            "redone_action": checkpoint_to_restore["action_name"],
            "redone_column": checkpoint_to_restore["column"],
            "current_step": checkpoint_to_restore["step"],
            "remaining_redo": len(self.redo_stack),
            "message": f"Redid step {checkpoint_to_restore['step']}: {checkpoint_to_restore['action_name']} on '{checkpoint_to_restore['column']}'.",
            "rows": len(self.current_df),
            "columns": list(self.current_df.columns),
        }

    def rollback_to(self, target_step: int) -> Dict[str, Any]:
        """Roll back or restore DataFrame to a specific checkpoint step index across history."""
        all_steps = [cp["step"] for cp in self.checkpoints] + [cp["step"] for cp in self.redo_stack]
        max_step = max(all_steps) if all_steps else 0
        if target_step < 0 or target_step > max_step:
            return {
                "status": "error",
                "message": f"Invalid target step {target_step}. Valid range is 0 to {max_step}.",
                "current_step": self.current_step,
            }

        if target_step == self.current_step:
            return {
                "status": "noop",
                "current_step": self.current_step,
                "message": f"Already at target step {target_step}.",
            }

        # If target_step < current_step: step backward into redo_stack
        while self.current_step > target_step and len(self.checkpoints) > 1:
            popped = self.checkpoints.pop()
            self.redo_stack.append(popped)

        # If target_step > current_step: step forward from redo_stack
        while self.current_step < target_step and self.redo_stack:
            popped = self.redo_stack.pop()
            self.checkpoints.append(popped)

        # Restore target DataFrame state
        self.current_df = self.checkpoints[-1]["df"].copy(deep=True)

        return {
            "status": "success",
            "restored_step": self.current_step,
            "current_step": self.current_step,
            "message": f"Successfully restored to step {self.current_step}.",
            "rows": len(self.current_df),
            "columns": list(self.current_df.columns),
            "total_checkpoints": len(self.checkpoints),
        }

    def rollback_all(self) -> Dict[str, Any]:
        """Roll back all cleaning actions and restore the pristine initial DataFrame."""
        return self.rollback_to(0)

    def get_history(self) -> List[Dict[str, Any]]:
        """Return a chronological summary of all checkpoints in the session."""
        history = []
        for cp in self.checkpoints:
            act_name = cp.get("action_name") or "Initial Dataset"
            desc = cp.get("description") or (f"Step {cp['step']}: {act_name}" if act_name != "INITIAL_STATE" else "Initial Dataset")
            history.append({
                "step": cp["step"],
                "action": act_name,
                "action_name": act_name,
                "column": cp.get("column"),
                "params": cp.get("params", {}),
                "rows": cp.get("rows", 0),
                "columns": cp.get("columns", []),
                "columns_count": len(cp.get("columns", [])),
                "timestamp": cp.get("timestamp"),
                "description": desc,
            })
        return history

    def diff(self, step_a: int = 0, step_b: Optional[int] = None) -> Dict[str, Any]:
        """Compute structural and cell-level difference between two session checkpoints."""
        if step_b is None:
            step_b = self.current_step

        if step_a < 0 or step_a >= len(self.checkpoints) or step_b < 0 or step_b >= len(self.checkpoints):
            raise ValueError("Step index out of range for diff computation.")

        df_a = self.checkpoints[step_a]["df"]
        df_b = self.checkpoints[step_b]["df"]

        cols_a = set(df_a.columns)
        cols_b = set(df_b.columns)

        common_cols = [c for c in df_b.columns if c in cols_a]
        added_cols = list(cols_b - cols_a)
        removed_cols = list(cols_a - cols_b)

        # Row comparison
        rows_a = len(df_a)
        rows_b = len(df_b)

        # Cell comparison on common columns and minimum rows (aligned with reset_index)
        common_len = min(rows_a, rows_b)
        cell_diffs_count = 0

        if common_len > 0 and common_cols:
            sub_a = df_a.iloc[:common_len][common_cols].reset_index(drop=True)
            sub_b = df_b.iloc[:common_len][common_cols].reset_index(drop=True)
            diff_mask = (sub_a != sub_b) & ~(sub_a.isna() & sub_b.isna())
            cell_diffs_count = int(diff_mask.sum().sum())

        return {
            "step_a": step_a,
            "step_b": step_b,
            "rows_before": rows_a,
            "rows_after": rows_b,
            "rows_delta": rows_b - rows_a,
            "columns_before": list(df_a.columns),
            "columns_after": list(df_b.columns),
            "columns_added": added_cols,
            "columns_removed": removed_cols,
            "modified_cells_count": cell_diffs_count,
            "total_missing_before": int(df_a.isna().sum().sum()),
            "total_missing_after": int(df_b.isna().sum().sum()),
        }

    def save_to_directory(self, dir_path: Union[str, Path]) -> None:
        """Persist session DataFrames, checkpoints, and redo stack to an isolated directory."""
        p = Path(dir_path).resolve()
        p.mkdir(parents=True, exist_ok=True)
        self.storage_dir = p

        cp_dir = p / "checkpoints"
        cp_dir.mkdir(parents=True, exist_ok=True)
        redo_dir = p / "redo"
        redo_dir.mkdir(parents=True, exist_ok=True)

        # 1. Save pristine and current DataFrames as Parquet
        try:
            self.initial_df.to_parquet(p / "original.parquet", index=False)
            self.current_df.to_parquet(p / "current.parquet", index=False)
        except Exception:
            pass

        # 2. Save checkpoints and metadata
        checkpoints_meta = []
        for cp in self.checkpoints:
            step = cp["step"]
            pq_path = cp_dir / f"step_{step}.parquet"
            if not pq_path.exists():
                df_to_save = cp.get_df() if isinstance(cp, LazyCheckpoint) else cp.get("df")
                if isinstance(df_to_save, pd.DataFrame):
                    try:
                        df_to_save.to_parquet(pq_path, index=False)
                    except Exception:
                        pass
            if isinstance(cp, LazyCheckpoint):
                cp._parquet_path = pq_path

            meta_item = {k: v for k, v in cp.items() if k != "df"}
            checkpoints_meta.append(meta_item)

        with open(p / "checkpoints_meta.json", "w", encoding="utf-8") as f:
            json.dump(checkpoints_meta, f, default=str)

        # 3. Save redo stack if present
        redo_meta = []
        for rcp in self.redo_stack:
            step = rcp["step"]
            pq_path = redo_dir / f"step_{step}.parquet"
            if not pq_path.exists():
                df_to_save = rcp.get_df() if isinstance(rcp, LazyCheckpoint) else rcp.get("df")
                if isinstance(df_to_save, pd.DataFrame):
                    try:
                        df_to_save.to_parquet(pq_path, index=False)
                    except Exception:
                        pass
            if isinstance(rcp, LazyCheckpoint):
                rcp._parquet_path = pq_path
            redo_meta.append({k: v for k, v in rcp.items() if k != "df"})

        with open(p / "redo_meta.json", "w", encoding="utf-8") as f:
            json.dump(redo_meta, f, default=str)

        # 4. Save session summary
        with open(p / "session_state.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "session_name": self.session_name,
                    "executed_history": self.executed_history,
                },
                f,
                default=str,
            )

    def load_from_directory(self, dir_path: Union[str, Path]) -> None:
        """Restore full session state, checkpoints, and history from an isolated directory with lazy loading."""
        p = Path(dir_path).resolve()
        self.storage_dir = p

        orig_pq = p / "original.parquet"
        curr_pq = p / "current.parquet"
        if orig_pq.exists():
            try:
                self.initial_df = pd.read_parquet(orig_pq)
            except Exception:
                pass
        if curr_pq.exists():
            try:
                self.current_df = pd.read_parquet(curr_pq)
            except Exception:
                pass

        # Restore checkpoints with lazy on-demand DataFrame loading
        cp_meta_file = p / "checkpoints_meta.json"
        cp_dir = p / "checkpoints"
        if cp_meta_file.exists():
            try:
                with open(cp_meta_file, "r", encoding="utf-8") as f:
                    meta_list = json.load(f)

                restored_checkpoints = CheckpointList(session=self)
                total_steps = len(meta_list)
                for idx, item in enumerate(meta_list):
                    step = item.get("step", 0)
                    pq_path = cp_dir / f"step_{step}.parquet"

                    # Only the active current state and step 0 have in-memory references;
                    # intermediate steps are loaded lazily on demand
                    cached_df = None
                    if step == 0 and self.initial_df is not None:
                        cached_df = self.initial_df
                    elif idx == total_steps - 1 and self.current_df is not None:
                        cached_df = self.current_df

                    cp_dict = dict(item)
                    cp_dict["df"] = cached_df
                    lazy_cp = LazyCheckpoint(
                        cp_dict,
                        session=self,
                        parquet_path=pq_path if pq_path.exists() else None,
                        df=cached_df,
                    )
                    restored_checkpoints.append(lazy_cp)

                if restored_checkpoints:
                    self.checkpoints = restored_checkpoints
            except Exception:
                pass

        # Restore redo stack
        redo_meta_file = p / "redo_meta.json"
        redo_dir = p / "redo"
        if redo_meta_file.exists():
            try:
                with open(redo_meta_file, "r", encoding="utf-8") as f:
                    meta_list = json.load(f)
                restored_redo = CheckpointList(session=self)
                for item in meta_list:
                    step = item.get("step", 0)
                    pq_path = redo_dir / f"step_{step}.parquet"
                    rcp_dict = dict(item)
                    rcp_dict["df"] = None
                    lazy_rcp = LazyCheckpoint(
                        rcp_dict,
                        session=self,
                        parquet_path=pq_path if pq_path.exists() else None,
                        df=None,
                    )
                    restored_redo.append(lazy_rcp)
                self.redo_stack = restored_redo
            except Exception:
                pass

        # Restore state
        state_file = p / "session_state.json"
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    st = json.load(f)
                    self.session_name = st.get("session_name", self.session_name)
                    self.executed_history = st.get("executed_history", self.executed_history)
            except Exception:
                pass
