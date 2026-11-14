"""Ranger station web server. Serves the UI and the station API on localhost."""

import json
import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from common.deployment import load_deployment
from station import (
    integration,
    routes_agent,
    routes_deployment,
    routes_eval,
    routes_events,
    routes_sim,
    routes_ws,
)
from station.config import ROOT, load_settings
from station.receiver import get_receiver

UI_DIR = ROOT / "ui"

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "testserver")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def allowed_hosts() -> set[str]:
    """Host names this station answers to. STATION_ALLOWED_HOSTS adds more (comma separated),
    e.g. a LAN address when the dashboard is opened from another device."""
    extra = (h.strip().lower() for h in os.getenv("STATION_ALLOWED_HOSTS", "").split(","))
    return {*LOCAL_HOSTS, *(h for h in extra if h)}


class LocalGuard:
    """The API has no login: it trusts whoever can reach localhost. A web page open in the
    ranger's browser can reach localhost too, so two checks keep other sites out:

    - Host must be a name this station answers to (stops DNS rebinding, where an outside
      domain is pointed at 127.0.0.1 to read the API as "same origin").
    - A write that carries an Origin header must come from this station's own page (stops
      another site posting ranger messages, pausing the sim or guessing at approvals).
      Scripts and the Photon bridge send no Origin and are unaffected.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "").lower()
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        problem = None
        if name not in allowed_hosts():
            problem = "Unknown host."
        elif scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
                    if parsed.scheme != scheme or parsed.netloc.lower() != host:
                        problem = "Cross-site request refused."
                except ValueError:
                    problem = "Malformed origin."
        if problem is None:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        body = json.dumps({"detail": problem}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Forest Guardian", docs_url=None, redoc_url=None)
    app.state.deployment = load_deployment()  # fail fast on a bad DEPLOYMENT
    app.add_middleware(LocalGuard)

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "model": settings.model_label, "mode": settings.llm_mode}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    # Track routers: each track adds ONE include line here for its own router.
    app.include_router(routes_ws.router)
    app.include_router(routes_sim.router)
    app.include_router(routes_events.router)
    app.include_router(routes_eval.router)
    app.include_router(routes_deployment.router)
    app.include_router(routes_agent.router)  # incident commander API + its startup (lifespan)

    # Integration: the commander reads verified records from the pipeline's store and
    # re-verifies signatures with the pipeline's keys.
    receiver = get_receiver()
    app.state.receiver = receiver
    app.state.event_store = receiver.store
    app.state.verify_record = lambda _node_id, _seq, raw: receiver.reverify(raw)

    integration.setup(app)  # real sim + node pipeline (its lifespan binds loop and sim feed)

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    return app


app = create_app()
