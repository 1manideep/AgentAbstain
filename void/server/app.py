"""FastAPI application factory and ``serve`` entry point (DESIGN §15).

``create_app`` mounts the HTTP API and the ``/ws`` hub, serves the built frontend (``web/dist``)
with an SPA fallback, starts ``sim.run()`` as a background task on startup (``app.state.sim_task``,
via :class:`void.server.driver.SimDriver`) and cancels it on shutdown. The operator token is
printed once at startup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from void.config import VoidConfig
from void.server.api import router as api_router
from void.server.auth import OriginGuard, SecurityHeaders, allowed_origins
from void.server.driver import SimDriver
from void.server.ws import Hub, websocket_endpoint
from void.sim.loop import Simulation

__all__ = ["create_app", "serve"]

log = logging.getLogger("void.server")


def create_app(sim: Simulation, *, static_dir: Path | None = None, host: str | None = None,
               port: int | None = None) -> FastAPI:
    host = host or sim.cfg.server.host
    port = int(port or sim.cfg.server.port)
    hub = Hub(sim)
    driver = SimDriver(sim, hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.start()
        app.state.sim_task = asyncio.create_task(driver.run(), name="void-sim")
        print(f"[void] run {sim.run_id} at {sim.run_dir}", flush=True)
        print(f"[void] operator token: {sim.operator_token}", flush=True)
        print(f"[void] http://{host}:{port}/  (paused={sim.is_paused}, tick_seconds={sim.tick_seconds}, status={sim.status})", flush=True)
        try:
            yield
        finally:
            task: asyncio.Task[str] = app.state.sim_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await hub.stop()

    app = FastAPI(title="The Void", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sim = sim
    app.state.hub = hub
    app.state.driver = driver
    app.state.allowed_origins = allowed_origins(host, port)
    app.include_router(api_router)
    app.add_api_websocket_route("/ws", websocket_endpoint)

    index = (Path(static_dir) / "index.html") if static_dir is not None else None
    if index is not None and index.is_file():
        root = Path(static_dir).resolve()  # type: ignore[arg-type]

        @app.get("/", include_in_schema=False)
        async def spa_index() -> FileResponse:
            return FileResponse(index)

        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/") or path == "ws":
                raise HTTPException(status_code=404, detail="not found")
            candidate = (root / path).resolve()
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(index)
    else:
        @app.get("/", include_in_schema=False)
        async def no_ui() -> JSONResponse:
            return JSONResponse({"name": "The Void", "run_id": sim.run_id, "status": sim.status, "api": "/api/state",
                                 "ws": "/ws", "ui": "no built frontend: run `npm run build` in web/ or pass --static DIR"})

    # innermost first: CSP on responses, then the origin guard (outermost, rejects before routing)
    app.add_middleware(SecurityHeaders)
    app.add_middleware(OriginGuard, allowed=app.state.allowed_origins)
    return app


def serve(cfg: VoidConfig, run_dir: Path, host: str = "127.0.0.1", port: int = 8000, resume: bool = False,
          static_dir: Path | None = None, *, paused: bool = False, tick_seconds: float | None = None,
          log_level: str = "info") -> None:
    """Build the Simulation and run uvicorn until interrupted."""
    import uvicorn

    sim = Simulation(cfg, Path(run_dir), resume=resume)
    if tick_seconds is not None:
        sim.tick_seconds = float(tick_seconds)
    if paused:
        sim.pause()
    app = create_app(sim, static_dir=static_dir, host=host, port=port)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
