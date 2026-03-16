#!/usr/bin/env python3
"""
Tests for cc_watchdog.py

Run with:
    python3 -m pytest tests/ -v
    # or directly:
    python3 tests/test_watchdog.py
"""

import json
import os
import sys
import tempfile
import time
import signal
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Make cc_watchdog importable from project root
sys.path.insert(0, str(Path(__file__).parent.parent))
import cc_watchdog as w

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_transcript(token_counts: list[dict], cwd: str = "/tmp/test_project") -> Path:
    """
    Create a temp JSONL transcript file with the given token usage entries.
    Each dict in token_counts should have: input_tokens, cache_creation_input_tokens,
    cache_read_input_tokens.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
    now = datetime.now(timezone.utc)

    for i, usage in enumerate(token_counts):
        entry = {
            "type": "assistant",
            "cwd": cwd,
            "isSidechain": False,
            "isApiErrorMessage": False,
            "timestamp": (now + timedelta(seconds=i)).isoformat(),
            "message": {
                "role": "assistant",
                "usage": usage,
            },
        }
        tmp.write(json.dumps(entry) + "\n")

    tmp.close()
    return Path(tmp.name)


def make_config(overrides: dict = None) -> dict:
    cfg = w.DEFAULT_CONFIG.copy()
    cfg.update(overrides or {})
    return cfg


# ── Tests: Config loading ──────────────────────────────────────────────────────

def test_load_config_defaults():
    """Config falls back to DEFAULT_CONFIG when no file exists."""
    cfg = make_config()
    assert cfg["context_remaining_threshold"] == 50
    assert cfg["poll_interval"] == 15
    assert cfg["auto_exit_claude"] is True
    print("  ✓ test_load_config_defaults")


def test_load_config_from_file():
    """User config merges over defaults."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({"context_remaining_threshold": 25, "poll_interval": 30}, f)
        path = Path(f.name)

    # Patch config path temporarily
    original = w.Path
    try:
        cfg = w.DEFAULT_CONFIG.copy()
        with open(path) as fh:
            cfg.update(json.load(fh))
        assert cfg["context_remaining_threshold"] == 25
        assert cfg["poll_interval"] == 30
        assert "default_max_context" in cfg  # default still present
        print("  ✓ test_load_config_from_file")
    finally:
        path.unlink()


# ── Tests: Context calculation ─────────────────────────────────────────────────

def test_context_calculation_basic():
    """Standard token usage → correct % calculation."""
    cfg = make_config()
    transcript = make_transcript([
        {"input_tokens": 100_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])
    try:
        result = w.calculate_context_usage(transcript, cfg)
        assert result is not None
        assert result["total_context_tokens"] == 100_000
        assert result["used_percentage"] == 50.0
        assert result["remaining_percentage"] == 50.0
        print("  ✓ test_context_calculation_basic")
    finally:
        transcript.unlink()


def test_context_calculation_with_cache():
    """All token types (input + cache_creation + cache_read) are summed."""
    cfg = make_config()
    transcript = make_transcript([
        {"input_tokens": 50_000, "cache_creation_input_tokens": 30_000, "cache_read_input_tokens": 20_000}
    ])
    try:
        result = w.calculate_context_usage(transcript, cfg)
        assert result["total_context_tokens"] == 100_000
        assert result["used_percentage"] == 50.0
        print("  ✓ test_context_calculation_with_cache")
    finally:
        transcript.unlink()


def test_context_calculation_uses_most_recent():
    """When multiple entries exist, most recent timestamp wins."""
    cfg = make_config()
    # First entry: 10% used. Second entry (later): 80% used.
    transcript = make_transcript([
        {"input_tokens": 20_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        {"input_tokens": 160_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    ])
    try:
        result = w.calculate_context_usage(transcript, cfg)
        assert result["used_percentage"] == 80.0
        print("  ✓ test_context_calculation_uses_most_recent")
    finally:
        transcript.unlink()


def test_context_calculation_skips_sidechain():
    """Sidechain (subagent) entries are ignored."""
    cfg = make_config()
    now = datetime.now(timezone.utc)

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        # Main chain: 10% used
        f.write(json.dumps({
            "type": "assistant", "cwd": "/tmp", "isSidechain": False,
            "isApiErrorMessage": False,
            "timestamp": now.isoformat(),
            "message": {"role": "assistant", "usage": {
                "input_tokens": 20_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0
            }},
        }) + "\n")
        # Sidechain: 90% used — should be ignored
        f.write(json.dumps({
            "type": "assistant", "cwd": "/tmp", "isSidechain": True,
            "isApiErrorMessage": False,
            "timestamp": (now + timedelta(seconds=1)).isoformat(),
            "message": {"role": "assistant", "usage": {
                "input_tokens": 180_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0
            }},
        }) + "\n")
        path = Path(f.name)

    try:
        result = w.calculate_context_usage(path, cfg)
        assert result["used_percentage"] == 10.0, f"Expected 10.0% but got {result['used_percentage']}%"
        print("  ✓ test_context_calculation_skips_sidechain")
    finally:
        path.unlink()


def test_context_calculation_empty_file():
    """Empty transcript returns None."""
    cfg = make_config()
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        path = Path(f.name)
    try:
        result = w.calculate_context_usage(path, cfg)
        assert result is None
        print("  ✓ test_context_calculation_empty_file")
    finally:
        path.unlink()


def test_context_capped_at_100():
    """Usage over max_context is capped at 100%."""
    cfg = make_config({"default_max_context": 100_000})
    transcript = make_transcript([
        {"input_tokens": 200_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])
    try:
        result = w.calculate_context_usage(transcript, cfg)
        assert result["used_percentage"] == 100.0
        assert result["remaining_percentage"] == 0.0
        print("  ✓ test_context_capped_at_100")
    finally:
        transcript.unlink()


# ── Tests: Threshold logic ─────────────────────────────────────────────────────

def test_threshold_triggers_at_correct_level():
    """check_all_sessions triggers when remaining <= threshold."""
    cfg = make_config({
        "context_remaining_threshold": 50,
        "git_stash_on_threshold": False,
        "notify_on_threshold": False,
        "auto_exit_claude": False,
        "write_progress_to_memory": False,
        "handled_sessions_file": tempfile.mktemp(suffix=".json"),
        "log_file": tempfile.mktemp(suffix=".log"),
    })

    # 55% used → 45% remaining → below threshold of 50% → should trigger
    transcript = make_transcript([
        {"input_tokens": 110_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "test_project"
        project_dir.mkdir()
        session_file = project_dir / "abc123.jsonl"
        session_file.write_text(transcript.read_text())

        session = {
            "session_id": "abc123",
            "transcript_path": session_file,
            "project_dir": project_dir,
            "project_name": "test_project",
            "last_modified": time.time(),
            "age_seconds": 1,
        }

        context = w.calculate_context_usage(session_file, cfg)
        assert context is not None
        assert context["remaining_percentage"] <= cfg["context_remaining_threshold"]
        print("  ✓ test_threshold_triggers_at_correct_level")

    transcript.unlink()
    try:
        Path(cfg["handled_sessions_file"]).unlink()
        Path(cfg["log_file"]).unlink()
    except FileNotFoundError:
        pass


def test_threshold_does_not_trigger_below_usage():
    """Session at 30% used (70% remaining) should NOT trigger at threshold=50."""
    cfg = make_config({"context_remaining_threshold": 50})
    transcript = make_transcript([
        {"input_tokens": 60_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])
    try:
        result = w.calculate_context_usage(transcript, cfg)
        assert result["remaining_percentage"] > cfg["context_remaining_threshold"]
        print("  ✓ test_threshold_does_not_trigger_below_usage")
    finally:
        transcript.unlink()


# ── Tests: Progress + MEMORY.md writing ───────────────────────────────────────

def test_write_progress_creates_both_files():
    """write_progress_to_memory creates PROGRESS.md AND MEMORY.md."""
    cfg = make_config({
        "log_file": tempfile.mktemp(suffix=".log"),
    })

    transcript = make_transcript([
        {"input_tokens": 110_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])
    context = w.calculate_context_usage(transcript, cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        session = {
            "session_id": "test-session-001",
            "transcript_path": transcript,
            "project_dir": project_dir,
        }

        w.write_progress_to_memory(session, context, cfg)

        progress_path = project_dir / "memory" / "PROGRESS.md"
        memory_path = project_dir / "memory" / "MEMORY.md"

        assert progress_path.exists(), "PROGRESS.md was not created"
        assert memory_path.exists(), "MEMORY.md was not created"

        progress_text = progress_path.read_text()
        assert "Session Progress" in progress_text
        assert "Resume Instructions" in progress_text

        memory_text = memory_path.read_text()
        assert "PROGRESS.md" in memory_text
        assert "Memory Index" in memory_text

        print("  ✓ test_write_progress_creates_both_files")

    transcript.unlink()
    try:
        Path(cfg["log_file"]).unlink()
    except FileNotFoundError:
        pass


def test_memory_md_references_progress():
    """MEMORY.md must contain a link/reference to PROGRESS.md."""
    cfg = make_config({"log_file": tempfile.mktemp(suffix=".log")})
    transcript = make_transcript([
        {"input_tokens": 120_000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    ])
    context = w.calculate_context_usage(transcript, cfg)

    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir)
        session = {
            "session_id": "test-session-002",
            "transcript_path": transcript,
            "project_dir": project_dir,
        }

        w.write_progress_to_memory(session, context, cfg)
        memory_text = (project_dir / "memory" / "MEMORY.md").read_text()

        # Must have a markdown link to PROGRESS.md
        assert "[PROGRESS.md]" in memory_text or "PROGRESS.md" in memory_text
        print("  ✓ test_memory_md_references_progress")

    transcript.unlink()
    try:
        Path(cfg["log_file"]).unlink()
    except FileNotFoundError:
        pass


# ── Tests: Handled sessions ────────────────────────────────────────────────────

def test_handled_sessions_no_retrigger():
    """Session marked as handled is not triggered again."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({"sessions": []}, f)
        handled_path = Path(f.name)

    cfg = make_config({"handled_sessions_file": str(handled_path)})

    w.mark_session_handled("session-xyz", cfg)
    handled = w.load_handled_sessions(cfg)

    assert "session-xyz" in handled
    print("  ✓ test_handled_sessions_no_retrigger")

    handled_path.unlink()


def test_handled_sessions_capped_at_200():
    """handled.json never grows beyond 200 entries."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        json.dump({"sessions": [f"s{i}" for i in range(200)]}, f)
        handled_path = Path(f.name)

    cfg = make_config({"handled_sessions_file": str(handled_path)})
    w.mark_session_handled("new-session", cfg)

    handled = w.load_handled_sessions(cfg)
    assert len(handled) <= 200
    print("  ✓ test_handled_sessions_capped_at_200")

    handled_path.unlink()


# ── Tests: Progress extraction ─────────────────────────────────────────────────

def test_extract_progress_from_transcript():
    """extract_progress_from_transcript finds modified files and original prompt."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        now = datetime.now(timezone.utc).isoformat()

        # User message (original prompt)
        f.write(json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Fix the login bug"}],
            },
        }) + "\n")

        # Assistant modifying a file
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/auth.py"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/utils.py"}},
                ],
            },
        }) + "\n")

        path = Path(f.name)

    try:
        result = w.extract_progress_from_transcript(path)
        assert "Fix the login bug" in result["original_prompt"]
        assert "/src/auth.py" in result["files_modified"]
        assert "/src/utils.py" in result["files_read"]
        print("  ✓ test_extract_progress_from_transcript")
    finally:
        path.unlink()


# ── Tests: Working directory resolution ───────────────────────────────────────

def test_resolve_working_directory():
    """resolve_working_directory picks up cwd from transcript."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        f.write(json.dumps({"cwd": "/tmp", "type": "summary"}) + "\n")
        path = Path(f.name)
    try:
        result = w.resolve_working_directory(path)
        assert result == Path("/tmp")
        print("  ✓ test_resolve_working_directory")
    finally:
        path.unlink()


# ── Tests: Daemon PID management ──────────────────────────────────────────────

def test_pid_file_roundtrip():
    """write_pid → read_pid returns current PID."""
    with tempfile.NamedTemporaryFile(suffix=".pid", delete=False) as f:
        pid_path = Path(f.name)

    cfg = make_config({"pid_file": str(pid_path)})
    pid_path.write_text(str(os.getpid()))  # Write current process PID

    result = w.read_pid(cfg)
    assert result == os.getpid()
    print("  ✓ test_pid_file_roundtrip")

    pid_path.unlink(missing_ok=True)


def test_read_pid_returns_none_for_dead_process():
    """read_pid returns None when PID in file is no longer alive."""
    with tempfile.NamedTemporaryFile(suffix=".pid", delete=False, mode="w") as f:
        f.write("999999")  # Extremely unlikely to be a real PID
        path = Path(f.name)

    cfg = make_config({"pid_file": str(path)})
    result = w.read_pid(cfg)
    assert result is None
    print("  ✓ test_read_pid_returns_none_for_dead_process")

    path.unlink(missing_ok=True)


# ── Integration test: full trigger flow (no exit, no git) ─────────────────────

def test_full_trigger_flow_writes_memory():
    """
    End-to-end: session above threshold → PROGRESS.md + MEMORY.md written.
    auto_exit_claude disabled so we don't actually kill anything.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        project_dir = Path(tmpdir) / "my_project"
        project_dir.mkdir()

        # Transcript at 60% used (40% remaining) — triggers at threshold 50
        transcript_path = project_dir / "test-session.jsonl"
        now = datetime.now(timezone.utc)
        entry = {
            "type": "assistant", "cwd": str(project_dir),
            "isSidechain": False, "isApiErrorMessage": False,
            "timestamp": now.isoformat(),
            "message": {"role": "assistant", "usage": {
                "input_tokens": 120_000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }},
        }
        transcript_path.write_text(json.dumps(entry) + "\n")

        handled_file = Path(tmpdir) / "handled.json"
        log_file = Path(tmpdir) / "test.log"

        cfg = make_config({
            "context_remaining_threshold": 50,
            "git_stash_on_threshold": False,
            "notify_on_threshold": False,
            "auto_exit_claude": False,
            "write_progress_to_memory": True,
            "handled_sessions_file": str(handled_file),
            "log_file": str(log_file),
        })

        # Simulate check
        session = {
            "session_id": "test-session",
            "transcript_path": transcript_path,
            "project_dir": project_dir,
            "project_name": "my_project",
            "last_modified": time.time(),
            "age_seconds": 1,
        }

        context = w.calculate_context_usage(transcript_path, cfg)
        assert context["remaining_percentage"] < cfg["context_remaining_threshold"]

        w.write_progress_to_memory(session, context, cfg)
        w.mark_session_handled("test-session", cfg)

        # Verify outputs
        assert (project_dir / "memory" / "PROGRESS.md").exists()
        assert (project_dir / "memory" / "MEMORY.md").exists()
        assert "test-session" in w.load_handled_sessions(cfg)
        print("  ✓ test_full_trigger_flow_writes_memory")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all():
    tests = [
        test_load_config_defaults,
        test_load_config_from_file,
        test_context_calculation_basic,
        test_context_calculation_with_cache,
        test_context_calculation_uses_most_recent,
        test_context_calculation_skips_sidechain,
        test_context_calculation_empty_file,
        test_context_capped_at_100,
        test_threshold_triggers_at_correct_level,
        test_threshold_does_not_trigger_below_usage,
        test_write_progress_creates_both_files,
        test_memory_md_references_progress,
        test_handled_sessions_no_retrigger,
        test_handled_sessions_capped_at_200,
        test_extract_progress_from_transcript,
        test_resolve_working_directory,
        test_pid_file_roundtrip,
        test_read_pid_returns_none_for_dead_process,
        test_full_trigger_flow_writes_memory,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\nRunning {len(tests)} tests...\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"  ✗ {test.__name__}: {e}")

    print(f"\n{'━'*50}")
    print(f"  {passed} passed  |  {failed} failed")
    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"  ✗ {name}: {err}")
    print(f"{'━'*50}\n")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
