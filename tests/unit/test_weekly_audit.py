"""Tests for scripts/weekly_audit.py."""
import os
import subprocess
import sys
from pathlib import Path

from db.store import MemoryStore


def test_weekly_audit_creates_logs_dir_and_report(tmp_path, monkeypatch):
    """weekly_audit.py must dynamically create logs/ and write the report."""
    data_dir = tmp_path / "custom_data"
    data_dir.mkdir()
    # Create empty store
    store = MemoryStore(data_dir / "memory.db")
    store.ensure_profile("default")

    script = Path(__file__).parent.parent.parent / "scripts" / "weekly_audit.py"
    env = dict(os.environ, MEMORYBRIDGE_DATA=str(data_dir))

    # logs/ directory does not exist yet
    logs_dir = data_dir / "logs"
    assert not logs_dir.exists()

    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"Script failed: {result.stderr}"
    assert logs_dir.exists()
    report_file = logs_dir / "weekly_audit_report.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "# Weekly Pruner Audit Report" in content
