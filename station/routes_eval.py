"""GET /api/eval/latest: the latest evaluation results (eval/results.json), 404 until one exists."""

import json
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

from common.contracts import ROOT

router = APIRouter()


def results_path() -> Path:
    return Path(os.getenv("EVAL_RESULTS") or ROOT / "eval" / "results.json")


@router.get("/api/eval/latest")
def latest() -> dict:
    try:
        data = json.loads(results_path().read_text())
    except (OSError, ValueError):
        raise HTTPException(404, "No evaluation yet.") from None
    if not isinstance(data, dict) or not data.get("runs"):
        raise HTTPException(404, "No evaluation yet.")
    return data
