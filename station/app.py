"""Ranger station web server. Serves the UI and the station API on localhost."""

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from station import routes_ws
from station.config import ROOT, load_settings

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

    app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    return app


app = create_app()
