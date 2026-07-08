"""
Unit tests for ui/views/portability.py validation and ingestion helper.
"""
import pytest
import tempfile
import sys
from pathlib import Path

# Add root directory to sys.path so we can import ui.views.portability
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from ui.views import portability
from ui.views.portability import (
    _validate_source,
    _validate_profile_name,
    _validate_input_file_path,
    run_ingestion,
)


def test_validate_source():
    assert _validate_source("claude") == "claude"
    assert _validate_source("chatgpt") == "chatgpt"
    assert _validate_source("gemini") == "gemini"

    with pytest.raises(ValueError, match="Invalid source"):
        _validate_source("invalid_source")


def test_validate_profile_name():
    assert _validate_profile_name("default") == "default"
    assert _validate_profile_name(" DEFAULT ") == "default"
    # Any validly-named profile is allowed now, not just "default" (#90).
    assert _validate_profile_name("work") == "work"
    assert _validate_profile_name("job_search") == "job_search"

    # Format-invalid names are still rejected.
    for bad in ("-invalid", "bad/name", "has space", "x" * 65):
        with pytest.raises(ValueError, match="Invalid profile name"):
            _validate_profile_name(bad)


def test_validate_input_file_path():
    # Test valid temp file
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp_path = Path(tf.name)

    try:
        validated = _validate_input_file_path(tmp_path)
        assert validated.resolve() == tmp_path.resolve()
    finally:
        tmp_path.unlink(missing_ok=True)

    # Test non-existent path
    with pytest.raises(ValueError, match="Invalid input file path"):
        _validate_input_file_path(Path("/tmp/does_not_exist_12345.json"))

    # Test wrong suffix
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp_txt_path = Path(tf.name)
    try:
        with pytest.raises(ValueError, match="Invalid input file type"):
            _validate_input_file_path(tmp_txt_path)
    finally:
        tmp_txt_path.unlink(missing_ok=True)

    # Test path traversal or outside temp directory
    outside_path = Path(__file__).resolve() # Current test file (not in tempdir)
    with pytest.raises(ValueError, match="File must be located in the temporary directory"):
        _validate_input_file_path(outside_path)


def test_run_ingestion_handles_subprocess_timeout(monkeypatch):
    """A subprocess timeout must be returned as a failed-run result, not raised
    as an uncaught TimeoutExpired the callers don't guard (#113)."""
    import subprocess
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ingest", timeout=300)
        monkeypatch.setattr(portability.subprocess, "run", boom)
        res = run_ingestion("claude", tmp, "default", preview=True, days=0)
        assert res["returncode"] == -1
        assert "timed out" in res["stderr"].lower()
    finally:
        tmp.unlink(missing_ok=True)
