#!/usr/bin/env bash
# memorybridge log groomer
# Maintenance script: checks log size, scans for crash signatures, reports process health.
# Safe to run at any frequency — observe-and-report only, never kills processes.
#
# Wired up as a nightly Cowork scheduled task (memorybridge-log-groomer).

set -euo pipefail

LOG="/Users/cale/memorybridge/logs/server.error.log"
LOG_LIMIT_MB=50
BRIDGE_PORT=8484

# --- 1. Check log size ---
if [ ! -f "$LOG" ]; then
  echo "Log not found at $LOG — nothing to do."
  exit 0
fi

LOG_SIZE_HUMAN=$(du -sh "$LOG" | cut -f1)
LOG_SIZE_MB=$(du -sm "$LOG" | cut -f1)

# --- 2. Scan last 200 lines for crash signatures BEFORE any truncation ---
CRASH_HITS=$(tail -200 "$LOG" 2>/dev/null \
  | grep -E 'Fatal Python error|Traceback|SIGTERM|MCP loop ended|Parent process gone|backfilled|embed failed' \
  | tail -20 || true)

# --- 3. Truncate if over limit ---
TRUNCATED=false
if [ "$LOG_SIZE_MB" -gt "$LOG_LIMIT_MB" ]; then
  > "$LOG"
  TRUNCATED=true
fi

# --- 4. Process health check ---
# Identify the HTTP bridge PID (legitimately runs with PROC_PPID 1)
BRIDGE_PID=$(lsof -ti:$BRIDGE_PORT 2>/dev/null || true)

# All server.py processes (excluding disclaimer wrappers)
PROCS=$(ps -axo pid,ppid,command | grep '[s]erver.py' | grep -v disclaimer || true)

ORPHANS=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  PID=$(echo "$line" | awk '{print $1}')
  PROC_PPID=$(echo "$line" | awk '{print $2}')
  # PROC_PPID 1 + not the bridge = orphan that survived the watchdog
  if [ "$PROC_PPID" = "1" ] && [ "$PID" != "$BRIDGE_PID" ]; then
    ORPHANS="${ORPHANS}\nORPHAN DETECTED: PID $PID (PROC_PPID 1, not bridge) — watchdog failed to clean up"
  fi
done <<< "$PROCS"

PROC_COUNT=$(echo "$PROCS" | grep -c '.' || true)

# --- 5. Report ---
ISSUES=false

if [ -n "$CRASH_HITS" ]; then
  ISSUES=true
  echo "=== CRASH SIGNATURES FOUND ==="
  echo "$CRASH_HITS"
fi

if [ -z "$BRIDGE_PID" ]; then
  ISSUES=true
  echo "WARNING: HTTP bridge is DOWN (nothing listening on port $BRIDGE_PORT)"
fi

if [ -n "$ORPHANS" ]; then
  ISSUES=true
  printf "%b\n" "$ORPHANS"
fi

if $TRUNCATED; then
  ISSUES=true
  echo "Log truncated: was $LOG_SIZE_HUMAN (over ${LOG_LIMIT_MB}MB limit)"
fi

if $ISSUES; then
  echo ""
  BRIDGE_STATUS="${BRIDGE_PID:-NONE}"
  AFTER_STATUS=$($TRUNCATED && echo 'truncated' || echo 'unchanged')
  echo "Summary: log $LOG_SIZE_HUMAN -> $AFTER_STATUS, $PROC_COUNT server.py process(es), bridge PID=$BRIDGE_STATUS"
fi

# Silence = all clear
