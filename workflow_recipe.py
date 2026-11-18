"""
DataRefine Workflow Recipe & Replay Engine.

Supports:
- Exporting session cleaning history as a portable, serializable JSON recipe.
- Importing and replaying recipes onto the current dataset or new datasets.
- Schema validation of recipe steps against the trusted deterministic action registry.
- Generating before/after execution reports and checkpoint rollback points.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import pandas as pd

from actions import execute_action
from schemas import CleaningAction


def _normalize_col_key(col_name: Any) -> str:
    """Normalize a column name for resilient matching (lowercase snake_case)."""
    return re.sub(r"[^\w]+", "_", str(col_name).strip().lower()).strip("_")


def _resolve_column_reference(col: Optional[str], current_columns: List[str], aliases: Dict[str, str]) -> Optional[str]:
    """Resolve column reference across sequential renaming or normalization operations."""
    if col is None:
        return None
    if col in current_columns:
        return col
    if col in aliases and aliases[col] in current_columns:
        return aliases[col]
    target_norm = _normalize_col_key(col)
    for c in current_columns:
        if _normalize_col_key(c) == target_norm:
            aliases[col] = c
            return c
    if col in aliases:
        return aliases[col]
    return col


# Map DataRefine actions to workflow operation types
ACTION_TO_OP_TYPE = {
    "trim_whitespace": "core/text-transform",
    "normalize_whitespace": "core/text-transform",
    "normalize_text": "core/text-transform",
    "standardize_case": "core/text-transform",
    "normalize_unicode": "core/text-transform",
    "remove_html_tags": "core/text-transform",
    "grel_transform": "core/grel-transform",
    "standardize_categories": "core/mass-edit",
    "merge_clusters": "core/mass-edit",
    "impute_missing": "core/impute",
    "fill_with_constant": "core/impute",
    "drop_missing_rows": "core/row-removal",
    "remove_duplicates": "core/row-removal",
    "remove_column": "core/column-removal",
    "rename_column": "core/column-rename",
    "reconcile_column": "core/reconciliation",
}


class WorkflowRecipeManager:
    """Manages creation, serialization, validation, and execution of JSON cleaning recipes."""

    @staticmethod
    def export_recipe_from_history(history: List[Dict[str, Any]], title: str = "DataRefine Recipe") -> Dict[str, Any]:
        """Convert session execution history into a reusable DataRefine JSON recipe."""
        operations = []
        for step in history:
            action_name = step.get("action")
            if not action_name or action_name == "INITIAL_STATE":
                continue

            op_type = ACTION_TO_OP_TYPE.get(action_name, "core/custom-operation")
            op_dict = {
                "op": op_type,
                "description": step.get("description", f"Execute {action_name}"),
                "action": action_name,
                "column": step.get("column"),
                "params": step.get("params", {}),
                "reasoning": step.get("reasoning", "User-approved or automated transformation"),
            }
            operations.append(op_dict)

        return {
            "title": title,
            "version": "2.0.0",
            "format": "openrefine_datarefine_v2",
            "created_at": datetime.now().isoformat(),
            "operations_count": len(operations),
            "operations": operations,
        }

    @staticmethod
    def validate_recipe(recipe: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Validate recipe format and verify all actions belong to allowed registry."""
        errors = []
        operations = recipe.get("operations")
        if operations is None or not isinstance(operations, list):
            return False, ["Recipe must contain an 'operations' list."]

        from schemas import ALLOWED_ACTIONS
        allowed_set = set(ALLOWED_ACTIONS.__args__)

        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                errors.append(f"Operation #{i + 1} is not a valid JSON object.")
                continue

            action = op.get("action")
            if not action:
                errors.append(f"Operation #{i + 1} is missing required 'action' field.")
            elif action not in allowed_set:
                errors.append(f"Operation #{i + 1} references unsupported action '{action}'.")

        return len(errors) == 0, errors

    @classmethod
    def replay_recipe(
        cls,
        df: pd.DataFrame,
        recipe: Dict[str, Any],
        stop_on_error: bool = False,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """
        Replay an entire recipe onto a DataFrame sequentially.
        Seamlessly resolves column renames and normalizations across sequential steps.
        Returns the transformed DataFrame and execution report.
        """
        valid, errors = cls.validate_recipe(recipe)
        if not valid:
            raise ValueError(f"Invalid recipe: {'; '.join(errors)}")

        operations = recipe["operations"]
        current_df = df.copy(deep=True)
        step_results = []
        column_aliases: Dict[str, str] = {str(c): str(c) for c in current_df.columns}

        initial_rows = len(current_df)
        initial_cols = len(current_df.columns)

        for idx, op in enumerate(operations):
            action_name = op["action"]
            column = op.get("column")
            params = dict(op.get("params", {}))
            reasoning = op.get("reasoning", f"Replaying recipe step {idx + 1}")

            before_cols = list(current_df.columns)
            resolved_column = _resolve_column_reference(column, before_cols, column_aliases)

            try:
                # If renaming a column, resolve old_name as well
                if action_name == "rename_column" and "old_name" in params:
                    params["old_name"] = _resolve_column_reference(params["old_name"], before_cols, column_aliases)

                action_obj = CleaningAction(
                    action=action_name,
                    column=resolved_column,
                    params=params,
                    reasoning=reasoning,
                )

                before_r = len(current_df)
                before_missing = int(current_df.isna().sum().sum())

                current_df = execute_action(current_df, action_obj)

                after_cols = list(current_df.columns)
                after_r = len(current_df)
                after_missing = int(current_df.isna().sum().sum())

                # Update column aliases mapping if columns were renamed or normalized
                if after_cols != before_cols:
                    if action_name == "normalize_column_names" and len(before_cols) == len(after_cols):
                        for old_c, new_c in zip(before_cols, after_cols):
                            for k, v in list(column_aliases.items()):
                                if v == old_c:
                                    column_aliases[k] = new_c
                            column_aliases[old_c] = new_c
                            column_aliases[new_c] = new_c
                    elif action_name == "rename_column":
                        old_name = params.get("old_name", column)
                        new_name = params.get("new_name")
                        if old_name and new_name:
                            for k, v in list(column_aliases.items()):
                                if v == old_name:
                                    column_aliases[k] = new_name
                            column_aliases[old_name] = new_name
                            column_aliases[new_name] = new_name
                    for c in after_cols:
                        if c not in column_aliases:
                            column_aliases[c] = c

                step_results.append({
                    "step": idx + 1,
                    "action": action_name,
                    "column": resolved_column,
                    "status": "success",
                    "rows_affected": abs(after_r - before_r),
                    "missing_delta": after_missing - before_missing,
                    "error": None,
                })
            except Exception as e:
                step_results.append({
                    "step": idx + 1,
                    "action": action_name,
                    "column": resolved_column,
                    "status": "failed",
                    "error": str(e),
                })
                if stop_on_error:
                    break

        report = {
            "title": recipe.get("title", "Replayed Recipe"),
            "total_steps": len(operations),
            "successful_steps": sum(1 for s in step_results if s["status"] == "success"),
            "executed_count": sum(1 for s in step_results if s["status"] == "success"),
            "failed_steps": sum(1 for s in step_results if s["status"] == "failed"),
            "initial_rows": initial_rows,
            "final_rows": len(current_df),
            "initial_cols": initial_cols,
            "final_cols": len(current_df.columns),
            "step_results": step_results,
            "executed_at": datetime.now().isoformat(),
        }

        return current_df, report

    @staticmethod
    def generate_python_script_from_history(
        history: List[Dict[str, Any]],
        input_filename: str = "dataset.csv",
        output_filename: str = "cleaned_data.csv",
    ) -> str:
        """
        Generate a standalone, reproducible Python script using Pandas that replays
        all executed cleaning operations in exact sequential order.
        Tracks sequential column renames so subsequent operations reference valid active columns.
        """
        lines = [
            '"""',
            'DataRefine Standalone Cleaning Pipeline Export',
            'Generated standalone Python script using Pandas.',
            'Production and batch ETL pipeline ready with zero external AI dependencies.',
            '"""',
            '',
            'from __future__ import annotations',
            'import re',
            'import pandas as pd',
            'import numpy as np',
            '',
            f'def run_pipeline(input_path: str = {json.dumps(input_filename)}, output_path: str = {json.dumps(output_filename)}) -> pd.DataFrame:',
            '    print(f"Loading raw dataset from {input_path}...")',
            '    df = pd.read_csv(input_path)',
            '    initial_rows = len(df)',
            '    initial_cols = len(df.columns)',
            '    initial_missing = int(df.isna().sum().sum())',
            '    print(f"Initial shape: {initial_rows} rows x {initial_cols} cols, missing cells: {initial_missing}")',
            '',
        ]

        col_aliases: Dict[str, str] = {}
        step_num = 0

        for step in history:
            action_name = step.get("action_name") or step.get("action")
            if not action_name or action_name == "INITIAL_STATE":
                continue

            step_num += 1
            raw_col = step.get("column")
            params = step.get("params") or {}

            # Resolve column with current aliases
            target_col = col_aliases.get(raw_col, raw_col) if raw_col else None
            lines.append(f"    # Step {step_num}: {action_name}" + (f" on '{target_col}'" if target_col else ""))

            if action_name == "normalize_column_names":
                lines.append("    # Normalize column names to snake_case")
                lines.append('    df.columns = [re.sub(r"[^\\w]+", "_", str(c).strip().lower()).strip("_") for c in df.columns]')
                for s in history:
                    c = s.get("column")
                    if c:
                        norm = re.sub(r"[^\w]+", "_", str(c).strip().lower()).strip("_")
                        col_aliases[c] = norm
                    old_n = s.get("params", {}).get("old_name") if isinstance(s.get("params"), dict) else None
                    if old_n:
                        norm = re.sub(r"[^\w]+", "_", str(old_n).strip().lower()).strip("_")
                        col_aliases[old_n] = norm
                if history and "columns" in history[0] and isinstance(history[0]["columns"], (list, tuple)):
                    for c in history[0]["columns"]:
                        norm = re.sub(r"[^\w]+", "_", str(c).strip().lower()).strip("_")
                        col_aliases[c] = norm
            elif action_name == "rename_column":
                old_name = params.get("old_name", target_col)
                resolved_old = col_aliases.get(old_name, old_name)
                new_name = params.get("new_name")
                if resolved_old and new_name:
                    lines.append(f"    df = df.rename(columns={{{json.dumps(resolved_old)}: {json.dumps(new_name)}}})")
                    col_aliases[old_name] = new_name
                    if target_col:
                        col_aliases[target_col] = new_name
                    col_aliases[new_name] = new_name
            elif action_name == "remove_column":
                if target_col:
                    lines.append(f"    if {json.dumps(target_col)} in df.columns:")
                    lines.append(f"        df = df.drop(columns=[{json.dumps(target_col)}])")
            elif action_name == "convert_to_date":
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = pd.to_datetime(df[{json.dumps(target_col)}].astype(str).str.strip(), format='mixed', errors='coerce')")
            elif action_name == "standardize_date_format":
                fmt = params.get("target_format", "%Y-%m-%d")
                if target_col:
                    lines.append(f"    _dt = pd.to_datetime(df[{json.dumps(target_col)}], format='mixed', errors='coerce')")
                    lines.append(f"    df[{json.dumps(target_col)}] = _dt.dt.strftime({json.dumps(fmt)}).where(_dt.notna(), pd.NA)")
            elif action_name == "convert_to_numeric":
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = pd.to_numeric(df[{json.dumps(target_col)}].astype(str).str.replace(r'[\\$,%]', '', regex=True).str.strip(), errors='coerce')")
            elif action_name == "convert_to_boolean":
                if target_col:
                    lines.append("    _bool_map = {'true': True, '1': True, 'yes': True, 't': True, 'y': True, 'false': False, '0': False, 'no': False, 'f': False, 'n': False}")
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.strip().str.lower().map(_bool_map)")
            elif action_name == "trim_whitespace":
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.strip().replace({{'nan': pd.NA, 'None': pd.NA, '<NA>': pd.NA}})")
            elif action_name == "normalize_whitespace":
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.replace(r'\\s+', ' ', regex=True).str.strip().replace({{'nan': pd.NA, 'None': pd.NA, '<NA>': pd.NA}})")
            elif action_name == "normalize_text":
                case = params.get("case", "lower").lower()
                case_method = "lower" if case == "lower" else ("upper" if case == "upper" else "title")
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.{case_method}()")
            elif action_name == "impute_missing":
                method = params.get("method", "median")
                if target_col:
                    if method == "mean":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].fillna(df[{json.dumps(target_col)}].mean())")
                    elif method == "mode":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].fillna(df[{json.dumps(target_col)}].mode()[0])")
                    elif method == "constant":
                        val = params.get("value", "")
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].fillna({json.dumps(val)})")
                    else:
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].fillna(df[{json.dumps(target_col)}].median())")
            elif action_name == "drop_missing_rows":
                if target_col:
                    lines.append(f"    df = df.dropna(subset=[{json.dumps(target_col)}])")
                else:
                    lines.append("    df = df.dropna()")
            elif action_name == "remove_duplicates":
                lines.append("    df = df.drop_duplicates()")
            elif action_name in {"standardize_categories", "merge_clusters"}:
                mapping = params.get("mapping") or {}
                if target_col and mapping:
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].replace({json.dumps(mapping)})")
            elif action_name == "grel_transform":
                expr = params.get("expression", "")
                if target_col:
                    if expr == "value.trim()":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.strip()")
                    elif expr == "value.toLowercase()":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.lower()")
                    elif expr == "value.toUppercase()":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.upper()")
                    elif expr == "value.toTitlecase()":
                        lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.title()")
                    else:
                        lines.append(f"    # GREL: {expr}")
                        lines.append("    from grel_engine import execute_grel_transform")
                        lines.append(f"    df = execute_grel_transform(df, {json.dumps(target_col)}, {json.dumps(expr)})")
            elif action_name == "clean_phone_number":
                if target_col:
                    lines.append(f"    df[{json.dumps(target_col)}] = df[{json.dumps(target_col)}].astype(str).str.replace(r'\\D+', '', regex=True)")
            else:
                lines.append(f"    # Custom action: {action_name}")
                lines.append("    from actions import execute_action")
                lines.append("    from schemas import CleaningAction")
                lines.append(f"    df = execute_action(df, CleaningAction(action={json.dumps(action_name)}, column={json.dumps(target_col)}, params={json.dumps(params)}, reasoning='Automated pipeline step'))")
            lines.append("")

        lines.extend([
            "    # Export Cleaned Output",
            '    print(f"Writing cleaned dataset to {output_path}...")',
            "    df.to_csv(output_path, index=False)",
            "    final_rows = len(df)",
            "    final_cols = len(df.columns)",
            "    final_missing = int(df.isna().sum().sum())",
            '    print(f"Finished pipeline: {final_rows} rows x {final_cols} cols, missing cells: {final_missing}")',
            "    return df",
            "",
            'if __name__ == "__main__":',
            "    run_pipeline()",
            "",
        ])

        return "\n".join(lines)
