#!/usr/bin/env python3
"""
CC-Watchdog status line integration for Claude Code.

Reads Claude Code's status line JSON from stdin and displays
context usage with a visual indicator. Changes color as usage increases.

Setup: Add to your Claude Code settings:
  "statusLine": "python3 ~/.claude/watchdog/statusline.py"
"""

import json
import sys

def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, EOFError):
        print("ccw: --")
        return

    ctx = data.get("context_window", {})
    used_pct = ctx.get("used_percentage")
    remaining_pct = ctx.get("remaining_percentage")

    if used_pct is None:
        # Fallback: try to calculate from tokens
        total_in = ctx.get("total_input_tokens", 0)
        window_size = ctx.get("context_window_size", 200000)
        if total_in and window_size:
            used_pct = round((total_in / window_size) * 100)
            remaining_pct = 100 - used_pct
        else:
            print("ccw: waiting...")
            return

    # Load threshold from config
    threshold = 40
    try:
        import pathlib
        config_path = pathlib.Path("~/.claude/watchdog/config.json").expanduser()
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            threshold = cfg.get("context_remaining_threshold", 40)
    except Exception:
        pass

    # Build visual bar (10 chars wide)
    bar_len = 10
    filled = int(bar_len * used_pct / 100)
    bar = "▓" * filled + "░" * (bar_len - filled)

    # Status indicator
    if remaining_pct <= threshold:
        indicator = "⚠"
        label = "SAVE SOON"
    elif remaining_pct <= threshold + 15:
        indicator = "●"
        label = f"{remaining_pct}% left"
    else:
        indicator = "○"
        label = f"{remaining_pct}% left"

    # Cost info if available
    cost = data.get("cost", {})
    cost_str = ""
    if cost.get("total_cost_usd"):
        cost_str = f" ${cost['total_cost_usd']:.2f}"

    print(f"{indicator} [{bar}] {label}{cost_str}")


if __name__ == "__main__":
    main()
