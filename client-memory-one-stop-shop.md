# Client Memory System

This file is a one-stop-shop artifact for setting up and maintaining client memory across AI assistants.

It contains:
1. Operating instructions for the assistant
2. The canonical client memory template
3. Minimal usage rules for keeping files current

---

## Assistant instructions

When you receive this file along with a client file, read both and let the client file shape every response silently.

- Do not narrate that you read it.
- Do not summarize it back unless explicitly asked.
- Use it to improve accuracy, tone, continuity, and risk management.
- If a section is empty or missing, treat it as unknown.
- Ask one clarifying question only if missing information blocks the current task.

Treat the client file as the source of truth for:
- current status
- preferences and constraints
- commitments and deadlines
- sensitivities and landmines

If the client file conflicts with the user’s latest message in the current session, the current session is the newer source of truth.

---

## How to use this system

Cale is a solo consultant who works with multiple clients. Each client gets one markdown memory file.

Recommended pattern:
- one shared instruction file: this file
- one primary client file per client, e.g. `acme.md`
- current conversation overrides stale file content

Use the client file to shape:
- what matters most
- what to emphasize or de-emphasize
- how concise or detailed to be
- how to avoid avoidable mistakes

---

## Client file template

Copy this section into a separate client-specific markdown file.

```md
# Client: [Name]

**Last updated:** YYYY-MM-DD
**Engagement goal (1 line):**
**Primary decision-maker(s):**
**Next deliverable (date):**
**Primary hub link(s):**

## Who they are
<!-- One sentence: name, role, company, what they hired you for -->

## Where things stand
### Current status
- 
- 
- 

### Open questions / decisions needed
- 
- 

### What “done” looks like for the current phase
- 

## Their preferences and constraints
### Do
- 
- 

### Don’t
- 
- 

### Examples / snippets they liked
- 
- 

## What I’ve committed to
- [ ] **[Deliverable]** — due: YYYY-MM-DD — status: not started | drafting | sent | done — link:
- [ ] **[Follow-up]** — due: YYYY-MM-DD — status: not started | drafting | sent | done — link:

## What not to do
- 
- 

## Notes
### Key contacts
- Name — role — relationship — email / handle

### Tools / systems
- 

### Backstory / context
- 

### Acronyms / definitions
- 
```

---

## File maintenance rules

Update the client file whenever:
- a decision is made or a commitment is given
- a deadline changes
- a preference or constraint becomes clear
- something goes wrong that should not be repeated

Always refresh:
- Last updated
- Next deliverable (date)
- commitments ledger

A stale file is worse than no file.

---

## Platform setup notes

### ChatGPT Projects
- Upload this file to the Project knowledge area
- Upload the client `.md` files alongside it
- At the start of each session, specify which client file applies

### Gemini Gems
- Paste the assistant instructions from this file into the Gem instructions
- Upload this file and the client `.md` files if the workspace supports files
- At the start of each session, specify which client applies

### Perplexity Spaces
- Paste the assistant instructions into the Space instructions
- Upload this file and all client `.md` files
- At the start of each session, specify which client applies

### Claude Projects / local files
- Keep this file plus client files in the working directory you use for Claude
- At the start of each session, specify which client applies

---

## File naming convention

Use lowercase, hyphenated markdown filenames.

Examples:
- `acme.md`
- `riverstone-group.md`
- `jane-smith.md`

Keep exactly one primary memory file per client.
