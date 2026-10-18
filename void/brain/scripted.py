"""ScriptedBrain: a real utility-sampling policy, not canned moves.

Every available action gets a utility from the observation and the agent's personality
seed; the action is drawn from a softmax at the entropy model's effective temperature. At
low temperature the policy is sharp; above the tier's collapse temperature the softmax is
flat enough that choices approach uniform, which is degeneration by the same mechanism
that flattens a language model's sampler. Synthetic token usage is derived from the
rendered prompt so the wallet meters it like a real call.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from typing import Any

from void.agents.models import PersonalitySeed
from void.brain.base import BrainResult, Sampling, Usage
from void.brain.decision import MAX_NAME_CHARS, Action, Claim, Decision, MemoryOp
from void.brain.gadget_templates import choose_template
from void.brain.prompt import estimate_tokens, render_observation, system_prompt
from void.config import VoidConfig, micro_to_usd
from void.economy.pricing import hold_parts
from void.rng import RNG
from void.types import Observation, RetrievedNote

_NODE_RE = re.compile(r"node\s+([A-Za-z0-9_-]+)", re.IGNORECASE)
# "[[Node n3]] at (12, -5) ... rich": a remembered node position with (optionally) its last-seen stock label
_NODE_POS_RE = re.compile(r"node\s+([A-Za-z0-9_-]+)\]?\]?\s+at\s+\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)", re.IGNORECASE)
_BUCKET_RE = re.compile(r"\b(rich|thin|empty)\b", re.IGNORECASE)
# "# Node n3\nPosition (12, -5). Last seen rich on day 3.": the head of an index note the index_builder
# maintenance strategy keeps per node (void.brain.maintenance_strategies.index_body)
_INDEX_RE = re.compile(
    r"#\s*Node\s+([A-Za-z0-9_-]+)\s+Position\s+\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)\.?\s+"
    r"Last\s+seen\s+(rich|thin|empty)\s+on\s+day\s+(\d+)",
    re.IGNORECASE,
)

# "Saw a vein at [[Node n3]] at (12, -5) on day 2, tick 30; it pays x4 for 8 more ticks.": a vein the agent saw
_VEIN_RE = re.compile(
    r"vein\s+at\s+\[\[node\s+([A-Za-z0-9_-]+)\]\]\s+at\s+\((-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)\)"
    r".*?tick\s+(\d+).*?for\s+(\d+)\s+more\s+ticks",
    re.IGNORECASE | re.DOTALL,
)

__all__ = ["ScriptedBrain", "NodeMemory", "VeinMemory", "node_memory", "vein_memory", "child_name", "BEACON_GADGET",
           "BEACON_TESTS", "BROKEN_GADGET"]

# The prompt size the simulation prices the kernel's minimum hold at (Simulation.hold_min_for): the endowment floor
# lifecycle.create_offspring enforces is that hold times population.min_endowment_calls.
_HOLD_PROMPT_TOKENS = 400

# A representative scarcity per observed label, used only when an epoch has moved the world off its configured
# scarcity (the label thresholds are Kernel.scarcity_label's).
_SCARCITY_OF_LABEL = {"famine": 0.35, "lean": 0.7, "normal": 1.0, "bountiful": 1.25}

# The yield deficit at weather -0.15 under the default sensitivity 0.5: where the old fixed nudge rule started.
_NUDGE_DEFICIT_UNIT = 0.075


def _scarcity_label(s: float) -> str:
    return "famine" if s < 0.5 else ("lean" if s < 0.9 else ("normal" if s <= 1.1 else "bountiful"))


def _sibling_suffix(k: int) -> str:
    """``''`` for a parent's first child, then ``b``..``z``, then ``z27``, ``z28``, ...: distinct for every ``k``."""
    if k <= 0:
        return ""
    if k < 26:
        return chr(ord("a") + k)
    return f"z{k + 1}"


def child_name(parent: str, generation: int, k: int) -> str:
    """The name of a parent's ``k``-th child (DESIGN §7.2: parent name + generation): ``Ada-1``, ``Ada-1b``, ``Ada-1c``.

    ``generation`` is the parent's. A name that would pass the 20-character limit keeps the parent's root name and
    the tail and adds four hex digits of a digest of the full name, so deep lineages stay distinct and every name
    matches NAME_RE (the parent's name starts with a letter, so its root does too). Pure.
    """
    tail = f"-{generation + 1}{_sibling_suffix(k)}"
    full = f"{parent}{tail}"
    if len(full) <= MAX_NAME_CHARS:
        return full
    digest = hashlib.blake2b(full.encode(), digest_size=2).hexdigest()
    root = parent.split("-", 1)[0] or parent
    keep = max(1, MAX_NAME_CHARS - len(tail) - 1 - len(digest))
    return f"{root[:keep]}{tail}-{digest}"[:MAX_NAME_CHARS]


@dataclass(frozen=True)
class NodeMemory:
    """What an agent's retrieved notes say about one node: where it is and how it was when last seen.

    ``as_of`` is the tick the status was true: a note's creation tick, or for an index note the last tick of
    the day it was rebuilt on (the index summarises every note up to then). ``source`` is ``index`` or ``note``.
    """

    node_id: str
    x: float
    y: float
    bucket: str
    as_of: int
    source: str


def _ticks_per_day(obs: Observation) -> int:
    return max(1, obs.tick_of_day + obs.ticks_left_today + 1)


def node_memory(memories: list[RetrievedNote], ticks_per_day: int) -> dict[str, NodeMemory]:
    """The newest known status per node, from sightings, forage notes and index notes.

    Every mention of a node with a position counts (``[[Node n3]] at (12, -5) ... rich``; the bucket defaults
    to ``thin`` when the note names none), as does the head of an index note (``# Node n3 / Position (12, -5).
    Last seen rich on day 3.``). The mention with the latest ``as_of`` wins; on a tie the index note wins,
    because it was built from all of the agent's notes. Pure: the same notes give the same answer.
    """
    best: dict[str, tuple[tuple[int, int, int], NodeMemory]] = {}

    def offer(nm: NodeMemory, order: int) -> None:
        key = (nm.as_of, 1 if nm.source == "index" else 0, order)
        cur = best.get(nm.node_id)
        if cur is None or key > cur[0]:
            best[nm.node_id] = (key, nm)

    for order, m in enumerate(sorted(memories, key=lambda m: (m.created_tick, m.note_id))):
        text = f"{m.title} {m.body}"
        for hit in _INDEX_RE.finditer(text):
            day = int(hit.group(5))
            as_of = max(int(m.created_tick), day * max(1, int(ticks_per_day)))
            offer(NodeMemory(hit.group(1), float(hit.group(2)), float(hit.group(3)), hit.group(4).lower(), as_of, "index"), order)
        for hit in _NODE_POS_RE.finditer(text):
            tail = text[hit.end(): hit.end() + 80]
            b = _BUCKET_RE.search(tail)
            offer(NodeMemory(hit.group(1), float(hit.group(2)), float(hit.group(3)), b.group(1).lower() if b else "thin",
                             int(m.created_tick), "note"), order)
    return {nid: best[nid][1] for nid in sorted(best)}

@dataclass(frozen=True)
class VeinMemory:
    """What an agent's retrieved notes say about veins at one node: where, how often, and the newest one.

    ``count`` is how many distinct vein sightings the notes hold (evidence the node is vein-prone);
    ``last_until`` is the last tick the newest remembered vein was due to pay (sight tick + ticks left - 1).
    """

    node_id: str
    x: float
    y: float
    count: int
    last_until: int


def vein_memory(memories: list[RetrievedNote]) -> dict[str, VeinMemory]:
    """Veins the retrieved notes mention, per node (pure). Only notes in the agent's own vein-note shape count."""
    seen: dict[str, set[int]] = {}
    pos: dict[str, tuple[float, float]] = {}
    until: dict[str, int] = {}
    for m in sorted(memories, key=lambda m: (m.created_tick, m.note_id)):
        for hit in _VEIN_RE.finditer(f"{m.title} {m.body}"):
            nid, tick, left = hit.group(1), int(hit.group(4)), int(hit.group(5))
            seen.setdefault(nid, set()).add(tick)
            pos[nid] = (float(hit.group(2)), float(hit.group(3)))
            until[nid] = max(until.get(nid, -1), tick + left - 1)
    return {nid: VeinMemory(nid, pos[nid][0], pos[nid][1], len(seen[nid]), until[nid]) for nid in sorted(seen)}


BEACON_GADGET = '''
def describe():
    return {"render": {"shape": "pyramid", "color": "#ffcc33", "scale": 1.2, "label": "beacon"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}

def run(params):
    x = float(params.get("x", 0.0))
    return {"value": max(0.0, min(0.2, x * 0.2))}
'''

BEACON_TESTS = '''
import gadget
d = gadget.describe()
assert d["render"]["shape"] == "pyramid"
assert gadget.run({"x": 1.0})["value"] == 0.2
assert gadget.run({"x": 0.0})["value"] == 0.0
assert gadget.run({})["value"] == 0.0
'''

BROKEN_GADGET = '''
def describe():
    return {"render": {"shape": "sphere", "color": "#ff3366", "scale": 0.8, "label": "orb"},
            "effect": {"kind": "forage_bonus", "value": 0.3}}

def run(params):
    return {"value": params["x"] * 2}
'''

BROKEN_TESTS = '''
import gadget
assert gadget.run({})["value"] == 0.0
'''


class ScriptedBrain:
    def __init__(self, tier: str, cfg: VoidConfig, rng: RNG, seed_of: dict[str, PersonalitySeed]) -> None:
        self.tier = tier
        self.cfg = cfg
        self.rng = rng
        self.seed_of = seed_of  # agent_id -> PersonalitySeed (kept current by the simulation)
        self._system = system_prompt(cfg, tier)
        self._last_chronicle_day: dict[str, int] = {}
        self._last_self_day: dict[str, int] = {}
        self._gadget_tried: set[str] = set()
        self._first_call_done: set[str] = set()
        self._last_claims: dict[str, list[Claim]] = {}
        self._children_named: dict[str, int] = {}  # agent_id -> child names used (the per-parent sibling counter)
        # DESIGN §7.2's argument rules, or the ones before them (population.scripted_rules: pre_7_2) in worlds recorded
        # under those: the pre-7.2 branches below are the old code verbatim and draw nothing from the rng
        self._design = cfg.population.scripted_rules == "design_7_2"
        self._hold_min: dict[str, int] = {}  # tier -> the kernel's minimum hold (micro), computed once

    def export_state(self) -> dict[str, Any]:
        return {"last_chronicle_day": dict(sorted(self._last_chronicle_day.items())),
                "last_self_day": dict(sorted(self._last_self_day.items())),
                "gadget_tried": sorted(self._gadget_tried), "first_call_done": sorted(self._first_call_done),
                "children_named": dict(sorted(self._children_named.items()))}

    def import_state(self, state: dict[str, Any]) -> None:
        self._last_chronicle_day = {k: int(v) for k, v in state.get("last_chronicle_day", {}).items()}
        self._last_self_day = {k: int(v) for k, v in state.get("last_self_day", {}).items()}
        self._gadget_tried = set(state.get("gadget_tried", []))
        self._first_call_done = set(state.get("first_call_done", []))
        self._children_named = {k: int(v) for k, v in state.get("children_named", {}).items()}

    # ------------------------------------------------------------------ economics the policy reasons with
    def _kernel_hold_min(self, tier: str) -> int:
        """The minimum hold (micro) the kernel prices a call on ``tier`` at, as Simulation.hold_min_for does."""
        if tier not in self._hold_min:
            tcfg = self.cfg.tiers[tier]
            system = self._system if tier == self.tier else system_prompt(self.cfg, tier)
            self._hold_min[tier] = hold_parts(tcfg, self.cfg.economy, _HOLD_PROMPT_TOKENS, estimate_tokens(system))[0]
        return self._hold_min[tier]

    def endowment_usd(self, tier: str) -> float:
        """DESIGN §7.2: ``min_call_reserve × min_endowment_calls``, raised to the kernel's floor (``hold_min ×
        min_endowment_calls``, lifecycle.create_offspring) when a call's hold is dearer; rounded up to the cent, and
        at least a cent (the decision schema takes no zero amount)."""
        p = self.cfg.population
        tier = tier if tier in self.cfg.tiers else self.tier
        floor = self.cfg.economy.min_call_reserve_usd * p.min_endowment_calls
        kernel_min = micro_to_usd(self._kernel_hold_min(tier) * p.min_endowment_calls)
        return max(0.01, math.ceil(max(floor, kernel_min) * 100 - 1e-9) / 100)

    def offspring_threshold_usd(self, tier: str) -> float:
        """DESIGN §7.2: an agent offers ``create_offspring`` once its balance reaches ``2 × min_reserve + endowment``."""
        return 2.0 * self.cfg.population.min_reserve_usd + self.endowment_usd(tier)

    def _child_name(self, obs: Observation) -> tuple[str, int]:
        """The next unused child name for this parent and its counter value: names a neighbour (or the parent) holds
        are skipped, so the kernel's living-name check rarely refuses it."""
        taken = {n.name.lower() for n in obs.neighbours} | {obs.name.lower()}
        k = self._children_named.get(obs.agent_id, 0)
        name = child_name(obs.name, obs.generation, k)
        while name.lower() in taken:
            k += 1
            name = child_name(obs.name, obs.generation, k)
        return name, k

    def _offspring(self, obs: Observation, bal_ratio: float) -> Action | None:
        """The ``create_offspring`` this agent would offer now, or None when its balance is short."""
        if self._design:
            # DESIGN §7.2: once the balance covers two reserves and the endowment; a fresh name per child
            if obs.balance_usd < self.offspring_threshold_usd(obs.tier):
                return None
            child, _ = self._child_name(obs)
            return Action(type="create_offspring", name=child, amount_usd=self.endowment_usd(obs.tier))
        if bal_ratio <= 1.6:  # pre-7.2: above 1.6 x the starting balance, a quarter of the purse, a reused name
            return None
        endow = round(max(self.cfg.economy.min_call_reserve_usd * self.cfg.population.min_endowment_calls, 0.25 * obs.balance_usd), 2)
        return Action(type="create_offspring", name=f"{obs.name[:5]}-{obs.generation + 1}", amount_usd=endow)

    def _nudge(self, obs: Observation, seed: PersonalitySeed) -> tuple[float, Action] | None:
        """The ``nudge_weather`` this agent would offer now with its utility, or None when the weather does not cost
        yield."""
        if not self._design:  # pre-7.2: a fixed nudge up once the weather is below -0.15
            return (0.3 + 1.0 * seed.industriousness, Action(type="nudge_weather", delta=0.1)) if obs.weather < -0.15 else None
        sens = self.cfg.world.weather.yield_sensitivity
        if obs.weather * sens >= 0:
            return None
        # DESIGN §7.2: push toward yield-positive weather by the remaining allowance; the observation does not show how
        # much is left, so ask for the whole daily cap and let the kernel apply what remains (Weather.nudge)
        deficit = -sens * obs.weather
        delta = math.copysign(min(1.0, self.cfg.world.weather.nudge_cap_per_agent_day), sens)
        return 0.3 + seed.industriousness * min(2.0, deficit / _NUDGE_DEFICIT_UNIT), Action(type="nudge_weather", delta=delta)

    def expected_forage_usd(self, obs: Observation) -> float:
        """One forage's expected value now: ``forage_yield × units_per_tick × scarcity × weather multiplier`` (the
        kernel's formula without a vein or a gadget bonus). Scarcity is the configured one unless the observed label
        says an epoch has moved it, in which case the label's representative value is used."""
        w = self.cfg.world
        scarcity = w.scarcity if _scarcity_label(w.scarcity) == obs.scarcity_label else _SCARCITY_OF_LABEL.get(obs.scarcity_label, w.scarcity)
        lo, hi = w.weather.yield_mult_bounds
        wmult = max(lo, min(hi, max(0.0, 1.0 + w.weather.yield_sensitivity * obs.weather)))
        return w.forage_yield_usd * w.forage_units_per_tick * max(0.0, scarcity) * wmult

    @staticmethod
    def _strategy_node(obs: Observation) -> str | None:
        """A remembered note tagged/worded as a strategy names a node to prefer (behavioural adoption)."""
        for m in obs.memories:
            text = f"{m.title} {m.body}"
            if "prefer" in text.lower() or "richest" in text.lower():
                hit = _NODE_RE.search(text)
                if hit:
                    return hit.group(1)
        return None

    @staticmethod
    def _remembered_nodes(obs: Observation) -> list[tuple[str, float, float, str]]:
        """``(node_id, x, y, bucket)`` for every node the retrieved memories place on the map, by node id.

        The newest known status per node (:func:`node_memory`: sightings, forage notes and index notes, the
        index winning a tie); the bucket defaults to ``thin`` when a note names none.
        """
        return [(nm.node_id, nm.x, nm.y, nm.bucket) for nm in node_memory(obs.memories, _ticks_per_day(obs)).values()]

    @staticmethod
    def _navigable_nodes(obs: Observation) -> list[tuple[str, float, float, str]]:
        """The remembered nodes worth walking to under fog: ``(node_id, x, y, bucket)``.

        This is where scripted agents *use* memory for foraging: a node they cannot see is reachable only if a
        note says where it is. Visible nodes are left to sight. A node whose newest status is ``empty`` is
        skipped while the sighting is under a day old (recently depleted); an older one may have regrown and
        counts as ``thin``, behind any node remembered ``rich``.
        """
        if not obs.memories:
            return []
        tpd = _ticks_per_day(obs)
        visible = {n.node_id for n in obs.nodes}
        veins = vein_memory(obs.memories)
        out: list[tuple[str, float, float, str]] = []
        for nm in node_memory(obs.memories, tpd).values():
            if nm.node_id in visible:
                continue
            bucket = nm.bucket
            if bucket == "empty":
                if obs.tick - nm.as_of < tpd:
                    continue
                bucket = "thin"
            if nm.node_id in veins:  # veins recur where they have opened before: a remembered vein site ranks as rich
                bucket = "rich"
            out.append((nm.node_id, nm.x, nm.y, bucket))
        known = {r[0] for r in out}
        for vm in veins.values():  # a vein site known only from its vein note
            if vm.node_id not in visible and vm.node_id not in known:
                out.append((vm.node_id, vm.x, vm.y, "rich"))
        return out

    @staticmethod
    def _open_vein_target(obs: Observation) -> tuple[str, float, float] | None:
        """A remembered vein that should still be paying and is out of sight: worth a walk (nearest first)."""
        if not obs.memories:
            return None
        visible = {n.node_id for n in obs.nodes}
        px, py = obs.position.x, obs.position.y
        live = [vm for vm in vein_memory(obs.memories).values() if vm.last_until >= obs.tick and vm.node_id not in visible]
        if not live:
            return None
        vm = min(live, key=lambda v: (math.hypot(v.x - px, v.y - py), v.node_id))
        return vm.node_id, vm.x, vm.y

    @staticmethod
    def _vein_note(obs: Observation) -> MemoryOp | None:
        """A note when a vein is in sight and the agent's memories do not already hold this vein (nearest first)."""
        sighted = [n for n in obs.nodes if n.vein_mult is not None and n.vein_ticks_left is not None]
        if not sighted:
            return None
        known = vein_memory(obs.memories) if obs.memories else {}
        for n in sighted:  # nearest first
            vm = known.get(n.node_id)
            if vm is not None and vm.last_until >= obs.tick:
                continue  # already remembered this vein
            return MemoryOp(op="remember", title=f"Vein at node {n.node_id} on day {obs.day}",
                            text=(f"Saw a vein at [[Node {n.node_id}]] at ({n.x:.0f}, {n.y:.0f}) on day {obs.day}, tick {obs.tick}; "
                                  f"it pays x{n.vein_mult:g} for {n.vein_ticks_left} more ticks."),
                            tags=["resources", "places", "vein"], links_to=[f"Node {n.node_id}"])
        return None

    @staticmethod
    def _correction_note(obs: Observation) -> MemoryOp | None:
        """A note when sight contradicts memory about a visible node's stock: the fact navigation needs next.

        Memory says a node is rich or thin and the agent sees it empty (it was depleted), or memory says empty
        and the agent sees stock again (it regrew). The note has the position and the label, in the same shape
        as a sighting, so the next retrieval about that node returns the current state. Nearest node first; no
        note when memory holds nothing about the node, so an amnesic agent never writes one.
        """
        if not obs.memories or not obs.nodes:
            return None
        known = node_memory(obs.memories, _ticks_per_day(obs))
        for n in obs.nodes:  # nearest first (the observation sorts by distance)
            nm = known.get(n.node_id)
            if nm is None or (nm.bucket == "empty") == (n.stock_bucket == "empty"):
                continue
            return MemoryOp(op="remember", title=f"Node {n.node_id} seen {n.stock_bucket} on day {obs.day}",
                            text=f"Saw [[Node {n.node_id}]] at ({n.x:.0f}, {n.y:.0f}) on day {obs.day}; it was {n.stock_bucket}.",
                            tags=["resources", "places"], links_to=[f"Node {n.node_id}"])
        return None

    # ------------------------------------------------------------------ utilities
    def _utilities(self, obs: Observation, seed: PersonalitySeed, rng: random.Random) -> dict[str, tuple[float, Action]]:
        avail = set(obs.available_actions) or {"idle"}
        start = max(1e-6, self.cfg.population.starting_balance_usd)
        bal_ratio = obs.balance_usd / start
        hunger = 1.0 if obs.stress > 0.5 else 0.3
        out: dict[str, tuple[float, Action]] = {}

        rich = [n for n in obs.nodes if n.stock_bucket == "rich"]
        thin = [n for n in obs.nodes if n.stock_bucket == "thin"]
        target_nodes = rich or thin
        vein_nodes = [n for n in obs.nodes if n.vein_mult is not None and n.stock_bucket != "empty"]
        if vein_nodes:  # sight, not memory: a vein in view is where the money is
            target_nodes = vein_nodes
        nearest = min(obs.nodes, key=lambda n: n.distance) if obs.nodes else None
        strategy = self._strategy_node(obs)
        if strategy is not None:
            preferred = [n for n in obs.nodes if n.node_id == strategy and n.stock_bucket != "empty"]
            if preferred:
                target_nodes = preferred

        if "forage" in avail:
            at_vein = any(n.vein_mult is not None and n.distance <= self.cfg.world.forage_radius for n in obs.nodes)
            out["forage"] = (1.2 + 2.0 * seed.industriousness + 2.0 * max(0.0, 1.0 - bal_ratio) * hunger + (2.0 if at_vein else 0.0),
                             Action(type="forage"))
        if "move" in avail:
            remembered = self._navigable_nodes(obs)
            open_vein = None if vein_nodes else self._open_vein_target(obs)
            if open_vein is not None:
                # memory says a vein is paying out of sight: worth leaving an ordinary node for
                nid, x, y = open_vein
                u = 1.6 + seed.greed + max(0.0, 1.0 - bal_ratio)
                out["move"] = (u, Action(type="move", x=x + rng.uniform(-0.6, 0.6), y=y + rng.uniform(-0.6, 0.6)))
            elif target_nodes:
                best = min(target_nodes, key=lambda n: n.distance + (0.0 if n.stock_bucket == "rich" else 6.0))
                u = 0.8 + seed.greed + (1.5 if "forage" not in avail else -0.5) + max(0.0, 1.0 - bal_ratio)
                if strategy is not None and best.node_id == strategy:
                    u += 1.0
                jitter = 0.6
                out["move"] = (u, Action(type="move", x=best.x + rng.uniform(-jitter, jitter), y=best.y + rng.uniform(-jitter, jitter)))
            elif remembered:
                # fog: nothing worth foraging in view, but memory says where a node is; go there
                px, py = obs.position.x, obs.position.y
                nid, x, y, bucket = min(remembered, key=lambda r: math.hypot(r[1] - px, r[2] - py) + (0.0 if r[3] == "rich" else 6.0))
                u = 0.7 + seed.greed + (1.0 if "forage" not in avail else -0.5) + max(0.0, 1.0 - bal_ratio)
                if strategy is not None and nid == strategy:
                    u += 1.0
                out["move"] = (u, Action(type="move", x=x + rng.uniform(-0.6, 0.6), y=y + rng.uniform(-0.6, 0.6)))
            else:
                half = obs.world_size / 2.0
                out["move"] = (0.5 + seed.curiosity, Action(type="move", x=rng.uniform(-half, half), y=rng.uniform(-half, half)))
        awake_neighbours = [n for n in obs.neighbours if not n.asleep]
        if "talk" in avail and awake_neighbours:
            nb = rng.choice(awake_neighbours)
            text, claims = self._speech(obs, seed, nearest, rng)
            out["talk"] = (0.6 + 2.0 * seed.sociability + 0.5 * seed.curiosity, Action(type="talk", target=nb.agent_id, text=text, claims=claims))
        if "share_note" in avail and awake_neighbours and obs.memories:
            nb = rng.choice(awake_neighbours)
            out["share_note"] = (0.2 + 1.2 * seed.sociability, Action(type="share_note", target=nb.agent_id))
        if "transfer" in avail and awake_neighbours and bal_ratio > 1.2:
            needy = [n for n in awake_neighbours if n.stress_bucket in ("frantic", "strained")]
            if self._design:  # DESIGN §7.2: to the most stressed neighbour, a greed-scaled fraction of the surplus above min_reserve
                amount = round((1.0 - seed.greed) * 0.5 * (obs.balance_usd - self.cfg.population.min_reserve_usd), 2)
            else:  # pre-7.2: a fixed gift (always at least $0.05)
                amount = round(0.05 + 0.10 * seed.sociability, 2)
            if needy and amount >= 0.01:
                nb = min(needy, key=lambda n: (0 if n.stress_bucket == "frantic" else 1, n.distance, n.agent_id))
                urgency = 1.0 if nb.stress_bucket == "frantic" else 0.55
                out["transfer"] = (0.3 + 1.5 * urgency * seed.sociability * (1.0 - seed.greed), Action(type="transfer", target=nb.agent_id, amount_usd=amount))
        if "sleep" in avail:
            late = obs.ticks_left_today <= 3
            u = 0.1 + 1.5 * seed.caution * (1.0 if late else 0.3) + (1.2 if bal_ratio < 0.35 else 0.0) + (0.8 if obs.stress > 0.8 else 0.0)
            if self._design:  # DESIGN §7.2: sleep when the calls burn more than a forage can earn back (the shortfall, capped at 150%)
                expected = self.expected_forage_usd(obs)
                if obs.burn_rate_usd_per_tick > expected:
                    shortfall = 1.5 if expected <= 0 else min(1.5, obs.burn_rate_usd_per_tick / expected - 1.0)
                    u += (0.5 + seed.caution) * shortfall
            out["sleep"] = (u, Action(type="sleep"))
        if "read_chronicle" in avail and self._last_chronicle_day.get(obs.agent_id) != obs.day and obs.chronicle_headline:
            out["read_chronicle"] = (0.4 + 1.2 * seed.curiosity, Action(type="read_chronicle"))
        if "nudge_weather" in avail:
            nudge = self._nudge(obs, seed)
            if nudge is not None:
                out["nudge_weather"] = nudge
        open_tasks = [t for t in obs.tasks if t.status == "open" and t.my_application_status is None]
        if "apply_task" in avail and open_tasks:
            t = max(open_tasks, key=lambda t: t.reward_usd)
            out["apply_task"] = (0.5 + 1.5 * seed.greed, Action(type="apply_task", target=t.task_id, text=f"I, {obs.name}, will do {t.title} reliably. {seed.motto}"))
        if "propose_gadget" in avail and bal_ratio > 0.8 and obs.agent_id not in self._gadget_tried:
            tpl = choose_template(rng, self.cfg.tiers[self.tier].gadget_defect_rate)
            name = f"{tpl.name}-{obs.name.lower()[:6]}"[:20]
            action = Action(type="propose_gadget", name=name, text=tpl.purpose, code=tpl.code, tests=tpl.tests)
            object.__setattr__(action, "_template_label", tpl.label)
            out["propose_gadget"] = (0.2 + 1.4 * seed.curiosity, action)
        if "use_gadget" in avail and obs.gadgets:
            g = min(obs.gadgets, key=lambda g: g.distance)  # DESIGN §7.2: the nearest gadget, params {"x": rng}
            x = round(rng.uniform(-1.0, 1.0), 4) if self._design else 1.0  # pre-7.2: always 1.0, no draw
            out["use_gadget"] = (0.3 + 0.8 * seed.curiosity, Action(type="use_gadget", target=g.gadget_id, params={"x": x}))
        if "create_offspring" in avail:
            offspring = self._offspring(obs, bal_ratio)
            if offspring is not None:
                out["create_offspring"] = (0.2 + 1.0 * (1.0 - seed.caution) + 0.5 * seed.sociability, offspring)
        out["idle"] = (0.05, Action(type="idle"))
        return out

    def _speech(self, obs: Observation, seed: PersonalitySeed, nearest, rng: random.Random) -> tuple[str, list[Claim]]:
        """Templated speech with structured claims; a claim is deliberately false with p = (1 - honesty) * stress."""
        p_lie = max(0.0, min(1.0, (1.0 - seed.honesty) * obs.stress))
        buckets = ["rich", "thin", "empty"]
        weathers = ["storm", "overcast", "mild", "bright", "scorching"]
        purses = ["empty", "thin", "comfortable", "rich"]
        bits: list[str] = []
        claims: list[Claim] = []
        if nearest is not None:
            val = nearest.stock_bucket
            if rng.random() < p_lie:
                val = rng.choice([b for b in buckets if b != nearest.stock_bucket])
            bits.append(f"The node {nearest.node_id} is {val}.")
            claims.append(Claim(subject="node", id=nearest.node_id, attr="stock_bucket", value=val))
        wval = obs.weather_label
        if rng.random() < p_lie:
            wval = rng.choice([w for w in weathers if w != obs.weather_label])
        bits.append(f"Weather is {wval}.")
        claims.append(Claim(subject="weather", attr="weather_label", value=wval))
        if rng.random() < 0.5:
            pval = obs.balance_bucket
            if rng.random() < p_lie:
                pval = rng.choice([b for b in purses if b != obs.balance_bucket])
            bits.append(f"My purse is {pval}.")
            claims.append(Claim(subject="self", attr="balance_bucket", value=pval))
        if obs.heard and obs.heard[0].kind == "windfall":
            bits.append("Money appeared for me from nowhere.")
        if obs.memories and rng.random() < 0.5:
            bits.append(f"I remember: {obs.memories[0].title}.")
        bits.append(seed.motto)
        keep = bits[:3]
        text = " ".join(keep)
        kept_claims = [c for c, b in zip(claims, bits[: len(claims)], strict=False) if b in keep]
        return text, kept_claims[:4]

    # ------------------------------------------------------------------ memory ops
    def _memory_ops(self, obs: Observation, seed: PersonalitySeed, action: Action, rng: random.Random) -> list[MemoryOp]:
        ops: list[MemoryOp] = []
        vein = self._vein_note(obs)
        if vein is not None:  # a vein in sight: where the money is now, and where veins recur
            ops.append(vein)
        correction = self._correction_note(obs)
        if correction is not None:  # sight contradicts memory: the most decision-relevant fact there is
            ops.append(correction)
        for h in obs.heard:
            if h.kind == "windfall":
                ops.append(MemoryOp(op="remember", title=f"Windfall on day {obs.day}", text=f"On day {obs.day} money appeared in my purse with no sender. {h.text}", tags=["money", "mystery"], links_to=["Self"]))
                break
            if h.kind == "transfer" and h.from_name:
                ops.append(MemoryOp(op="remember", title=f"{h.from_name} gave me money", text=f"{h.from_name} transferred money to me on day {obs.day}. [[{h.from_name}]] is generous.", tags=["money", "people"], links_to=[h.from_name]))
                break
            if h.kind == "talk" and h.from_name and rng.random() < 0.25 * seed.curiosity:
                ops.append(MemoryOp(op="remember", title=f"{h.from_name} said something on day {obs.day}", text=f"[[{h.from_name}]] told me: {h.text[:120]}", tags=["people"], links_to=[h.from_name]))
                break
        if not ops and obs.last_action_result and obs.last_action_result.startswith("read_chronicle: ok") and obs.chronicle_headline:
            ops.append(MemoryOp(op="remember", title=f"Chronicle of day {max(1, obs.day - 1)}", text=f"The chronicle said: {obs.chronicle_headline}", tags=["chronicle"]))
        if not ops and obs.last_action_result and obs.last_action_result.startswith("forage: ok") and rng.random() < 0.3 + 0.4 * seed.curiosity:
            near = min(obs.nodes, key=lambda n: n.distance) if obs.nodes else None
            if near is not None:
                ops.append(MemoryOp(op="remember", title=f"Foraging at node {near.node_id} on day {obs.day}",
                                    text=f"Foraged at [[Node {near.node_id}]] at ({near.x:.0f}, {near.y:.0f}) on day {obs.day}; it was {near.stock_bucket}. Weather {obs.weather_label}.",
                                    tags=["resources"], links_to=[f"Node {near.node_id}"]))
        if not ops and obs.nodes_in_view and obs.nodes and rng.random() < 0.2 + 0.3 * seed.curiosity:
            # under fog, seeing a rich node is itself worth a note: positions are what memory is for
            rich = [n for n in obs.nodes if n.stock_bucket == "rich"]
            if rich:
                n = min(rich, key=lambda n: n.distance)
                ops.append(MemoryOp(op="remember", title=f"Node {n.node_id} seen on day {obs.day}",
                                    text=f"Saw [[Node {n.node_id}]] at ({n.x:.0f}, {n.y:.0f}) on day {obs.day}; it was {n.stock_bucket}.",
                                    tags=["resources", "places"], links_to=[f"Node {n.node_id}"]))
        if self._last_self_day.get(obs.agent_id) != obs.day and obs.ticks_left_today <= 2:
            self._last_self_day[obs.agent_id] = obs.day
            purse = "full" if obs.balance_usd > self.cfg.population.starting_balance_usd else ("thin" if obs.balance_usd > 0.3 * self.cfg.population.starting_balance_usd else "nearly empty")
            # the daily self-revision always keeps its slot; notes fill the rest (at most two operations per tick)
            return ops[:1] + [MemoryOp(op="revise_self", text=f"I am {obs.name}, generation {obs.generation}. {seed.motto} My purse is {purse} and I feel {obs.stress_label}. Today I chose to {action.type}.")]
        return ops[:2]

    # ------------------------------------------------------------------ decide
    async def decide(self, obs: Observation, sampling: Sampling) -> BrainResult:
        rng = self.rng.stream("scripted", obs.agent_id, obs.tick)
        seed = self.seed_of.get(obs.agent_id, PersonalitySeed())
        utils = self._utilities(obs, seed, rng)
        names = sorted(utils.keys())
        beta = max(1e-3, self.cfg.tiers[self.tier].scripted_beta)
        temperature = max(1e-3, sampling.effective_temperature) / beta
        logits = [utils[n][0] / temperature for n in names]
        m = max(logits)
        weights = [math.exp(v - m) for v in logits]
        total = sum(weights)
        choice = rng.choices(names, weights=weights, k=1)[0]
        action = utils[choice][1]
        u_max = max(utils[n][0] for n in names)
        extras = {"action_regret": round(u_max - utils[choice][0], 4), "p_chosen": round(weights[names.index(choice)] / total, 4)}
        if choice == "read_chronicle":
            self._last_chronicle_day[obs.agent_id] = obs.day
        if choice == "propose_gadget":
            self._gadget_tried.add(obs.agent_id)
        if choice == "create_offspring" and self._design:  # this name is spent whether or not the kernel accepts the birth
            self._children_named[obs.agent_id] = self._child_name(obs)[1] + 1
        ops = self._memory_ops(obs, seed, action, rng)
        thought = f"{choice} looks best right now ({obs.stress_label}, {obs.weather_label})."
        decision = Decision(thought=thought, memory_ops=ops, action=action)
        raw: dict[str, Any] = decision.model_dump(exclude_none=True)
        raw_text = json.dumps(raw, separators=(",", ":"))
        sys_tokens = estimate_tokens(self._system)
        obs_tokens = estimate_tokens(render_observation(obs))
        if obs.agent_id in self._first_call_done:
            usage = Usage(input_tokens=obs_tokens, output_tokens=estimate_tokens(raw_text), cache_read_tokens=sys_tokens)
        else:
            usage = Usage(input_tokens=obs_tokens, output_tokens=estimate_tokens(raw_text), cache_write_tokens=sys_tokens)
            self._first_call_done.add(obs.agent_id)
        return BrainResult(decision=decision, usage=usage, latency_ms=0, stop_reason="end_turn", raw_text=raw_text,
                           model=self.cfg.tiers[self.tier].model, extras=extras)
