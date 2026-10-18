"""Reward-exam outcomes (MEMORY_EVOLUTION §5.8, §7.1, H2): the manipulation check and the Goodhart measure of arm B.

* ``reward_exam_accuracy``: ``correct / asked`` over the graded reward-exam items (``exam_items.exam = 'reward'``,
  questions about canary facts), abstentions counting as not correct and items whose paid call returned no usable
  answer left out, exactly as :func:`void.research.analysis.exam_counts` treats the audit exam. None when the run asked
  no reward item: every arm but B, or a B run too short for any canary to reach ``reward_delay_days``.
* ``goodhart_gap``: ``reward_exam_accuracy`` minus the audit exam's accuracy
  (:func:`void.research.analysis.exam_accuracy`) in the same run. Positive when agents recall the paid-for facts better
  than their own unrewarded history. None when either exam has no graded item.
* ``recall_reward_usd_total``: dollars the research pool paid to agents for correct reward answers over the run (the
  ``recall_reward`` credits of the wallet ledger, kernel wallets excluded). 0.0 in a run that paid nothing (every arm
  but B included), so arms always pair on it.

Each takes an optional tier as its argument (``reward_exam_accuracy.scripted``): only the items, audit items and
payments of agents on that tier. The daily versions count the items asked on that day (the day of ``tick_asked``,
``(tick - 1) // ticks_per_day + 1``) and the rewards paid on it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from void.config import micro_to_usd
from void.economy.wallet import KERNEL_WALLETS
from void.research.analysis import _has_tables, connect, exam_accuracy, run_config
from void.research.exams import AUDIT, RECALL_REWARD_KIND, REWARD
from void.research.outcomes import register

__all__ = ["exam_counts_for", "goodhart_gap", "recall_reward_usd_total", "reward_exam_accuracy"]


def _day_clause(column: str, day: int | None, ticks_per_day: int) -> tuple[str, tuple[Any, ...]]:
    if day is None:
        return "", ()
    lo = (int(day) - 1) * ticks_per_day + 1
    return f" AND {column} BETWEEN ? AND ?", (lo, lo + ticks_per_day - 1)


def exam_counts_for(run_dir: str | Path, exam: str, tier: str | None = None, day: int | None = None) -> tuple[int, int, int] | None:
    """``(asked, correct, abstained)`` over the graded items of ``exam`` (``'reward'`` or ``'audit'``), optionally the
    agents of one tier and the items asked on one day; None when there is none. An item whose paid call returned no
    usable answer (``call_id`` set, ``answer`` NULL) measured nothing and is not graded."""
    tpd = int(run_config(run_dir).run.ticks_per_day) if day is not None else 1
    sql = ("SELECT COUNT(*) AS n, COALESCE(SUM(a.correct), 0) AS c, COALESCE(SUM(a.abstained), 0) AS ab "
           "FROM exam_answers a JOIN exam_items i ON i.item_id = a.item_id")
    params: list[Any] = []
    if tier:
        sql += " JOIN agents g ON g.agent_id = i.agent_id WHERE i.exam=? AND g.model_tier=?"
        params += [exam, tier]
    else:
        sql += " WHERE i.exam=?"
        params.append(exam)
    sql += " AND NOT (a.call_id IS NOT NULL AND a.answer IS NULL)"
    clause, extra = _day_clause("i.tick_asked", day, tpd)
    with connect(run_dir) as db:
        if not _has_tables(db, "exam_items", "exam_answers"):
            return None
        row = db.execute(sql + clause, (*params, *extra)).fetchone()
    asked = int(row["n"]) if row is not None else 0
    if asked == 0:
        return None
    return asked, int(row["c"]), int(row["ab"])


def _accuracy(counts: tuple[int, int, int] | None) -> float | None:
    return None if counts is None else counts[1] / counts[0]


def _reward_accuracy(run_dir: str | Path, arg: str | None, day: int | None = None) -> float | None:
    return _accuracy(exam_counts_for(run_dir, REWARD, arg or None, day))


def _gap(reward: float | None, audit: float | None) -> float | None:
    return None if reward is None or audit is None else reward - audit


def _paid_micro(db: sqlite3.Connection, tier: str | None, day: int | None, tpd: int) -> int:
    kernel = ",".join("?" for _ in KERNEL_WALLETS)
    sql = "SELECT COALESCE(SUM(l.delta), 0) AS s FROM wallet_ledger l"
    params: list[Any] = []
    if tier:
        sql += " JOIN agents g ON g.agent_id = l.agent_id"
    sql += f" WHERE l.kind=? AND l.agent_id NOT IN ({kernel})"
    params += [RECALL_REWARD_KIND, *KERNEL_WALLETS]
    if tier:
        sql += " AND g.model_tier=?"
        params.append(tier)
    clause, extra = _day_clause("l.tick", day, tpd)
    return int(db.execute(sql + clause, (*params, *extra)).fetchone()["s"])


def _reward_usd(run_dir: str | Path, arg: str | None, day: int | None = None) -> float | None:
    tpd = int(run_config(run_dir).run.ticks_per_day) if day is not None else 1
    with connect(run_dir) as db:
        if not _has_tables(db, "wallet_ledger"):
            return None
        return micro_to_usd(_paid_micro(db, arg or None, day, tpd))


@register("reward_exam_accuracy", daily=lambda rd, arg, day: _reward_accuracy(rd, arg, day))
def reward_exam_accuracy(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Share of graded reward-exam items answered correctly (arm B's manipulation check)."""
    return _reward_accuracy(run_dir, arg)


@register("goodhart_gap", daily=lambda rd, arg, day: _gap(_reward_accuracy(rd, arg, day),
                                                          _accuracy(exam_counts_for(rd, AUDIT, arg or None, day))))
def goodhart_gap(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Reward-exam accuracy minus audit-exam accuracy in the same run (the Goodhart measure, §5.8)."""
    return _gap(_reward_accuracy(run_dir, arg), exam_accuracy(run_dir, arg or None))


@register("recall_reward_usd_total", daily=lambda rd, arg, day: _reward_usd(rd, arg, day))
def recall_reward_usd_total(run_dir: str | Path, arg: str | None = None) -> float | None:
    """Dollars paid to agents as recall rewards over the run (0.0 when none were paid)."""
    return _reward_usd(run_dir, arg)
