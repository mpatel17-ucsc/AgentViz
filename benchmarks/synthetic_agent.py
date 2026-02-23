#!/usr/bin/env python3
"""
Synthetic agent that simulates a realistic coding agent lifecycle.

Writes JSONL events to the state file (same format as Claude Code hooks)
and produces terminal output to exercise the full AgentViz pipeline.

Controlled via environment variables:
  AGENTVIZ_STATE_FILE  - path to JSONL state file (required, set by adapter)
  SYNTH_TOOL_CYCLES    - number of tool use cycles (default: 5)
  SYNTH_PERMISSION_PROMPTS - number of permission prompt pauses (default: 1)
  SYNTH_OUTPUT_KB      - KB of terminal output to generate (default: 10)
  SYNTH_THINK_TIME     - seconds to simulate thinking (default: 0.5)
  SYNTH_WORK_TIME      - seconds per tool cycle (default: 0.5)
  SYNTH_AUTO_INPUT     - if 1, auto-proceed without waiting for stdin (default: 0)
"""

import json
import os
import sys
import time


def get_env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def get_env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


STATE_FILE = os.environ.get("AGENTVIZ_STATE_FILE")
TOOL_CYCLES = get_env_int("SYNTH_TOOL_CYCLES", 5)
PERMISSION_PROMPTS = get_env_int("SYNTH_PERMISSION_PROMPTS", 1)
OUTPUT_KB = get_env_int("SYNTH_OUTPUT_KB", 10)
THINK_TIME = get_env_float("SYNTH_THINK_TIME", 0.5)
WORK_TIME = get_env_float("SYNTH_WORK_TIME", 0.5)
AUTO_INPUT = get_env_int("SYNTH_AUTO_INPUT", 0) == 1


def write_event(event_type, extra=None):
    """Append a JSONL event to the state file."""
    if not STATE_FILE:
        return
    data = {
        "event": event_type,
        "timestamp": int(time.time() * 1000),
        "agent_id": "synthetic",
    }
    if extra:
        data.update(extra)
    with open(STATE_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")
        f.flush()


def wait_for_input():
    """Wait for user to press Enter (or skip if AUTO_INPUT)."""
    if AUTO_INPUT:
        time.sleep(0.05)
        return
    sys.stdin.readline()


def generate_output(kb):
    """Generate terminal output of approximately `kb` kilobytes."""
    line = "  Processing file src/components/App.tsx ... modified 42 lines\n"
    bytes_needed = kb * 1024
    bytes_written = 0
    while bytes_written < bytes_needed:
        sys.stdout.write(line)
        bytes_written += len(line)
    sys.stdout.flush()


def permission_prompt_indices():
    """Determine at which tool cycle indices a permission prompt should appear."""
    if PERMISSION_PROMPTS <= 0 or TOOL_CYCLES <= 0:
        return set()
    if PERMISSION_PROMPTS >= TOOL_CYCLES:
        return set(range(TOOL_CYCLES))
    # Spread permission prompts evenly across tool cycles
    step = TOOL_CYCLES / (PERMISSION_PROMPTS + 1)
    return {int(step * (i + 1)) for i in range(PERMISSION_PROMPTS)}


def main():
    if not STATE_FILE:
        print("ERROR: AGENTVIZ_STATE_FILE environment variable not set", file=sys.stderr)
        sys.exit(1)

    prompt_indices = permission_prompt_indices()
    output_per_cycle = max(1, OUTPUT_KB // max(TOOL_CYCLES, 1))

    # --- Phase 1: Session start ---
    write_event("session_start")
    print("=" * 60)
    print("Synthetic Agent v1.0 - Ready")
    print(f"  Tool cycles: {TOOL_CYCLES}")
    print(f"  Permission prompts: {PERMISSION_PROMPTS}")
    print(f"  Output: ~{OUTPUT_KB} KB")
    print("=" * 60)
    sys.stdout.flush()

    # Wait for initial user input (simulates entering a prompt)
    if not AUTO_INPUT:
        print("\nEnter your prompt (press Enter to start): ", end="", flush=True)
    wait_for_input()

    # --- Phase 2: User prompt submitted ---
    write_event("user_prompt_submit")
    print("\nThinking...", flush=True)
    time.sleep(THINK_TIME)

    # --- Phase 3: Tool cycles ---
    for i in range(TOOL_CYCLES):
        # Check for permission prompt at this cycle
        if i in prompt_indices:
            write_event("permission_request", {"tool_name": f"Bash(command='npm test')"})
            print(f"\n[Permission Required] Allow Bash command: npm test")
            print("  Allow once | Deny", flush=True)
            wait_for_input()

        # Pre-tool
        tool_name = ["Read", "Edit", "Write", "Bash", "Glob"][i % 5]
        write_event("pre_tool_use", {"tool_name": tool_name})
        print(f"\n[{i+1}/{TOOL_CYCLES}] Executing {tool_name}...", flush=True)

        # Simulate work with terminal output
        generate_output(output_per_cycle)
        time.sleep(WORK_TIME)

        # Post-tool
        write_event("post_tool_use", {"tool_name": tool_name})

    # --- Phase 4: Complete ---
    write_event("stop")
    print("\n" + "=" * 60)
    print("Task complete.")
    print("=" * 60, flush=True)

    write_event("session_end")
    sys.exit(0)


if __name__ == "__main__":
    main()
