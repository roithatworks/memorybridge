#!/bin/bash
# memorybridge nightly log groomer — replaces the Claude scheduled task
# "memorybridge-log-groomer" (retired 2026-09-06 to cut nightly token cost).
#
# Observe-and-report only. NEVER kills processes (see repo history: the old
# task killed live servers out from under sessions). Only write action is
# truncating server.error.log when it exceeds 50MB.
#
# Installed as launchd agent com.memorybridge.log-groomer (3:10 AM daily).
# Status: /Users/cale/memorybridge/logs/groomer-status.txt
# Alerts: macOS notification only when something needs attention.

DATA_DIR="${MEMORYBRIDGE_DATA:-/Users/cale/memorybridge}"
LOG="$DATA_DIR/logs/server.error.log"
HTTP_LOG="$DATA_DIR/logs/http-bridge.error.log"
STATUS="$DATA_DIR/logs/groomer-status.txt"
HISTORY="$DATA_DIR/logs/groomer-history.log"
MAX_BYTES=$((50 * 1024 * 1024))
PATH="/usr/bin:/bin:/usr/sbin:/sbin"

issues=()
notes=()

# 1. Log size
if [ -f "$LOG" ]; then
  size=$(stat -f%z "$LOG")
else
  size=0
  notes+=("error log not found")
fi
human=$(du -sh "$LOG" 2>/dev/null | cut -f1)
[ -z "$human" ] && human="0"

# 2. Crash signatures (before any truncation)
sigs=$(tail -200 "$LOG" 2>/dev/null \
  | grep -E 'Fatal Python error|Traceback|SIGTERM|MCP loop ended|Parent process gone|backfilled|embed failed' \
  | tail -20)
[ -n "$sigs" ] && issues+=("crash signatures in last 200 lines")

# 3. Truncate if oversized
for l in "$LOG" "$HTTP_LOG"; do
  if [ -f "$l" ]; then
    l_size=$(stat -f%z "$l")
    if [ "$l_size" -gt "$MAX_BYTES" ]; then
      : > "$l"
      issues+=("$(basename "$l") was oversized (>50MB) — truncated")
      echo "truncated $(basename "$l")"
    fi
  fi
done

# 3.5 Cleanup stale .bak DB files
bak_count=$(find "$DATA_DIR" -maxdepth 1 -name "memory.db.bak-*" -type f -mtime +13 2>/dev/null | wc -l | tr -d ' ')
if [ "$bak_count" -gt 0 ]; then
  find "$DATA_DIR" -maxdepth 1 -name "memory.db.bak-*" -type f -mtime +13 -delete 2>/dev/null
  echo "Deleted $bak_count stale .bak DB file(s)"
fi

# 4. HTTP bridge on :8484
bridge_pid=$(lsof -ti:8484 2>/dev/null | head -1)
if [ -z "$bridge_pid" ]; then
  issues+=("HTTP bridge DOWN (nothing on :8484)")
fi

# 5. Orphan server.py (PPID 1, not the bridge)
procs=$(ps -axo pid,ppid,command | grep '[s]erver.py' | grep -v disclaimer)
orphans=$(echo "$procs" | awk -v b="$bridge_pid" '$2==1 && $1!=b {print $1}')
if [ -n "$orphans" ]; then
  issues+=("orphan server.py (PPID 1, watchdog failed): $(echo $orphans | tr '\n' ' ')")
fi
proc_count=$(echo "$procs" | grep -c .)

# Write status
ts=$(date '+%Y-%m-%d %H:%M:%S')
{
  if [ ${#issues[@]} -eq 0 ]; then
    echo "$ts OK"
  else
    echo "$ts ATTENTION"
    for i in "${issues[@]}"; do echo "  - $i"; done
  fi
  echo "  log: $human | bridge pid: ${bridge_pid:-none} | server.py procs: $proc_count"
  for n in "${notes[@]}"; do echo "  note: $n"; done
  if [ -n "$sigs" ]; then
    echo "  signatures:"
    echo "$sigs" | sed 's/^/    /'
  fi
} > "$STATUS"

head -1 "$STATUS" >> "$HISTORY"

# Notify only on issues
if [ ${#issues[@]} -gt 0 ]; then
  msg=$(printf '%s; ' "${issues[@]}")
  msg=${msg%; }
  msg=${msg//\"/\'}
  osascript -e "display notification \"${msg:0:200}\" with title \"memorybridge groomer\" subtitle \"needs attention\"" 2>/dev/null
fi

exit 0
