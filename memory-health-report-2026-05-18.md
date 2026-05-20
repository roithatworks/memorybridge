# MemoryBridge Memory Health Report
**Date:** 2026-05-18 (automated weekly audit)
**Profile:** default
**Total entries:** 50 memories + 5 projects in registry

---

## KEEP (38 entries)

These are clean, non-redundant, and still accurate.

| ID | Summary | Tokens |
|----|---------|--------|
| mem_ca58d487 | Professional identity + $126M impact + targeting | 139 |
| mem_a3e963a9 | Control Alt Recover company facts (name, domain, brand, services) | 154 |
| mem_af6f0f2c | Voice profile file pointer (VOICE_PROFILE_CALE_v3.md) | 127 |
| mem_2c06be1e | Voice quick reference (hard rules, banned words, tiers, rhythm) | 358 |
| mem_a9f985c3 | Writing preferences summary | 82 |
| mem_8bfcfa9e | Skills and tools (prompt eng, GitHub repo, Gemini gem, NB2, Workiva workflow) | 156 |
| mem_0b0731ab | Job search targeting (Director+, $180K+, OKC, relocation, content pillars, audience) | 119 |
| mem_ef543319 | LinkedIn/CAR content strategy (hooks, pillars, target audience, no CTAs) | 108 |
| mem_577356c0 | cale-voice-profile.skill created 2026-05-15, install path | 107 |
| mem_6d0518b0 | CAR toolkit PDF enhancement (7 tools rebuilt, week of 2026-05-15) | 122 |
| mem_a7000a8f | controlaltrecover.com live config (stack, GitHub, Cloudflare, D1, Resend, offers) | 351 |
| mem_5d29c1d9 | Strategy Alignment Playbook v3.0, 17-framework diagnostic toolkit | 45 |
| mem_51b4087c | Claude 4.6 does not support prefilled responses | 44 |
| mem_cae2229d | Structured weekly job search automation (ATS boards + exclusion list) | 59 |
| mem_1c2fdedb | Family in Portland, Maine; can relocate within 2 weeks | 41 |
| mem_2c520e1b | NotebookLM for expert research notebooks (Hormozi, Voss, Buffett) | 46 |
| mem_dfb40678 | Mindy / ROIThatWorks.com -- healthcare revenue recovery platform | 44 |
| mem_33aad452 | calecorbett.com site state (hosting, pages, design, content, pending items) | 621 |
| mem_c822de8c | Writing preferences (unique: architecture-first, LinkedIn connection requests, offline HTML) | 78 |
| mem_ea1e3868 | Client call prep scheduled tasks paused 2026-05-15 (re-enable when client activity resumes) | 89 |
| mem_ef986ef8 | Fortune 50 engagement: 23 projects flagged for sunsetting in 90 days | 49 |
| mem_c45fc3a3 | MemoryBridge version history (Phase 1, v1.2, Phase 2.5) | 183 |
| mem_c71fb3f5 | Xtivia PMO startup (2012-2014): 3-person team, 15 projects, $10M portfolio | 71 |
| mem_85cdad69 | Claude skills must not depend on marketing-context.md (Claude Code pattern, not Claude.ai) | 49 |
| mem_874d3f37 | skill-creator description optimizer requires subprocess/claude -p, unavailable in Claude.ai | 50 |
| mem_32469da0 | Both domains on Cloudflare; calecorbett.com deployed via Cloudflare Pages + GitHub | 45 |
| mem_b82597dc | Core approach: fix operating model/workflow before applying AI | 44 |
| mem_95cb2128 | Comfortable acknowledging background gaps; does not fabricate | 47 |
| mem_98cae4f3 | Cloudflare AI scraper protection blocks all major AI crawlers on both domains | 67 |
| mem_b9482a25 | LinkedIn job search: short keyword terms + vertical-specific searches | 44 |
| mem_0c1f6ad3 | Uses PARA method for Google Workspace (Drive, Gmail, Calendar) | 47 |
| mem_26b65f36 | Gemini: explicit 'no fabrication' + [inference] labeling reduces hallucinations | 43 |
| mem_adee36f4 | Outreach targets: newly promoted Directors/VPs using LinkedIn boolean + Sales Navigator | 47 |
| mem_f663ad1c | NB2 on Gemini: photorealistic glass whiteboard images with handwritten text/sticky notes | 48 |
| mem_5429b550 | NB2 prompt must instruct blank text areas to prevent hallucinated text | 45 |
| mem_ddad1260 | ROIThatWorks outreach targets: medical billing mgrs, revenue cycle mgrs, practice mgrs | 41 |
| mem_e5d7be4d | Never use em dashes in any output | 42 |
| mem_002 | Reddit URLs unfetchable -- request copy-paste immediately | 38 |

**Subtotal: ~3,890 tokens**

---

## MERGE (7 entries -- 4 pairs + 1 trio)

### Merge Group 1: Writing preferences split across two entries
**mem_a9f985c3** (82 tokens) + **mem_c822de8c** (78 tokens)

These overlap significantly. mem_a9f985c3 covers AI tells/passive voice/punchy copy/LinkedIn connection requests/architecture-first. mem_c822de8c covers architecture-first/LinkedIn connection requests/offline HTML -- three of five points are duplicates.

**Suggested consolidated text** (keep as mem_a9f985c3, delete mem_c822de8c):
> Writing preferences: Avoids AI tells, robotic phrasing, overexplaining, hype. Direct, confident language -- no passive constructions. Punchy biographical copy that reads aloud well. LinkedIn connection requests use recipient's first name, direct, pattern-interrupting. Architecture-first -- decisions locked before code begins. HTML tools packaged offline by removing all external dependencies (Google Fonts, etc.) to ensure portability.

**Tokens freed:** ~78 (net after updated entry: ~90 saved - 8 added = ~70 freed)

---

### Merge Group 2: Ageism decisions split across two entries
**mem_ce31b097** (41 tokens) + **mem_a1054dae** (38 tokens)

Both are ageism mitigation decisions on the resume. Near-identical in scope.

**Suggested consolidated text** (new single entry):
> Resume ageism mitigation decisions: Omits PwC and Accenture from resume. Omits USAF background. Does not mention years of experience or graduation years. Strategy: show impact, not tenure.

**Tokens freed:** ~79 (two 40-token entries collapse to one ~42-token entry = ~38 freed)

---

### Merge Group 3: calecorbett.com AI SEO -- redundant pair
**mem_d8818e57** (52 tokens) + **mem_98cae4f3** (67 tokens)

These are two sides of the same coin: calecorbett.com has great content for AI citation (mem_d8818e57), BUT Cloudflare is blocking all AI crawlers on both sites (mem_98cae4f3). They need to be together to make sense -- the insight is useless without the constraint.

**Suggested consolidated text** (new single entry):
> calecorbett.com has high-quality AI-citation-ready content (specific metrics, named orgs, clear role definitions). However, Cloudflare AI scraper protection currently blocks all major AI crawlers (GPTBot, ClaudeBot, Google-Extended, PerplexityBot, Applebot-Extended) on both controlaltrecover.com and calecorbett.com. Value will only be realized if/when crawler rules are relaxed.

**Tokens freed:** ~119 (two entries at ~119 total collapse to ~75 = ~44 freed)

---

### Merge Group 4: Job search method split across two entries
**mem_25c3572c** (40 tokens) + **mem_b9482a25** (44 tokens)

mem_25c3572c says searches are run manually via saved prompt. mem_b9482a25 says LinkedIn searches use short keyword terms + vertical-specific searches. These are two facts about the same behavior.

**Suggested consolidated text** (new single entry):
> Job search method: Runs manually using a saved prompt (not automated daily). Uses short keyword terms and separate vertical-specific LinkedIn searches rather than long natural-language queries. Uses 7-day filter window for Director PMO roles (24-hour filter unreliable at senior volume).

Note: this also absorbs mem_29e2f8e1 (LinkedIn 7-day filter insight) -- see below.

**Tokens freed from this trio:** ~132 (three entries at ~132 total collapse to ~65 = ~67 freed)

---

### Merge Group 5 (trio): LinkedIn job search tactics -- three thin entries that belong together
**mem_25c3572c** + **mem_b9482a25** + **mem_29e2f8e1** (48 tokens)

All three are tactical job search preferences. Covered in the consolidated text above (Group 4 already includes mem_29e2f8e1).

---

## DELETE (5 entries)

| ID | Content | Reason | Tokens |
|----|---------|--------|--------|
| mem_509010f8 | "Cale Corbett has strong project management experience with Fortune 500 companies, including managing $80M, $40M, and $4M programs and a GM divestiture." | Fully subsumed by mem_ca58d487 (professional identity) which has the same figures with more context. Zero marginal value. | 57 |
| mem_6f2f4bd4 | "ChatGPT (GPT-5.x) requires a verbosity cap and explicit 'no preamble' rule to prevent padded or overly enthusiastic responses." | This is Claude-session-level context about prompting another model. Not actionable for a Claude agent and likely stale (GPT model version may have changed since recorded in May 2026). Low operational value. | 49 |
| mem_123773e5 | "The user uses a reusable PDF template (car_pdf_template.py) for brand-consistent artifacts." | Extremely thin -- one file reference with no path, no context about what it does or where it lives. Not actionable. | 39 |
| mem_a377704d | "User has access to Apollo.io and is willing to use it for prospecting." | Apollo.io is connected as an MCP tool -- this is derivable from the tool list. The fact that Cale "is willing to use it" is not a meaningful constraint to store. | 36 |
| mem_07f45b76 | "Control Alt Recover (controlaltrecover.com) is built as a React/Vite Single Page Application (SPA), which delivers no static HTML content to web crawlers." | Subsumed by mem_a7000a8f (full live config), which already specifies "React/Vite" as the stack. Redundant. | 53 |

**Total tokens freed by DELETE: ~234**

---

## Summary

| Category | Count | Tokens |
|----------|-------|--------|
| KEEP (clean) | 38 | ~3,890 |
| MERGE (to consolidate) | 7 entries -> 3 consolidated entries | ~466 -> ~232 (~234 freed) |
| DELETE (remove) | 5 | ~234 |
| **Total current** | **50** | **~5,221** |
| **After changes** | **~38** | **~4,753** |
| **Tokens freed** | | **~468** |

Token budget used currently: 5,221 / 8,000 (65%).
After cleanup: ~4,753 / 8,000 (59%) -- modest improvement, more importantly removes noise that degrades retrieval precision.

**Biggest quality wins:**
1. Merge Group 1 (writing prefs) -- eliminates ambiguity about which entry governs
2. Merge Group 3 (AI SEO insight + crawler block) -- currently these contradict each other if loaded separately
3. Delete mem_509010f8 -- exact duplicate of data already in identity entry

---

## Ready to execute?

Say **"do it"** and I'll run the following changes:
- **Merge** 7 entries into 3 consolidated entries (update mem_a9f985c3, create 2 new combined entries, delete the absorbed originals)
- **Delete** 5 stale/redundant entries (mem_509010f8, mem_6f2f4bd4, mem_123773e5, mem_a377704d, mem_07f45b76)

No reads-only entries (KEEP) will be touched.
