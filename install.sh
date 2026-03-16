#!/bin/bash
# cc-watchdog installer
# Installs the watchdog into ~/.claude/watchdog/ and wires up Claude Code hooks.
#
# Usage:
#   bash install.sh
#   bash install.sh --uninstall

set -e

WATCHDOG_DIR="$HOME/.claude/watchdog"
SETTINGS_FILE="$HOME/.claude/settings.json"
ZSHRC="$HOME/.zshrc"
BASHRC="$HOME/.bashrc"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}  →${NC} $1"; }
success() { echo -e "${GREEN}  ✓${NC} $1"; }
warn()    { echo -e "${YELLOW}  ⚠${NC} $1"; }
error()   { echo -e "${RED}  ✗${NC} $1"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Uninstall ─────────────────────────────────────────────────────────────────

if [[ "$1" == "--uninstall" ]]; then
  echo ""
  echo "  Uninstalling cc-watchdog..."

  # Stop daemon if running
  if [ -f "$WATCHDOG_DIR/watchdog.pid" ]; then
    PID=$(cat "$WATCHDOG_DIR/watchdog.pid" 2>/dev/null)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" && success "Stopped watchdog daemon (PID $PID)"
    fi
  fi

  # Remove watchdog dir
  rm -rf "$WATCHDOG_DIR" && success "Removed $WATCHDOG_DIR"

  # Remove shell function from rc files
  for RC in "$ZSHRC" "$BASHRC"; do
    if [ -f "$RC" ]; then
      # Remove the claude() function block
      python3 - <<PYEOF
import re, pathlib
path = pathlib.Path("$RC")
text = path.read_text()
# Remove the cc-watchdog block
cleaned = re.sub(
    r'\n# Claude Code.*?cc-watchdog.*?\n}\n?',
    '',
    text,
    flags=re.DOTALL
)
if cleaned != text:
    path.write_text(cleaned)
    print(f"  Removed claude() function from $RC")
PYEOF
    fi
  done

  # Remove SessionStart hook from settings.json
  if [ -f "$SETTINGS_FILE" ]; then
    python3 - <<PYEOF
import json, pathlib
path = pathlib.Path("$SETTINGS_FILE")
try:
    data = json.loads(path.read_text())
    hooks = data.get("hooks", {})
    ss = hooks.get("SessionStart", [])
    filtered = [
        h for h in ss
        if not any("watchdog" in str(cmd.get("command","")) for cmd in h.get("hooks",[]))
    ]
    if len(filtered) != len(ss):
        hooks["SessionStart"] = filtered
        path.write_text(json.dumps(data, indent=2))
        print("  Removed SessionStart hook from settings.json")
except Exception as e:
    print(f"  Could not update settings.json: {e}")
PYEOF
  fi

  echo ""
  success "cc-watchdog uninstalled."
  echo ""
  exit 0
fi

# ── Install ───────────────────────────────────────────────────────────────────

echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "    cc-watchdog — Claude Code context watchdog"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Prerequisite checks
if ! command -v python3 &>/dev/null; then
  error "python3 not found. Please install Python 3.9+."
  exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python $PYTHON_VERSION found"

if ! command -v claude &>/dev/null; then
  warn "claude CLI not found in PATH. Make sure Claude Code is installed."
fi

# 1. Copy files to ~/.claude/watchdog/
info "Installing files to $WATCHDOG_DIR..."
mkdir -p "$WATCHDOG_DIR"

cp "$SCRIPT_DIR/cc_watchdog.py" "$WATCHDOG_DIR/"
cp "$SCRIPT_DIR/statusline.py"  "$WATCHDOG_DIR/"
cp "$SCRIPT_DIR/ensure_running.sh" "$WATCHDOG_DIR/"
cp "$SCRIPT_DIR/claude_loop.sh" "$WATCHDOG_DIR/"
chmod +x "$WATCHDOG_DIR/ensure_running.sh"
chmod +x "$WATCHDOG_DIR/claude_loop.sh"
chmod +x "$WATCHDOG_DIR/cc_watchdog.py"

# Copy default config only if user doesn't already have one
if [ ! -f "$WATCHDOG_DIR/config.json" ]; then
  cp "$SCRIPT_DIR/config.default.json" "$WATCHDOG_DIR/config.json"
  success "Installed config.json (default settings)"
else
  warn "Existing config.json kept (not overwritten)"
fi

success "Files installed to $WATCHDOG_DIR"

# 2. Wire up SessionStart hook in settings.json
info "Configuring Claude Code SessionStart hook..."

if [ ! -f "$SETTINGS_FILE" ]; then
  mkdir -p "$(dirname "$SETTINGS_FILE")"
  echo '{}' > "$SETTINGS_FILE"
fi

python3 - <<PYEOF
import json, pathlib, sys

path = pathlib.Path("$SETTINGS_FILE")
try:
    data = json.loads(path.read_text()) if path.stat().st_size > 0 else {}
except Exception:
    data = {}

hook_cmd = "bash $WATCHDOG_DIR/ensure_running.sh"
new_hook = {"type": "command", "command": hook_cmd}

hooks = data.setdefault("hooks", {})
ss_hooks = hooks.setdefault("SessionStart", [])

# Check if already installed
for entry in ss_hooks:
    for h in entry.get("hooks", []):
        if "watchdog" in h.get("command", ""):
            print("  SessionStart hook already present — skipping")
            sys.exit(0)

# Add the hook
ss_hooks.append({"matcher": "", "hooks": [new_hook]})

path.write_text(json.dumps(data, indent=2))
print("  Added SessionStart hook to settings.json")
PYEOF

success "SessionStart hook configured"

# 3. Wire up status line in settings.json
info "Configuring status line..."

python3 - <<PYEOF
import json, pathlib

path = pathlib.Path("$SETTINGS_FILE")
data = json.loads(path.read_text())

if "statusLine" not in data:
    data["statusLine"] = {
        "type": "command",
        "command": "python3 $WATCHDOG_DIR/statusline.py"
    }
    path.write_text(json.dumps(data, indent=2))
    print("  Status line configured")
else:
    print("  Status line already configured — skipping")
PYEOF

success "Status line configured"

# 4. Add claude() restart function to shell rc
info "Adding claude() shell function..."

CLAUDE_FUNCTION='
# Claude Code — auto-restart loop (cc-watchdog exits at threshold, this resumes fresh)
claude() {
  while true; do
    command claude "$@"
    local code=$?
    [[ $code -eq 130 || $code -eq 2 ]] && break
    echo "[claude] Restarting with fresh context..."
    sleep 1
  done
}'

for RC in "$ZSHRC" "$BASHRC"; do
  if [ -f "$RC" ]; then
    if grep -q "cc-watchdog" "$RC" 2>/dev/null; then
      warn "claude() function already in $RC — skipping"
    else
      echo "$CLAUDE_FUNCTION" >> "$RC"
      success "Added claude() function to $RC"
    fi
  fi
done

# 5. Start the daemon
info "Starting cc-watchdog daemon..."
python3 "$WATCHDOG_DIR/cc_watchdog.py" ensure 2>/dev/null
sleep 1

if python3 "$WATCHDOG_DIR/cc_watchdog.py" status 2>/dev/null | grep -q "running"; then
  success "Daemon is running"
else
  warn "Daemon may not have started — run: python3 ~/.claude/watchdog/cc_watchdog.py start"
fi

echo ""
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo ""
echo "  Run this to activate the shell function:"
echo "    source ~/.zshrc   (or open a new terminal)"
echo ""
echo "  Then just use claude as normal:"
echo "    claude"
echo ""
echo "  At 50% context remaining, it will:"
echo "    1. Save progress to memory"
echo "    2. Exit Claude automatically"
echo "    3. Restart with fresh 200k context"
echo "    4. Load your previous task automatically"
echo ""
echo "  Check status:  python3 ~/.claude/watchdog/cc_watchdog.py status"
echo "  View logs:     tail -f ~/.claude/watchdog/watchdog.log"
echo "  Uninstall:     bash install.sh --uninstall"
echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
