# MemoryBridge — Setup Guide

MemoryBridge gives Claude a persistent, local memory that works across every conversation — Claude desktop app, Cowork mode, Claude.ai (via export), and even other AI models. Your memories stay on your machine. Nothing goes to the cloud.

---

## What You Need

- A Mac (instructions below are Mac-specific; Windows/Linux paths differ slightly)
- Python 3.10 or later
- The Claude desktop app installed

---

## Step 1 — Put the Files in Place

Create a folder called `memorybridge` in your home directory and copy `server.py` and `requirements.txt` into it:

```bash
mkdir ~/memorybridge
cp server.py ~/memorybridge/
cp requirements.txt ~/memorybridge/
```

Your folder should look like this:
```
~/memorybridge/
  server.py
  requirements.txt
```

---

## Step 2 — Install Dependencies

Open Terminal and run:

```bash
pip3 install -r ~/memorybridge/requirements.txt
```

If you get a permissions error, try:
```bash
pip3 install --user fastmcp tiktoken
```

To verify it worked:
```bash
python3 -c "import fastmcp; print('fastmcp OK')"
```

---

## Step 3 — Register MemoryBridge with Claude

1. Open the Claude desktop app
2. Go to **Settings** (top menu: Claude → Settings, or ⌘,)
3. Click **Developer** in the sidebar
4. Click **Edit Config** — this opens `claude_desktop_config.json` in a text editor
5. Add the MemoryBridge server to the `"mcpServers"` section:

```json
{
  "mcpServers": {
    "memorybridge": {
      "command": "python3",
      "args": [
        "/Users/YOUR_USERNAME/memorybridge/server.py"
      ]
    }
  }
}
```

**Important:** Replace `YOUR_USERNAME` with your actual Mac username (you can find it by running `whoami` in Terminal).

If you already have other MCP servers configured, add the `"memorybridge"` block alongside them — don't replace the whole file. The structure looks like:

```json
{
  "mcpServers": {
    "memorybridge": {
      "command": "python3",
      "args": ["/Users/YOUR_USERNAME/memorybridge/server.py"]
    },
    "some-other-server": { ... }
  }
}
```

6. Save the file and **fully quit and relaunch** the Claude desktop app (⌘Q, then reopen)

---

## Step 4 — Verify It's Working

Start a new conversation in Claude and ask:

> "Can you check if MemoryBridge is connected?"

Claude will be able to call the memory tools if everything is set up correctly. You can also ask:

> "Add a memory: I prefer concise bullet-point answers."

And then in a later conversation:

> "What do you know about me?"

---

## Step 5 — Create Your First Profile

MemoryBridge uses "profiles" to organize memory. To get started, ask Claude:

> "Create a memory profile for me and add a few facts: my name is [name], I work in [field], and I prefer [communication style]."

---

## Everyday Usage

You don't need to do anything special once it's set up. Claude will automatically have access to the memory tools. You can just talk to it naturally:

- "Remember that I'm working on a project called X"
- "What do you remember about my preferences?"
- "Search my memories for anything about [topic]"
- "Delete the memory about [thing]"

---

## Where Your Data Lives

All memories are stored locally in two files:

- `~/memorybridge/memory.json` — your memories
- `~/memorybridge/analytics.json` — usage stats

You can open these in any text editor. They're plain JSON — human-readable and easy to back up.

---

## Works With Claude Desktop + Cowork

MemoryBridge is registered as a system-level MCP server, which means it's available in **all** Claude desktop conversations — including Cowork mode. You don't need to configure it separately for Cowork.

---

## Troubleshooting

**Claude says it doesn't have memory tools:**
- Make sure you fully quit and relaunched Claude (not just closed the window)
- Check that the path in `claude_desktop_config.json` matches your actual username
- Run `python3 ~/memorybridge/server.py` in Terminal — if it errors, fix those errors first

**"Module not found" error:**
- Run `pip3 install fastmcp` again
- If you have multiple Python versions, make sure the `python3` command in the config matches where fastmcp is installed. You can use the full path: `which python3` in Terminal gives you the exact path.

**Config file not found:**
- On Mac, it's at: `~/Library/Application Support/Claude/claude_desktop_config.json`
- If it doesn't exist, create it with the content from `claude_desktop_config_snippet.json` included in this package

---

## Exporting Memories to Other AI Models

MemoryBridge can export your memory in a format compatible with ChatGPT, Gemini, and local models (Ollama). Ask Claude:

> "Export my memories for ChatGPT"

This gives you a formatted text block you can paste into a ChatGPT system prompt or conversation start.
