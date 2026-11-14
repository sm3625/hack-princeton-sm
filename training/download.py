"""Download ESC-50 and UrbanSound8K into data/ (git-ignored).

Resumable: partial downloads live in *.part and continue with an HTTP Range request.
Skips anything already downloaded and extracted.

    uv run python training/download.py            # both datasets
    uv run python training/download.py --only esc50
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CHUNK = 1 << 20


@dataclass(frozen=True)
class Dataset:
    name: str
    url: str
    archive: str
    extracted: str  # directory name inside DATA_DIR once extracted
    marker: str  # file inside `extracted` that proves extraction finished
    md5: str | None = None


DATASETS = {
    "esc50": Dataset(
        name="ESC-50",
        url="https://github.com/karolpiczak/ESC-50/archive/refs/heads/master.zip",
        archive="ESC-50-master.zip",
        extracted="ESC-50-master",
        marker="meta/esc50.csv",
    ),
    "us8k": Dataset(
        name="UrbanSound8K",
        url="https://zenodo.org/records/1203745/files/UrbanSound8K.tar.gz?download=1",
        archive="UrbanSound8K.tar.gz",
        extracted="UrbanSound8K",
        marker="metadata/UrbanSound8K.csv",
        md5="9aa69802bbf37fb986f71ec1483a196e",
    ),
}


def _fmt(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fetch(url: str, dest: Path, retries: int = 5) -> None:
    """Download url to dest, resuming from dest.part if present."""
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"User-Agent": "dataset-fetch/1.0"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if have and resp.status != 206:
                    # server ignored the range: start over
                    have = 0
                mode = "ab" if have else "wb"
                length = resp.headers.get("Content-Length")
                total = have + int(length) if length else None
                done = have
                last = 0.0
                with open(part, mode) as f:
                    while chunk := resp.read(CHUNK):
                        f.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if now - last > 5:
                            pct = f" ({100 * done / total:.1f}%)" if total else ""
                            print(f"  {dest.name}: {_fmt(done)}{pct}", flush=True)
                            last = now
            part.rename(dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and have:  # range past end: file already complete
                part.rename(dest)
                return
            print(f"  HTTP {e.code} (attempt {attempt}/{retries})", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print(f"  {e} (attempt {attempt}/{retries})", file=sys.stderr)
        time.sleep(min(30, 2**attempt))
    raise SystemExit(f"failed to download {url}")


def md5sum(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK * 8):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, into: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)  # zipfile strips absolute paths and ".."
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")  # refuses links/paths outside `into`


def ensure(ds: Dataset, data_dir: Path, keep_archive: bool) -> Path:
    target = data_dir / ds.extracted
    if (target / ds.marker).exists():
        print(f"{ds.name}: present at {target}")
        return target
    data_dir.mkdir(parents=True, exist_ok=True)
    archive = data_dir / ds.archive
    if not archive.exists():
        print(f"{ds.name}: downloading {ds.url}")
        fetch(ds.url, archive)
    if ds.md5:
        print(f"{ds.name}: checking md5")
        got = md5sum(archive)
        if got != ds.md5:
            archive.unlink()
            raise SystemExit(f"{ds.name}: md5 mismatch ({got}); deleted archive, re-run")
    print(f"{ds.name}: extracting")
    tmp = data_dir / (ds.extracted + ".extracting")
    if tmp.exists():
        shutil.rmtree(tmp)
    extract(archive, tmp)
    inner = tmp / ds.extracted
    if not (inner / ds.marker).exists():
        raise SystemExit(f"{ds.name}: archive layout unexpected, missing {ds.marker}")
    if target.exists():
        shutil.rmtree(target)
    inner.rename(target)
    shutil.rmtree(tmp)
    if not keep_archive:
        archive.unlink()
    print(f"{ds.name}: ready at {target}")
    return target


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--only", choices=sorted(DATASETS), action="append")
    p.add_argument("--data-dir", type=Path, default=DATA_DIR)
    p.add_argument("--keep-archive", action="store_true", help="keep the .zip/.tar.gz")
    args = p.parse_args()
    for key in args.only or list(DATASETS):
        ensure(DATASETS[key], args.data_dir, args.keep_archive)


if __name__ == "__main__":
    main()
