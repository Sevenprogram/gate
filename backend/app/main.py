"""FastAPI entry point.

The API secret never leaves this process: the browser talks only to this server,
which signs the outbound Gate requests. That is the whole reason a backend
exists here rather than calling Gate from the page.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, settings
from .db import Database
from .gate_client import ClientRegistry
from .routers.api import router as api_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("gate-dashboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = Database(settings.db_path, settings.tz)
    app.state.gate = ClientRegistry(settings.profiles, settings.api_host)
    log.info(
        "profiles=%s tz=%s quote=%s db=%s demo=%s",
        [f"{p.id}:{list(p.accounts)}" for p in settings.profiles],
        settings.tz_name,
        settings.quote,
        settings.db_path,
        settings.demo,
    )
    for profile in settings.profiles:
        if not profile.has_credentials:
            log.warning(
                "profile '%s' has no credentials — add a read-only key to "
                "profiles.toml (or .env for a single account)",
                profile.id,
            )
    if settings.demo:
        log.warning(
            "DASHBOARD_DEMO is on: account snapshots are FABRICATED, not real data"
        )
    try:
        yield
    finally:
        await app.state.gate.aclose()
        app.state.db.close()


app = FastAPI(title="Gate Account Dashboard", version="0.1.0", lifespan=lifespan)

# Vite dev server origins. The API is bound to localhost and holds no secrets in
# its responses, but keeping the list explicit avoids surprises.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Serve the built frontend when it exists, so production is a single process.
_dist = PROJECT_ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
else:
    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "message": "Backend is running. Start the frontend with "
            "`cd frontend && pnpm dev`, or build it with `pnpm build` to have it "
            "served from here.",
            "api": "/docs",
        }
