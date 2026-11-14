"""The pre-commit hook must block secrets. Fake keys are built at runtime so
this file itself never contains a key-shaped string."""

import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "hooks" / "pre-commit"


@pytest.fixture
def repo(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], check=False, cwd=tmp_path, capture_output=True, text=True
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / "pre-commit")
    (hooks / "pre-commit").chmod(0o755)
    git("config", "core.hooksPath", str(hooks))
    return tmp_path, git


def commit(repo, name, content):
    path, git = repo
    (path / name).parent.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(content)
    git("add", name)
    return git("commit", "-q", "-m", "test")


def test_blocks_anthropic_key(repo):
    fake = "sk-" + "ant-" + "api03-" + "A" * 40
    assert commit(repo, "config.py", f'KEY = "{fake}"\n').returncode != 0


def test_blocks_private_key(repo):
    pem = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\n"
    assert commit(repo, "notes.txt", pem).returncode != 0


def test_blocks_env_file(repo):
    assert commit(repo, ".env", "LLM_MODE=ollama\n").returncode != 0


def test_blocks_node_keys(repo):
    assert commit(repo, "keys/node7.key", "x\n").returncode != 0


def test_allows_env_example_and_normal_code(repo):
    assert commit(repo, ".env.example", "ANTHROPIC_API_KEY=\n").returncode == 0
    assert commit(repo, "app.py", "print('hello')\n").returncode == 0
