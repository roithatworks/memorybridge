"""Domain routing: assign each extracted fact to a memory profile.

A fact carries category + project + the fact text. We route it to ONE of the
domain profiles below, defaulting to "default" (the cross-cutting bucket for
identity / voice / tooling that should apply everywhere) whenever the signal is
weak. Misrouting to "default" is recoverable; misrouting into the wrong domain
is silent damage — so the bias is intentionally toward "default".

Signal priority:
  1. explicit project name keywords  (strongest)
  2. fact-text keywords
  3. fall back to "default"

Tune the keyword sets below — that's the whole control surface.
"""

import re

# Profiles that domain facts route into. "default" is the catch-all and is NOT
# in this map (it's the fallback).
DOMAIN_PROFILES = ("job_search", "consulting", "teaching", "personal")

# Identity / voice / cross-cutting facts belong in "default" so EVERY domain
# sees them. If a fact matches these, it overrides domain routing.
_DEFAULT_KEYWORDS = {
    "voice", "tone", "writing style", "content style", "em dash", "em-dash",
    "oxford comma", "prompt", "prompting", "claude", "gemini", "chatgpt",
    "llm", "model", "cale corbett", "identity", "bluf",
    "prefers", "style", "messages over",
}

# A named consulting/brand anchor. If present, a "voice/style" fact is
# domain-specific (consulting voice), NOT universal default voice — so these
# override the default-voice rule and pull the fact into consulting.
_CONSULTING_ANCHORS = {
    "control alt recover", "car ", " car", "roi that works", "roithatworks",
    "client", "fairy chains", "71 model",
}

# Domain keyword sets. Order matters only for readability; scoring picks the
# best match. Keep these specific — generic words cause misrouting.
_DOMAIN_KEYWORDS = {
    "job_search": {
        "job search", "director pmo", "program management role", "resume",
        "cover letter", "recruiter", "application", "ats", "greenhouse",
        "lever", "interview", "remote role", "salary", "linkedin sales navigator",
        "changed jobs", "hiring", "candidate",
        # LinkedIn job-search mechanics (Sales Nav / saved-search filters used
        # for role hunting): f_wt / f_e / boolean filter codes, saved search.
        "f_wt", "remote work filter", "saved search", "boolean search",
        "linkedin filter", "search url", "prospecting filter",
    },
    "consulting": {
        "roi that works", "roithatworks", "control alt recover", "car ",
        "client", "consulting", "pmo design", "transformation", "fractional",
        "medical billing", "rcm", "revenue cycle", "prior auth", "apollo",
        "prospect", "icp", "outreach", "71 model", "fairy chains",
    },
    "teaching": {
        "canvas", "student", "grading", "grade", "course", "syllabus",
        "adjunct", "lecture", "assignment", "rubric", "discussion post",
    },
    "personal": {
        "wife", "mindy", "family", "daughter", "son", "father", "husband",
        "personal", "home", "health", "vacation",
    },
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def route_profile(fact: dict, base_profile: str = "default") -> str:
    """Return the profile this fact should be written to.

    base_profile is the run-level profile (what --profile was set to). When the
    run targets a specific profile other than 'default', we honor it as the
    fallback instead of 'default' — so `--profile job_search` still works as an
    override while auto-routing refines within a 'default' run.
    """
    text = _norm(fact.get("fact", ""))
    project = _norm(fact.get("project", ""))
    haystack = f"{project} {text}"

    # 1. Score each domain by keyword hits (project text weighted 2x).
    best, best_score = None, 0
    for profile, kws in _DOMAIN_KEYWORDS.items():
        score = 0
        for k in kws:
            if k in project:
                score += 2
            elif k in text:
                score += 1
        if score > best_score:
            best, best_score = profile, score

    default_signal = any(k in haystack for k in _DEFAULT_KEYWORDS)
    consulting_anchor = any(k in haystack for k in _CONSULTING_ANCHORS)

    # 2. A STRONG domain signal (score >= 2, e.g. a project-name hit or two
    #    keyword hits) wins even if cross-cutting words are also present — e.g.
    #    "Mindy (wife) uses a LinkedIn prompt" is personal, not default.
    if best and best_score >= 2:
        return best

    # 3. Cross-cutting identity/voice/tooling → default (applies everywhere).
    #    BUT if the fact names a consulting/brand anchor (CAR, ROI, client), the
    #    voice is domain-specific → consulting, not universal default. Runs
    #    before the LinkedIn rule so a LinkedIn *voice* fact with no client
    #    anchor ("prefers direct messages") still lands in default.
    if default_signal:
        return "consulting" if consulting_anchor else "default"

    # 4. LinkedIn special-case (Cale's rule): LinkedIn is primarily his
    #    job-search and consulting/brand surface, not a generic default topic.
    #    - LinkedIn + consulting/CAR/ROI signal → consulting (handled by step 2
    #      already; if a weak consulting hit exists, prefer it here too).
    #    - LinkedIn + job-search mechanics → job_search.
    #    - any OTHER bare LinkedIn (posting, content, engagement, strategy with
    #      no job-search signal) → consulting (it's his brand work).
    if "linkedin" in haystack:
        if best == "consulting" and best_score >= 1:
            return "consulting"
        if best == "job_search" and best_score >= 1:
            return "job_search"
        # Bare LinkedIn with no domain signal: job-search if it reads like the
        # hunt (connections, search, hiring), else consulting (brand/content).
        if any(k in haystack for k in (
            "connect", "connection", "search", "filter", "recruiter",
            "hiring", "job", "role", "candidate", "sales navigator",
        )):
            return "job_search"
        return "consulting"

    # 5. A weak-but-present domain signal still routes to its domain.
    if best and best_score >= 1:
        return best

    # 6. No signal → safe fallback.
    return base_profile if base_profile in DOMAIN_PROFILES else "default"
