"""Tests for the `mb` CLI (init path + argument parsing)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import cli
import config


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    monkeypatch.setenv("MEMORYBRIDGE_NO_EMBED", "1")
    config.reset_cache()
    yield
    config.reset_cache()


def test_init_creates_scaffold(tmp_path, capsys):
    rc = cli.main(["init"])
    assert rc == 0
    assert (tmp_path / ".env").exists()
    assert (tmp_path / "memorybridge.yaml").exists()
    assert (tmp_path / "memory.db").exists()
    assert (tmp_path / "inbox").is_dir()
    assert (tmp_path / "logs").is_dir()

    # Store has a usable default profile.
    from db.store import MemoryStore
    store = MemoryStore(tmp_path / "memory.db")
    assert store.get_profile("default") is not None

    # Prints a Claude Desktop config snippet with the `mb serve` command.
    out = capsys.readouterr().out
    assert '"command": "mb"' in out
    assert '"serve"' in out


def test_init_is_idempotent(tmp_path):
    assert cli.main(["init"]) == 0
    # Second run must not clobber an existing .env / config.
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=secret\n")
    assert cli.main(["init"]) == 0
    assert "secret" in (tmp_path / ".env").read_text()


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_ingest_args_parse():
    args = cli.build_parser().parse_args(["ingest", "--source", "claude", "--file", "x.json"])
    assert args.source == "claude" and args.file == "x.json"
