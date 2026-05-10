# Notion Context Public Release Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Strip personal information from the Notion context integration so it can be used by any MemoryBridge user without modification.

**Architecture:** Replace hardcoded values (page ID, identity, file paths) with env vars and DB-driven config. Add a launchd install script that substitutes $HOME at install time.

**Tech Stack:** Python, notion-client, launchd plist, bash install script.

---

## What's currently hardcoded (must fix)

| File | Hardcoded value | Fix |
|---|---|---|
| `ingestion/notion_context.py` | `DEFAULT_PAGE_ID = "35c9e117..."` | Remove default; require `NOTION_CONTEXT_PAGE_ID` env var |
| `ingestion/notion_context.py` | `identity = {"name": "Cale", "role": "Director..."}` | Read from `profiles` table in DB |
| `launchd/com.memorybridge.notion-context.plist` | `/Users/cale/` (6 occurrences) | Template with `{{HOME}}`; generate via install script |

## What's already clean (no changes needed)

- `ingestion/notion_queue.py` — fully generic
- `_passport_to_blocks()` — no personal data
- `_clear_page_blocks()` / `_append_blocks()` — generic Notion API wrappers
- All unit tests

---

## Task 1: Remove hardcoded page ID

**Files:**
- Modify: `ingestion/notion_context.py`

### Step 1: Change DEFAULT_PAGE_ID

Replace:
```python
DEFAULT_PAGE_ID = "35c9e117-3e5c-81a2-8b62-e10338eef2ef"
```

With:
```python
DEFAULT_PAGE_ID = ""  # set NOTION_CONTEXT_PAGE_ID in .env
```

### Step 2: Add validation in refresh_context_page()

After the token check, add:
```python
if not page_id:
    raise RuntimeError(
        "NOTION_CONTEXT_PAGE_ID not set — run notion_context.py --page-id <id> "
        "or set NOTION_CONTEXT_PAGE_ID in your .env"
    )
```

### Step 3: Update --page-id default in main()

The argparse default already reads from env var — no change needed if env var is set.

### Step 4: Update .env.example

Add to `docs/.env.example` (or create it):
```
NOTION_CONTEXT_PAGE_ID=your-notion-page-id-here
```

### Step 5: Run tests

```bash
/usr/local/bin/python3 -m pytest tests/unit/test_notion_context.py -v
```

Expected: all tests pass (the test that passes page_id explicitly is unaffected).

### Step 6: Commit

```bash
git add ingestion/notion_context.py
git commit -m "fix: remove hardcoded Notion page ID from notion_context.py"
```

---

## Task 2: Read identity from profiles table

**Files:**
- Modify: `ingestion/notion_context.py`

### Step 1: Check profiles table schema

```bash
sqlite3 ~/memorybridge/memory.db ".schema profiles"
```

Expected columns include: `name`, `role`, `communication_style`, etc.

### Step 2: Add _load_identity() function

```python
def _load_identity(profile: str, db_path: Path) -> dict:
    """Load identity fields from the profiles table. Returns {} if not found."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE name = ? LIMIT 1", (profile,)
        ).fetchone()
        if row is None:
            return {}
        return {k: row[k] for k in row.keys() if row[k] is not None}
    except Exception:
        return {}
    finally:
        conn.close()
```

### Step 3: Replace hardcoded identity in refresh_context_page()

Replace:
```python
identity = {
    "name": "Cale",
    "role": "Director-level BizOps/PMO professional",
}
```

With:
```python
identity = _load_identity(profile, db_path)
```

### Step 4: Run tests

```bash
/usr/local/bin/python3 -m pytest tests/unit/test_notion_context.py -v
```

Update any test that relied on identity being present (dry-run test may need a profiles table in the temp DB fixture).

### Step 5: Commit

```bash
git add ingestion/notion_context.py tests/unit/test_notion_context.py
git commit -m "fix: read identity from profiles table instead of hardcoded values"
```

---

## Task 3: Templatize the launchd plist

**Files:**
- Rename: `launchd/com.memorybridge.notion-context.plist` → `launchd/com.memorybridge.notion-context.plist.template`
- Create: `launchd/install.sh`

### Step 1: Convert plist to template

Replace all `/Users/cale/` with `{{HOME}}/` in the plist file. Rename it to `.plist.template`.

### Step 2: Create launchd/install.sh

```bash
#!/usr/bin/env bash
# MemoryBridge launchd installer
# Generates personalized plists from templates and loads them.
#
# Usage: bash launchd/install.sh [--dry-run]

set -euo pipefail

LAUNCHD_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DRY_RUN=false

[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

install_plist() {
    local template="$1"
    local plist_name
    plist_name="$(basename "$template" .template)"
    local dest="$AGENTS_DIR/$plist_name"

    # Substitute {{HOME}} with actual home directory
    sed "s|{{HOME}}|$HOME|g" "$template" > "/tmp/$plist_name"

    if $DRY_RUN; then
        echo "[dry-run] Would install: $dest"
        cat "/tmp/$plist_name"
        return
    fi

    mkdir -p "$AGENTS_DIR"
    cp "/tmp/$plist_name" "$dest"
    launchctl load "$dest"
    echo "Installed and loaded: $dest"
}

for template in "$LAUNCHD_DIR"/*.plist.template; do
    install_plist "$template"
done
```

### Step 3: Make executable

```bash
chmod +x launchd/install.sh
```

### Step 4: Update README install instructions

Point users to:
```bash
bash launchd/install.sh
```

### Step 5: Commit

```bash
git add launchd/
git commit -m "feat: templatize launchd plists with install.sh for portability"
```

---

## Task 4: Add .env.example

**Files:**
- Create: `.env.example`

### Step 1: Create .env.example

```bash
# MemoryBridge environment configuration
# Copy to ~/memorybridge/.env and fill in values

# Notion integration (opt-in)
NOTION_API_TOKEN=your_notion_integration_token_here
NOTION_FLAGGED_DB_ID=your_notion_flagged_database_id_here
NOTION_CONTEXT_PAGE_ID=your_notion_context_page_id_here

# DeepSeek (for extraction)
DEEPSEEK_API_KEY=your_deepseek_api_key_here

# Anthropic (for escalation resolver)
ANTHROPIC_API_KEY=your_anthropic_api_key_here
```

### Step 2: Commit

```bash
git add .env.example
git commit -m "docs: add .env.example for new user setup"
```

---

## Task 5: Full verification

### Step 1: Run full test suite

```bash
/usr/local/bin/python3 -m pytest tests/ -v 2>&1 | tail -10
```

Expected: 110+ passed.

### Step 2: Test install script dry-run

```bash
bash launchd/install.sh --dry-run
```

Expected: prints generated plist with real $HOME paths, no files written.

### Step 3: Verify notion_context.py fails cleanly without env vars

```bash
NOTION_API_TOKEN=tok /usr/local/bin/python3 ingestion/notion_context.py --dry-run
```

Expected: `Error: NOTION_CONTEXT_PAGE_ID not set`

### Step 4: Push

```bash
git push origin main
```

---

## Verification checklist

- [ ] No hardcoded `/Users/cale/` anywhere in committed files
- [ ] No hardcoded page IDs or database IDs in Python source
- [ ] `notion_context.py --dry-run` fails with clear message when `NOTION_CONTEXT_PAGE_ID` unset
- [ ] `launchd/install.sh --dry-run` generates correct plist with `$HOME` substituted
- [ ] `.env.example` documents all required env vars
- [ ] Full test suite passes
- [ ] README install section updated to reference `install.sh`
