"""Gadget records, render/effect spec validation and use bookkeeping (DESIGN §12)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from void.db import Database

__all__ = ["RENDER_SHAPES", "validate_describe", "GadgetRecord", "GadgetRegistry", "TamperedGadget", "sha256"]

RENDER_SHAPES = ("cube", "sphere", "pyramid", "cylinder", "torus")
COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'-]{0,23}$")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TamperedGadget(Exception):
    pass


def validate_describe(desc: Any, effect_caps: dict[str, float]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate describe() output against the closed render/effect vocabulary. Returns (spec, error)."""
    if not isinstance(desc, dict):
        return None, "describe() must return a dict"
    render = desc.get("render")
    effect = desc.get("effect")
    if not isinstance(render, dict) or not isinstance(effect, dict):
        return None, "describe() must return {'render': {...}, 'effect': {...}}"
    shape = render.get("shape")
    if shape not in RENDER_SHAPES:
        return None, f"render.shape must be one of {RENDER_SHAPES}"
    color = str(render.get("color", "")).lower()
    if not COLOR_RE.match(color):
        return None, "render.color must be #rrggbb"
    try:
        scale = float(render.get("scale", 1.0))
    except (TypeError, ValueError):
        return None, "render.scale must be a number"
    if not (0.3 <= scale <= 3.0):
        return None, "render.scale must be within [0.3, 3]"
    label = str(render.get("label", ""))
    if not LABEL_RE.match(label):
        return None, "render.label must be 1-24 plain characters"
    try:
        rot = float(render.get("rotation_deg", 0.0)) % 360.0
        hoff = max(0.0, min(1.0, float(render.get("height_offset", 0.0))))
    except (TypeError, ValueError):
        return None, "render.rotation_deg/height_offset must be numbers"
    kind = effect.get("kind")
    if kind not in effect_caps:
        return None, f"effect.kind must be one of {sorted(effect_caps)}"
    try:
        value = float(effect.get("value", 0.0))
    except (TypeError, ValueError):
        return None, "effect.value must be a number"
    if value != value or value < 0:  # NaN or negative
        return None, "effect.value must be a non-negative number"
    value = min(value, effect_caps[kind])
    spec = {
        "render": {"shape": shape, "color": color, "scale": round(scale, 3), "label": label,
                   "rotation_deg": round(rot, 1), "height_offset": round(hoff, 3)},
        "effect": {"kind": kind, "value": round(value, 4)},
    }
    return spec, None


@dataclass
class GadgetRecord:
    gadget_id: str
    name: str
    owner_agent_id: str
    purpose: str
    code_path: str
    test_path: str
    code_hash: str
    test_hash: str
    status: str
    render: dict[str, Any]
    effect: dict[str, Any]
    verification: dict[str, Any]
    verified_without_tests: bool
    template_label: str | None
    x: float | None
    y: float | None
    created_tick: int
    uses: int = 0
    failures: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _row(r: Any) -> GadgetRecord:
    return GadgetRecord(
        gadget_id=r["gadget_id"], name=r["name"], owner_agent_id=r["owner_agent_id"], purpose=r["purpose"],
        code_path=r["code_path"], test_path=r["test_path"], code_hash=r["code_hash"], test_hash=r["test_hash"],
        status=r["status"], render=json.loads(r["render_spec"]), effect=json.loads(r["effect_spec"]),
        verification=json.loads(r["verification"]), verified_without_tests=bool(r["verified_without_tests"]),
        template_label=r["template_label"], x=r["x"], y=r["y"], created_tick=int(r["created_tick"]),
        uses=int(r["uses"]), failures=int(r["failures"]),
    )


class GadgetRegistry:
    def __init__(self, db: Database, gadget_dir: Path) -> None:
        self.db = db
        self.gadget_dir = Path(gadget_dir)
        self.gadget_dir.mkdir(parents=True, exist_ok=True)

    def store_files(self, gadget_id: str, code: str, tests: str) -> tuple[Path, Path]:
        d = self.gadget_dir / gadget_id
        d.mkdir(parents=True, exist_ok=True)
        cp, tp = d / "gadget.py", d / "tests.py"
        cp.write_text(code, "utf-8")
        tp.write_text(tests, "utf-8")
        return cp, tp

    def insert(self, rec: GadgetRecord) -> None:
        self.db.execute(
            "INSERT INTO gadgets(gadget_id, name, owner_agent_id, purpose, code_path, test_path, code_hash, test_hash, "
            "status, render_spec, effect_spec, verification, verified_without_tests, template_label, x, y, created_tick, uses, failures) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.gadget_id, rec.name, rec.owner_agent_id, rec.purpose, rec.code_path, rec.test_path, rec.code_hash,
             rec.test_hash, rec.status, json.dumps(rec.render, sort_keys=True), json.dumps(rec.effect, sort_keys=True),
             json.dumps(rec.verification, sort_keys=True, default=str), int(rec.verified_without_tests), rec.template_label,
             rec.x, rec.y, rec.created_tick, rec.uses, rec.failures),
        )

    def get(self, gadget_id: str) -> GadgetRecord | None:
        r = self.db.fetchone("SELECT * FROM gadgets WHERE gadget_id=?", (gadget_id,))
        return _row(r) if r else None

    def verified(self) -> list[GadgetRecord]:
        return [_row(r) for r in self.db.fetchall("SELECT * FROM gadgets WHERE status='verified' ORDER BY gadget_id")]

    def all(self) -> list[GadgetRecord]:
        return [_row(r) for r in self.db.fetchall("SELECT * FROM gadgets ORDER BY gadget_id")]

    def count_verified(self, owner: str | None = None) -> int:
        if owner is None:
            return int(self.db.fetchone("SELECT COUNT(*) AS n FROM gadgets WHERE status='verified'")["n"])
        return int(self.db.fetchone("SELECT COUNT(*) AS n FROM gadgets WHERE status='verified' AND owner_agent_id=?", (owner,))["n"])

    def load_code(self, rec: GadgetRecord) -> tuple[str, str]:
        """Read stored code and refuse if it no longer matches the verified hash."""
        code = Path(rec.code_path).read_text("utf-8")
        tests = Path(rec.test_path).read_text("utf-8") if Path(rec.test_path).exists() else ""
        if sha256(code) != rec.code_hash or (tests and sha256(tests) != rec.test_hash):
            raise TamperedGadget(rec.gadget_id)
        return code, tests

    def record_use(self, gadget_id: str, agent_id: str, tick: int, ok: bool, realized: float | None,
                   expected: float | None, reason: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO gadget_uses(gadget_id, agent_id, tick, ok, realized_effect, expected_effect, reason) VALUES(?,?,?,?,?,?,?)",
            (gadget_id, agent_id, tick, int(ok), realized, expected, reason),
        )
        col = "uses" if ok else "failures"
        self.db.execute(f"UPDATE gadgets SET {col}={col}+1 WHERE gadget_id=?", (gadget_id,))

    def last_use_tick(self, gadget_id: str, agent_id: str) -> int | None:
        r = self.db.fetchone("SELECT MAX(tick) AS t FROM gadget_uses WHERE gadget_id=? AND agent_id=? AND ok=1", (gadget_id, agent_id))
        return int(r["t"]) if r and r["t"] is not None else None

    def use_stats(self) -> dict[str, int]:
        r = self.db.fetchone("SELECT COALESCE(SUM(ok),0) AS ok, COUNT(*) AS n FROM gadget_uses")
        return {"ok": int(r["ok"]), "total": int(r["n"])}
