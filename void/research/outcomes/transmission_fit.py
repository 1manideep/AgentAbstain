"""The replicator-mutator fit for policy lineages (MEMORY_EVOLUTION §7.1, H3, exploratory), after Group Selection
Promotes Prosocial Prompts. A third-party review of that paper asked whether its transmission kernel was documented;
here every choice is stated, so the kernel can be re-derived by hand from the run database (:func:`fit_table` returns
the counts it is built from).

Types
    The nine practice categories ``T1`` to ``T9`` of the codebook (§7.3). An agent's *type* is the dominant category
    (:func:`~void.research.outcomes.practices.dominant_code`: the most frequent code of its bullets, a tie to the
    lower code number) of its *final* policy: its highest ``policy_versions`` version, which is the current policy of
    an agent alive at the end of the run and the one it died with otherwise. Bullets are coded as ``cluster_entropy``
    codes them (:func:`~void.research.outcomes.practices.code_table`): from ``practice_codes.csv`` in the run directory
    when there is one, by the keyword coder for the bullets it does not cover; ``.keyword`` uses the keyword coder
    only. An agent without a policy, or whose final policy has no bullet, has no type and is left out of everything
    below.

Transitions, one per policy-carrying birth
    A *birth* is an ``agents`` row with a ``parent_id`` (offspring and replacements, within this one run). It *carries
    a policy* when the child's first ``policy_versions`` row was copied from that parent: its ``source`` is
    ``inherited``, ``adapted`` or ``mutated`` and its ``source_agent_id`` is the parent or empty. (At birth the kernel
    writes the parent's text as ``inherited`` naming the parent, then possibly a ``mutated`` version; a child that got
    the seed, because its parent had no policy or ``memory.policy.inherit`` is off, carries none.) Each policy-carrying
    birth whose parent and child both have a type is one transition ``(j, i)``: parent type ``j``, child type ``i``.
    ``N(j, i)`` counts them, ``N(j) = sum_i N(j, i)`` and ``n = sum_j N(j)`` (``n_transitions``).

The transmission kernel Q
    ``Q(j, i) = (N(j, i) + alpha) / (N(j) + 9 alpha)`` over the nine categories, with ``alpha = 1/9`` (:data:`ALPHA`):
    add-alpha smoothing with one pseudo-transition per row spread evenly over the nine categories (the Perks prior), so
    every row sums to 1 and a category no child of that row took gets a small positive probability. A row with
    ``N(j) = 0`` is None: no parent of type ``j`` had a policy-carrying child, and nothing is invented for it.

Relative fitness f
    The parent pool is every typed agent whose generation (``agents.generation``) is below the deepest generation a
    typed agent reached (an agent of that deepest generation cannot have a child in the data, so its zero would be the
    end of the run, not selection), plus, defensively, the parent of every transition. ``A(j)`` is the number of
    type-``j`` agents in the pool, ``A = sum_j A(j)``, and ``f(j) = (N(j) / A(j)) / (n / A)``: the mean number of
    policy-carrying (typed) children per type-``j`` agent over the pool's mean, so the ``A(j)``-weighted mean of f is 1.
    None for a type without an agent in the pool; 0 for a type whose agents had no policy-carrying child.

The fit
    The cohort of generation ``g`` is every typed agent with ``agents.generation = g``, dead or alive, and ``x_g(j)``
    is the share of type ``j`` in it. For every ``g`` whose next cohort ``g + 1`` is not empty, the replicator-mutator
    step predicts ``x'(i) = sum_j x_g(j) f(j) Q(j, i) / sum_j x_g(j) f(j)`` (a type with ``f(j) = 0`` adds nothing, so
    its empty row is never used; a generation whose denominator is 0 is skipped). A prediction is scored against the
    observed ``x_{g+1}`` by the total-variation distance ``TVD(p, q) = 1/2 sum_i |p(i) - q(i)|`` over the nine
    categories. The baseline, with neither selection nor mutation, predicts ``x' = x_g``.

Outcomes (``end_of_run_outcome(run, "replicator_mutator.<statistic>[.keyword]")``; end of run only)
    ``mutation_rate``     ``1 - sum_j N(j) Q(j, j) / n``: one minus Q's diagonal averaged over the rows, each row
                          weighted by its transitions (one per parent-child pair). The smoothing moves a row's term
                          off the raw share of its children that changed type by at most ``(8/9) / (N(j) + 1)``,
                          upwards whenever more than a ninth of them kept the parent's type.
    ``selection_spread``  ``max f / min f`` over the types with at least :data:`MIN_PARENTS` (3) parents (distinct
                          agents of that type with a transition, so every such f is positive); None with fewer than
                          two such types.
    ``fit_tvd``           the mean TVD between the predicted and the observed cohort shares over the generations
                          predicted.
    ``baseline_tvd``      the same for the baseline ``x' = x_g``, over the same generations.
    ``n_transitions``     ``n``.
    ``q.<Ti>.<Tj>``       ``Q(Ti, Tj)``; None when row ``Ti`` has no transition.
    ``f.<Ti>``            ``f(Ti)``; None when no agent of type ``Ti`` is in the pool.

Every outcome, ``n_transitions`` included, is None (not 0) when the run has fewer than :data:`MIN_TRANSITIONS` (10)
transitions, the threshold below which the fit is not reported, or no policy table; ``fit_tvd`` and ``baseline_tvd``
are None too when no generation could be predicted. :func:`fit_table` gives the whole fit, counts and per-generation
predictions included, whatever ``n`` is. Any other argument raises ``ValueError``.

Determinism: nothing here draws randomness; agents, rows, categories and generations are visited in a fixed order.
No interval is attached to the estimates: uncertainty across seeds is ``compare``'s paired analysis.

Caveats
    * The fit is in-sample: Q and f are estimated from the transitions the cohorts are made of, pooled over the
      generations. ``fit_tvd`` below ``baseline_tvd`` says a stationary selection-plus-transmission model describes the
      cohort changes better than no change at all; it is not an out-of-sample test.
    * Q is a kernel between final types. It mixes the copy at birth, the kernel's mutation at birth and both agents'
      later edits (a parent's edits after the birth included); it is not a measure of copying fidelity.
    * Censoring: a later generation is born later and has had less time to edit and to reproduce, so compare arms of
      the same length.
    * One run only: E3a's generations are separate runs whose children are roster agents without a ``parent_id``, so
      its cross-run lineages are not transitions here.
    * Scripted brains do not act on their policy (only reading it costs money), so on scripted runs f reflects who
      the agents are, not their practices; and the keyword coder is a baseline, not a validated instrument (§7.3).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from void.research.analysis import _has_tables, connect
from void.research.outcomes import register
from void.research.outcomes.practices import CODE_NAMES, CODES, code_profile, code_table, dominant_code

__all__ = [
    "ALPHA",
    "CARRYING_SOURCES",
    "MIN_PARENTS",
    "MIN_TRANSITIONS",
    "STATISTICS",
    "Transition",
    "fit_table",
    "outcome_paths",
    "replicator_mutator",
]

ALPHA = 1.0 / len(CODES)         # add-alpha smoothing per cell: one pseudo-transition per row
MIN_TRANSITIONS = 10             # below this many transitions every outcome is None
MIN_PARENTS = 3                  # a type enters selection_spread with at least this many parents
CARRYING_SOURCES: tuple[str, ...] = ("inherited", "adapted", "mutated")  # a child's first version copied from a parent
STATISTICS: tuple[str, ...] = ("mutation_rate", "selection_spread", "fit_tvd", "baseline_tvd", "n_transitions")


@dataclass(frozen=True)
class Transition:
    """One policy-carrying birth between typed agents."""

    parent_id: str
    child_id: str
    parent_type: str
    child_type: str


def _tvd(p: Mapping[str, float], q: Mapping[str, float]) -> float:
    return 0.5 * sum(abs(p[c] - q[c]) for c in CODES)


def _shares(counts: Mapping[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {c: counts.get(c, 0) / total for c in CODES}


def _read(run_dir: str | Path, keyword: bool) -> tuple[dict[str, str], dict[str, int], list[Transition]] | None:
    """``(type by agent, generation by agent, transitions)`` of a run (typed agents only); None without a policy
    table."""
    with connect(run_dir) as db:
        if not _has_tables(db, "policy_versions"):
            return None
        agents = db.execute("SELECT agent_id, parent_id, generation FROM agents ORDER BY agent_id").fetchall()
        first: dict[str, Any] = {}
        last: dict[str, Any] = {}
        for r in db.execute("SELECT agent_id, version, text, source, source_agent_id FROM policy_versions "
                            "ORDER BY agent_id, version"):
            first.setdefault(str(r["agent_id"]), r)
            last[str(r["agent_id"])] = r
    table = code_table(run_dir, keyword=keyword)
    types: dict[str, str] = {}
    generation: dict[str, int] = {}
    for r in agents:
        aid = str(r["agent_id"])
        profile = code_profile(last[aid]["text"], table) if aid in last else Counter()
        if profile:
            types[aid] = dominant_code(profile)
            generation[aid] = int(r["generation"])
    transitions: list[Transition] = []
    for r in agents:
        child, parent = str(r["agent_id"]), r["parent_id"]
        if parent is None or child not in types or str(parent) not in types:
            continue
        parent = str(parent)
        v1 = first[child]
        source_agent = v1["source_agent_id"]
        if v1["source"] in CARRYING_SOURCES and (source_agent is None or str(source_agent) == parent):
            transitions.append(Transition(parent, child, types[parent], types[child]))
    return types, generation, transitions


def fit_table(run_dir: str | Path, *, keyword: bool = False) -> dict[str, Any] | None:
    """The whole replicator-mutator fit of a run (the module docstring defines every entry), computed whatever the
    number of transitions; None without a policy table. Keys: ``keyword``, ``alpha``, ``min_transitions``,
    ``min_parents``, ``n_transitions``, ``fitted`` (``n_transitions >= min_transitions``: whether the outcomes report
    it), ``counts`` (``N(j, i)`` for every pair of :data:`~void.research.outcomes.practices.CODES`), ``pool``
    (``A(j)``), ``parents`` (distinct parents per type), ``q`` (row ``j``: ``{i: Q(j, i)}`` or None), ``f``,
    ``mutation_rate``, ``selection_spread``, ``cohorts`` (one dict per predicted generation ``g``: ``generation``,
    ``agents`` and ``shares`` of cohort ``g``, ``predicted`` and ``observed`` shares of cohort ``g + 1``, ``fit_tvd``,
    ``baseline_tvd``), ``fit_tvd``, ``baseline_tvd`` and ``transitions`` (dicts with ``parent_id``, ``child_id``,
    ``parent_type``, ``child_type``)."""
    loaded = _read(run_dir, keyword)
    if loaded is None:
        return None
    types, generation, transitions = loaded
    n = len(transitions)
    counts = {j: dict.fromkeys(CODES, 0) for j in CODES}
    for t in transitions:
        counts[t.parent_type][t.child_type] += 1
    rows = {j: sum(counts[j].values()) for j in CODES}
    q: dict[str, dict[str, float] | None] = {
        j: None if rows[j] == 0 else {i: (counts[j][i] + ALPHA) / (rows[j] + len(CODES) * ALPHA) for i in CODES}
        for j in CODES}

    deepest = max(generation.values(), default=0)
    pool = {aid for aid in types if generation[aid] < deepest} | {t.parent_id for t in transitions}
    pooled = Counter(types[aid] for aid in pool)
    f: dict[str, float | None] = dict.fromkeys(CODES)
    if n and pool:
        mean = n / len(pool)
        for j in CODES:
            if pooled[j]:
                f[j] = (rows[j] / pooled[j]) / mean
    parents = {j: len({t.parent_id for t in transitions if t.parent_type == j}) for j in CODES}

    mutation_rate = None
    if n:
        mutation_rate = 1.0 - sum(rows[j] * row[j] for j, row in q.items() if row is not None) / n
    qualifying = [v for j in CODES if parents[j] >= MIN_PARENTS and (v := f[j]) is not None and v > 0.0]
    selection_spread = max(qualifying) / min(qualifying) if len(qualifying) >= 2 else None

    cohorts_by_gen: dict[int, Counter[str]] = {}
    for aid, kind in types.items():
        cohorts_by_gen.setdefault(generation[aid], Counter())[kind] += 1
    cohorts: list[dict[str, Any]] = []
    for g in sorted(cohorts_by_gen):
        if g + 1 not in cohorts_by_gen:
            continue
        x = _shares(cohorts_by_gen[g])
        # a type adds x f Q; one with f 0 (or no f: no agent in the pool) adds nothing, so its empty row is not used
        terms = [(x[j] * fj, row) for j in CODES if (fj := f[j]) and x[j] > 0.0 and (row := q[j]) is not None]
        total = sum(w for w, _ in terms)
        if total <= 0.0:
            continue
        predicted = {i: sum(w * row[i] for w, row in terms) / total for i in CODES}
        observed = _shares(cohorts_by_gen[g + 1])
        cohorts.append({"generation": g, "agents": sum(cohorts_by_gen[g].values()), "shares": x,
                        "predicted": predicted, "observed": observed, "fit_tvd": _tvd(predicted, observed),
                        "baseline_tvd": _tvd(x, observed)})
    fit_tvd = sum(c["fit_tvd"] for c in cohorts) / len(cohorts) if cohorts else None
    baseline_tvd = sum(c["baseline_tvd"] for c in cohorts) / len(cohorts) if cohorts else None
    return {
        "keyword": keyword, "alpha": ALPHA, "min_transitions": MIN_TRANSITIONS, "min_parents": MIN_PARENTS,
        "n_transitions": n, "fitted": n >= MIN_TRANSITIONS, "counts": counts, "pool": {j: pooled[j] for j in CODES},
        "parents": parents, "q": q, "f": f, "mutation_rate": mutation_rate, "selection_spread": selection_spread,
        "cohorts": cohorts, "fit_tvd": fit_tvd, "baseline_tvd": baseline_tvd,
        "transitions": [asdict(t) for t in transitions],
    }


def _parse(arg: str | None) -> tuple[str, tuple[str, ...], bool]:
    """``(statistic, cells, keyword)`` of a ``replicator_mutator`` argument; ``ValueError`` for anything else."""
    parts = [] if arg is None else arg.split(".")
    keyword = bool(parts) and parts[-1] == "keyword"
    if keyword:
        parts = parts[:-1]
    if len(parts) == 1 and parts[0] in STATISTICS:
        return parts[0], (), keyword
    if len(parts) == 2 and parts[0] == "f" and parts[1] in CODE_NAMES:
        return "f", (parts[1],), keyword
    if len(parts) == 3 and parts[0] == "q" and parts[1] in CODE_NAMES and parts[2] in CODE_NAMES:
        return "q", (parts[1], parts[2]), keyword
    raise ValueError(f"replicator_mutator argument {arg!r} must be one of {', '.join(STATISTICS)}, q.<Ti>.<Tj> or "
                     f"f.<Ti> ({CODES[0]} to {CODES[-1]}), optionally followed by .keyword")


def outcome_paths(*, keyword: bool = False) -> list[str]:
    """Every ``replicator_mutator`` outcome path: the statistics, then the f cells, then the Q cells by row."""
    suffix = ".keyword" if keyword else ""
    paths = [f"replicator_mutator.{s}" for s in STATISTICS]
    paths += [f"replicator_mutator.f.{i}" for i in CODES]
    paths += [f"replicator_mutator.q.{j}.{i}" for j in CODES for i in CODES]
    return [p + suffix for p in paths]


@register("replicator_mutator")
def replicator_mutator(run_dir: str | Path, arg: str | None = None) -> float | None:
    """One statistic or cell of the replicator-mutator fit (see the module docstring); None below
    :data:`MIN_TRANSITIONS` transitions."""
    statistic, cells, keyword = _parse(arg)
    fit = fit_table(run_dir, keyword=keyword)
    if fit is None or not fit["fitted"]:
        return None
    if statistic == "q":
        row = fit["q"][cells[0]]
        return None if row is None else float(row[cells[1]])
    if statistic == "f":
        value = fit["f"][cells[0]]
        return None if value is None else float(value)
    if statistic == "n_transitions":
        return float(fit["n_transitions"])
    value = fit[statistic]
    return None if value is None else float(value)
