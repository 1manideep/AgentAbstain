"""The WebSocket hub (DESIGN §15).

Per subscriber: one "latest snapshot" slot (a newer snapshot overwrites an unsent one) plus a bounded
message queue fed with ``put_nowait``; a sender task drains slot-then-queue. On overflow the socket
is closed with 1013 and the client reconnects (it catches up through ``/api/events?since_seq=``).

Inputs: the simulation's synchronous ``snapshot_listeners`` (snapshot, metrics, rev-driven gadgets /
tasks / chronicle refreshes) and a consumer task over ``sim.bus.queue()`` (events, roster after a
birth or death, ``day`` rows, chronicle refreshes). ``status`` is broadcast by the API on pause /
resume / speed and by :meth:`Hub.on_run_end`.

``void mock-feed`` drives the very same :class:`Hub` with a :class:`Subscriber` it drains by hand
and a :class:`SyntheticClock`, so the fixture is exactly the stream a live socket would see.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import time
from collections import OrderedDict
from typing import Any, Protocol

import anyio
from fastapi import WebSocket, WebSocketDisconnect

from void.events import Event

__all__ = ["Clock", "WallClock", "SyntheticClock", "Subscriber", "Hub", "websocket_endpoint",
           "OVERFLOW_CLOSE_CODE", "QUEUE_SIZE", "BACKLOG_SNAPSHOTS", "event_message"]

log = logging.getLogger("void.server.ws")

OVERFLOW_CLOSE_CODE = 1013  # "try again later": the client reconnects and catches up
QUEUE_SIZE = 512
BACKLOG_SNAPSHOTS = 60
ROSTER_KINDS = frozenset({"birth", "death", "replacement"})
SNAPSHOT_TS_RING = 2048


class Clock(Protocol):
    def now_ms(self) -> float: ...

    def snapshot_ms(self, tick: int) -> float: ...


class WallClock:
    def now_ms(self) -> float:
        return time.time() * 1000.0

    def snapshot_ms(self, tick: int) -> float:
        return self.now_ms()


class SyntheticClock:
    """Deterministic timestamps for ``void mock-feed``.

    Snapshots are spaced 250-1500 ms apart, drawn from ``seed``, with one 20 s gap at ``gap_tick``
    (a paused stretch the renderer's virtual timeline must absorb).
    """

    def __init__(self, seed: int, *, gap_tick: int | None = None, base_ms: float = 1_700_000_000_000.0,
                 gap_ms: float = 20_000.0) -> None:
        self._rng = random.Random(f"mock-feed:{seed}")
        self._t = float(base_ms)
        self._by_tick: dict[int, float] = {}
        self.gap_tick = gap_tick
        self.gap_ms = gap_ms

    def now_ms(self) -> float:
        return self._t

    def snapshot_ms(self, tick: int) -> float:
        if tick in self._by_tick:
            return self._by_tick[tick]
        self._t += self._rng.uniform(250.0, 1500.0)
        if self.gap_tick is not None and tick == self.gap_tick:
            self._t += self.gap_ms
        self._t = round(self._t, 1)
        self._by_tick[tick] = self._t
        return self._t


def event_message(ev: Event) -> dict[str, Any]:
    payload = ev.payload
    try:
        json.dumps(payload)
    except (TypeError, ValueError):
        payload = json.loads(json.dumps(payload, default=str))
    return {"type": "event", "seq": ev.seq, "tick": ev.tick, "day": ev.day, "kind": ev.kind,
            "agent_id": ev.agent_id, "visibility": ev.visibility, "payload": payload}


class Subscriber:
    """One consumer: a latest-snapshot slot plus a bounded queue. Producers only ever ``put_nowait``."""

    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self.slot: dict[str, Any] | None = None
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.wake = asyncio.Event()
        self.overflow = False
        self.last_snapshot_tick = -1

    def offer_snapshot(self, msg: dict[str, Any]) -> None:
        self.slot = msg
        self.wake.set()

    def offer(self, msg: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            self.overflow = True
        self.wake.set()

    def next_message(self) -> dict[str, Any] | None:
        """Slot first, then the queue; a snapshot not newer than the last sent one is dropped."""
        while True:
            if self.slot is not None:
                msg, self.slot = self.slot, None
                if int(msg["tick"]) <= self.last_snapshot_tick:
                    continue
                self.last_snapshot_tick = int(msg["tick"])
                return msg
            try:
                return self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return None

    def drain(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while (msg := self.next_message()) is not None:
            out.append(msg)
        return out


class Hub:
    def __init__(self, sim: Any, *, clock: Clock | None = None, bus_queue_size: int = 8192,
                 backlog: int = BACKLOG_SNAPSHOTS) -> None:
        self.sim = sim
        self.clock: Clock = clock or WallClock()
        self.backlog_size = backlog
        self.subscribers: list[Subscriber] = []
        self._snapshot_ts: OrderedDict[int, float] = OrderedDict()
        self._revs = self._current_revs()
        self._last_day_sent = self._max_day_row()
        self._bus_queue: asyncio.Queue[Event] | None = None
        self._bus_queue_size = bus_queue_size
        self._consumer: asyncio.Task[None] | None = None
        self._started = False

    # --- lifecycle -----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.sim.snapshot_listeners.append(self._on_snapshot)
        self._bus_queue = self.sim.bus.queue(self._bus_queue_size)
        self._consumer = asyncio.create_task(self._consume(), name="void-hub-events")

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._on_snapshot in self.sim.snapshot_listeners:
            self.sim.snapshot_listeners.remove(self._on_snapshot)
        if self._consumer is not None:
            self._consumer.cancel()
            await asyncio.gather(self._consumer, return_exceptions=True)
            self._consumer = None
        if self._bus_queue is not None:
            self.sim.bus.drop_queue(self._bus_queue)
            self._bus_queue = None

    async def flush(self) -> None:
        """Wait until every event emitted so far has been fanned out (used by ``mock-feed`` and tests)."""
        if self._bus_queue is not None:
            await self._bus_queue.join()

    # --- subscribers ---------------------------------------------------------------------------------------
    def subscribe(self, sub: Subscriber) -> None:
        self.subscribers.append(sub)

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)

    def broadcast(self, msg: dict[str, Any]) -> None:
        for sub in list(self.subscribers):
            sub.offer(msg)

    def broadcast_status(self) -> None:
        self.broadcast(self.status_message())

    # --- message builders ----------------------------------------------------------------------------------
    def hello(self) -> dict[str, Any]:
        sim = self.sim
        row = sim.db.fetchone("SELECT COALESCE(MAX(seq), 0) AS s FROM events")
        return {"type": "hello", "run_id": sim.run_id, "config": sim.cfg.public_subset(), "tick": sim.clock.tick,
                "day": sim.clock.day, "last_seq": int(row["s"]) if row else 0, "paused": sim.is_paused,
                "tick_seconds": float(sim.tick_seconds), "server_ts_ms": self.clock.now_ms(), "status": sim.status}

    def roster_message(self) -> dict[str, Any]:
        return {"type": "roster", "agents": self.sim.roster()}

    def gadgets_message(self) -> dict[str, Any]:
        msg = dict(self.sim.gadgets_message())
        msg["type"] = "gadgets"
        return msg

    def tasks_message(self) -> dict[str, Any]:
        return {"type": "tasks", "rev": int(self.sim.taskboard.rev), "items": self.sim.taskboard.snapshot()}

    def chronicle_message(self) -> dict[str, Any]:
        rev = int(self.sim.db.kv_get("chronicle_rev", 0) or 0)
        doc = self.sim.chronicle.latest() if self.sim.chronicle is not None else None
        if doc is None:
            return {"type": "chronicle", "rev": rev, "day": None, "headline": "", "markdown": ""}
        return {"type": "chronicle", "rev": rev, "day": int(doc.day), "headline": doc.headline, "markdown": doc.markdown}

    def status_message(self) -> dict[str, Any]:
        return {"type": "status", "paused": self.sim.is_paused, "tick_seconds": float(self.sim.tick_seconds),
                "status": self.sim.status}

    def snapshot_ts(self, tick: int, newest_tick: int | None = None) -> float:
        """The ts_ms a snapshot was (or would have been) sent with: remembered when live, else spaced back from now."""
        ts = self._snapshot_ts.get(tick)
        if ts is not None:
            return ts
        newest = newest_tick if newest_tick is not None else int(self.sim.clock.tick)
        spacing = max(float(self.sim.tick_seconds), 0.25) * 1000.0
        return self.clock.now_ms() - max(0, newest - tick) * spacing

    def backlog(self, limit: int | None = None) -> list[dict[str, Any]]:
        """The last ``limit`` persisted snapshots, oldest first, each with ``ts_ms``."""
        n = self.backlog_size if limit is None else limit
        rows = self.sim.db.fetchall("SELECT tick, json FROM snapshots ORDER BY tick DESC LIMIT ?", (int(n),))
        out: list[dict[str, Any]] = []
        newest = int(rows[0]["tick"]) if rows else 0
        for r in reversed(rows):
            snap = json.loads(r["json"])
            snap["type"] = "snapshot"
            snap["ts_ms"] = self.snapshot_ts(int(r["tick"]), newest)
            out.append(snap)
        return out

    def latest_snapshot(self) -> dict[str, Any] | None:
        snap = self.sim.last_snapshot
        if snap is None:
            row = self.sim.db.fetchone("SELECT tick, json FROM snapshots ORDER BY tick DESC LIMIT 1")
            if row is None:
                return None
            snap = json.loads(row["json"])
        msg = dict(snap)
        msg["type"] = "snapshot"
        msg["ts_ms"] = self.snapshot_ts(int(msg["tick"]))
        return msg

    def preamble(self) -> list[dict[str, Any]]:
        """What a fresh connection receives before live traffic (DESIGN §15 "on connect")."""
        return [self.hello(), self.roster_message(), self.gadgets_message(), self.tasks_message(),
                self.chronicle_message(), *self.backlog()]

    # --- inputs --------------------------------------------------------------------------------------------
    def _current_revs(self) -> dict[str, int]:
        db = self.sim.db
        return {"gadgets": int(db.kv_get("gadgets_rev", 0) or 0), "tasks": int(self.sim.taskboard.rev),
                "chronicle": int(db.kv_get("chronicle_rev", 0) or 0)}

    def _max_day_row(self) -> int:
        row = self.sim.db.fetchone("SELECT COALESCE(MIN(tick), 0) AS t FROM metrics WHERE tick < 0")
        return -int(row["t"]) if row else 0

    def _remember_ts(self, tick: int, ts: float) -> None:
        self._snapshot_ts[tick] = ts
        while len(self._snapshot_ts) > SNAPSHOT_TS_RING:
            self._snapshot_ts.popitem(last=False)

    def _on_snapshot(self, snap: dict[str, Any]) -> None:
        """Synchronous listener called by the simulation at the end of every tick."""
        tick = int(snap["tick"])
        msg = dict(snap)
        msg["type"] = "snapshot"
        ts = self.clock.snapshot_ms(tick)
        msg["ts_ms"] = ts
        self._remember_ts(tick, ts)
        for sub in list(self.subscribers):
            sub.offer_snapshot(msg)
        row = self.sim.last_metrics_row
        if row is not None and int(row.get("tick", tick)) == tick:
            self.broadcast({"type": "metrics", "tick": tick, "day": int(snap["day"]), "row": row})
        self._check_revs(snap)

    def _check_revs(self, snap: dict[str, Any]) -> None:
        builders = {"gadgets": self.gadgets_message, "tasks": self.tasks_message, "chronicle": self.chronicle_message}
        for key, build in builders.items():
            rev = int(snap.get(f"{key}_rev", 0) or 0)
            if rev != self._revs[key]:
                self._revs[key] = rev
                self.broadcast(build())

    def _send_day(self, day: int) -> None:
        if day <= self._last_day_sent:
            return
        row = self.sim.db.fetchone("SELECT json FROM metrics WHERE tick=?", (-int(day),))
        if row is None:
            return
        self._last_day_sent = day
        self.broadcast({"type": "day", "day": int(day), "row": json.loads(row["json"])})

    def _on_event(self, ev: Event) -> None:
        self.broadcast(event_message(ev))
        if ev.kind in ROSTER_KINDS:
            self.broadcast(self.roster_message())
        elif ev.kind == "day_end":
            self._send_day(int(ev.payload.get("day", ev.day - 1)))
        elif ev.kind == "chronicle":
            rev = int(ev.payload.get("rev", 0) or 0)
            if rev != self._revs["chronicle"]:
                self._revs["chronicle"] = rev
                self.broadcast(self.chronicle_message())

    async def _consume(self) -> None:
        assert self._bus_queue is not None
        q = self._bus_queue
        while True:
            ev = await q.get()
            try:
                self._on_event(ev)
            except Exception:  # a broken message must never stop the fan-out
                log.exception("hub: failed to fan out event %s", getattr(ev, "kind", "?"))
            finally:
                q.task_done()

    async def on_run_end(self) -> None:
        """After ``sim.run()`` returns: fan out what ``finish()`` emitted, the final day row(s), then ``status`` last."""
        await self.flush()
        newest = self._max_day_row()
        for day in range(self._last_day_sent + 1, newest + 1):
            self._send_day(day)
        self.broadcast_status()


# --- the /ws route --------------------------------------------------------------------------------------------
async def websocket_endpoint(ws: WebSocket) -> None:
    hub: Hub = ws.app.state.hub
    sim = ws.app.state.sim
    token = ws.query_params.get("token")
    if token is not None and not hmac.compare_digest(token.encode(), sim.operator_token.encode()):
        await ws.close(code=1008)  # before accept: the handshake fails with 403
        return
    await ws.accept()
    sub = Subscriber()
    hub.subscribe(sub)
    try:
        for msg in hub.preamble():
            await ws.send_json(msg)
            if msg.get("type") == "snapshot":
                sub.last_snapshot_tick = max(sub.last_snapshot_tick, int(msg["tick"]))
        await _pump(ws, sub)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # client vanished mid-send, transport errors: the client reconnects
        log.debug("ws: connection ended: %s: %s", type(e).__name__, e)
    finally:
        hub.unsubscribe(sub)


async def _pump(ws: WebSocket, sub: Subscriber) -> None:
    """Sender and receiver under one anyio task group: whichever side finishes first ends the connection.

    anyio (Starlette's own concurrency layer) is used instead of raw ``asyncio.wait`` so the
    connection composes with the cancel scopes Starlette and its test client wrap around a route.
    """

    async with anyio.create_task_group() as tg:
        async def sender() -> None:
            try:
                while True:
                    await sub.wake.wait()
                    sub.wake.clear()
                    if sub.overflow:
                        await ws.close(code=OVERFLOW_CLOSE_CODE)
                        return
                    while (msg := sub.next_message()) is not None:
                        await ws.send_json(msg)
                        if sub.overflow:
                            await ws.close(code=OVERFLOW_CLOSE_CODE)
                            return
            except (WebSocketDisconnect, RuntimeError, OSError) as e:  # the client went away mid-send
                log.debug("ws: send ended: %s: %s", type(e).__name__, e)
            finally:
                tg.cancel_scope.cancel()

        async def receiver() -> None:
            # The socket never accepts commands: incoming frames are ignored; a close ends the connection.
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
            except (WebSocketDisconnect, RuntimeError, OSError):
                pass
            finally:
                tg.cancel_scope.cancel()

        tg.start_soon(sender)
        tg.start_soon(receiver)
