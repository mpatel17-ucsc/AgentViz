#!/usr/bin/env python3
"""
Equal cross-dashboard benchmark: AGentviz vs TmuxCC vs Agent of Empires

Runs the SAME synthetic coding task under each dashboard and measures
the same metrics for each:

  1. State-detection latency   — time from agent state change → dashboard reflects it
  2. Detection accuracy        — fraction of state transitions correctly classified
  3. Monitoring overhead (RAM) — extra RSS consumed by dashboard process

Architecture of the measurement:
  - unified_agent.py writes a timestamped transition log (BENCH_LOG_FILE)
    at the exact moment each state changes.
  - AGentviz: Socket.IO push — arrival_ts - emit_ts
  - TmuxCC: actually run in a tmux pane with a fake 'claude' wrapper;
    capture TmuxCC's pane every 20ms and detect when it shows "Pending approval".
  - AoE: 'aoe session start' (requires $TMUX) runs the fake 'claude' wrapper
    via a modified PATH; poll 'aoe session show --json' at 20ms for 'waiting'.

Both TmuxCC and AoE require running inside tmux.  The benchmark auto-bootstraps
into a new tmux session if $TMUX is not set, so no manual setup is needed.

Usage:
  # Make sure agentviz is on PATH (activate venv first)
  cd agentviz && source venv/bin/activate
  python3 ../eval/benchmark_equal.py [--scenario simple|medium|complex] [--trials N]

Output: eval/results/equal_YYYYMMDD_HHMMSS.json
"""

import argparse
import atexit
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import shutil
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev

EVAL_DIR      = Path(__file__).parent
AOE           = str(EVAL_DIR / "bin" / "aoe")
TMUXCC        = str(EVAL_DIR / "bin" / "tmuxcc")
AGENT_SCRIPT  = str(EVAL_DIR / "unified_agent.py")
RESULTS_DIR   = EVAL_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

AGENTVIZ_URL  = "http://localhost:8787"
AGENTVIZ_CMD  = "agentviz"

_TMUX_SESSIONS_TO_CLEAN: set[str] = set()
_TMUX_WINDOWS_TO_CLEAN: set[str] = set()
_CLEANUP_INSTALLED = False
_CLEANUP_RUNNING = False


# ─────────────────────────────────────────────────────────────────────────────
# Tmux bootstrap helpers
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_relaunch_in_tmux():
    """
    TmuxCC and AoE require $TMUX to be set (both tools work through tmux).
    If we're not inside tmux, create a new tmux session running this exact
    script with the same arguments and attach to it.  The user lands back at
    their shell prompt when the benchmark finishes.
    """
    if os.environ.get("TMUX"):
        return  # already inside tmux

    script  = os.path.abspath(__file__)
    argv    = [sys.executable, script] + sys.argv[1:]
    cmd_str = " ".join(shlex.quote(a) for a in argv)
    sid     = f"bench-{int(time.time())}"

    print()
    print("  TmuxCC and AoE require $TMUX.  Auto-starting tmux session …")
    print(f"  Session name: {sid}")
    print()

    # -s sid  — session name
    # No -d   — creates AND attaches (blocks until session exits)
    try:
        subprocess.run(["tmux", "new-session", "-s", sid, cmd_str], check=True)
    except subprocess.CalledProcessError:
        pass
    sys.exit(0)


def _register_tmux_session_for_cleanup(session_name: str | None):
    if session_name:
        _TMUX_SESSIONS_TO_CLEAN.add(session_name)


def _unregister_tmux_session_for_cleanup(session_name: str | None):
    if session_name:
        _TMUX_SESSIONS_TO_CLEAN.discard(session_name)


def _register_tmux_window_for_cleanup(window_target: str | None):
    if window_target:
        _TMUX_WINDOWS_TO_CLEAN.add(window_target)


def _unregister_tmux_window_for_cleanup(window_target: str | None):
    if window_target:
        _TMUX_WINDOWS_TO_CLEAN.discard(window_target)


def _cleanup_tmux_resources():
    global _CLEANUP_RUNNING
    if _CLEANUP_RUNNING:
        return
    _CLEANUP_RUNNING = True
    try:
        for window_target in list(_TMUX_WINDOWS_TO_CLEAN):
            subprocess.run(["tmux", "kill-window", "-t", window_target], capture_output=True)
            _TMUX_WINDOWS_TO_CLEAN.discard(window_target)
        for session_name in list(_TMUX_SESSIONS_TO_CLEAN):
            subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
            _TMUX_SESSIONS_TO_CLEAN.discard(session_name)
    finally:
        _CLEANUP_RUNNING = False


def _install_cleanup_handlers():
    global _CLEANUP_INSTALLED
    if _CLEANUP_INSTALLED:
        return
    _CLEANUP_INSTALLED = True
    atexit.register(_cleanup_tmux_resources)

    def _handle_signal(signum, _frame):
        print(f"\n  [cleanup] signal {signum}; removing benchmark tmux resources...")
        _cleanup_tmux_resources()
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except Exception:
            pass


def _cleanup_stale_benchmark_tmux_resources():
    """
    Best-effort cleanup of stale benchmark tmux sessions/windows from prior runs.
    Preserves the current attached session (where this benchmark may be running).
    """
    current_session = _current_tmux_session_name()

    # Remove stale detached benchmark sessions (bench-* and AoE-created aoe_bench-*).
    ls_r = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_attached} #{session_name}"],
        capture_output=True, text=True,
    )
    if ls_r.returncode == 0:
        for line in ls_r.stdout.splitlines():
            try:
                attached, session_name = line.split(" ", 1)
            except ValueError:
                continue
            is_bench = session_name.startswith("bench-") or session_name.startswith("aoe_bench-")
            if not is_bench:
                continue
            if session_name == current_session:
                continue
            # Only auto-kill detached sessions during preflight.
            if attached == "0":
                subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

    # Remove stale benchmark windows in the current attached session (from interrupted runs).
    if current_session:
        panes_r = subprocess.run(
            ["tmux", "list-panes", "-t", current_session, "-a",
             "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{window_name}"],
            capture_output=True, text=True,
        )
        if panes_r.returncode == 0:
            seen_windows = set()
            for line in panes_r.stdout.splitlines():
                if "\t" not in line:
                    continue
                pane_target, window_name = line.split("\t", 1)
                if not pane_target.startswith(f"{current_session}:"):
                    continue
                if not (window_name.startswith("bench-agent-") or window_name.startswith("bench-acc-")):
                    continue
                window_target = pane_target.rsplit(".", 1)[0]
                if window_target in seen_windows:
                    continue
                seen_windows.add(window_target)
                subprocess.run(["tmux", "kill-window", "-t", window_target], capture_output=True)


def _make_fake_claude_bin(scenario: str, log_file: str) -> str:
    """
    Create a directory containing a 'claude' executable that runs unified_agent.py.
    Returns the directory path so callers can prepend it to PATH.

    Compiles a tiny C wrapper that fork()s python3 as a child while the parent
    C process stays alive as the process-group leader named 'claude'.
    tmux's #{pane_current_command} returns the foreground pgrp leader, so
    TmuxCC (which filters on that name) correctly identifies the pane as a
    claude agent.

    The C binary embeds baked-in env-var defaults (used when AoE creates the
    tmux pane without forwarding the benchmark's env).  If the vars are already
    set in the environment (e.g. via tmux new-session -e) the baked-in values
    are ignored.

    Falls back to a shell script if clang is unavailable — #{pane_current_command}
    will then show 'Python', and TmuxCC detection will fail.
    """
    fake_dir   = tempfile.mkdtemp(prefix="bench-fake-bin-")
    claude_bin = Path(fake_dir) / "claude"
    c_src_path = Path(fake_dir) / "claude.c"

    # json.dumps() produces valid C string literals (double-quoted, escaped).
    # f-string uses {{ / }} for literal braces in the C source.
    c_source = f"""\
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>

int main(void) {{
    if (!getenv("BENCH_SCENARIO"))   setenv("BENCH_SCENARIO",   {json.dumps(scenario)}, 1);
    if (!getenv("BENCH_LOG_FILE"))   setenv("BENCH_LOG_FILE",   {json.dumps(log_file)}, 1);
    if (!getenv("SYNTH_AUTO_INPUT")) setenv("SYNTH_AUTO_INPUT", "1", 1);
    if (!getenv("SYNTH_APPROVAL_HOLD_SEC")) setenv("SYNTH_APPROVAL_HOLD_SEC", "5", 1);
    setenv("AGENTVIZ_STATE_FILE", "", 1);
    char *const args[] = {{"python3", {json.dumps(AGENT_SCRIPT)}, NULL}};
    pid_t pid = fork();
    if (pid == 0) {{
        execvp("python3", args);
        perror("claude: execvp");
        return 127;
    }}
    if (pid < 0) {{ perror("claude: fork"); return 1; }}
    int st = 0;
    waitpid(pid, &st, 0);
    return WEXITSTATUS(st);
}}
"""
    c_src_path.write_text(c_source)
    compile_r = subprocess.run(
        ["clang", "-o", str(claude_bin), str(c_src_path)],
        capture_output=True, text=True,
    )
    c_src_path.unlink()

    if compile_r.returncode == 0:
        claude_bin.chmod(0o755)
        return fake_dir

    # Fallback: shell script.  TmuxCC will likely not detect this pane because
    # #{pane_current_command} shows 'Python' rather than 'claude'.
    print(
        "  [warn] clang compile failed; using shell-script fallback "
        "(TmuxCC needs #{pane_current_command}='claude').\n"
        f"  clang stderr: {compile_r.stderr.strip()[:120]}"
    )
    claude_bin.write_text(
        "#!/bin/sh\n"
        f"export BENCH_SCENARIO={shlex.quote(scenario)}\n"
        f"export BENCH_LOG_FILE={shlex.quote(log_file)}\n"
        "export SYNTH_AUTO_INPUT=1\n"
        "export SYNTH_APPROVAL_HOLD_SEC=5\n"
        "export AGENTVIZ_STATE_FILE=\n"
        f"exec python3 {shlex.quote(AGENT_SCRIPT)} \"$@\"\n"
    )
    claude_bin.chmod(0o755)
    return fake_dir


def _set_tmux_global_path(new_path: str):
    """Set PATH in tmux's global environment so new windows/sessions inherit it."""
    subprocess.run(["tmux", "set-environment", "-g", "PATH", new_path], capture_output=True)


def _current_tmux_session_name() -> str | None:
    """Return the current attached tmux session name."""
    r = subprocess.run(
        ["tmux", "display-message", "-p", "#S"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        name = r.stdout.strip()
        return name or None
    return None


def _tmux_new_window_in_session(session_name: str, window_name: str, env_pairs: list[str], cmd: str) -> str | None:
    """
    Create a new tmux window in an existing (attached) session and return pane target.
    Uses -P/-F to get the exact pane target (session:window.pane).
    """
    args = ["tmux", "new-window", "-d", "-P", "-F", "#{session_name}:#{window_index}.#{pane_index}",
            "-t", session_name, "-n", window_name]
    for pair in env_pairs:
        args.extend(["-e", pair])
    args.append(cmd)
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    target = r.stdout.strip()
    target = target or None
    if target:
        _register_tmux_window_for_cleanup(target.rsplit(".", 1)[0])
    return target


def pct(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return round(s[f] + (k - f) * (s[c] - s[f]), 3)


def _extract_status_strings(obj, path="$") -> list[tuple[str, str]]:
    """
    Recursively collect status-like key/value pairs from a JSON-compatible object.
    Used to debug/normalize AoE JSON where the waiting state may not be top-level.
    """
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}"
            if isinstance(v, str) and k.lower() in {"status", "state", "agent_status", "session_status"}:
                out.append((child_path, v))
            out.extend(_extract_status_strings(v, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            out.extend(_extract_status_strings(item, f"{path}[{i}]"))
    return out


def _json_has_waiting_state(obj) -> bool:
    """Case-insensitive check across status-like fields for a waiting state."""
    for _, value in _extract_status_strings(obj):
        if value.strip().lower() == "waiting":
            return True
    return False


def make_workspace():
    d = tempfile.mkdtemp(prefix="bench-ws-")
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "commit", "--allow-empty", "-m", "init", "-q"],
                   check=True,
                   env={**os.environ,
                        "GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "b@b.com",
                        "GIT_COMMITTER_NAME": "bench", "GIT_COMMITTER_EMAIL": "b@b.com"})
    # Create a small Python file for the agent to "work on"
    (Path(d) / "src").mkdir()
    (Path(d) / "src" / "main.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef main():\n    print(add(1, 2))\n"
    )
    (Path(d) / "tests").mkdir()
    (Path(d) / "tests" / "test_main.py").write_text(
        "from src.main import add\ndef test_add(): assert add(1, 2) == 3\n"
    )
    return d


def cleanup(d):
    try:
        shutil.rmtree(d)
    except Exception:
        pass


def read_transition_log(log_file: str) -> list[dict]:
    """Read the timestamped state transitions written by unified_agent.py."""
    transitions = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    transitions.append(json.loads(line))
    except FileNotFoundError:
        pass
    return transitions


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 1: AGentviz — approval detection latency + detection accuracy
# ─────────────────────────────────────────────────────────────────────────────
def run_agentviz_trial(scenario: str, log_file: str) -> dict:
    """
    Run unified_agent under AGentviz and collect Socket.IO event timestamps.
    Returns approval-detection latencies:
      AWAITING_APPROVAL in BENCH_LOG_FILE -> waiting_for_input socket event arrival.
    """
    try:
        import socketio as sio_lib
    except ImportError:
        return {"status": "skipped", "reason": "pip install python-socketio"}

    import urllib.request
    # Check server is up
    try:
        urllib.request.urlopen(f"{AGENTVIZ_URL}/", timeout=2)
    except Exception:
        return {"status": "skipped",
                "reason": f"AGentviz server not running at {AGENTVIZ_URL}. "
                           "Start it with: agentviz server"}

    agent_id = f"bench-{scenario}-{int(time.time())}"
    socket_events = []   # list of (event_type, socket_arrival_ts)
    done = False

    sio = sio_lib.Client()

    @sio.on("agent_event")
    def on_event(data):
        nonlocal done
        if data.get("agent_id") != agent_id:
            return
        recv_ts = time.time()
        socket_events.append({
            "event_type": data.get("event_type"),
            "arrival_ts": recv_ts,
            "emitted_ts": data.get("timestamp", recv_ts),  # monitor.py: time.time() (seconds)
        })
        if data.get("event_type") in ("agent_stopped", "session_end"):
            done = True

    sio.connect(AGENTVIZ_URL)

    ws = make_workspace()
    env = {**os.environ,
           "BENCH_SCENARIO": scenario,
           "BENCH_LOG_FILE": log_file,
           "SYNTH_AUTO_INPUT": "1"}

    proc = subprocess.Popen(
        [AGENTVIZ_CMD, "run", "-w", ws, "-i", agent_id, "synthetic",
         "python3", AGENT_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
    )

    deadline = time.time() + 60
    while not done and time.time() < deadline:
        time.sleep(0.05)

    proc.wait(timeout=5)
    sio.disconnect()
    cleanup(ws)

    transitions = read_transition_log(log_file)
    # Fair/comparable metric across all dashboards:
    # agent transition log timestamp (AWAITING_APPROVAL) -> dashboard waiting signal observed.
    approval_ts_list = [t["ts"] for t in transitions if t["state"] == "AWAITING_APPROVAL"]
    waiting_arrivals = [e["arrival_ts"] for e in socket_events if e.get("event_type") == "waiting_for_input"]

    latencies = []
    waiting_idx = 0
    for approval_ts in approval_ts_list:
        while waiting_idx < len(waiting_arrivals) and waiting_arrivals[waiting_idx] < approval_ts:
            waiting_idx += 1
        if waiting_idx < len(waiting_arrivals):
            latencies.append((waiting_arrivals[waiting_idx] - approval_ts) * 1000)
            waiting_idx += 1

    # Accuracy: did each major task phase generate at least one socket event?
    # claude_adapter.py maps raw events → socket event types:
    #   session_start/user_prompt_submit/pre_tool_use/post_tool_use → "state_change"
    #   permission_request                                           → "waiting_for_input"
    #   stop                                                         → "task_completed"
    phase_socket_map = {
        "STARTING":          {"state_change"},
        "THINKING":          {"state_change"},
        "TOOL_USE":          {"state_change"},
        "AWAITING_APPROVAL": {"waiting_for_input", "state_change"},
        "COMPLETED":         {"task_completed", "state_change", "agent_stopped"},
    }
    detected_event_types = {e["event_type"] for e in socket_events if e.get("event_type")}
    phases_expected = {t["state"] for t in transitions if t["state"] in phase_socket_map}
    phases_hit = {ph for ph in phases_expected
                  if detected_event_types & phase_socket_map[ph]}
    accuracy = round(len(phases_hit) / len(phases_expected) * 100, 1) if phases_expected else 0

    return {
        "status": "ok",
        "scenario": scenario,
        "expected_phases": sorted(phases_expected),
        "phases_with_events": sorted(phases_hit),
        "detected_event_types": sorted(detected_event_types),
        "detection_accuracy_pct": accuracy,
        "latency_ms": {
            "p50": pct(latencies, 50),
            "p95": pct(latencies, 95),
            "p99": pct(latencies, 99),
            "mean": round(mean(latencies), 3) if latencies else None,
        },
        "latencies_measured": len(latencies),
        "socket_events": len(socket_events),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 2: tmux baseline — how fast does capture-pane reflect changes?
# ─────────────────────────────────────────────────────────────────────────────
def measure_tmux_capture_latency(n_trials: int = 20) -> dict:
    """
    Measure tmux capture-pane refresh latency — the shared data source for
    both TmuxCC and AoE.  Both tools read pane content via tmux capture-pane.

    Method: Write a unique sentinel string to a tmux pane, then poll
    capture-pane until the string appears. The time difference is the
    tmux IPC + capture latency.
    """
    print("  [tmux] measuring capture-pane IPC latency...")

    # Create a tmux session for the test
    sid = f"bench-tmux-{int(time.time())}"
    subprocess.run(["tmux", "new-session", "-d", "-s", sid, "cat"],
                   capture_output=True, check=True)
    _register_tmux_session_for_cleanup(sid)

    latencies = []
    try:
        for i in range(n_trials):
            sentinel = f"BENCH_SENTINEL_{i}_{int(time.time()*1000)}"
            t_write = time.time()

            # Send the sentinel string to the pane via tmux send-keys
            subprocess.run(
                ["tmux", "send-keys", "-t", sid, f"echo {sentinel}", "Enter"],
                capture_output=True
            )

            # Poll capture-pane until sentinel appears
            deadline = time.time() + 2.0
            detected = False
            while time.time() < deadline:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", sid, "-p"],
                    capture_output=True, text=True
                )
                if sentinel in result.stdout:
                    t_detect = time.time()
                    latencies.append((t_detect - t_write) * 1000)
                    detected = True
                    break
                time.sleep(0.001)

            if not detected:
                latencies.append(2000.0)  # timeout

    finally:
        subprocess.run(["tmux", "kill-session", "-t", sid], capture_output=True)
        _unregister_tmux_session_for_cleanup(sid)

    result = {
        "method": "write sentinel via send-keys, poll capture-pane until visible",
        "trials": n_trials,
        "p50_ms":  pct(latencies, 50),
        "p95_ms":  pct(latencies, 95),
        "p99_ms":  pct(latencies, 99),
        "mean_ms": round(mean(latencies), 3) if latencies else None,
    }
    print(f"  tmux capture-pane: p50={result['p50_ms']}ms  p95={result['p95_ms']}ms")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 3a: TmuxCC — actually run it and measure detection latency
# ─────────────────────────────────────────────────────────────────────────────
def extract_poll_interval_ms(binary: str) -> float | None:
    """
    Extract the default poll interval by running '<binary> --help' at runtime
    and parsing the '[default: NNN]' annotation on the poll-interval flag.
    Returns the value in milliseconds, or None if it cannot be parsed.
    """
    import re
    try:
        r = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=5)
        for line in (r.stdout + r.stderr).splitlines():
            if "poll" in line.lower():
                m = re.search(r'\[default:\s*(\d+)\]', line, re.IGNORECASE)
                if m:
                    return float(m.group(1))
    except Exception:
        pass
    return None


def measure_tmuxcc_detection_latency(scenario: str, log_file: str,
                                     n_trials: int = 3) -> dict:
    """
    Actually run TmuxCC and measure when it detects agent state changes.

    Flow per trial:
      1. Create a fake 'claude' wrapper that runs unified_agent.py.
      2. Start the agent in a tmux session (pane_current_command == 'claude').
      3. Start TmuxCC in a second tmux session — it discovers the claude pane.
      4. Record AWAITING_APPROVAL timestamps from the agent log.
      5. Poll TmuxCC's pane at 20ms; detect when it shows 'Pending approval'
         or a 'Waiting' indicator (the state TmuxCC displays for approval).
      6. Latency = first detection timestamp − AWAITING_APPROVAL log timestamp.

    Requires $TMUX (auto-bootstrapped by main() if needed).
    TmuxCC detects agents by #{pane_current_command}; the fake 'claude' wrapper
    makes our synthetic agent look like Claude Code to TmuxCC.
    """
    if scenario == "simple":
        return {
            "status": "skipped",
            "reason": "simple scenario has no permission prompts",
        }

    if not os.environ.get("TMUX"):
        return {
            "status": "requires_tmux",
            "reason": "TmuxCC needs a tmux server; run the benchmark from inside tmux.",
        }

    print(f"  [tmuxcc] running TmuxCC + fake claude wrapper "
          f"(scenario={scenario}, trials={n_trials})...")

    POLL_SEC = 0.020
    poll_ms  = extract_poll_interval_ms(TMUXCC) or 500.0
    latencies: list[float] = []
    current_attached_session = _current_tmux_session_name()
    if not current_attached_session:
        return {
            "status": "requires_attached_tmux",
            "reason": "TmuxCC only monitors panes in attached tmux sessions; no current attached session found.",
        }

    for trial in range(n_trials):
        open(log_file, "w").close()

        # 1. Build fake 'claude' that runs our agent
        fake_dir   = _make_fake_claude_bin(scenario, log_file)
        fake_claude = str(Path(fake_dir) / "claude")
        new_path   = f"{fake_dir}:{os.environ.get('PATH', '')}"
        agent_sid  = f"bench-agent-{int(time.time())}"
        agent_window_name = f"bench-agent-{trial+1}"
        tmuxcc_sid = f"bench-tmuxcc-{int(time.time()) + 1}"
        agent_pane_target = None

        # 2. Set PATH in tmux global env (new windows inherit it)
        old_path_r = subprocess.run(
            ["tmux", "show-environment", "-g", "PATH"],
            capture_output=True, text=True,
        )
        old_path = (old_path_r.stdout.strip().split("=", 1)[1]
                    if "=" in old_path_r.stdout else os.environ.get("PATH", ""))
        _set_tmux_global_path(new_path)

        try:
            # 3. Create agent in a NEW WINDOW of the current attached tmux session.
            # TmuxCC filters to attached sessions, so detached sessions are invisible.
            agent_pane_target = _tmux_new_window_in_session(
                current_attached_session,
                agent_window_name,
                [
                    f"PATH={new_path}",
                    f"BENCH_SCENARIO={scenario}",
                    f"BENCH_LOG_FILE={log_file}",
                    "SYNTH_AUTO_INPUT=1",
                    "SYNTH_APPROVAL_HOLD_SEC=5",
                    "AGENTVIZ_STATE_FILE=",
                ],
                fake_claude,
            )
            if not agent_pane_target:
                print(f"    Trial {trial+1}: failed to create attached agent window in tmux")
                continue

            # Debug snapshot: confirm the fake agent pane is in an attached session and looks like claude.
            pane_dbg = subprocess.run(
                ["tmux", "list-panes", "-a", "-F",
                 "#{session_attached} #{session_name}:#{window_index}.#{pane_index} "
                 "#{pane_current_command} #{pane_title}"],
                capture_output=True, text=True,
            )
            if pane_dbg.returncode == 0:
                match_line = next(
                    (ln for ln in pane_dbg.stdout.splitlines() if agent_pane_target in ln),
                    None,
                )
                if match_line:
                    print(f"    Trial {trial+1}: agent pane -> {match_line}")
                    if " claude " not in f" {match_line} ":
                        print("      [warn] fake wrapper not active (expected pane_current_command 'claude')")

            # 4. Start TmuxCC in its own session
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", tmuxcc_sid, TMUXCC],
                capture_output=True,
            )
            _register_tmux_session_for_cleanup(tmuxcc_sid)

            # Let TmuxCC initialize and discover the agent pane
            time.sleep(1.5)

        finally:
            # Restore tmux PATH immediately after sessions are created
            _set_tmux_global_path(old_path)

        # TmuxCC state indicators (allow variants to avoid locale/version UI drift).
        WAITING_INDICATORS = (
            "Pending approval",
            "Pending Approval",
            "Waiting:",
            "Waiting",
            "Pending",
        )
        missed_tmuxcc_samples: list[str] = []

        trial_latencies: list[float] = []
        seen_approval_ts = None
        deadline = time.time() + 90

        while time.time() < deadline:
            transitions = read_transition_log(log_file)

            new_approval = next(
                (t for t in transitions
                 if t["state"] == "AWAITING_APPROVAL"
                 and (seen_approval_ts is None or t["ts"] > seen_approval_ts)),
                None,
            )

            if new_approval:
                approval_ts      = new_approval["ts"]
                seen_approval_ts = approval_ts

                detect_deadline = time.time() + (poll_ms / 1000 * 3 + 1.0)
                detected = False
                while time.time() < detect_deadline:
                    # TmuxCC runs as a full-screen TUI; its UI is usually rendered on the
                    # terminal's alternate screen, so capture that first, with fallback to
                    # the normal pane buffer for portability across tmux versions.
                    cap_alt_r = subprocess.run(
                        ["tmux", "capture-pane", "-t", tmuxcc_sid, "-a", "-p"],
                        capture_output=True, text=True,
                    )
                    content = cap_alt_r.stdout
                    if not content.strip():
                        cap_r = subprocess.run(
                            ["tmux", "capture-pane", "-t", tmuxcc_sid, "-p"],
                            capture_output=True, text=True,
                        )
                        content = cap_r.stdout
                    if any(ind in content for ind in WAITING_INDICATORS):
                        trial_latencies.append((time.time() - approval_ts) * 1000)
                        detected = True
                        break
                    if len(missed_tmuxcc_samples) < 2 and content.strip():
                        missed_tmuxcc_samples.append(content[-800:])
                    time.sleep(POLL_SEC)

                if not detected:
                    print(f"    Trial {trial+1}: approval at t={approval_ts:.3f} — "
                          f"TmuxCC never showed waiting indicator within "
                          f"{round(poll_ms / 1000 * 3 + 1, 1)}s")
                    if missed_tmuxcc_samples:
                        sample = missed_tmuxcc_samples[-1].replace("\n", " | ")
                        print(f"      TmuxCC pane sample: {sample[:220]}")

            if any(t["state"] == "COMPLETED" for t in transitions):
                break
            if agent_pane_target and subprocess.run(
                ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True, text=True
            ).returncode == 0:
                pane_listing = subprocess.run(
                    ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"],
                    capture_output=True, text=True
                )
                if agent_pane_target not in pane_listing.stdout.splitlines():
                    break
            elif agent_pane_target is None:
                break
            time.sleep(POLL_SEC)

        latencies.extend(trial_latencies)
        status_str = (
            f"{len(trial_latencies)} latencies, mean={round(mean(trial_latencies), 1)}ms"
            if trial_latencies else "no waiting indicator detected in TmuxCC pane"
        )
        print(f"    Trial {trial+1}/{n_trials}: {status_str}")

        if agent_pane_target:
            agent_window_target = agent_pane_target.rsplit(".", 1)[0]
            subprocess.run(["tmux", "kill-window", "-t", agent_window_target], capture_output=True)
            _unregister_tmux_window_for_cleanup(agent_window_target)
        subprocess.run(["tmux", "kill-session", "-t", tmuxcc_sid], capture_output=True)
        _unregister_tmux_session_for_cleanup(tmuxcc_sid)
        shutil.rmtree(fake_dir, ignore_errors=True)

    if not latencies:
        return {
            "status": "no_waiting_detected",
            "poll_interval_ms": poll_ms,
            "note": (
                "TmuxCC did not show a waiting indicator in its pane during any "
                "AWAITING_APPROVAL transition. TmuxCC may filter on #{pane_current_command} "
                "and reject the fake 'claude' wrapper, or its waiting patterns did not "
                "match the approval prompt format."
            ),
        }

    return {
        "status": "ok",
        "measurement": "tmuxcc_pane_captured_at_20ms",
        "detection_event": "AWAITING_APPROVAL in log → 'Pending approval' in TmuxCC pane",
        "tool": "TmuxCC",
        "trials_run":         n_trials,
        "latencies_measured": len(latencies),
        "poll_interval_ms":   poll_ms,
        "poll_interval_source": "extracted from 'tmuxcc --help' at runtime",
        "latency_ms": {
            "p50":  pct(latencies, 50),
            "p95":  pct(latencies, 95),
            "mean": round(mean(latencies), 3) if latencies else None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 3b: AoE — actual detection latency via 'aoe session show --json'
# ─────────────────────────────────────────────────────────────────────────────
def measure_aoe_detection_latency(scenario: str, log_file: str, n_trials: int = 3) -> dict:
    """
    Measure how long AoE takes to detect an agent waiting for approval.

    Flow per trial:
      1. Create a fake 'claude' wrapper (exec's unified_agent.py).
      2. Override PATH in tmux's global env so 'aoe session start' finds
         the wrapper when it runs 'claude' in a new tmux window.
      3. 'aoe add' + 'aoe session start' — AoE creates the tmux session and
         starts tracking it with content-based state detection:
           waiting: (?i)[y/n]|[yes/no]|confirm|approve|allow
           running: (?i)(thinking|processing|generating|analyzing|working)
      4. Poll 'aoe session show --json' every 20ms for status == 'waiting'.
      5. Latency = detection_ts − AWAITING_APPROVAL log timestamp.

    unified_agent.py now outputs "Allow this action? [y/n]" which matches
    AoE's waiting detection pattern.
    Requires $TMUX (auto-bootstrapped by main() if needed).
    """
    if scenario == "simple":
        return {
            "status": "skipped",
            "reason": "simple scenario has no permission prompts; no 'waiting' state to detect",
        }

    if not os.environ.get("TMUX"):
        return {
            "status": "requires_tmux",
            "reason": (
                "AoE's 'aoe session start' requires $TMUX. "
                "main() should have auto-bootstrapped into tmux — "
                "if you see this, run the benchmark from inside tmux."
            ),
        }

    print(f"  [aoe] measuring detection latency "
          f"(aoe session start + fake claude, scenario={scenario}, trials={n_trials})...")

    POLL_SEC = 0.020
    latencies: list[float] = []

    for trial in range(n_trials):
        ws = make_workspace()
        open(log_file, "w").close()
        bench_title = f"bench-aoe-{int(time.time())}"
        aoe_profile = f"bench-{int(time.time())}-{trial+1}"

        # 1. Build fake 'claude' that runs our agent
        fake_dir = _make_fake_claude_bin(scenario, log_file)
        new_path = f"{fake_dir}:{os.environ.get('PATH', '')}"

        # 2. Save and override PATH in tmux global env so new windows inherit it
        old_path_r = subprocess.run(
            ["tmux", "show-environment", "-g", "PATH"],
            capture_output=True, text=True,
        )
        old_path = (old_path_r.stdout.strip().split("=", 1)[1]
                    if "=" in old_path_r.stdout else os.environ.get("PATH", ""))
        _set_tmux_global_path(new_path)
        env_with_fake = {
            **os.environ,
            "PATH": new_path,
            "AGENT_OF_EMPIRES_PROFILE": aoe_profile,
            "SYNTH_APPROVAL_HOLD_SEC": "5",
        }

        try:
            # 3. Register workspace with AoE
            add_r = subprocess.run(
                [AOE, "add", ws, "-t", bench_title, "-c", "claude"],
                capture_output=True, text=True, cwd=ws, env=env_with_fake,
            )
            if add_r.returncode != 0:
                print(f"    Trial {trial+1}: aoe add failed — {add_r.stderr.strip()[:80]}")
                _set_tmux_global_path(old_path)
                shutil.rmtree(fake_dir, ignore_errors=True)
                cleanup(ws)
                continue

            # 4. 'aoe session start' runs our fake 'claude' (inherits modified PATH)
            start_r = subprocess.run(
                [AOE, "session", "start", bench_title],
                capture_output=True, text=True, env=env_with_fake,
            )
        finally:
            _set_tmux_global_path(old_path)

        if start_r.returncode != 0:
            print(f"    Trial {trial+1}: aoe session start failed — {start_r.stderr.strip()[:80]}")
            subprocess.run([AOE, "remove", bench_title], capture_output=True, env=env_with_fake)
            shutil.rmtree(fake_dir, ignore_errors=True)
            cleanup(ws)
            continue

        # 5. Resolve tmux session name AoE created
        list_r = subprocess.run([AOE, "list", "--json"], capture_output=True, text=True, env=env_with_fake)
        try:
            sessions = json.loads(list_r.stdout)
        except json.JSONDecodeError:
            print(f"    Trial {trial+1}: could not parse aoe list --json")
            subprocess.run([AOE, "remove", bench_title], capture_output=True, env=env_with_fake)
            shutil.rmtree(fake_dir, ignore_errors=True)
            cleanup(ws)
            continue

        sess = next((s for s in sessions if s.get("title") == bench_title), None)
        if not sess:
            print(f"    Trial {trial+1}: session not in aoe list after start")
            subprocess.run([AOE, "remove", bench_title], capture_output=True, env=env_with_fake)
            shutil.rmtree(fake_dir, ignore_errors=True)
            cleanup(ws)
            continue

        session_id = sess.get("id", "")
        tmux_name  = f"aoe_{bench_title}_{session_id[:8]}"

        # Wait up to 3s for AoE to create the tmux session
        tmux_found = False
        for _ in range(15):
            if subprocess.run(["tmux", "has-session", "-t", tmux_name],
                              capture_output=True).returncode == 0:
                tmux_found = True
                break
            time.sleep(0.2)

        if not tmux_found:
            ls_r = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            candidates = [s for s in ls_r.stdout.strip().splitlines()
                          if bench_title in s or (session_id and session_id[:8] in s)]
            if candidates:
                tmux_name  = candidates[0]
                tmux_found = True

        if not tmux_found:
            print(f"    Trial {trial+1}: AoE tmux session not found (expected {tmux_name!r})")
            subprocess.run([AOE, "remove", bench_title], capture_output=True, env=env_with_fake)
            shutil.rmtree(fake_dir, ignore_errors=True)
            cleanup(ws)
            continue
        _register_tmux_session_for_cleanup(tmux_name)

        # 6. Poll `aoe status --json` for live waiting count at each AWAITING_APPROVAL.
        # `aoe session show --json` returns stored instance metadata and does not
        # refresh status on demand, so it is not suitable for latency measurement.
        trial_latencies: list[float] = []
        seen_approval_ts = None
        deadline = time.time() + 90

        while time.time() < deadline:
            transitions = read_transition_log(log_file)

            new_approval = next(
                (t for t in transitions
                 if t["state"] == "AWAITING_APPROVAL"
                 and (seen_approval_ts is None or t["ts"] > seen_approval_ts)),
                None,
            )

            if new_approval:
                approval_ts      = new_approval["ts"]
                seen_approval_ts = approval_ts

                detect_deadline = time.time() + 5.0
                detected = False
                aoe_status_samples: list[str] = []
                while time.time() < detect_deadline:
                    show_r = subprocess.run(
                        [AOE, "status", "--json"],
                        capture_output=True, text=True,
                        env=env_with_fake,
                    )
                    if show_r.returncode == 0:
                        try:
                            data = json.loads(show_r.stdout)
                            waiting_count = data.get("waiting") if isinstance(data, dict) else None
                            if isinstance(waiting_count, int) and waiting_count > 0:
                                trial_latencies.append(
                                    (time.time() - approval_ts) * 1000
                                )
                                detected = True
                                break
                            if len(aoe_status_samples) < 4:
                                if isinstance(data, dict):
                                    aoe_status_samples.append(json.dumps(data, sort_keys=True))
                                else:
                                    status_pairs = _extract_status_strings(data)
                                    if status_pairs:
                                        aoe_status_samples.append(
                                            ", ".join(f"{k}={v}" for k, v in status_pairs[:4])
                                        )
                        except json.JSONDecodeError:
                            pass
                    time.sleep(POLL_SEC)

                if not detected:
                    print(f"    Trial {trial+1}: approval at t={approval_ts:.3f} — "
                          f"aoe never reported 'waiting' within 5s")
                    if aoe_status_samples:
                        print(f"      AoE status samples: {aoe_status_samples[-1][:240]}")

            if any(t["state"] == "COMPLETED" for t in transitions):
                break
            if subprocess.run(["tmux", "has-session", "-t", tmux_name],
                               capture_output=True).returncode != 0:
                break
            time.sleep(POLL_SEC)

        latencies.extend(trial_latencies)
        status_str = (
            f"{len(trial_latencies)} latencies, mean={round(mean(trial_latencies), 1)}ms"
            if trial_latencies else "no 'waiting' state detected"
        )
        print(f"    Trial {trial+1}/{n_trials}: {status_str}")

        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        _unregister_tmux_session_for_cleanup(tmux_name)
        subprocess.run([AOE, "remove", bench_title], capture_output=True, env=env_with_fake)
        shutil.rmtree(fake_dir, ignore_errors=True)
        cleanup(ws)

    if not latencies:
        return {
            "status": "no_waiting_states_detected",
            "note": (
                "AoE did not report 'waiting' during AWAITING_APPROVAL transitions. "
                "AoE may require the real claude binary for hook-based state updates, "
                "its status may be reported under a different JSON field/value, or the "
                "fake 'claude' PATH override did not propagate to the new tmux window."
            ),
        }

    return {
        "status": "ok",
        "measurement": "aoe_status_json_polled_at_20ms",
        "detection_event": "AWAITING_APPROVAL in log → aoe status --json waiting > 0",
        "trials_run":         n_trials,
        "latencies_measured": len(latencies),
        "poll_interval_ms":   POLL_SEC * 1000,
        "latency_ms": {
            "p50":  pct(latencies, 50),
            "p95":  pct(latencies, 95),
            "mean": round(mean(latencies), 3) if latencies else None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 4: Detection accuracy from pane text
# ─────────────────────────────────────────────────────────────────────────────
def measure_text_detection_accuracy(scenario: str, log_file: str) -> dict:
    """
    Run the unified agent in a bare tmux pane (no AGentviz wrapper) and classify
    state using the SAME patterns that TmuxCC and AoE actually use at runtime:

      Processing:        spinner char in pane title  (TmuxCC: #{pane_title})
                         OR pane content matches (?i)(thinking|processing|...)
      AwaitingApproval:  pane content matches (?i)[y/n]|[yes/no]|allow
                         (exactly the patterns from TmuxCC/AoE binary strings)
      Idle:              neither

    unified_agent.py now outputs "Allow this action? [y/n]" in approval prompts
    so both patterns match.
    """
    import re
    print(f"  [accuracy] running agent in bare tmux pane (scenario={scenario})...")

    sid = f"bench-acc-{int(time.time())}"
    ws = make_workspace()
    open(log_file, "w").close()

    pane_target = f"{sid}:0.0"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", sid,
         "-e", f"BENCH_SCENARIO={scenario}",
         "-e", f"BENCH_LOG_FILE={log_file}",
         "-e", "SYNTH_AUTO_INPUT=1",
         "-e", f"BENCH_PANE_TARGET={pane_target}",
         f"python3 {AGENT_SCRIPT}"],
        capture_output=True, check=True
    )
    _register_tmux_session_for_cleanup(sid)

    # Patterns extracted from TmuxCC and AoE binary strings
    SPINNER_CHARS      = set("⠋⠙⠸⠴⠦⠧⠖⠏")
    PROCESSING_RE      = re.compile(r'(?i)(thinking|processing|generating|analyzing|working)')
    AWAITING_RE        = re.compile(r'(?i)(\[y/n\]|\[yes/no\]|allow\b|confirm\b|approve\b)')

    # Poll the pane content every 100ms, record detected states
    detected_states = []
    deadline = time.time() + 90

    while time.time() < deadline:
        title_result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", sid, "#{pane_title}"],
            capture_output=True, text=True
        )
        title = title_result.stdout.strip()

        content_result = subprocess.run(
            ["tmux", "capture-pane", "-t", sid, "-p"],
            capture_output=True, text=True
        )
        content = content_result.stdout

        has_spinner_in_title = any(c in title for c in SPINNER_CHARS)
        has_processing       = bool(PROCESSING_RE.search(content))
        has_awaiting         = bool(AWAITING_RE.search(content))

        # Priority: spinner title → Processing (agent set title, can trust it)
        # Then: awaiting pattern → AwaitingApproval
        # Then: processing pattern in content → Processing
        if has_spinner_in_title:
            state = "Processing"
        elif has_awaiting:
            state = "AwaitingApproval"
        elif has_processing:
            state = "Processing"
        else:
            if "Task complete" in content:
                detected_states.append({"ts": time.time(), "state": "Completed"})
                break
            if subprocess.run(["tmux", "has-session", "-t", sid],
                               capture_output=True).returncode != 0:
                detected_states.append({"ts": time.time(), "state": "Completed"})
                break
            state = "Idle"

        detected_states.append({"ts": time.time(), "state": state})
        time.sleep(0.1)

    subprocess.run(["tmux", "kill-session", "-t", sid], capture_output=True)
    _unregister_tmux_session_for_cleanup(sid)
    cleanup(ws)

    transitions = read_transition_log(log_file)

    # Map expected states
    expected_states = {"THINKING": "Processing", "TOOL_USE": "Processing",
                       "AWAITING_APPROVAL": "AwaitingApproval",
                       "COMPLETED": "Completed", "STARTING": "Idle"}

    expected = [expected_states[t["state"]] for t in transitions
                if t["state"] in expected_states]

    # Count detected occurrences of each expected state
    detected_state_set = {d["state"] for d in detected_states}
    hits = sum(1 for e in set(expected) if e in detected_state_set)
    accuracy = round(hits / len(set(expected)) * 100, 1) if expected else 0

    return {
        "scenario": scenario,
        "expected_unique_states": list(set(expected)),
        "detected_unique_states": list(detected_state_set),
        "state_coverage_pct": accuracy,
        "total_transitions_in_log": len(transitions),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT 5: Monitoring overhead (CPU + RAM)
# ─────────────────────────────────────────────────────────────────────────────
def measure_overhead() -> dict:
    """
    Measure peak RSS for each dashboard process using /usr/bin/time -l.

    TmuxCC and AoE are interactive TUI processes requiring a real PTY —
    they cannot run headlessly in a subprocess. We measure peak RSS from
    a single invocation of each tool's non-interactive command, which is
    the standard proxy for Rust/native binary overhead measurement.

    AGentviz server RSS is sampled live if the server is running.
    """
    print("  [overhead] measuring peak RSS per dashboard...")

    results = {}

    # ── AGentviz server RSS ──
    import urllib.request
    try:
        urllib.request.urlopen(f"{AGENTVIZ_URL}/", timeout=1)
        # Server is up — find its process
        import psutil
        # Use lsof-equivalent: find the process actually listening on port 8787
        # Fallback: find max-RSS process with uvicorn/agentviz+server in cmdline
        server_rss = None
        for proc in psutil.process_iter(["pid", "cmdline", "memory_info"]):
            try:
                cmdline = proc.info["cmdline"] or []
                is_server = (
                    any("uvicorn" in c for c in cmdline) or
                    (any("agentviz" in c for c in cmdline) and any("server" in c for c in cmdline))
                )
                if is_server:
                    rss = proc.info["memory_info"].rss / (1024 * 1024)
                    if server_rss is None or rss > server_rss:
                        server_rss = rss   # take the largest (actual uvicorn, not wrapper)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if server_rss:
            results["agentviz_server"] = {
                "peak_rss_mb": round(server_rss, 1),
                "method": "max RSS among uvicorn/agentviz-server processes (ps)",
                "note": "Persistent daemon — always resident while monitoring",
            }
            print(f"  agentviz server: {round(server_rss,1)}MB")
    except Exception:
        results["agentviz_server"] = {"status": "server_not_running"}
        print("  agentviz server: not running")

    # ── TmuxCC peak RSS (/usr/bin/time -l) ──
    r = subprocess.run(
        ["/usr/bin/time", "-l", TMUXCC, "--help"],
        capture_output=True, text=True
    )
    combined = r.stdout + r.stderr
    rss_kb = None
    for line in combined.splitlines():
        if "maximum resident" in line.lower():
            try:
                rss_kb = int(line.strip().split()[0])
            except ValueError:
                pass
    tmuxcc_rss = round(rss_kb / (1024 * 1024), 1) if rss_kb else None
    results["tmuxcc"] = {
        "peak_rss_mb": tmuxcc_rss,
        "method": "/usr/bin/time -l tmuxcc --help  (binary init, no active monitoring)",
        "poll_interval_ms": extract_poll_interval_ms(TMUXCC),
        "note": "TUI — requires terminal for live use; binary init cost shown here",
    }
    print(f"  tmuxcc:         {tmuxcc_rss}MB (binary peak RSS)")

    # ── AoE peak RSS (/usr/bin/time -l) ──
    r = subprocess.run(
        ["/usr/bin/time", "-l", AOE, "status"],
        capture_output=True, text=True
    )
    combined = r.stdout + r.stderr
    rss_kb = None
    for line in combined.splitlines():
        if "maximum resident" in line.lower():
            try:
                rss_kb = int(line.strip().split()[0])
            except ValueError:
                pass
    aoe_rss = round(rss_kb / (1024 * 1024), 1) if rss_kb else None
    results["aoe"] = {
        "peak_rss_mb": aoe_rss,
        "method": "/usr/bin/time -l aoe status  (binary init + session query)",
        "poll_interval_ms": extract_poll_interval_ms(AOE),
        "note": "TUI — requires terminal for live use; binary init cost shown here",
    }
    print(f"  aoe:            {aoe_rss}MB (binary peak RSS)")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Equal cross-dashboard benchmark")
    parser.add_argument("--scenario", default="medium",
                        choices=["simple", "medium", "complex"])
    parser.add_argument("--trials",   type=int, default=5)
    parser.add_argument("--skip-agentviz", action="store_true")
    args = parser.parse_args()
    _install_cleanup_handlers()

    # TmuxCC and AoE both require $TMUX.  Auto-bootstrap into a tmux session
    # so the user never has to set this up manually.
    _maybe_relaunch_in_tmux()
    _cleanup_stale_benchmark_tmux_resources()

    print()
    print("=" * 62)
    print("  AGentviz vs TmuxCC vs Agent of Empires — Equal Benchmark")
    print(f"  Scenario: {args.scenario}   Trials: {args.trials}")
    print("=" * 62)

    log_file = tempfile.mktemp(suffix=".jsonl")
    results  = {
        "timestamp":  datetime.now().isoformat(),
        "scenario":   args.scenario,
        "trials":     args.trials,
        "methodology": (
            "unified_agent.py runs the same coding task under each dashboard. "
            "AGentviz latency: AWAITING_APPROVAL in the synthetic agent log to "
            "'waiting_for_input' Socket.IO event arrival (same approval-detection "
            "latency definition used for all dashboards). "
            "TmuxCC latency: fake 'claude' wrapper (exec unified_agent.py) runs in a new "
            "window of the current attached tmux session so TmuxCC can discover it; "
            "TmuxCC's TUI pane is captured at 20ms (alternate-screen capture first) until "
            "a waiting indicator appears. "
            "AoE latency: 'aoe session start' (requires $TMUX) runs the fake 'claude' "
            "wrapper via PATH override in an isolated AoE profile; 'aoe status --json' is "
            "polled at 20ms until waiting > 0. "
            "Benchmark auto-bootstraps into tmux if $TMUX is not set. "
            "Approval prompts include 'Allow this action? [Y/n]' plus 'Yes, allow once/always' "
            "to match TmuxCC/AoE Claude waiting patterns. "
            "Detection accuracy: pane content polled at 100ms using TmuxCC/AoE patterns: "
            "spinner-in-title or (?i)(thinking|processing|...) → Processing; "
            "(?i)[y/n]|allow|confirm|approve → AwaitingApproval."
        ),
    }

    # ── tmux capture baseline ──────────────────────────────────────────
    print("\n─── tmux capture-pane baseline ───")
    tmux_baseline = measure_tmux_capture_latency(n_trials=20)
    results["tmux_capture_baseline"] = tmux_baseline

    # ── AGentviz (approval detection via Socket.IO) ────────────────────
    if not args.skip_agentviz:
        print("\n─── AGentviz (approval detection via Socket.IO) ───")
        all_latencies = {"p50": [], "p95": [], "mean": []}
        all_accuracy  = []
        for trial in range(args.trials):
            print(f"  Trial {trial+1}/{args.trials}...", end=" ", flush=True)
            r = run_agentviz_trial(args.scenario, log_file)
            print(f"  latency_p50={r.get('latency_ms', {}).get('p50')}ms  "
                  f"accuracy={r.get('detection_accuracy_pct')}%")
            if r["status"] == "ok":
                lms = r["latency_ms"]
                if lms["p50"]: all_latencies["p50"].append(lms["p50"])
                if lms["p95"]: all_latencies["p95"].append(lms["p95"])
                if lms["mean"]: all_latencies["mean"].append(lms["mean"])
                all_accuracy.append(r["detection_accuracy_pct"])
            elif r["status"] == "skipped":
                print(f"  SKIP: {r['reason']}")
                break

        results["agentviz"] = {
            "detection_mechanism": "push (Socket.IO), waiting_for_input arrival",
            "detection_event": "AWAITING_APPROVAL in log → waiting_for_input socket event arrival",
            "latency_ms": {
                "p50_mean_across_trials":  round(mean(all_latencies["p50"]), 3)  if all_latencies["p50"]  else None,
                "p95_mean_across_trials":  round(mean(all_latencies["p95"]), 3)  if all_latencies["p95"]  else None,
                "mean_mean_across_trials": round(mean(all_latencies["mean"]), 3) if all_latencies["mean"] else None,
            },
            "detection_accuracy_pct_mean": round(mean(all_accuracy), 1) if all_accuracy else None,
            "trials_completed": len(all_accuracy),
        }

    # ── TmuxCC — actually run it and measure detection latency ────────────
    print("\n─── TmuxCC (actually running with fake claude wrapper) ───")
    results["tmuxcc"] = measure_tmuxcc_detection_latency(
        args.scenario, log_file, n_trials=args.trials
    )
    tc = results["tmuxcc"]
    if tc.get("status") == "ok":
        lms = tc["latency_ms"]
        print(f"  p50={lms['p50']}ms  p95={lms['p95']}ms  "
              f"[{tc['latencies_measured']} samples, poll={tc['poll_interval_ms']}ms]")
    else:
        print(f"  {tc.get('status')}: {tc.get('note', tc.get('reason', ''))[:80]}")

    # ── Agent of Empires — actually run via aoe session start ──────────
    print("\n─── Agent of Empires (aoe session start + fake claude wrapper) ───")
    results["aoe"] = measure_aoe_detection_latency(
        args.scenario, log_file, n_trials=args.trials
    )
    ao = results["aoe"]
    if ao.get("status") == "ok":
        lms = ao["latency_ms"]
        print(f"  p50={lms['p50']}ms  p95={lms['p95']}ms  "
              f"[{ao['latencies_measured']} samples]")
    else:
        print(f"  {ao.get('status')}: {ao.get('note', ao.get('reason', ''))[:80]}")

    # ── Detection accuracy (text parsing) ──────────────────────────────
    print("\n─── Detection Accuracy (state classification) ───")
    acc = measure_text_detection_accuracy(args.scenario, log_file)
    results["tui_detection_accuracy"] = acc
    print(f"  Expected states: {acc['expected_unique_states']}")
    print(f"  Detected:        {acc['detected_unique_states']}")
    print(f"  Coverage:        {acc['state_coverage_pct']}%")

    # ── Monitoring overhead ─────────────────────────────────────────────
    print("\n─── Monitoring Overhead (RAM) ───")
    results["overhead"] = measure_overhead()

    # ── Save results ────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"equal_{args.scenario}_{ts}.json"
    out.write_text(json.dumps(results, indent=2))

    # ── Print summary ───────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  RESULTS SUMMARY")
    print("=" * 62)
    av = results.get("agentviz", {})
    tc = results.get("tmuxcc", {})
    ao = results.get("aoe", {})
    print(f"\n  Detection Latency (scenario={args.scenario}):")

    # AGentviz
    av_lms = av.get("latency_ms", {})
    print(f"    AGentviz  p50={av_lms.get('p50_mean_across_trials')}ms  "
          f"p95={av_lms.get('p95_mean_across_trials')}ms  [approval detection via socket]")

    # TmuxCC
    if tc.get("status") == "ok":
        tc_lms = tc["latency_ms"]
        print(f"    TmuxCC    p50={tc_lms['p50']}ms  p95={tc_lms['p95']}ms  "
              f"[measured, poll={tc.get('poll_interval_ms')}ms]")
    else:
        print(f"    TmuxCC    {tc.get('status', 'n/a')}: "
              f"{tc.get('note', tc.get('reason', ''))[:70]}")

    # AoE
    if ao.get("status") == "ok":
        ao_lms = ao["latency_ms"]
        print(f"    AoE       p50={ao_lms['p50']}ms  p95={ao_lms['p95']}ms  "
              f"[measured, 20ms poll]")
    else:
        print(f"    AoE       {ao.get('status', 'n/a')}: "
              f"{ao.get('note', ao.get('reason', ''))[:70]}")

    ov = results.get("overhead", {})
    print(f"\n  Monitoring Overhead (peak RSS):")
    for tool in ["agentviz_server", "tmuxcc", "aoe"]:
        t = ov.get(tool, {})
        print(f"    {tool:<20} {t.get('peak_rss_mb')}MB")

    print(f"\n  Saved → {out}")


if __name__ == "__main__":
    main()
