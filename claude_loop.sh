#!/bin/bash
# claude_loop.sh — Run Claude Code in a restart loop.
#
# Use this instead of running `claude` directly.
# When the watchdog exits Claude at 50% context, this script
# immediately restarts it in the same directory with fresh context.
# The new session auto-loads PROGRESS.md from memory to resume the task.
#
# USAGE:
#   ~/.claude/watchdog/claude_loop.sh
#   ~/.claude/watchdog/claude_loop.sh /path/to/project
#
# TIP: Add an alias to your ~/.zshrc:
#   alias cl="~/.claude/watchdog/claude_loop.sh"

DIR="${1:-$(pwd)}"
cd "$DIR" || exit 1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Claude Loop — auto-restart on context exit"
echo "  Directory: $DIR"
echo "  Stop: Ctrl+C twice (or Ctrl+C inside Claude)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

RESTART_COUNT=0

while true; do
  if [ "$RESTART_COUNT" -gt 0 ]; then
    echo ""
    echo "[claude_loop] Restarting (session $RESTART_COUNT complete — fresh context loaded)"
    echo ""
    sleep 1
  fi

  claude
  EXIT_CODE=$?

  # Exit code 130 = Ctrl+C by user — stop the loop
  if [ $EXIT_CODE -eq 130 ] || [ $EXIT_CODE -eq 2 ]; then
    echo ""
    echo "[claude_loop] Stopped by user."
    break
  fi

  RESTART_COUNT=$((RESTART_COUNT + 1))
done
