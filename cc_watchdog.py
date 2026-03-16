#!/usr/bin/env python3
"""
CC-Watchdog: Context-aware session monitor for Claude Code.

Runs as a background daemon, monitors all active Claude Code sessions,
and automatically saves progress when context usage crosses a threshold.

Usage:
    python cc_watchdog.py start          # Start the daemon
    python cc_watchdog.py stop           # Stop the daemon
    python cc_watchdog.py status         # Check if running
    python cc_watchdog.py check          # One-shot check of all sessions
    python cc_watchdog.py ensure         # Start only if not already running (used by hooks)
"""

import json
import os
import sys
import time
import signal
import subprocess
import glob
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ─── Configuration ────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # Context threshold: trigger save when remaining % drops below this
    "context_remaining_threshold": 50,

    # How often to check (seconds)
    "poll_interval": 15,

    # Maximum context window size (tokens) — fallback if not in transcript
    "default_max_context": 200_000,

    # Auto-compact buffer Claude Code reserves (~40-45k tokens)
    "autocompact_buffer": 42_000,

    # Whether to auto-stash git changes on threshold
    "git_stash_on_threshold": True,

    # Whether to send desktop notification
    "notify_on_threshold": True,

    # Whether to write progress to memory folder
    "write_progress_to_memory": True,

    # Whether to auto-exit the Claude process when threshold hit (triggers clean restart)
    "auto_exit_claude": True,

    # Log file location
    "log_file": "~/.claude/watchdog/watchdog.log",

    # PID file location
    "pid_file": "~/.claude/watchdog/watchdog.pid",

    # Sessions already handled (don't re-trigger)
    "handled_sessions_file": "~/.claude/watchdog/handled.json",
}


def load_config() -> dict:
    """Load config from ~/.claude/watchdog/config.json, merged with defaults."""
    config = DEFAULT_CONFIG.copy()
    config_path = Path("~/.claude/watchdog/config.json").expanduser()
    if config_path.exists():
        try:
            with open(config_path) as f:
                user_config = json.load(f)
            config.update(user_config)
        except (json.JSONDecodeError, IOError):
            pass
    return config


def expand_path(p: str) -> Path:
    return Path(p).expanduser()


# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg: str, config: dict = None):
    """Simple file + stderr logger."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"

    # Always print to stderr for debugging
    print(line, file=sys.stderr)

    # Also write to log file
    if config:
        log_path = expand_path(config["log_file"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(line + "\n")


# ─── Session Discovery ───────────────────────────────────────────────────────

def find_claude_projects_dir() -> Path:
    """Find the Claude Code projects directory."""
    return Path("~/.claude/projects").expanduser()


def find_active_sessions() -> list[dict]:
    """
    Find all active/recent Claude Code sessions by looking at transcript files.
    Returns list of dicts with session info.
    """
    projects_dir = find_claude_projects_dir()
    if not projects_dir.exists():
        return []

    sessions = []

    # Walk through all project directories
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if project_dir.name.startswith("."):
            continue

        # Find JSONL transcript files (these are the session files)
        for jsonl_file in project_dir.glob("*.jsonl"):
            # Check if recently modified (active session = modified in last 5 min)
            try:
                mtime = jsonl_file.stat().st_mtime
                age_seconds = time.time() - mtime
                if age_seconds > 300:  # Skip sessions idle for > 5 minutes
                    continue

                sessions.append({
                    "session_id": jsonl_file.stem,
                    "transcript_path": jsonl_file,
                    "project_dir": project_dir,
                    "project_name": project_dir.name,
                    "last_modified": mtime,
                    "age_seconds": age_seconds,
                })
            except OSError:
                continue

    return sessions


# ─── Context Calculation ─────────────────────────────────────────────────────

def calculate_context_usage(transcript_path: Path, config: dict) -> Optional[dict]:
    """
    Parse a JSONL transcript file and calculate context window usage.
    Returns dict with token counts and percentages, or None if can't determine.
    """
    try:
        content = transcript_path.read_text(encoding="utf-8", errors="ignore")
    except (IOError, OSError):
        return None

    lines = content.strip().split("\n")
    if not lines:
        return None

    most_recent_entry = None
    most_recent_time = None

    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip sidechain (subagent) entries
        if data.get("isSidechain", False):
            continue
        # Skip error messages
        if data.get("isApiErrorMessage", False):
            continue

        # Look for entries with usage data
        message = data.get("message", {})
        if not isinstance(message, dict):
            continue

        usage = message.get("usage")
        if not usage:
            continue

        timestamp = data.get("timestamp")
        if not timestamp:
            continue

        try:
            entry_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        if most_recent_time is None or entry_time > most_recent_time:
            most_recent_time = entry_time
            most_recent_entry = data

    if not most_recent_entry:
        return None

    usage = most_recent_entry["message"]["usage"]
    input_tokens = usage.get("input_tokens", 0)
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)

    # Context usage = all input token types (matching Claude Code's own calculation)
    total_context = input_tokens + cache_creation + cache_read
    max_context = config["default_max_context"]

    used_pct = min(100, (total_context / max_context) * 100)
    remaining_pct = max(0, 100 - used_pct)

    return {
        "total_context_tokens": total_context,
        "input_tokens": input_tokens,
        "cache_creation_tokens": cache_creation,
        "cache_read_tokens": cache_read,
        "max_context": max_context,
        "used_percentage": round(used_pct, 1),
        "remaining_percentage": round(remaining_pct, 1),
        "timestamp": most_recent_time.isoformat() if most_recent_time else None,
    }


# ─── Transcript Analysis ─────────────────────────────────────────────────────

def extract_progress_from_transcript(transcript_path: Path) -> dict:
    """
    Parse the transcript to extract:
    - The original user prompt/task
    - Files that were read/modified
    - A rough summary of actions taken
    """
    try:
        content = transcript_path.read_text(encoding="utf-8", errors="ignore")
    except (IOError, OSError):
        return {"original_prompt": "Unknown", "files_modified": [], "actions": []}

    lines = content.strip().split("\n")

    original_prompt = None
    files_modified = set()
    files_read = set()
    actions = []
    tool_calls = []

    for line in lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("isSidechain", False):
            continue

        msg = data.get("message", {})
        if not isinstance(msg, dict):
            continue

        role = msg.get("role") or data.get("type")

        # Extract the first user message as the original prompt
        if role == "user" and original_prompt is None:
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, str):
                original_prompt = content_blocks[:500]
            elif isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        original_prompt = block.get("text", "")[:500]
                        break
                    elif isinstance(block, str):
                        original_prompt = block[:500]
                        break

        # Extract tool calls to understand what was done
        if role == "assistant":
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue

                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})

                        if tool_name in ("Write", "Edit", "MultiEdit"):
                            fpath = tool_input.get("file_path", "")
                            if fpath:
                                files_modified.add(fpath)
                                actions.append(f"Modified: {fpath}")

                        elif tool_name == "Read":
                            fpath = tool_input.get("file_path", "")
                            if fpath:
                                files_read.add(fpath)

                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            if cmd and len(cmd) < 200:
                                actions.append(f"Ran: {cmd[:100]}")

                        tool_calls.append(tool_name)

    return {
        "original_prompt": original_prompt or "Could not extract original prompt",
        "files_modified": sorted(files_modified),
        "files_read": sorted(files_read),
        "actions": actions[-20:],  # Last 20 actions
        "total_tool_calls": len(tool_calls),
    }


# ─── Progress Saving ─────────────────────────────────────────────────────────

def resolve_working_directory(transcript_path: Path) -> Optional[Path]:
    """Try to find the actual working directory from the transcript."""
    try:
        content = transcript_path.read_text(encoding="utf-8", errors="ignore")
    except (IOError, OSError):
        return None

    for line in content.strip().split("\n"):
        try:
            data = json.loads(line)
            cwd = data.get("cwd")
            if cwd:
                p = Path(cwd)
                if p.exists():
                    return p
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def write_progress_to_memory(session: dict, context: dict, config: dict):
    """
    Write a PROGRESS.md file to the project's memory folder so the next
    session automatically picks up where this one left off.
    Also updates MEMORY.md so Claude auto-loads the progress on next start.
    """
    project_dir = session["project_dir"]
    memory_dir = project_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    progress = extract_progress_from_transcript(session["transcript_path"])

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    content = f"""# Session Progress (Auto-saved by CC-Watchdog)

**Saved at:** {now}
**Reason:** Context usage reached {context['used_percentage']}% ({context['remaining_percentage']}% remaining)
**Session ID:** {session['session_id']}

## Original Task

{progress['original_prompt']}

## Files Modified This Session

{chr(10).join(f'- {f}' for f in progress['files_modified']) or '- None detected'}

## Files Read This Session

{chr(10).join(f'- {f}' for f in progress['files_read'][:15]) or '- None detected'}

## Recent Actions (last 20)

{chr(10).join(f'- {a}' for a in progress['actions']) or '- None detected'}

## Resume Instructions

Context was saved at 50% remaining. This is a fresh session with full context.
Continue the original task described above from where it left off.
Check current state of modified files before making further changes.
If git stash was used, run `git stash pop` to restore work-in-progress changes.

**Total tool calls this session:** {progress['total_tool_calls']}

## Previous Conversation Transcript

The full conversation from the terminated session is available at:

```
{session['transcript_path']}
```

Read this file if you need to recover reasoning, decisions, or context that
is not captured in the structured summary above. Use selectively — it is large.
"""

    progress_path = memory_dir / "PROGRESS.md"
    progress_path.write_text(content, encoding="utf-8")
    log(f"  Wrote progress to {progress_path}", config)

    # Update MEMORY.md index so Claude auto-loads this on next session start
    memory_index_path = memory_dir / "MEMORY.md"
    memory_index_content = f"""# Memory Index

This file is automatically loaded at the start of every Claude Code session.

## Active Context

- [PROGRESS.md](PROGRESS.md) — Session progress saved {now} (context was at {context['used_percentage']}% used). Read this first to resume the previous task.
"""
    memory_index_path.write_text(memory_index_content, encoding="utf-8")
    log(f"  Updated MEMORY.md index at {memory_index_path}", config)


def find_claude_pid_for_dir(work_dir: Path) -> Optional[int]:
    """
    Find the PID of the claude process whose working directory matches work_dir.
    Uses lsof on macOS to check process cwd.
    """
    try:
        # Use `ps` instead of `pgrep` — pgrep can't see the Claude process on macOS
        # (likely due to entitlements). `ps -eo pid,comm` is reliable.
        result = subprocess.run(
            ["ps", "-eo", "pid,comm"],
            capture_output=True, text=True, timeout=5
        )
        pids = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[1] == "claude" and parts[0].isdigit():
                pids.append(int(parts[0]))
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None

    work_dir_str = str(work_dir.resolve())

    for pid in pids:
        try:
            result = subprocess.run(
                ["lsof", "-p", str(pid), "-a", "-d", "cwd"],
                capture_output=True, text=True, timeout=5
            )
            # lsof output has the cwd path at the end of the cwd line
            for line in result.stdout.splitlines():
                if "cwd" in line and work_dir_str in line:
                    return pid
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    return None


def exit_claude_session(session: dict, config: dict) -> bool:
    """
    Gracefully exit the Claude Code process for this session.
    Sends SIGTERM so Claude can flush state before exiting.
    """
    work_dir = resolve_working_directory(session["transcript_path"])
    if not work_dir:
        log("  Could not determine working directory, skipping auto-exit", config)
        return False

    pid = find_claude_pid_for_dir(work_dir)
    if not pid:
        log(f"  Could not find Claude process for {work_dir}, skipping auto-exit", config)
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log(f"  Sent SIGTERM to Claude process PID {pid} — session will restart fresh", config)
        return True
    except ProcessLookupError:
        log(f"  Claude process {pid} already gone", config)
        return False
    except PermissionError:
        log(f"  Permission denied killing PID {pid}", config)
        return False


def git_stash_changes(session: dict, config: dict) -> bool:
    """Stash any uncommitted changes in the working directory."""
    work_dir = resolve_working_directory(session["transcript_path"])
    if not work_dir:
        log("  Could not determine working directory for git stash", config)
        return False

    # Check if it's a git repo
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=work_dir, capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            log(f"  {work_dir} is not a git repo, skipping stash", config)
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    # Check if there are changes to stash
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=work_dir, capture_output=True, text=True, timeout=5
        )
        if not result.stdout.strip():
            log("  No changes to stash", config)
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    # Stash with descriptive message
    stash_msg = f"cc-watchdog auto-stash: context at {datetime.now().strftime('%H:%M:%S')}"
    try:
        result = subprocess.run(
            ["git", "stash", "push", "-m", stash_msg, "--include-untracked"],
            cwd=work_dir, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log(f"  Git stash saved: {stash_msg}", config)
            return True
        else:
            log(f"  Git stash failed: {result.stderr}", config)
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def send_notification(session: dict, context: dict, config: dict):
    """Send a desktop notification to alert the user."""
    title = "CC-Watchdog: Context threshold reached"
    msg = (
        f"Session in {session['project_name']} is at "
        f"{context['used_percentage']}% context usage. "
        f"Progress has been saved to memory."
    )

    # Try notify-send (Linux)
    try:
        subprocess.run(
            ["notify-send", title, msg],
            capture_output=True, timeout=3
        )
        return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try osascript (macOS)
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            capture_output=True, timeout=3
        )
        return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: terminal bell
    print("\a", end="", flush=True)


# ─── Handled Sessions Tracking ───────────────────────────────────────────────

def load_handled_sessions(config: dict) -> set:
    """Load the set of session IDs we've already triggered on."""
    path = expand_path(config["handled_sessions_file"])
    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            return set(data.get("sessions", []))
        except (json.JSONDecodeError, IOError):
            pass
    return set()


def mark_session_handled(session_id: str, config: dict):
    """Mark a session as handled so we don't re-trigger."""
    path = expand_path(config["handled_sessions_file"])
    path.parent.mkdir(parents=True, exist_ok=True)

    handled = load_handled_sessions(config)
    handled.add(session_id)

    # Keep only the last 200 sessions to avoid unbounded growth
    if len(handled) > 200:
        handled = set(sorted(handled)[-200:])

    with open(path, "w") as f:
        json.dump({"sessions": sorted(handled)}, f, indent=2)


# ─── Main Loop ────────────────────────────────────────────────────────────────

def check_all_sessions(config: dict) -> list[dict]:
    """
    One-shot check of all active sessions.
    Returns list of sessions that crossed the threshold.
    """
    sessions = find_active_sessions()
    handled = load_handled_sessions(config)
    triggered = []

    for session in sessions:
        sid = session["session_id"]

        # Skip already-handled sessions
        if sid in handled:
            continue

        context = calculate_context_usage(session["transcript_path"], config)
        if not context:
            continue

        remaining = context["remaining_percentage"]
        threshold = config["context_remaining_threshold"]

        log(
            f"  Session {sid[:12]}... in {session['project_name']}: "
            f"{context['used_percentage']}% used, {remaining}% remaining",
            config
        )

        if remaining <= threshold:
            log(
                f"  ⚠ THRESHOLD REACHED: {remaining}% remaining "
                f"(threshold: {threshold}%)",
                config
            )
            triggered.append(session)

            # Save progress to memory (writes PROGRESS.md + MEMORY.md index)
            if config["write_progress_to_memory"]:
                write_progress_to_memory(session, context, config)

            # Git stash
            if config["git_stash_on_threshold"]:
                git_stash_changes(session, config)

            # Notify
            if config["notify_on_threshold"]:
                send_notification(session, context, config)

            # Mark as handled before exit so we don't trigger again on same session
            mark_session_handled(sid, config)

            # Auto-exit Claude so the loop wrapper can restart with fresh context
            if config.get("auto_exit_claude", False):
                # Small delay so notification fires and memory writes flush
                time.sleep(2)
                exit_claude_session(session, config)

    return triggered


def daemon_loop(config: dict):
    """Main daemon loop — runs forever, polling sessions."""
    log("CC-Watchdog daemon started", config)
    log(f"  Threshold: {config['context_remaining_threshold']}% remaining", config)
    log(f"  Poll interval: {config['poll_interval']}s", config)

    while True:
        try:
            check_all_sessions(config)
        except Exception as e:
            log(f"Error in check loop: {e}", config)

        time.sleep(config["poll_interval"])


# ─── Daemon Management ───────────────────────────────────────────────────────

def write_pid(config: dict):
    pid_path = expand_path(config["pid_file"])
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def read_pid(config: dict) -> Optional[int]:
    pid_path = expand_path(config["pid_file"])
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text().strip())
        # Check if process is still running
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pid_path.unlink(missing_ok=True)
        return None


def remove_pid(config: dict):
    pid_path = expand_path(config["pid_file"])
    pid_path.unlink(missing_ok=True)


def start_daemon(config: dict):
    """Start the watchdog as a background daemon."""
    existing_pid = read_pid(config)
    if existing_pid:
        print(f"Watchdog is already running (PID {existing_pid})")
        return

    # Fork into background
    pid = os.fork()
    if pid > 0:
        # Parent process
        print(f"CC-Watchdog started (PID {pid})")
        return

    # Child process — become daemon
    os.setsid()

    # Silence stdout/stderr — log() already writes to the log file directly.
    # Redirecting stderr to the log AND writing explicitly would duplicate every line.
    devnull = open(os.devnull, "w")
    os.dup2(devnull.fileno(), sys.stdout.fileno())
    os.dup2(devnull.fileno(), sys.stderr.fileno())

    write_pid(config)

    def handle_shutdown(signum, frame):
        log("CC-Watchdog stopping (signal received)", config)
        remove_pid(config)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    daemon_loop(config)


def stop_daemon(config: dict):
    """Stop the running watchdog daemon."""
    pid = read_pid(config)
    if not pid:
        print("Watchdog is not running")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Watchdog stopped (PID {pid})")
        remove_pid(config)
    except ProcessLookupError:
        print("Watchdog process not found, cleaning up")
        remove_pid(config)


def show_status(config: dict):
    """Show current watchdog status and session overview."""
    pid = read_pid(config)
    if pid:
        print(f"✓ CC-Watchdog is running (PID {pid})")
    else:
        print("✗ CC-Watchdog is not running")

    print(f"\nConfiguration:")
    print(f"  Context threshold: {config['context_remaining_threshold']}% remaining")
    print(f"  Poll interval:     {config['poll_interval']}s")
    print(f"  Git stash:         {'enabled' if config['git_stash_on_threshold'] else 'disabled'}")
    print(f"  Notifications:     {'enabled' if config['notify_on_threshold'] else 'disabled'}")
    print(f"  Memory progress:   {'enabled' if config['write_progress_to_memory'] else 'disabled'}")

    print(f"\nActive sessions:")
    sessions = find_active_sessions()
    if not sessions:
        print("  No active sessions found")
    else:
        for s in sessions:
            ctx = calculate_context_usage(s["transcript_path"], config)
            if ctx:
                bar_len = 20
                filled = int(bar_len * ctx["used_percentage"] / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                status = "⚠ " if ctx["remaining_percentage"] <= config["context_remaining_threshold"] else "  "
                print(
                    f"  {status}{s['session_id'][:12]}... "
                    f"[{bar}] {ctx['used_percentage']}% used "
                    f"({ctx['remaining_percentage']}% remaining)"
                )
            else:
                print(f"    {s['session_id'][:12]}... (no usage data yet)")

    # Show handled sessions count
    handled = load_handled_sessions(config)
    print(f"\nSessions auto-saved: {len(handled)}")


def ensure_running(config: dict):
    """Start daemon only if not already running. Used by hooks."""
    pid = read_pid(config)
    if pid:
        return  # Already running, do nothing
    start_daemon(config)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    config = load_config()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "start":
        start_daemon(config)
    elif command == "stop":
        stop_daemon(config)
    elif command == "status":
        show_status(config)
    elif command == "check":
        triggered = check_all_sessions(config)
        if triggered:
            print(f"\n⚠ {len(triggered)} session(s) crossed the threshold")
        else:
            print("\n✓ All sessions within threshold")
    elif command == "ensure":
        ensure_running(config)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
