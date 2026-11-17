"""
Phase 4 Performance & Resource Protection Benchmarking Suite for DataRefine.

Measures real execution time, peak memory usage (tracemalloc), throughput,
and disk footprint across Small, Medium, and Large datasets:
1. CSV Ingestion & Profiling Performance
2. Memory-Efficient Lazy Checkpoint vs Eager Deep-Copy Scaling
3. Undo / Redo / Rollback Cycle Performance
4. High-Cardinality Clustering Protection & Throughput
5. GREL AST Sandbox Expression Evaluation Throughput
"""

import json
import os
import shutil
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from grel_engine import execute_grel_transform
from openrefine_clustering import detect_clusters
from profiler import profile_dataset
from schemas import CleaningAction
from session import CleaningSession


def generate_benchmark_dataset(num_rows: int, num_cols: int = 12, seed: int = 42) -> pd.DataFrame:
    """Generate realistic messy tabular data for performance benchmarking."""
    np.random.seed(seed)
    cities = ["New York", "new york", "NEW YORK", "San Francisco", "San Fransisco", "London", "LONDON", "Tokyo", "Paris", "Berlin"]
    departments = ["Engineering", "engineering", "HR", "Marketing", "Sales", "Finance", "Legal", "Operations"]
    roles = ["Software Engineer", "Data Scientist", "Product Manager", "DevOps Engineer", "QA Lead"]

    data = {
        "employee_id": [f"EMP-{i:06d}" for i in range(num_rows)],
        "name": [f" Person_{i} " if i % 4 == 0 else f"person_{i}" for i in range(num_rows)],
        "city": np.random.choice(cities, num_rows),
        "department": np.random.choice(departments, num_rows),
        "role": np.random.choice(roles, num_rows),
        "salary": np.random.randint(40000, 180000, num_rows).astype(str),
        "bonus_pct": np.random.uniform(0.05, 0.35, num_rows),
        "rating": np.random.choice([1.0, 2.0, 3.0, 4.0, 5.0, np.nan], num_rows, p=[0.1, 0.2, 0.3, 0.25, 0.1, 0.05]),
        "email": [f"user_{i}@company.com" if i % 10 != 0 else f"user_{i}at_company.com" for i in range(num_rows)],
        "join_date": ["2023-01-15" if i % 5 == 0 else "15/01/2023" for i in range(num_rows)],
        "is_active": np.random.choice(["true", "false", "yes", "no", "1", "0"], num_rows),
        "temp_notes": [f"Note text for row {i}" if i % 3 != 0 else None for i in range(num_rows)],
    }
    return pd.DataFrame(data)


def run_benchmark():
    print("=" * 70)
    print("DATAREFINE PHASE 4 PERFORMANCE BENCHMARK")
    print("=" * 70)

    temp_dir = Path(tempfile.mkdtemp(prefix="datarefine_bench_"))
    results: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hardware": {
            "cpu_cores": os.cpu_count(),
        },
        "datasets": {},
    }

    test_scales = [
        ("Small", 1_000),
        ("Medium", 10_000),
        ("Large", 50_000),
    ]

    try:
        for scale_name, num_rows in test_scales:
            print(f"\n---> Benchmarking {scale_name} Dataset ({num_rows:,} rows)...")
            df = generate_benchmark_dataset(num_rows)
            csv_path = temp_dir / f"{scale_name.lower()}_{num_rows}.csv"
            df.to_csv(csv_path, index=False)
            file_size_mb = round(os.path.getsize(csv_path) / (1024 * 1024), 2)

            scale_metrics: Dict[str, Any] = {
                "rows": num_rows,
                "columns": len(df.columns),
                "file_size_mb": file_size_mb,
            }

            # 1. Profiling & Ingestion
            tracemalloc.start()
            t0 = time.perf_counter()
            profiled_df, profile = profile_dataset(str(csv_path))
            profile_duration_s = round(time.perf_counter() - t0, 3)
            current_mem, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            scale_metrics["profiling"] = {
                "duration_seconds": profile_duration_s,
                "throughput_rows_per_sec": round(num_rows / max(0.001, profile_duration_s), 1),
                "peak_memory_mb": round(peak_mem / (1024 * 1024), 2),
            }
            print(f"  [Profiling] {profile_duration_s}s | Peak RAM: {scale_metrics['profiling']['peak_memory_mb']} MB")

            # 2. Checkpoint Memory Optimization (10 Sequential Operations)
            session_dir = temp_dir / f"session_{scale_name}"
            session = CleaningSession(df, session_name=scale_name, storage_dir=session_dir, max_in_memory_checkpoints=2)

            actions = [
                CleaningAction(action="trim_whitespace", column="name", params={}, reasoning="Trim names"),
                CleaningAction(action="normalize_whitespace", column="city", params={}, reasoning="Normalize cities"),
                CleaningAction(action="normalize_text", column="department", params={"case": "title"}, reasoning="Titlecase departments"),
                CleaningAction(action="remove_column", column="temp_notes", params={}, reasoning="Remove notes"),
                CleaningAction(action="impute_missing", column="rating", params={"method": "median"}, reasoning="Impute ratings"),
                CleaningAction(action="trim_whitespace", column="role", params={}, reasoning="Trim roles"),
                CleaningAction(action="normalize_text", column="role", params={"case": "title"}, reasoning="Titlecase roles"),
                CleaningAction(action="normalize_text", column="city", params={"case": "title"}, reasoning="Titlecase city"),
                CleaningAction(action="normalize_text", column="department", params={"case": "upper"}, reasoning="Uppercase department"),
                CleaningAction(action="trim_whitespace", column="salary", params={}, reasoning="Trim salary"),
            ]

            tracemalloc.start()
            t0 = time.perf_counter()
            for act in actions:
                session.apply_action(act)
            ckpt_duration_s = round(time.perf_counter() - t0, 3)
            current_mem, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            # Measure how many checkpoints have in-memory df vs evicted
            in_memory_count = sum(1 for cp in session.checkpoints if getattr(cp, "_cached_df", None) is not None)
            total_ckpts = len(session.checkpoints)

            scale_metrics["checkpoints"] = {
                "total_checkpoints": total_ckpts,
                "in_memory_cached_dfs": in_memory_count,
                "apply_10_actions_duration_s": ckpt_duration_s,
                "peak_memory_mb": round(peak_mem / (1024 * 1024), 2),
            }
            print(f"  [10 Checkpoints] {ckpt_duration_s}s | Total Steps: {total_ckpts} | In-RAM DFs: {in_memory_count} | Peak RAM: {scale_metrics['checkpoints']['peak_memory_mb']} MB")

            # 3. Undo / Redo Operations
            tracemalloc.start()
            t0 = time.perf_counter()
            for _ in range(5):
                session.undo()
            for _ in range(5):
                session.redo()
            undo_redo_duration_s = round(time.perf_counter() - t0, 3)
            _, peak_mem = tracemalloc.get_traced_memory()
            tracemalloc.stop()

            scale_metrics["undo_redo"] = {
                "5_undo_5_redo_duration_s": undo_redo_duration_s,
                "peak_memory_mb": round(peak_mem / (1024 * 1024), 2),
            }
            print(f"  [Undo/Redo Cycle] {undo_redo_duration_s}s | Peak RAM: {scale_metrics['undo_redo']['peak_memory_mb']} MB")

            # 4. Clustering Protection & Performance
            clustering_metrics = {}
            for method in ["fingerprint", "metaphone", "levenshtein"]:
                t0 = time.perf_counter()
                clusters = detect_clusters(df["city"], method=method)
                dur_s = round(time.perf_counter() - t0, 3)
                clustering_metrics[method] = {
                    "duration_seconds": dur_s,
                    "clusters_found": len(clusters),
                    "evaluated_unique": getattr(clusters, "evaluated_unique", len(df["city"].unique())),
                    "truncated": getattr(clusters, "truncated", False),
                }
            scale_metrics["clustering"] = clustering_metrics
            print(f"  [Clustering] Fingerprint: {clustering_metrics['fingerprint']['duration_seconds']}s | Metaphone: {clustering_metrics['metaphone']['duration_seconds']}s | Levenshtein: {clustering_metrics['levenshtein']['duration_seconds']}s")

            # 5. GREL AST Sandbox Throughput
            t0 = time.perf_counter()
            grel_df = execute_grel_transform(df, column="city", expression="value.trim().toLowercase()")
            grel_duration_s = round(time.perf_counter() - t0, 3)
            grel_throughput = round(num_rows / max(0.001, grel_duration_s), 1)
            scale_metrics["grel"] = {
                "duration_seconds": grel_duration_s,
                "throughput_rows_per_sec": grel_throughput,
            }
            print(f"  [GREL Engine] {grel_duration_s}s ({grel_throughput:,} rows/sec)")

            results["datasets"][scale_name] = scale_metrics

        # Save benchmark results
        output_file = Path("benchmark_results.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        print("\n" + "=" * 70)
        print("BENCHMARK SUMMARY TABLE")
        print("=" * 70)
        header = f"{'Dataset':<10} | {'Rows':<8} | {'Profile (s)':<12} | {'10 Actions (s)':<14} | {'RAM Peak (MB)':<14} | {'GREL (rows/s)':<14}"
        print(header)
        print("-" * len(header))
        for name, data in results["datasets"].items():
            row_str = (
                f"{name:<10} | "
                f"{data['rows']:<8,} | "
                f"{data['profiling']['duration_seconds']:<12.3f} | "
                f"{data['checkpoints']['apply_10_actions_duration_s']:<14.3f} | "
                f"{data['checkpoints']['peak_memory_mb']:<14.2f} | "
                f"{data['grel']['throughput_rows_per_sec']:<14,.1f}"
            )
            print(row_str)
        print("=" * 70)
        print(f"Detailed benchmark metrics written to {output_file.resolve()}\n")

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_benchmark()
