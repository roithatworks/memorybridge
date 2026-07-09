"""Extraction-time filters: ephemeral noise + Hermes-infra plumbing trivia.
Run: python -m pytest tests/unit/test_noise_filter.py -v
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ingestion"))
from extractor import _is_noise, _is_infra_trivia  # noqa: E402


def test_config_noise_patterns_are_applied(tmp_path, monkeypatch):
    """User-supplied noise_patterns from config extend the built-in set (#7)."""
    import config
    import extractor
    (tmp_path / "memorybridge.json").write_text(
        json.dumps({"noise_patterns": ["widget factory heartbeat"]}))
    monkeypatch.setenv("MEMORYBRIDGE_DATA", str(tmp_path))
    config.reset_cache()
    extractor._NOISE_RE = None  # force rebuild with config applied
    try:
        assert _is_noise("widget factory heartbeat ok at 03:00")
        assert not _is_noise("The user prefers concise summaries")
    finally:
        extractor._NOISE_RE = None
        config.reset_cache()


# --- ephemeral operational telemetry -> dropped ---------------------------

def test_load_average_is_noise():
    assert _is_noise("System load averages spiked to 27.18 on June 18.")


def test_disk_usage_is_noise():
    assert _is_noise("Disk usage on Cale's system ranges from 16-22%.")


def test_no_commits_is_noise():
    assert _is_noise("No git commits were made in any monitored repo today.")


def test_durable_fact_is_not_noise():
    assert not _is_noise("Cale is seeking remote Director PMO roles.")


def test_schedule_config_is_not_ephemeral_noise():
    # A cron *schedule* is durable config, not ephemeral telemetry — the noise
    # filter must NOT catch it (the infra-trivia filter handles plumbing).
    assert not _is_noise("The morning brief cron job is scheduled for 9am CDT.")


# --- Hermes-infra plumbing trivia -> dropped (scoped to hermes projects) ---

def test_cron_schedule_infra_trivia():
    assert _is_infra_trivia("The Morning Brief cron job runs daily at 9:07 AM.", "Hermes Agent")


def test_launchd_infra_trivia():
    assert _is_infra_trivia(
        "Hermes gateway runs as a launchd background service, auto-starting on boot.", "Hermes Agent")


def test_script_internals_infra_trivia():
    assert _is_infra_trivia(
        "The capture_to_notion.py script does not set Status=Inbox on new captures.", "Hermes Agent")


def test_business_pipeline_survives_even_with_cron():
    # project_id is None (business fact) -> infra filter must not touch it.
    assert not _is_infra_trivia(
        "ROI signal report pipeline runs on the 1st and 15th via cron, pushed to Notion.", None)


def test_real_capability_survives():
    assert not _is_infra_trivia("MemoryBridge now supports HTTP transport on port 8484.", "Hermes Agent")


def test_job_fact_mistagged_hermes_survives():
    # No infra pattern -> survives even if wrongly tagged to a hermes project.
    assert not _is_infra_trivia("Cale is seeking remote Director PMO roles.", "Hermes Agent")


def test_infra_filter_scoped_to_hermes_projects():
    # Same cron text under a non-hermes project is NOT dropped by this filter.
    assert not _is_infra_trivia("The cron job runs daily at 9am.", "roithatworks")
