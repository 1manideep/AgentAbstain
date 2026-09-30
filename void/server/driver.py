"""Drives ``sim.run()`` for the server and serialises it with ``/api/control/step`` (DESIGN §14.1)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from void.server.ws import Hub
from void.sim.loop import Simulation, TickReport

__all__ = ["SimDriver", "RunEnded"]

log = logging.getLogger("void.server.driver")


class RunEnded(Exception):
    """Raised by :meth:`SimDriver.step` when the run is no longer running."""


class SimDriver:
    """Owns the background ``sim.run()`` task and keeps ``/api/control/step`` from interleaving with it.

    The Simulation has no tick lock and no "setup finished" signal, so the driver wraps the
    instance's ``tick``/``setup`` coroutines: every tick (loop or step) runs under one
    ``asyncio.Lock`` and ``ready`` is set once the sandbox self-test in ``setup()`` has completed.
    A step that ends the run finishes it here (``finish()`` + ``hub.on_run_end()``) and leaves the
    loop parked on ``paused``: ``run()`` re-checks ``status`` only before that wait, so releasing
    it would execute one tick past the end.
    """

    def __init__(self, sim: Simulation, hub: Hub) -> None:
        self.sim = sim
        self.hub = hub
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.finished = asyncio.Event()
        self.error: BaseException | None = None
        self._tick = sim.tick
        self._setup = sim.setup
        sim.tick = self._locked_tick  # type: ignore[method-assign]
        sim.setup = self._wrapped_setup  # type: ignore[method-assign]

    async def _locked_tick(self) -> TickReport:
        async with self.lock:
            return await self._tick()

    async def _wrapped_setup(self) -> dict[str, Any]:
        try:
            return await self._setup()
        finally:
            self.ready.set()

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self.ready.wait(), timeout)
            return True
        except TimeoutError:
            return False

    async def step(self) -> TickReport:
        """Exactly one tick while paused; raises :class:`RunEnded` if the run is already over."""
        async with self.lock:
            if self.sim.status != "running":
                raise RunEnded(self.sim.status)
            report = await self._tick()
            ended = self.sim.status != "running"
        if ended:
            await self._end_run()
        return report

    async def _end_run(self) -> None:
        if self.finished.is_set():
            return
        self.finished.set()
        try:
            await self.sim.finish()
        except Exception:
            log.exception("sim.finish failed")
        try:
            await self.hub.on_run_end()
        except Exception:
            log.exception("hub.on_run_end failed")

    async def run(self) -> str:
        try:
            status = await self.sim.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # keep the server up so the operator can inspect the run
            self.error = e
            log.exception("simulation task failed")
            status = self.sim.status
        finally:
            self.ready.set()
        if not self.finished.is_set():  # run() already called finish(); only the fan-out remains
            self.finished.set()
            try:
                await self.hub.on_run_end()
            except Exception:
                log.exception("hub.on_run_end failed")
        return status
