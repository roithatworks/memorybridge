"""Environment guard: fail loudly on the WRONG interpreter.

Why this exists (2026-09-25). The suite was run from `python -m pytest`, where
`python` is whatever PATH resolves — on this machine a Homebrew 3.14 whose
fastmcp was 2.14.4. Tests passed, which was the dangerous part: `requirements.txt`
pins `fastmcp>=3.2.0,<4.0.0` specifically to resolve CVE-2026-32871,
CVE-2026-32870, CVE-2026-32869 and DiskCache CVE-2025-69872, so a green run on
2.x was validating the code against a version the project has explicitly
rejected, and no output said so.

A missing fastmcp was also cryptic: two collection errors with a bare
ModuleNotFoundError from `import server`, which reads as a broken test module
rather than the wrong environment.

So: assert the environment once, in one place, with a message that says how to
fix it. Both failure modes are environment errors, so both exit rather than
letting a run proceed into misleading results.

Correct interpreter is the repo's `.venv` (Python 3.12 + the pinned fastmcp),
which matches the CI matrix (3.11/3.12). See README "Tests".
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Keep in lockstep with requirements.txt / pyproject.toml.
MIN_FASTMCP = (3, 2, 0)
PIN_TEXT = "fastmcp>=3.2.0,<4.0.0"


def _parse_version(text) -> tuple[int, int, int] | None:
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(text or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _how_to_fix() -> str:
    venv = Path(__file__).resolve().parents[1] / ".venv"
    return (
        f"\n  interpreter : {sys.executable}"
        f"\n  expected    : {venv / 'bin' / 'python'}"
        f"\n\n  Fix:\n"
        f"    uv pip install --python {venv / 'bin' / 'python'} -r requirements.txt -r requirements-dev.txt\n"
        f"    {venv / 'bin' / 'python'} -m pytest tests/unit -q\n"
    )


def pytest_configure(config):
    try:
        import fastmcp
    except ImportError:
        pytest.exit(
            "fastmcp is not installed in this interpreter, but the suite imports "
            "it (server.py) — and requirements.txt pins it " + PIN_TEXT + "."
            + _how_to_fix(),
            returncode=4,
        )

    version = _parse_version(getattr(fastmcp, "__version__", None))
    if version is None:
        # Unparseable version: warn rather than block, so this guard can never be
        # the reason a valid environment fails.
        config.issue_config_time_warning(
            pytest.PytestWarning(
                f"could not parse fastmcp version {getattr(fastmcp, '__version__', None)!r}; "
                f"expected {PIN_TEXT}"
            ),
            stacklevel=2,
        )
        return

    if version < MIN_FASTMCP:
        got = ".".join(str(p) for p in version)
        pytest.exit(
            f"WRONG fastmcp: found {got}, need {PIN_TEXT}.\n"
            f"  requirements.txt pins fastmcp >=3.2.0 for CVE-2026-32871 / "
            f"CVE-2026-32870 / CVE-2026-32869 (and DiskCache CVE-2025-69872),\n"
            f"  so a passing run on {got} does not validate this code."
            + _how_to_fix(),
            returncode=4,
        )
