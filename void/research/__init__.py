"""Research harness (DESIGN §17): per-run analysis and paired arm comparison."""

from void.research.analysis import end_of_run_outcome, load_metrics, summarize
from void.research.compare import IncomparableRuns, compare, render

__all__ = ["IncomparableRuns", "compare", "end_of_run_outcome", "load_metrics", "render", "summarize"]
