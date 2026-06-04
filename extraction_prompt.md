# Cognitive Blueprint Extraction Prompt
*Adapted for MemoryBridge ingestion — Cale Corbett*

---

## WHEN TO USE THIS

Run this against any conversation history export (ChatGPT chat.html, Claude export, Gemini takeout, etc.) to generate a structured knowledge base ready for MemoryBridge ingestion or direct paste into Claude memory.

Works best in: Claude Cowork (local files, no size limit) or Claude Projects (uploaded .md chunks under 31MB).

---

## THE PROMPT

```
ROLE

You are an expert at analyzing conversation history and extracting durable, high-signal knowledge from it. Your job is to read through the provided conversation history and reconstruct it into a structured knowledge base — a cognitive blueprint that captures who this person is, how they think, what they're working on, and how they prefer to work.

You are not summarizing. You are building a portable context layer for model-agnostic AI memory.

TASK

Analyze the full conversation history and produce a structured knowledge base in the following sections. Infer implicit traits when evidence is strong; label them [inferred]. Omit anything one-off, low-signal, or clearly outdated.

---

1. IDENTITY PROFILE

- Core personality traits
- Cognitive style and problem-solving approach
- Communication preferences and voice
- Motivators and drivers
- Core values
- Key strengths
- Friction points and frustrations

---

2. LONG-TERM VISION & GOALS

- Goals stated explicitly
- Goals implied by behavioral patterns [inferred]
- Recurring ambitions
- How strategic direction has shifted over time

---

3. ACTIVE & RECURRING PROJECTS

For each project, capture:
- Name (or best inference)
- Purpose and context
- Current state (active / paused / complete)
- Open loops and unresolved decisions
- Key decisions already made
- Constraints and dependencies
- Tools or platforms involved

---

4. KNOWLEDGE DOMAINS & EXPERTISE

- Areas of deep competence
- Areas of active learning
- Tools, platforms, and frameworks used regularly
- Technical stack (if applicable)
- Conceptual frameworks this person gravitates toward

---

5. DECISION-MAKING PATTERNS

- Risk tolerance
- Speed vs. precision tradeoffs
- Iterative vs. perfectionist tendencies
- Strategic vs. tactical bias
- Whether efforts tend to compound or restart frequently

---

6. BEHAVIORAL PATTERNS

- Recurring themes or questions across conversations
- Shifts in interest or focus over time
- Signs of scaling ambition, burnout, curiosity spikes, or pivots

---

7. CONSTRAINTS & OPERATING CONDITIONS

- Time constraints
- Resource or budget limitations
- Skill gaps acknowledged
- Structural or environmental limits

---

8. PREFERRED OUTPUT STYLE

- Formatting preferences
- Tone (casual vs. formal, concise vs. thorough)
- Level of detail typically wanted
- How responses should be structured
- Any explicit "always do / never do" instructions

---

9. OPEN LOOPS & UNFINISHED THREADS

- Ideas mentioned but never developed
- Projects started and paused
- Strategic questions not yet resolved

---

10. MEMORYBRIGE CORE LAYER

Create a final compressed section titled "MemoryBridge Core Layer" that distills everything above into a dense, high-signal reference block.

This section should:
- Be highly compressed — every sentence carries weight
- Capture only durable traits, active priorities, and recurring patterns
- Contain zero filler or repetition
- Be written as if it's the first thing any AI model reads before any conversation with this person
- Be structured for model-agnostic ingestion (no platform-specific syntax)

---

RULES

- Do not summarize chronologically — extract patterns, not timelines
- Do not restate full conversations
- Distill repetition into behavioral signals
- Infer implicit traits when evidence is strong; label them [inferred]
- Preserve nuance that affects long-term strategy or behavior
- Omit anything one-off, low-signal, or clearly outdated

---

OUTPUT FORMAT

Clean markdown with section headers and bullet points. No JSON. No meta-commentary about your process. No preamble. Just the knowledge base.

The final "MemoryBridge Core Layer" section should also be output as a standalone code block so it can be copy-pasted directly into Claude memory or ingested by MemoryBridge without modification.
```

---

## AFTER YOU RUN IT

**Option A — Direct Claude memory paste:**
Settings → Capabilities → Memory → paste the MemoryBridge Core Layer block with a note:
`"Cognitive profile synthesized from [source] conversation history — [date]"`

**Option B — MemoryBridge ingestion:**
Save the full output as a `.md` file and run it through the ingestion engine. The section headers map directly to MemoryBridge's layer taxonomy. The Core Layer block goes into the local JSON store as the primary context seed.

**Option C — Claude Project context:**
Drop the full output `.md` into a Project as a background document. Every conversation in that Project will reference it automatically.

---

## FILE SIZE WORKAROUND (if chat.html > 31MB)

In Claude Cowork or Claude Code, run:

```
"Within this folder, look at chat.html. Break it down into .md files that are 25MB max each, named by conversation title."
```

Then run the blueprint prompt against each chunk and merge the resulting Core Layer blocks.
```
