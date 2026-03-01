#!/usr/bin/env python3
"""
Unified synthetic coding-task agent for equal cross-dashboard benchmarking.

Designed to be monitorable by ALL THREE dashboards simultaneously:
  - AGentviz:  writes JSONL state events to AGENTVIZ_STATE_FILE
  - TmuxCC:    sets tmux window title to spinner (triggers "Processing" detection)
               prints "Yes / No" approval prompts (triggers "AwaitingApproval")
  - AoE:       same tmux title + text patterns as TmuxCC

Also writes a human-readable transition log to BENCH_LOG_FILE so the
harness can compute exact state-change timestamps for latency measurement.

Simulates three realstic coding task scenarios:
  simple:  3 tool cycles, 0 permission prompts  (~6s)
  medium:  5 tool cycles, 1 permission prompt   (~12s)
  complex: 8 tool cycles, 2 permission prompts  (~20s)

Usage (run under agentviz):
  BENCH_SCENARIO=medium BENCH_LOG_FILE=/tmp/bench.log SYNTH_AUTO_INPUT=1 \\
    agentviz run -w /tmp/ws -i agent-1 synthetic python3 eval/unified_agent.py

Usage (run in bare tmux pane for TmuxCC/AoE):
  BENCH_SCENARIO=medium BENCH_LOG_FILE=/tmp/bench.log SYNTH_AUTO_INPUT=1 \\
    python3 eval/unified_agent.py
"""

import json
import os
import subprocess
import sys
import time

# ─── Configuration ───────────────────────────────────────────────────────────
SCENARIO       = os.environ.get("BENCH_SCENARIO", "medium")
LOG_FILE       = os.environ.get("BENCH_LOG_FILE", "/tmp/bench_agent.log")
STATE_FILE     = os.environ.get("AGENTVIZ_STATE_FILE")   # set by agentviz run
AUTO_INPUT     = os.environ.get("SYNTH_AUTO_INPUT", "0") == "1"
TMUX_PANE      = os.environ.get("BENCH_PANE_TARGET") or os.environ.get("TMUX_PANE", "")
AUTO_INPUT_DELAY_SEC = float(os.environ.get("SYNTH_AUTO_INPUT_DELAY_SEC", "0.8"))
AUTO_APPROVAL_HOLD_SEC = float(os.environ.get("SYNTH_APPROVAL_HOLD_SEC", "3.0"))

SCENARIOS = {
    "simple":  {"tool_cycles": 3, "permission_prompts": 0, "think_sec": 0.4, "work_sec": 0.4},
    "medium":  {"tool_cycles": 5, "permission_prompts": 1, "think_sec": 0.5, "work_sec": 0.5},
    "complex": {"tool_cycles": 8, "permission_prompts": 2, "think_sec": 0.6, "work_sec": 0.6},
}
cfg = SCENARIOS.get(SCENARIO, SCENARIOS["medium"])

TOOL_NAMES = ["Read", "Edit", "Bash", "Write", "Glob"]

# Spinner frames — same chars TmuxCC watches for in window title
SPINNERS = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠧", "⠖", "⠏"]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def ts() -> float:
    return time.time()


def log_transition(state: str, detail: str = ""):
    """Write a timestamped state-transition record to LOG_FILE."""
    entry = {"ts": ts(), "state": state, "detail": detail}
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()


def write_agentviz_event(event_type: str, extra: dict = None):
    """Write a JSONL event to AGentviz state file (same format as Claude Code hooks)."""
    if not STATE_FILE:
        return
    data = {"event": event_type, "timestamp": int(ts() * 1000)}
    if extra:
        data.update(extra)
    with open(STATE_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")
        f.flush()


def set_tmux_title(title: str):
    """
    Set the tmux pane title via 'tmux select-pane -T'.
    TmuxCC reads #{pane_title} from list-panes output for spinner detection.
    Using the tmux CLI directly works regardless of the set-titles option.
    """
    if not TMUX_PANE:
        return
    subprocess.run(
        ["tmux", "select-pane", "-t", TMUX_PANE, "-T", title],
        capture_output=True
    )


def clear_tmux_title():
    """Reset pane title to plain name (signals Idle to TmuxCC/AoE)."""
    set_tmux_title("claude-bench")


def spin(duration_sec: float, label: str):
    """Animate a spinner in tmux title + print output (TmuxCC "Processing" trigger)."""
    start = time.time()
    frame = 0
    while time.time() - start < duration_sec:
        spinner_char = SPINNERS[frame % len(SPINNERS)]
        # Set title with spinner — TmuxCC detects this as Processing
        set_tmux_title(f"{spinner_char} {label}")
        # Avoid printing Unicode spinner glyphs into pane history because AoE's
        # Claude status detector scans captured lines for spinner chars and will
        # keep reporting Running even after an approval prompt appears.
        print(f"\r  {label}...", end="", flush=True)
        time.sleep(0.12)
        frame += 1
    print()  # newline after spinner


def approval_prompt(tool: str, target: str):
    """
    Print a Claude Code-style Yes/No approval prompt.

    The format is designed to trigger detection in both TmuxCC and AoE:
      TmuxCC pattern: (?i)[y/n]|[yes/no]|Allow?|Do you want to (allow|...)
      AoE pattern:    (?i)[y/n]|[yes/no]|confirm|approve|allow
    Both match on '[y/n]' and the word 'allow'.
    """
    # Clear spinner in title first — we're now "AwaitingApproval"
    set_tmux_title("claude-bench")
    print(f"\n⏺ {tool}({target})")
    # AoE's Claude detector looks for (Y/n)/(y/N)/[Y/n]/[y/N] and
    # button labels like "Yes, allow once"/"Yes, allow always".
    # TmuxCC also matches these plus generic approval wording.
    print(f"\nAllow this action? [Y/n]")
    print()
    print("  Yes, allow once")
    print("  Yes, allow always")
    print("  No")
    sys.stdout.flush()


def wait_for_input(delay_sec: float | None = None):
    if AUTO_INPUT:
        # Keep prompts visible long enough for slower tmux dashboard poll loops.
        time.sleep(AUTO_INPUT_DELAY_SEC if delay_sec is None else delay_sec)
    else:
        sys.stdin.readline()


def print_tool_output(tool: str, cycle: int):
    """Realistic Claude Code tool output."""
    outputs = {
        "Read":  f"  Reading src/main.py ({40 + cycle * 3} lines)",
        "Edit":  f"  Editing src/main.py  (+{8 + cycle} / -{3} lines)",
        "Bash":  f"  $ pytest tests/ ... {4 + cycle} passed in 0.{30 + cycle}s",
        "Write": f"  Writing src/utils.py ({20 + cycle * 2} lines)",
        "Glob":  f"  Found {12 + cycle} matching files",
    }
    print(outputs.get(tool, f"  {tool} completed"))
    sys.stdout.flush()


# ─── Task phases ─────────────────────────────────────────────────────────────

def phase_start():
    log_transition("STARTING")
    write_agentviz_event("session_start")
    set_tmux_title("claude-bench")
    print("=" * 56)
    print(f"  Claude Code  (scenario: {SCENARIO})")
    print(f"  Tool cycles: {cfg['tool_cycles']}   "
          f"Permission prompts: {cfg['permission_prompts']}")
    print("=" * 56)
    sys.stdout.flush()

    if not AUTO_INPUT:
        print("\nEnter your task (press Enter): ", end="", flush=True)
    wait_for_input()


def phase_thinking():
    log_transition("THINKING")
    write_agentviz_event("user_prompt_submit")
    spin(cfg["think_sec"], "Thinking")


def phase_tool(cycle: int, total: int, tool: str):
    log_transition("TOOL_USE", tool)
    write_agentviz_event("pre_tool_use", {"tool_name": tool})
    spin(cfg["work_sec"], f"Using {tool} [{cycle}/{total}]")
    print_tool_output(tool, cycle)
    write_agentviz_event("post_tool_use", {"tool_name": tool})
    clear_tmux_title()


def phase_permission(tool: str, target: str):
    log_transition("AWAITING_APPROVAL", f"{tool}({target})")
    write_agentviz_event("permission_request", {"tool_name": tool})
    # Push prior processing lines out of AoE's 50-line pane capture window so
    # stale spinner frames do not mask Waiting detection.
    print("\n" * 60, end="")
    approval_prompt(tool, target)
    wait_for_input(AUTO_APPROVAL_HOLD_SEC)
    log_transition("APPROVED")


def phase_complete():
    log_transition("COMPLETED")
    write_agentviz_event("stop")
    clear_tmux_title()
    print("\n" + "=" * 56)
    print("  Task complete.")
    print("=" * 56, flush=True)
    write_agentviz_event("session_end")


# ─── Main ────────────────────────────────────────────────────────────────────

def permission_indices(total_cycles: int, n_prompts: int) -> set:
    if n_prompts <= 0 or total_cycles <= 0:
        return set()
    step = total_cycles / (n_prompts + 1)
    return {int(step * (i + 1)) for i in range(n_prompts)}


def main():
    # Clear log file for this run
    open(LOG_FILE, "w").close()

    phase_start()
    phase_thinking()

    total = cfg["tool_cycles"]
    perm_at = permission_indices(total, cfg["permission_prompts"])

    for i in range(total):
        if i in perm_at:
            phase_permission("Bash", f"command='pytest tests/ -x'")

        tool = TOOL_NAMES[i % len(TOOL_NAMES)]
        phase_tool(i + 1, total, tool)

    phase_complete()
    sys.exit(0)


if __name__ == "__main__":
    main()
