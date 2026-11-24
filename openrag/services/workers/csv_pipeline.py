"""Compatibility import for callers transitioning to the tabular pipeline."""

from services.workers.tabular_pipeline import run_tabular_batches

run_csv_batches = run_tabular_batches

__all__ = ["run_csv_batches"]
