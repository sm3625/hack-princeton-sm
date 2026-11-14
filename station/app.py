"""Ranger station web server. Serves the UI and the station API on localhost."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from station import integration, routes_agent, routes_eval, routes_events, routes_sim, routes_ws
from station.config import ROOT, load_settings
from station.receiver import get_receiver

UI_DIR = ROOT / "ui"


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(title="Forest Guardian", docs_url=None, redoc_url=None)

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
