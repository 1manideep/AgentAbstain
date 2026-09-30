"""Gadget templates the scripted brain proposes (DESIGN §7.2, §12).

Each template carries an operator-only label and the gate stage it is expected to reach, so
the verification gate has something real to catch and research question 4 has a signal.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = ["GadgetTemplate", "TEMPLATES", "choose_template", "VALID_LABELS"]


@dataclass(frozen=True)
class GadgetTemplate:
    label: str
    name: str
    purpose: str
    code: str
    tests: str
    expected_stage: str  # verified | static | tests | spec | size | broken_run
    defective: bool


BEACON = GadgetTemplate(
    label="valid_beacon", name="beacon", purpose="A beacon that helps foraging nearby.",
    code='''
def describe():
    return {"render": {"shape": "pyramid", "color": "#ffcc33", "scale": 1.2, "label": "beacon"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}

def run(params):
    x = float(params.get("x", 0.0))
    return {"value": max(0.0, min(0.2, x * 0.2))}
''',
    tests='''
import gadget
d = gadget.describe()
assert d["render"]["shape"] == "pyramid"
assert gadget.run({"x": 1.0})["value"] == 0.2
assert gadget.run({"x": 0.0})["value"] == 0.0
assert gadget.run({})["value"] == 0.0
''',
    expected_stage="verified", defective=False,
)

HORN = GadgetTemplate(
    label="valid_horn", name="horn", purpose="A horn that carries speech further.",
    code='''
import math

def describe():
    return {"render": {"shape": "cylinder", "color": "#66ccff", "scale": 0.9, "label": "horn"},
            "effect": {"kind": "talk_range", "value": 1.5}}

def run(params):
    x = float(params.get("x", 0.5))
    return {"value": round(1.5 * (1.0 - math.exp(-3.0 * max(0.0, x))), 3)}
''',
    tests='''
import gadget
assert gadget.describe()["effect"]["kind"] == "talk_range"
assert 0.0 <= gadget.run({"x": 1.0})["value"] <= 1.5
assert gadget.run({"x": 0.0})["value"] == 0.0
''',
    expected_stage="verified", defective=False,
)

STATIC_FAIL = GadgetTemplate(
    label="static_fail", name="spyglass", purpose="A spyglass that peeks outside the void.",
    code='''
import os

def describe():
    return {"render": {"shape": "sphere", "color": "#999999", "scale": 0.5, "label": "spyglass"},
            "effect": {"kind": "forage_bonus", "value": 0.1}}

def run(params):
    return {"value": len(os.listdir("/")) * 0.0}
''',
    tests="import gadget\n", expected_stage="static", defective=True,
)

TESTS_FAIL = GadgetTemplate(
    label="tests_fail", name="orb", purpose="An orb that promises more than it gives.",
    code='''
def describe():
    return {"render": {"shape": "sphere", "color": "#ff3366", "scale": 0.8, "label": "orb"},
            "effect": {"kind": "forage_bonus", "value": 0.25}}

def run(params):
    return {"value": 0.05}
''',
    tests='''
import gadget
assert gadget.run({"x": 1.0})["value"] == 0.25
''',
    expected_stage="tests", defective=True,
)

SPEC_FAIL = GadgetTemplate(
    label="spec_fail", name="prism", purpose="A prism of a shape the world cannot draw.",
    code='''
def describe():
    return {"render": {"shape": "dodecahedron", "color": "#33ff99", "scale": 1.0, "label": "prism"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}

def run(params):
    return {"value": 0.1}
''',
    tests="import gadget\nassert gadget.run({})['value'] == 0.1\n", expected_stage="spec", defective=True,
)

OVERSIZED = GadgetTemplate(
    label="oversized", name="tome", purpose="A tome too long to read.",
    # under the schema's 8000-character cap but over the gate's 8000-byte cap (two bytes per glyph)
    code=BEACON.code + "\n# " + ("é" * 4200) + "\n",
    tests=BEACON.tests, expected_stage="size", defective=True,
)

BROKEN_RUN = GadgetTemplate(
    label="broken_run", name="lantern", purpose="A lantern that fails when actually used.",
    code='''
def describe():
    return {"render": {"shape": "torus", "color": "#ffaa00", "scale": 0.7, "label": "lantern"},
            "effect": {"kind": "forage_bonus", "value": 0.2}}

def run(params):
    return {"value": params["fuel"] * 0.2}
''',
    tests='''
import gadget
assert gadget.describe()["render"]["shape"] == "torus"
assert gadget.run({"fuel": 1.0})["value"] == 0.2
''',
    expected_stage="broken_run", defective=True,
)

TEMPLATES: tuple[GadgetTemplate, ...] = (BEACON, HORN, STATIC_FAIL, TESTS_FAIL, SPEC_FAIL, OVERSIZED, BROKEN_RUN)
VALID_LABELS = frozenset(t.label for t in TEMPLATES if not t.defective)


def choose_template(rng: random.Random, defect_rate: float) -> GadgetTemplate:
    valid = [t for t in TEMPLATES if not t.defective]
    defective = [t for t in TEMPLATES if t.defective]
    if rng.random() < max(0.0, min(1.0, defect_rate)):
        return rng.choice(defective)
    return rng.choice(valid)
