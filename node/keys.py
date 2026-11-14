"""Provision one Ed25519 keypair per node (SPEC 3.2).

    keys/<node_id>.key   private seed, hex, mode 0600 (git-ignored, blocked by the pre-commit hook)
    keys/public.json     {"<node_id>": {"name": ..., "public_key": hex}} for the station

Idempotent: existing private keys are kept; public.json is rewritten from them.
Run: uv run python -m node.keys   (or make keys)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from nacl.encoding import HexEncoder
from nacl.signing import SigningKey, VerifyKey

from common.contracts import ROOT, World, load_world

PUBLIC_FILE = "public.json"


def default_keys_dir() -> Path:
    return Path(os.getenv("KEYS_DIR") or ROOT / "keys")


def _private_path(keys_dir: Path, node_id: int) -> Path:
    return keys_dir / f"{node_id}.key"


def _write_new_private(path: Path, key: SigningKey) -> None:
    # O_EXCL: never overwrite an existing key, even if two processes race.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.encode(encoder=HexEncoder).decode() + "\n")


def load_signing_key(node_id: int, keys_dir: Path | None = None) -> SigningKey:
    path = _private_path(keys_dir or default_keys_dir(), node_id)
    return SigningKey(path.read_text().strip().encode(), encoder=HexEncoder)


def provision(world: World | None = None, keys_dir: Path | None = None) -> dict[int, str]:
    """Create missing keypairs for every node in the world. Returns {node_id: public hex}."""
    world = world or load_world()
    keys_dir = keys_dir or default_keys_dir()
    keys_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o700)

    public: dict[int, str] = {}
    entries: dict[str, dict[str, str]] = {}
    for node in sorted(world.nodes, key=lambda n: n.id):
        path = _private_path(keys_dir, node.id)
        if not path.exists():
            try:
                _write_new_private(path, SigningKey.generate())
            except FileExistsError:
                pass  # another process created it first; use theirs
        pub = load_signing_key(node.id, keys_dir).verify_key.encode(encoder=HexEncoder).decode()
        public[node.id] = pub
        entries[str(node.id)] = {"name": node.name, "public_key": pub}

    tmp = keys_dir / f".{PUBLIC_FILE}.tmp"
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, keys_dir / PUBLIC_FILE)
    return public


def load_public_keys(keys_dir: Path | None = None) -> dict[int, VerifyKey]:
    """Station side: node_id -> VerifyKey from public.json."""
    data = json.loads(((keys_dir or default_keys_dir()) / PUBLIC_FILE).read_text())
    return {
        int(node_id): VerifyKey(entry["public_key"].encode(), encoder=HexEncoder)
        for node_id, entry in data.items()
    }


def ensure_keys(world: World | None = None, keys_dir: Path | None = None) -> dict[int, VerifyKey]:
    """Provision if any node is missing a key, then return the public keys."""
    world = world or load_world()
    keys_dir = keys_dir or default_keys_dir()
    missing = not (keys_dir / PUBLIC_FILE).exists() or any(
        not _private_path(keys_dir, n.id).exists() for n in world.nodes
    )
    if missing:
        provision(world, keys_dir)
    return load_public_keys(keys_dir)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Provision node keypairs.")
    parser.add_argument("--keys-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    keys_dir = args.keys_dir or default_keys_dir()
    public = provision(keys_dir=keys_dir)
    print(f"{len(public)} node keys in {keys_dir} (public keys: {keys_dir / PUBLIC_FILE})")


if __name__ == "__main__":
    main()
