"""Domain profile-routing tests (real facts from the Hermes 7-day preview).
Run: python -m pytest tests/unit/test_profile_router.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ingestion"))
from profile_router import route_profile  # noqa: E402


def r(fact, project=""):
    return route_profile({"fact": fact, "project": project})


def test_consulting_by_project():
    assert r("ROI addresses medical billing prior auth denials and A/R aging.", "roithatworks") == "consulting"


def test_consulting_by_text():
    assert r("User targets billing managers and RCM directors at small medical practices.") == "consulting"


def test_job_search_role():
    assert r("User is seeking remote Director PMO and Program Management roles.", "Director PMO job search") == "job_search"


def test_job_search_sales_nav():
    assert r("User uses LinkedIn Sales Navigator 'Changed Jobs in past 90 days' filter.") == "job_search"


def test_teaching():
    assert r("Canvas grading rubric for student discussion posts in the course.") == "teaching"


def test_linkedin_filter_mechanics_are_job_search():
    # f_WT / saved-search filter codes are job-hunt tooling, not generic LinkedIn.
    assert r("LinkedIn's remote work filter code f_WT=2 may change, breaking saved search URLs.") == "job_search"


# --- LinkedIn routing rules (Cale's heuristics) ---------------------------
# Bare LinkedIn (no CAR/ROI/consulting) defaults to job_search if it's hunt
# mechanics, else consulting (brand). Voice/style always wins to default.

def test_linkedin_voice_stays_default():
    assert r("Cale prefers direct, mutual-connection LinkedIn connection messages over salesy pitches.") == "default"


def test_linkedin_voice_with_client_anchor_is_consulting():
    # Named CAR/ROI makes it domain-specific voice → consulting, not default.
    assert r("CAR LinkedIn content style uses identity hooks and practitioner CTAs.", "0e3e3912") == "consulting"


def test_linkedin_recruiter_connections_are_job_search():
    assert r("Cale sends LinkedIn connection requests to recruiters during the job hunt.") == "job_search"


def test_bare_linkedin_content_is_consulting():
    assert r("Cale posts LinkedIn thought-leadership content weekly on operations topics.") == "consulting"


def test_personal_strong_signal_beats_default_override():
    # "wife"/"Mindy" (personal) must win over "linkedin"/"prompt" (default).
    assert r("Mindy Corbett (Cale's wife) uses a LinkedIn post-writing prompt.") == "personal"


def test_identity_metric_goes_default():
    assert r("Cale Corbett claims $126M in business impact: $80M cost reduction, $46M revenue growth.") == "default"


def test_tooling_goes_default():
    assert r("Claude responds better to XML; Gemini requires explicit format examples.") == "default"


def test_voice_goes_default():
    assert r("Cale prefers direct, mutual-connection LinkedIn messages over salesy pitches.") == "default"


def test_weak_signal_falls_back_to_default():
    assert r("Cale stood up a PMO from scratch at Xtivia as Senior PM.") == "default"


def test_base_profile_override_honored():
    # An explicit --profile job_search run keeps weak-signal facts in job_search.
    assert route_profile({"fact": "Some neutral note with no domain words."},
                         base_profile="job_search") == "job_search"
