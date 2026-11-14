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
    def read(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    data = read(results_path()) or {}
    data["cloud_comparison"] = read(ROOT / "eval/cloud_comparison.json")
    data["edge_benchmark"] = read(ROOT / "emulators/evidence/local/report.json")
    if not any((data.get("runs"), data["cloud_comparison"], data["edge_benchmark"])):
        raise HTTPException(404, "No evaluation yet.")
    return data
