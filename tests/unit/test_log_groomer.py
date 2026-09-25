"""Regression tests for issue #181 (P2-6): stale .bak DB cleanup in
scripts/log_groomer.sh.

Log rotation itself for server.error.log and http-bridge.error.log was
already fixed under issue #177 (both logs are in the LOGS array with a
50MB threshold) -- these tests cover only the new .bak DB cleanup logic:
one-off scripts (backfill-tags.py, backfill-entities.py) each write a
timestamped memory.db.bak-<label>-<ts> before mutating the DB, and nothing
ever deleted them (53MB of stale .bak DBs accumulated per the issue).

NOTE on the issue numbers above: #177 and #181 do not resolve to those
topics in this repo's tracker, so treat them as unreliable provenance. The
rotation test below is load-bearing regardless -- it was the only thing
catching a real portability bug (#197): the size lookup used BSD-only
`stat -f%z`, which yields empty output on GNU stat, so rotation silently
never happened on Linux/CI while passing on macOS.
"""
import os
import subprocess
import time
from pathlib import Path

GROOMER = Path(__file__).resolve().parent.parent.parent / "scripts" / "log_groomer.sh"


def _run_groomer(data_dir: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MEMORYBRIDGE_DATA"] = str(data_dir)
    return subprocess.run(
        ["bash", str(GROOMER)], env=env, capture_output=True, text=True, timeout=30
    )


def _touch_days_old(path: Path, days: int):
    path.touch()
    old_time = time.time() - (days * 86400)
    os.utime(path, (old_time, old_time))


def test_groomer_script_exists_and_is_executable_bash():
    assert GROOMER.exists()


def test_groomer_deletes_stale_bak_db_older_than_threshold(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "memory.db").touch()
    stale = tmp_path / "memory.db.bak-backfill-tags-20260101-000000"
    _touch_days_old(stale, days=20)  # older than BAK_MAX_AGE_DAYS=14

    result = _run_groomer(tmp_path)

    assert not stale.exists(), f"stale .bak file should be deleted; stderr={result.stderr}"
    assert (tmp_path / "memory.db").exists(), "the live DB itself must never be touched"


def test_groomer_preserves_recent_bak_db(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "memory.db").touch()
    recent = tmp_path / "memory.db.bak-backfill-entities-20260811-000000"
    _touch_days_old(recent, days=1)  # well under BAK_MAX_AGE_DAYS=14

    _run_groomer(tmp_path)

    assert recent.exists(), "a backup made yesterday (e.g. mid-migration) must not be deleted"


def test_groomer_reports_deleted_bak_count_in_output(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "memory.db").touch()
    stale1 = tmp_path / "memory.db.bak-a-20260101-000000"
    stale2 = tmp_path / "memory.db.bak-b-20260102-000000"
    _touch_days_old(stale1, days=30)
    _touch_days_old(stale2, days=25)

    result = _run_groomer(tmp_path)

    assert "Deleted 2 stale .bak DB file(s)" in result.stdout


def test_groomer_no_bak_files_is_silent_on_that_front(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "memory.db").touch()

    result = _run_groomer(tmp_path)

    assert "stale .bak DB file(s)" not in result.stdout


def test_groomer_still_rotates_both_logs_over_threshold(tmp_path):
    """Confirms the P2-6 addition didn't regress the issue #177 log
    rotation this script already had for both server.error.log and
    http-bridge.error.log."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    big_log = logs_dir / "http-bridge.error.log"
    big_log.write_bytes(b"x" * (51 * 1024 * 1024))  # over the 50MB LOG_LIMIT_MB

    result = _run_groomer(tmp_path)

    assert big_log.exists()
    assert big_log.stat().st_size == 0, "over-threshold log must be truncated"
    assert "truncated" in result.stdout.lower()
