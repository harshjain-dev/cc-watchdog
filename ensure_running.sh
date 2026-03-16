#!/usr/bin/env bash
# Called by Claude Code hooks on SessionStart.
# Ensures the watchdog daemon is running (only one instance).
PID_FILE="/Users/harsh.jain/.claude/watchdog/watchdog.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  exit 0  # Already running
fi
python3 "/Users/harsh.jain/.claude/watchdog/cc_watchdog.py" ensure 2>/dev/null &
