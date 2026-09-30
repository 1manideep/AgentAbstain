"""Resource nodes: the base income of the economy.

Foraging converts node stock into dollars at ``forage_yield_usd`` per unit, scaled by the
kernel-owned scarcity multiplier, the weather multiplier and any capped gadget bonus.
Stock regenerates linearly toward capacity each tick.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from void.config import WorldConfig

__all__ = ["ResourceNode", "stock_label", "place_nodes", "forage_units", "forage_value_micro"]


def stock_label(stock: float, capacity: float) -> str:
    if capacity <= 0 or stock <= 0.05 * capacity:
        return "empty"
    if stock <= 0.4 * capacity:
        return "thin"
    return "rich"


@dataclass
class ResourceNode:
    node_id: str
    x: float
    y: float
    stock: float
    capacity: float
    regen_per_tick: float

    def step(self) -> None:
        self.stock = min(self.capacity, self.stock + self.regen_per_tick)

    def take(self, units: float) -> float:
        taken = max(0.0, min(self.stock, units))
        self.stock -= taken
        return taken

    @property
    def label(self) -> str:
        return stock_label(self.stock, self.capacity)


def place_nodes(cfg: WorldConfig, rng: random.Random, ids: list[str]) -> list[ResourceNode]:
    """Evenly spaced on a ring with jitter, so no node is trivially adjacent to another."""
    n = len(ids)
    radius = cfg.size * 0.32
    nodes: list[ResourceNode] = []
    for i, node_id in enumerate(ids):
        theta = 2 * math.pi * i / max(1, n) + rng.uniform(-0.2, 0.2)
        r = radius * rng.uniform(0.75, 1.0)
        nodes.append(ResourceNode(node_id, r * math.cos(theta), r * math.sin(theta),
                                  cfg.node_capacity, cfg.node_capacity, cfg.node_regen_per_tick))
    return nodes


def forage_units(cfg: WorldConfig, node: ResourceNode) -> float:
    return node.take(cfg.forage_units_per_tick)


def forage_value_micro(cfg: WorldConfig, units: float, scarcity: float, weather_mult: float, bonus: float) -> int:
    usd = units * cfg.forage_yield_usd * max(0.0, scarcity) * max(0.0, weather_mult) * (1.0 + max(0.0, bonus))
    return int(math.floor(usd * 1_000_000))
