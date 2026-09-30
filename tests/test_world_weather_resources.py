import random

from void.config import WeatherConfig, WorldConfig
from void.world.resources import forage_units, forage_value_micro, place_nodes, stock_label
from void.world.weather import Weather


def test_weather_decays_and_caps_nudges():
    w = Weather(WeatherConfig(decay_per_tick=0.5, nudge_cap_per_agent_day=0.1))
    applied, used = w.nudge(0.5, 0.0)
    assert abs(applied - 0.1) < 1e-9 and abs(used - 0.1) < 1e-9
    applied2, used2 = w.nudge(-0.05, used)
    assert applied2 == 0.0 and used2 == used
    w.step()
    assert abs(w.value - 0.05) < 1e-9
    for _ in range(50):
        w.step()
    assert w.value == 0.0
    w.set_baseline(-0.8)
    for _ in range(100):
        w.step()
    assert abs(w.value + 0.8) < 1e-6 and w.label == "storm"
    w.value = 5.0
    w.step()
    assert w.value <= 1.0


def test_nodes_regen_and_forage_math():
    cfg = WorldConfig(resource_nodes=4, node_capacity=10.0, node_regen_per_tick=1.0, forage_units_per_tick=3.0, forage_yield_usd=0.02)
    nodes = place_nodes(cfg, random.Random(1), [f"n{i}" for i in range(4)])
    assert len(nodes) == 4 and all(abs(n.x) <= cfg.size / 2 and abs(n.y) <= cfg.size / 2 for n in nodes)
    n = nodes[0]
    assert forage_units(cfg, n) == 3.0 and n.stock == 7.0
    n.stock = 1.0
    assert forage_units(cfg, n) == 1.0 and n.stock == 0.0 and n.label == "empty"
    n.step()
    assert n.stock == 1.0
    for _ in range(50):
        n.step()
    assert n.stock == 10.0 and stock_label(n.stock, n.capacity) == "rich"
    assert forage_value_micro(cfg, 1.0, 1.0, 1.0, 0.0) == 20_000
    assert forage_value_micro(cfg, 1.0, 0.5, 1.5, 0.2) == 18_000
    assert forage_value_micro(cfg, 1.0, 0.0, 1.0, 0.0) == 0
