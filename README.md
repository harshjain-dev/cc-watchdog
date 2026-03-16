# cc-watchdog

**Automatic context management for Claude Code** — exits at 50% context remaining, saves your progress to memory, and restarts with a full 200k token window. Zero manual intervention.

---

## The Problem

Claude Code has a 200k token context window. When it fills up, Claude auto-compacts — compressing the conversation and losing resolution on older turns. By that point you've usually spent a lot of context on back-and-forth exploration that's now gone.

The better approach: **exit early, at 50% remaining**, save exactly what matters to memory, and restart fresh. The new session reads the saved progress and picks up exactly where you left off — with full context available.

---

## How It Works

```
You type: claude

         ┌─────────────────────────────┐
         │   claude() shell function   │
         │   (restart loop)            │
         └──────────────┬──────────────┘
                        │ starts
                        ▼
         ┌─────────────────────────────┐
         │     Claude Code session     │
         │                             │
         │  Context: [████████░░] 60%  │◄─── you work here
         └──────────────┬──────────────┘
                        │ context hits 50% remaining
                        ▼
         ┌─────────────────────────────┐
         │     cc-watchdog daemon      │ ← polls every 15s
         │                             │
         │  1. Writes PROGRESS.md      │
         │  2. Writes MEMORY.md index  │
         │  3. Git stash (if changes)  │
         │  4. Sends SIGTERM to claude │
         └──────────────┬──────────────┘
                        │ claude exits
                        ▼
         ┌─────────────────────────────┐
         │   claude() shell function   │
         │   sees exit, restarts       │
         └──────────────┬──────────────┘
                        │ new session
                        ▼
         ┌─────────────────────────────┐
         │  New Claude Code session    │
         │                             │
         │  Context: [░░░░░░░░░░] 0%   │ ← fresh 200k tokens
         │  Reads MEMORY.md → loads   │
         │  PROGRESS.md → resumes task │
         └─────────────────────────────┘
```

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/cc-watchdog
cd cc-watchdog
bash install.sh
source ~/.zshrc   # or open a new terminal
```

That's it. `claude` now runs with automatic context management.

### What the installer does

1. Copies files to `~/.claude/watchdog/`
2. Adds a `SessionStart` hook to `~/.claude/settings.json` (starts daemon on every Claude session)
3. Configures the status line widget in `~/.claude/settings.json`
4. Adds the `claude()` restart function to your `~/.zshrc` / `~/.bashrc`
5. Starts the daemon

### Uninstall

```bash
bash install.sh --uninstall
source ~/.zshrc
```

---

## Usage

Just use `claude` as you normally would:

```bash
cd your-project
claude
```

When context hits 50% remaining, the watchdog handles everything automatically. You'll see:

```
[claude] Restarting with fresh context...
```

The new session opens and immediately continues your task.

---

## Configuration

Edit `~/.claude/watchdog/config.json`:

```json
{
    "context_remaining_threshold": 50,
    "auto_exit_claude": true,
    "poll_interval": 15,
    "default_max_context": 200000,
    "git_stash_on_threshold": true,
    "notify_on_threshold": true,
    "write_progress_to_memory": true
}
```

| Option | Default | Description |
|---|---|---|
| `context_remaining_threshold` | `50` | Exit when context remaining drops below this % |
| `auto_exit_claude` | `true` | Kill the Claude process at threshold (enables auto-restart) |
| `poll_interval` | `15` | Seconds between context checks |
| `default_max_context` | `200000` | Token limit fallback (Claude's current limit) |
| `git_stash_on_threshold` | `true` | Auto-stash uncommitted changes before exit |
| `notify_on_threshold` | `true` | macOS desktop notification at threshold |
| `write_progress_to_memory` | `true` | Write PROGRESS.md + MEMORY.md for next session |

---

## Status Line Widget

The included `statusline.py` adds a context usage bar to your Claude Code status line:

```
○ [████░░░░░░] 65% left
⚠ [██████████] SAVE SOON   ← at threshold
```

Configured automatically by the installer. To set it manually, add to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/watchdog/statusline.py"
  }
}
```

---

## CLI Commands

```bash
python3 ~/.claude/watchdog/cc_watchdog.py status   # Show daemon status + active sessions
python3 ~/.claude/watchdog/cc_watchdog.py start    # Start daemon manually
python3 ~/.claude/watchdog/cc_watchdog.py stop     # Stop daemon
python3 ~/.claude/watchdog/cc_watchdog.py check    # One-shot check right now
```

View logs:

```bash
tail -f ~/.claude/watchdog/watchdog.log
```

---

## What Gets Saved

When the threshold is hit, the watchdog writes two files to the project's memory directory (`~/.claude/projects/[project]/memory/`):

**`PROGRESS.md`** — session snapshot:
- Original task / first user message
- All files modified this session
- All files read this session
- Last 20 actions taken
- Total tool call count

**`MEMORY.md`** — index file Claude auto-loads:
- Points to PROGRESS.md
- Claude reads this on startup and knows to resume the task

---

## Requirements

- macOS or Linux
- Python 3.9+
- [Claude Code](https://claude.ai/code) CLI installed
- `lsof` (pre-installed on macOS; `apt install lsof` on Linux)

---

## Running Tests

```bash
python3 tests/test_watchdog.py
```

Or with pytest:

```bash
pip install pytest
pytest tests/ -v
```

19 tests covering: config loading, context calculation, threshold logic, progress writing, MEMORY.md generation, handled session deduplication, PID management, and the full end-to-end trigger flow.

---

## Contributing

PRs welcome. Areas where contributions are especially useful:

- **Windows support** — the PID/lsof logic is macOS/Linux only
- **Context window size detection** — auto-detect from transcript instead of config
- **Smarter progress summaries** — LLM-generated summary instead of raw action list
- **Multiple session support** — handle multiple concurrent Claude windows
- **Test coverage** — more edge cases for the auto-exit flow

---

## How the Claude() Function Works

The `claude()` shell function (added to your `~/.zshrc`) wraps the real `claude` binary:

```bash
claude() {
  while true; do
    command claude "$@"      # run real binary
    local code=$?
    [[ $code -eq 130 || $code -eq 2 ]] && break  # Ctrl+C = stop
    echo "[claude] Restarting with fresh context..."
    sleep 1
  done
}
```

- `command claude` bypasses the function and calls the real binary
- Exit code 130 = Ctrl+C (user quit intentionally) → stops the loop
- Any other exit (including SIGTERM from watchdog) → restarts Claude

---

## License

MIT
