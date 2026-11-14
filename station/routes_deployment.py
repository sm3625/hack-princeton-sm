"""GET /api/deployment: the active deployment profile, read-only. Profiles hold no secrets."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/deployment")
def deployment(request: Request) -> dict:
    return request.app.state.deployment.public()
