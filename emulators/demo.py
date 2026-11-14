"""Launch the existing sound-detection dashboard with the edge classifier connected."""

import argparse
import os

from emulators.run import ROOT, build_image, prepare


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", choices=["local", "arm64"], default="local")
    p.add_argument("--profile", choices=["pi-zero2", "pi4"], default="pi-zero2")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--mock-agent", action="store_true", help="Use mock commander; classifier stays real"
    )
    args = p.parse_args()
    if args.backend == "arm64":
        build_image()
    audio = ROOT / "emulators/audio"
    if not all(
        (audio / f"{s}.wav").exists() for s in ("background", "chainsaw", "gunshot", "vehicle")
    ):
        prepare(audio)
    os.environ.update(EDGE_BACKEND=args.backend, EDGE_PROFILE=args.profile, NODE_PIPELINE="on")
    if args.mock_agent:
        os.environ["LLM_MODE"] = "mock"
    import uvicorn

    print(f"Connected edge demo: http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run("station.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
