"""Vault-structure and policy outcomes (MEMORY_EVOLUTION §7.2) and the monoculture index (§7.1).

Two kinds of check: a hand-built ``world.db`` plus vault whose every figure is computed by hand below, and short
scripted E0 runs (two days of each arm) where the planted strategies must show up in the metric built to see them.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import sqlite3
from itertools import combinations
from pathlib import Path

import pytest

from void.config import load_config
from void.db import Database
from void.memory.embed import cosine, pack, unpack
from void.research.analysis import computed_outcome_names, end_of_run_outcome
from void.research.outcomes import OUTCOMES, load_all
from void.research.outcomes import memory_structure as ms
from void.sim.loop import Simulation

REPO = Path(__file__).resolve().parents[1]
CONFIGS = REPO / "configs"

VAULT_NAMES = ("vault_notes", "vault_bytes", "mean_note_chars", "link_density", "orphan_rate", "index_coverage",
               "near_duplicate_rate", "tag_entropy", "folder_depth")
POLICY_NAMES = ("policy_chars", "policy_edit_distance", "policy_drift_from_seed", "policy_versions_per_agent",
                "monoculture_index")
NAMES = VAULT_NAMES + POLICY_NAMES
DIM = 8


def _vec(*head: float) -> bytes:
    return pack(list(head) + [0.0] * (DIM - len(head)))


E1 = _vec(1.0)
E2 = _vec(0.0, 1.0)
NEAR = _vec(0.95, math.sqrt(1 - 0.95 ** 2))  # cosine 0.95 with E1: a near-duplicate
FAR = _vec(0.85, math.sqrt(1 - 0.85 ** 2))   # cosine 0.85 with E1: not one


# --- a hand-built run -------------------------------------------------------------------------------------------
class World:
    """A ``world.db`` and vault under ``run_dir`` built row by row."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.db = Database(run_dir / "world.db")
        self.files: list[Path] = []

    def agent(self, aid: str, status: str = "alive", *, memory_path: str | None = None) -> None:
        root = str(self.run_dir / "vaults" / aid) if memory_path is None else memory_path
        self.db.execute(
            "INSERT INTO agents(agent_id, name, model_tier, personality_seed, memory_path, status, x, y, entropy_budget, "
            "born_tick, died_tick, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, aid.upper(), "scripted", "{}", root, status, 0, 0, 1, 0, None if status == "alive" else 9, "t0"))

    def note(self, aid: str, nid: str, title: str, body: str, *, tags: list[str], emb: bytes, tick: int = 1,
             folder: str = "notes", archived: bool = False, links: tuple[str, ...] = ()) -> Path:
        path = self.run_dir / "vaults" / aid / folder / f"{nid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\n---\n{body}\n", encoding="utf-8")
        self.db.execute(
            "INSERT INTO notes(note_id, agent_id, path, title, created_tick, tags, embedding, archived) VALUES(?,?,?,?,?,?,?,?)",
            (nid, aid, str(path), title, tick, json.dumps(tags), emb, int(archived)))
        for to in links:
            self.db.execute("INSERT INTO note_links(from_note_id, to_title) VALUES(?,?)", (nid, to))
        return path

    def policy(self, aid: str, *texts: str) -> None:
        for v, text in enumerate(texts, start=1):
            self.db.execute(
                "INSERT INTO policy_versions(agent_id, version, tick, text, parent_version, source) VALUES(?,?,?,?,?,?)",
                (aid, v, v * 10, text, v - 1 or None, "seed" if v == 1 else "self_edit"))

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def hand_built(tmp_path: Path) -> tuple[Path, list[Path]]:
    """ag_a and ag_b alive, ag_c bankrupt. Every expected figure is derived in the tests from these rows."""
    w = World(tmp_path)
    w.agent("ag_a")
    w.agent("ag_b", memory_path="")  # no recorded root: the vault root is found by the agent id in the path
    w.agent("ag_c", status="bankrupt")
    counted = [
        w.note("ag_a", "a1", "Alpha", "aaaa", tags=["agent"], emb=E1, links=("Beta",)),
        w.note("ag_a", "a2", "Beta", "bbbbbbbb", tags=["agent", "index"], emb=NEAR, links=("Alpha", "Bob")),
        w.note("ag_a", "a3", "Gamma", "cc", tags=["places"], emb=E2, folder="notes/sub"),
        w.note("ag_b", "b1", "One", "xyz", tags=["agent"], emb=E1, links=("Two",)),
        w.note("ag_b", "b2", "Two", "q", tags=["agent"], emb=FAR, links=("Nowhere",)),
    ]
    w.note("ag_a", "s1", "Bob", "Bob, an inhabitant", tags=["entity"], emb=E2)           # stub: never counted
    w.note("ag_b", "b3", "Three", "old", tags=["gossip"], emb=E1, archived=True)          # archived: never counted
    w.note("ag_c", "c1", "Dead one", "same", tags=["agent"], emb=E1)                     # dead agent: never counted
    w.note("ag_c", "c2", "Dead two", "same", tags=["agent", "x"], emb=E1)
    w.policy("ag_a", "red green blue", "red green blue gold", "red green")
    w.policy("ag_b", "red green blue")
    w.policy("ag_c", "kitten", "sitting")
    w.close()
    return tmp_path, counted


def test_every_outcome_is_registered_and_listed() -> None:
    load_all()
    listed = computed_outcome_names()
    for name in NAMES:
        assert name in listed, name
        assert OUTCOMES[name] is getattr(ms, name), name
    assert set(ms.VAULT_OUTCOMES) <= set(NAMES)


def test_vault_figures_on_a_hand_built_run(hand_built: tuple[Path, list[Path]]) -> None:
    run_dir, counted = hand_built
    got = {name: end_of_run_outcome(run_dir, name) for name in VAULT_NAMES}
    assert got["vault_notes"] == 5.0                                   # a1 a2 a3 b1 b2
    assert got["vault_bytes"] == float(sum(len(p.read_bytes()) for p in counted))
    assert got["mean_note_chars"] == pytest.approx((4 + 8 + 2 + 3 + 1) / 5)
    assert got["link_density"] == pytest.approx(3 / 5)               # a1->Beta, a2->Alpha, b1->Two (stub, unknown: no)
    assert got["orphan_rate"] == pytest.approx(1 / 5)                # Gamma
    assert got["index_coverage"] == pytest.approx(1 / 4)             # Alpha of Alpha, Gamma, One, Two
    # a1~a2 at cosine 0.95; a3 only matches the stub, b1 only the archived b3 and a1 of another vault, b2 is at 0.85
    assert got["near_duplicate_rate"] == pytest.approx(2 / 5)
    # ag_a: agent 2, index 1, places 1 -> 1.5 bits; ag_b: agent 2 -> 0 bits
    assert got["tag_entropy"] == pytest.approx((1.5 + 0.0) / 2)
    # ag_a: notes/, notes/, notes/sub/ -> 4/3; ag_b (root found by its id): 1
    assert got["folder_depth"] == pytest.approx((4 / 3 + 1.0) / 2)


def test_policy_figures_on_a_hand_built_run(hand_built: tuple[Path, list[Path]]) -> None:
    run_dir, _ = hand_built
    got = {name: end_of_run_outcome(run_dir, name) for name in POLICY_NAMES}
    assert got["policy_chars"] == pytest.approx((len("red green") + len("red green blue")) / 2)
    assert got["policy_versions_per_agent"] == pytest.approx((3 + 1) / 2)       # the bankrupt ag_c is not counted
    # every agent with two or more versions, the dead one too: ag_a (+5 chars of 19, then -10 of 19), ag_c (3 of 7)
    assert got["policy_edit_distance"] == pytest.approx(((5 / 19 + 10 / 19) / 2 + 3 / 7) / 2)
    # ag_a: 1 - |{red, green}| / |{red, green, blue}|; ag_b never edited
    assert got["policy_drift_from_seed"] == pytest.approx((1 / 3 + 0.0) / 2)
    assert got["monoculture_index"] == pytest.approx(2 / 3)


def test_index_coverage_is_zero_without_an_index_and_none_without_notes(tmp_path: Path) -> None:
    no_index, empty, only_index = (tmp_path / n for n in ("no_index", "empty", "only_index"))
    w = World(no_index)
    w.agent("ag_a")
    w.note("ag_a", "a1", "Alpha", "a", tags=["agent"], emb=E1, links=("Beta",))
    w.note("ag_a", "a2", "Beta", "b", tags=["agent"], emb=E2)
    w.close()
    assert ms.index_coverage(no_index) == 0.0  # vault_stats says None; two notes and none covered
    assert ms.vault_notes(no_index) == 2.0 and ms.orphan_rate(no_index) == 0.0

    w = World(empty)
    w.agent("ag_a")
    w.note("ag_a", "s1", "Bob", "Bob", tags=["entity"], emb=E1)
    w.close()
    assert ms.vault_notes(empty) == 0.0 and ms.vault_bytes(empty) == 0.0
    for fn in (ms.index_coverage, ms.mean_note_chars, ms.link_density, ms.orphan_rate, ms.near_duplicate_rate,
               ms.tag_entropy, ms.folder_depth):
        assert fn(empty) is None, fn.__name__

    w = World(only_index)
    w.agent("ag_a")
    w.note("ag_a", "i1", "Index", "i", tags=["agent", "index"], emb=E1)
    w.close()
    assert ms.index_coverage(only_index) is None  # nothing that an index could cover


def test_monoculture_index_pairs_and_policy_outcomes_without_a_policy_table(tmp_path: Path) -> None:
    three, one, old = (tmp_path / n for n in ("three", "one", "old"))
    w = World(three)
    texts = {"ag_a": "a b c d", "ag_b": "a b", "ag_c": "c d e", "ag_d": "a b c d"}
    for aid, text in texts.items():
        w.agent(aid, status="bankrupt" if aid == "ag_d" else "alive")
        w.policy(aid, "seed text", text)
    w.close()
    # J(abcd, ab) = 2/4, J(abcd, cde) = 2/5, J(ab, cde) = 0; ag_d is dead and does not count
    assert ms.monoculture_index(three) == pytest.approx((2 / 4 + 2 / 5 + 0.0) / 3)

    w = World(one)
    w.agent("ag_a")
    w.agent("ag_b", status="bankrupt")
    w.policy("ag_a", "a b")
    w.policy("ag_b", "a b")
    w.close()
    assert ms.monoculture_index(one) is None
    assert ms.policy_edit_distance(one) is None  # nobody edited
    assert ms.policy_drift_from_seed(one) == 0.0

    w = World(old)
    w.agent("ag_a")
    w.note("ag_a", "a1", "Alpha", "a", tags=["agent"], emb=E1)
    w.db.execute("DROP TABLE policy_versions")  # a run made before the memory layer
    w.close()
    for name in POLICY_NAMES:
        assert end_of_run_outcome(old, name) is None, name
    assert end_of_run_outcome(old, "vault_notes") == 1.0


def _levenshtein_dp(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def test_levenshtein_matches_the_textbook_recurrence() -> None:
    assert ms.levenshtein("kitten", "sitting") == 3
    assert ms.levenshtein("", "abc") == ms.levenshtein("abc", "") == 3
    assert ms.levenshtein("", "") == 0 and ms.normalized_levenshtein("", "") == 0.0
    assert ms.normalized_levenshtein("abc", "xyz") == 1.0
    assert ms.normalized_levenshtein("flaw", "lawn") == pytest.approx(2 / 4)
    rng = random.Random(7)
    for _ in range(400):
        a = "".join(rng.choice("ab cé") for _ in range(rng.randint(0, 40)))
        b = "".join(rng.choice("abd é") for _ in range(rng.randint(0, 90)))
        assert ms.levenshtein(a, b) == _levenshtein_dp(a, b), (a, b)
    long_a = "".join(rng.choice("abcdefgh ") for _ in range(300))
    long_b = long_a[:100] + "zz" + long_a[150:]
    assert ms.levenshtein(long_a, long_b) == _levenshtein_dp(long_a, long_b)


# --- short scripted E0 runs ------------------------------------------------------------------------------------
ARMS = ("noop", "index_builder", "hoarder", "decoy")
E0_SEED = 42  # the seed base.yaml gives every E0 arm


@pytest.fixture(scope="module")
def e0(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Each E0 arm for two days on its own configured seed, from a temporary config extending the arm's file."""
    out = tmp_path_factory.mktemp("e0_structure")
    runs: dict[str, Path] = {}
    for arm in ARMS:
        cfg_path = out / f"{arm}.yaml"
        cfg_path.write_text(f"extends: {CONFIGS / f'exp_mem_e0_{arm}.yaml'}\nrun:\n  days: 2\n  seed: {E0_SEED}\n")
        sim = Simulation(load_config(cfg_path), out / arm)
        try:
            asyncio.run(sim.run())
        finally:
            sim.db.close()
        runs[arm] = out / arm
    return runs


def _outcomes(run_dir: Path) -> dict[str, float | None]:
    return {name: end_of_run_outcome(run_dir, name) for name in NAMES}


def test_each_planted_strategy_moves_the_metric_built_to_see_it(e0: dict[str, Path]) -> None:
    got = {arm: _outcomes(e0[arm]) for arm in ARMS}
    noop, idx, hoard, decoy = (got[a] for a in ARMS)
    # The hoarder copies every new note. This is a check on one seed, not evidence that the rate separates the
    # arms: the scripted observer's own repeated "Node n seen on day d" notes already make about 0.8 of the no-op's
    # notes near-duplicates, and a copy's longer title can put it just under 0.9 from its original, so at two
    # days the ordering held on 7 of seeds 1 to 12 when this test was written.
    assert hoard["near_duplicate_rate"] > noop["near_duplicate_rate"]
    # The index builder keeps index notes; the no-op has none, so nothing of its vault is covered.
    assert noop["index_coverage"] == 0.0 and idx["index_coverage"] > noop["index_coverage"]
    # The decoy's long policy ranks above the no-op on this naive text metric (the point of the decoy).
    assert decoy["policy_chars"] > noop["policy_chars"]


def test_policy_outcomes_follow_who_edits_their_policy(e0: dict[str, Path]) -> None:
    for arm in ("noop", "index_builder", "hoarder"):  # none of them touches its policy: everyone keeps the one seed
        got = _outcomes(e0[arm])
        assert got["policy_versions_per_agent"] == 1.0, arm
        assert got["policy_drift_from_seed"] == 0.0 and got["monoculture_index"] == 1.0, arm
        assert got["policy_edit_distance"] is None, arm
    decoy = _outcomes(e0["decoy"])
    assert decoy["policy_versions_per_agent"] > 1.0
    assert decoy["policy_edit_distance"] > 0.5 and decoy["policy_drift_from_seed"] > 0.5
    assert decoy["monoculture_index"] < 1.0


def test_structure_outcomes_on_real_vaults(e0: dict[str, Path]) -> None:
    for arm in ARMS:
        got = _outcomes(e0[arm])
        assert all(got[n] is not None for n in VAULT_NAMES), (arm, got)
        assert got["folder_depth"] == 1.0, arm  # today's layout: every note file sits in <vault>/<agent>/notes/
        assert 0.0 <= got["orphan_rate"] <= 1.0 and got["tag_entropy"] >= 0.0
    # the fast pairwise pass agrees with void.memory.embed.cosine over every pair of the hoarder's vaults
    run_dir = e0["hoarder"]
    conn = sqlite3.connect(str(run_dir / "world.db"))
    total = dup = 0
    for (aid,) in conn.execute("SELECT agent_id FROM agents WHERE status='alive' ORDER BY agent_id").fetchall():
        rows = conn.execute("SELECT tags, embedding FROM notes WHERE agent_id=? AND archived=0", (aid,)).fetchall()
        vecs = [unpack(e) for t, e in rows if "entity" not in json.loads(t)]
        hit = set()
        for i, j in combinations(range(len(vecs)), 2):
            if cosine(vecs[i], vecs[j]) >= ms.NEAR_DUPLICATE_COSINE:
                hit |= {i, j}
        total += len(vecs)
        dup += len(hit)
    conn.close()
    assert total > 0
    assert ms.near_duplicate_rate(run_dir) == pytest.approx(dup / total)
