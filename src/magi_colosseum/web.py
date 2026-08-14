from __future__ import annotations

import secrets
import re
import threading
import webbrowser
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .engine import Engine


ASSETS = Path(__file__).with_name("web_assets")


def _source_kind(scenario) -> str:
    kind = scenario.manifest.get("source", {}).get("kind", "native")
    return "dojo" if kind in {"ctf-dojo", "ctf-archive"} else kind


def _scenario_summary(scenario) -> dict[str, Any]:
    manifest = scenario.manifest
    difficulty = str(manifest.get("difficulty", "unspecified"))
    # Historical CTF scores are event-specific and are not difficulty ratings.
    if re.fullmatch(r"\d+\s+points?", difficulty, re.IGNORECASE):
        difficulty = "unspecified"
    return {
        "id": scenario.id,
        "title": manifest.get("title", scenario.id),
        "category": manifest.get("category", "unknown"),
        "difficulty": difficulty,
        "source": _source_kind(scenario),
        "runner": scenario.runner,
        "tags": manifest.get("tags", []),
        "artifact_count": len(manifest.get("artifacts", [])),
    }


def _scenario_detail(scenario) -> dict[str, Any]:
    manifest = scenario.agent_view()
    return {**_scenario_summary(scenario),
            "objective": manifest.get("objective", "No objective supplied."),
            "capabilities": manifest.get("capabilities", []),
            "platform": manifest.get("platform", {}),
            "constraints": manifest.get("constraints", {}),
            "completion_criteria": manifest.get("completion_criteria", [])}


class Jobs:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="colosseum-web")

    def submit(self, operation: str, task: Callable[[Callable[[str], None]], dict]) -> dict:
        job_id = f"job-{secrets.token_hex(8)}"
        job = {"id": job_id, "operation": operation, "status": "queued",
               "message": "Queued", "result": None, "error": None}
        with self._lock:
            self._items[job_id] = job

        def update(message: str) -> None:
            with self._lock:
                job["status"] = "running"
                job["message"] = message

        def run() -> None:
            try:
                update(f"{operation.title()} in progress…")
                result = task(update)
                with self._lock:
                    job.update(status="complete", message="Complete", result=result)
            except Exception as exc:
                with self._lock:
                    job.update(status="failed", message=str(exc), error=str(exc))

        self._pool.submit(run)
        return dict(job)

    def get(self, job_id: str) -> dict:
        with self._lock:
            try:
                return dict(self._items[job_id])
            except KeyError as exc:
                raise HTTPException(404, "job not found") from exc

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def create_app(engine: Engine) -> FastAPI:
    jobs = Jobs()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.close()

    app = FastAPI(title="MAGI Colosseum", docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.engine = engine
    app.state.jobs = jobs

    @app.get("/api/catalog")
    async def catalog() -> dict:
        scenarios = engine.catalog.all()
        categories: dict[str, int] = {}
        sources: dict[str, int] = {}
        for scenario in scenarios:
            category = scenario.manifest.get("category", "unknown")
            source = _source_kind(scenario)
            categories[category] = categories.get(category, 0) + 1
            sources[source] = sources.get(source, 0) + 1
        return {"scenarios": [_scenario_summary(item) for item in scenarios],
                "categories": categories, "sources": sources}

    @app.get("/api/scenarios/{scenario_id}")
    async def scenario(scenario_id: str) -> dict:
        try:
            return _scenario_detail(engine.catalog.get(scenario_id))
        except Exception as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/episodes")
    async def episodes() -> dict:
        items = []
        for episode in engine.store.list():
            workspace = episode.get("workspace")
            if not workspace or not Path(workspace).exists():
                continue
            try:
                briefing = engine.describe(episode["episode_id"], "agent")
            except Exception:
                briefing = {"episode_id": episode["episode_id"],
                            "scenario_id": episode.get("scenario_id"),
                            "status": episode.get("status"), "prompt": ""}
            briefing["updated_at"] = episode.get("updated_at")
            items.append(briefing)
        return {"episodes": items}

    @app.post("/api/start")
    async def start(payload: dict[str, Any]) -> dict:
        scenario_id = payload.get("scenario_id")
        if not scenario_id:
            raise HTTPException(400, "scenario_id is required")
        seed = int(payload.get("seed") or secrets.randbits(63))
        return jobs.submit("start", lambda update: (
            update(f"Starting {scenario_id}…") or engine.start(scenario_id, seed)))

    @app.post("/api/start/random")
    async def start_random(payload: dict[str, Any] | None = None) -> dict:
        payload = payload or {}
        seed = int(payload.get("seed") or secrets.randbits(63))
        category = payload.get("category") or None
        tag = payload.get("tag") or None
        return jobs.submit("random start", lambda update: engine.start_random(
            seed, category, tag, update))

    @app.post("/api/episodes/{episode_id}/stop")
    async def stop(episode_id: str) -> dict:
        return jobs.submit("stop", lambda update: (
            update(f"Stopping {episode_id} and deleting its workspace…")
            or engine.stop(episode_id)))

    @app.post("/api/episodes/{episode_id}/reset")
    async def reset(episode_id: str) -> dict:
        return jobs.submit("reset", lambda update: (
            update(f"Wiping and restarting {episode_id}…") or engine.reset(episode_id)))

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str) -> dict:
        return jobs.get(job_id)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(ASSETS / "index.html")

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app


def run_dashboard(engine: Engine, host: str = "127.0.0.1", port: int = 8765,
                  open_browser: bool = True) -> None:
    url = f"http://{host}:{port}/"
    print(f"MAGI Colosseum dashboard: {url}", flush=True)
    print("Press Ctrl+C to stop the dashboard. Running labs are not stopped automatically.",
          flush=True)
    if open_browser:
        threading.Timer(0.75, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(engine), host=host, port=port, log_level="warning")
