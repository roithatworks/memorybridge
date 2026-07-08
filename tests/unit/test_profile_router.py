"""Config-driven domain-routing engine tests.

These exercise the GENERIC engine with a synthetic routing config — user-specific
keyword sets now live in the user's config file, not in the code.
Run: python -m pytest tests/unit/test_profile_router.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ingestion"))
from profile_router import route_profile  # noqa: E402

ROUTING = {
    "domains": {
        "work": ["deadline", "sprint", "roadmap", "acme project", "stakeholder"],
        "personal": ["family", "home", "vacation", "health"],
    },
    "default_keywords": ["my name is", "i prefer", "writing style", "tone"],
    "anchors": {"work": ["acme"]},
    "custom_router": None,
}


def r(fact, project="", base="default", routing=ROUTING):
    return route_profile({"fact": fact, "project": project}, base_profile=base, routing=routing)


def test_routing_off_when_no_domains():
    off = {"domains": {}, "default_keywords": [], "anchors": {}, "custom_router": None}
    assert r("family vacation at home", routing=off) == "default"
    assert r("family vacation", base="work", routing=off) == "work"


def test_strong_domain_signal_by_project_wins():
    # project hit counts double -> score 2 -> strong.
    assert r("notes about the launch", project="acme project") == "work"


def test_weak_text_signal_routes_to_domain():
    assert r("we planned a family vacation") == "personal"


def test_default_keywords_force_default():
    assert r("i prefer a direct tone in writing") == "default"


def test_anchor_pulls_default_signal_into_domain():
    # default_keyword ("writing style") + anchor ("acme") -> work, not default.
    assert r("acme writing style guide: i prefer short sentences") == "work"


def test_strong_domain_beats_default_keyword():
    # Two work hits (deadline + sprint) => score 2 => wins over the default kw.
    assert r("i prefer to hit the sprint deadline") == "work"


def test_no_signal_falls_back_to_base_profile():
    assert r("a neutral note with nothing notable", base="personal") == "personal"


def test_no_signal_defaults_when_base_is_default():
    assert r("a neutral note with nothing notable") == "default"


def test_custom_router_is_used(tmp_path):
    mod = tmp_path / "myrouter.py"
    mod.write_text(
        "def route_profile(fact, base_profile='default'):\n"
        "    return 'from_custom'\n"
    )
    routing = {"domains": {"work": ["x"]}, "custom_router": str(mod)}
    assert r("anything", routing=routing) == "from_custom"
