#!/usr/bin/env python3
"""
Benchmark harness for AgentViz.

Runs 4 benchmark suites against a running AgentViz server:
  a) Latency   - per-event propagation latency (state file -> socket)
  b) Throughput - sustained events/sec with rapid tool cycles
  c) Scalability - degradation curve across 1/2/4/8 concurrent agents
  d) Reliability - graceful handling of SIGINT kills

Usage:
  1. Start the AgentViz server:  agentviz server
  2. Run benchmarks:             python3 -m benchmarks.benchmark_harness

Output: benchmarks/results/benchmark_YYYYMMDD_HHMMSS.json
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, median

# Optional: psutil for CPU/memory measurement
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# Optional: socketio for latency measurement
try:
    import socketio
    SOCKETIO_AVAILABLE = True
except ImportError:
    SOCKETIO_AVAILABLE = False


SERVER_URL = "http://localhost:8787"
SYNTH_AGENT_SCRIPT = os.path.join(os.path.dirname(__file__), "synthetic_agent.py")
AGENTVIZ_RUN_CMD = "agentviz"


def create_temp_workspace():
    """Create an isolated temp workspace with dummy files."""
    workspace = tempfile.mkdtemp(prefix="agentviz-bench-")
    # Create minimal file structure
    (Path(workspace) / "src").mkdir()
    (Path(workspace) / "src" / "main.py").write_text("# benchmark dummy\nprint('hello')\n")
    (Path(workspace) / "README.md").write_text("# Benchmark workspace\n")
    return workspace


def cleanup_workspace(workspace):
    """Remove temp workspace."""
    import shutil
    try:
        shutil.rmtree(workspace)
    except Exception:
        pass


def percentile(data, p):
    """Compute the p-th percentile of a sorted list."""
    if not data:
        return 0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


# ---------------------------------------------------------------------------
# a) Latency Test
# ---------------------------------------------------------------------------
def run_latency_test():
    """
    Measure per-event latency from state file write to socket delivery.

    Launches 1 synthetic agent with auto-input, records timestamps of
    agent_state_change events received via socketio, and computes latencies
    relative to the event timestamps embedded in the data.
    """
    print("\n--- Latency Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    workspace = create_temp_workspace()
    event_times = []  # (emitted_ts, received_ts)
    agent_id = f"bench-latency-{int(time.time())}"
    done = False

    sio = socketio.Client()

    @sio.on("agent_event")
    def on_event(data):
        nonlocal done
        if data.get("agent_id") != agent_id:
            return
        received_at = time.time()
        emitted_at = data.get("timestamp", received_at)
        event_times.append((emitted_at, received_at))
        if data.get("event_type") == "agent_stopped":
            done = True

    try:
        sio.connect(SERVER_URL)
    except Exception as e:
        print(f"  ERROR: Cannot connect to server: {e}")
        cleanup_workspace(workspace)
        return {"status": "error", "reason": str(e)}

    # Launch synthetic agent via agentviz run
    env = os.environ.copy()
    env["SYNTH_AUTO_INPUT"] = "1"
    env["SYNTH_TOOL_CYCLES"] = "10"
    env["SYNTH_WORK_TIME"] = "0.1"
    env["SYNTH_THINK_TIME"] = "0.1"
    env["SYNTH_OUTPUT_KB"] = "5"
    env["SYNTH_PERMISSION_PROMPTS"] = "0"

    proc = subprocess.Popen(
        [AGENTVIZ_RUN_CMD, "run", "-w", workspace, "-i", agent_id, "synthetic",
         "python3", SYNTH_AGENT_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    # Wait for completion (timeout 30s)
    deadline = time.time() + 30
    while not done and time.time() < deadline:
        time.sleep(0.1)

    proc.wait(timeout=5)
    sio.disconnect()
    cleanup_workspace(workspace)

    if not event_times:
        print("  WARNING: No events received")
        return {"status": "no_events", "events_count": 0}

    latencies = [(recv - emit) * 1000 for emit, recv in event_times if recv >= emit]

    if not latencies:
        print("  WARNING: No valid latencies computed")
        return {"status": "no_valid_latencies", "events_count": len(event_times)}

    results = {
        "status": "ok",
        "events_count": len(event_times),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(max(latencies), 2),
            "mean": round(mean(latencies), 2),
        }
    }
    print(f"  Events: {results['events_count']}")
    print(f"  Latency p50={results['latency_ms']['p50']}ms  "
          f"p95={results['latency_ms']['p95']}ms  "
          f"max={results['latency_ms']['max']}ms")
    return results


# ---------------------------------------------------------------------------
# b) Throughput Test
# ---------------------------------------------------------------------------
def run_throughput_test():
    """
    Measure sustained events/sec with rapid tool cycles.
    """
    print("\n--- Throughput Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    workspace = create_temp_workspace()
    event_count = 0
    first_event_at = None
    last_event_at = None
    agent_id = f"bench-throughput-{int(time.time())}"
    done = False

    sio = socketio.Client()

    @sio.on("agent_event")
    def on_event(data):
        nonlocal event_count, first_event_at, last_event_at, done
        if data.get("agent_id") != agent_id:
            return
        now = time.time()
        event_count += 1
        if first_event_at is None:
            first_event_at = now
        last_event_at = now
        if data.get("event_type") == "agent_stopped":
            done = True

    try:
        sio.connect(SERVER_URL)
    except Exception as e:
        print(f"  ERROR: Cannot connect to server: {e}")
        cleanup_workspace(workspace)
        return {"status": "error", "reason": str(e)}

    env = os.environ.copy()
    env["SYNTH_AUTO_INPUT"] = "1"
    env["SYNTH_TOOL_CYCLES"] = "200"
    env["SYNTH_WORK_TIME"] = "0"
    env["SYNTH_THINK_TIME"] = "0"
    env["SYNTH_OUTPUT_KB"] = "1"
    env["SYNTH_PERMISSION_PROMPTS"] = "0"

    proc = subprocess.Popen(
        [AGENTVIZ_RUN_CMD, "run", "-w", workspace, "-i", agent_id, "synthetic",
         "python3", SYNTH_AGENT_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    deadline = time.time() + 60
    while not done and time.time() < deadline:
        time.sleep(0.1)

    proc.wait(timeout=5)
    sio.disconnect()
    cleanup_workspace(workspace)

    if first_event_at and last_event_at and last_event_at > first_event_at:
        duration = last_event_at - first_event_at
        events_per_sec = event_count / duration
    else:
        duration = 0
        events_per_sec = 0

    results = {
        "status": "ok",
        "events_count": event_count,
        "duration_sec": round(duration, 2),
        "events_per_sec": round(events_per_sec, 2),
    }
    print(f"  Events: {event_count} in {results['duration_sec']}s")
    print(f"  Throughput: {results['events_per_sec']} events/sec")
    return results


# ---------------------------------------------------------------------------
# c) Scalability Test
# ---------------------------------------------------------------------------
def run_scalability_test():
    """
    Launch 1, 2, 4, 8 agents simultaneously and measure degradation.
    """
    print("\n--- Scalability Test ---")

    scale_levels = [1, 2, 4, 8]
    results = {"status": "ok", "levels": []}

    for n_agents in scale_levels:
        print(f"  Testing with {n_agents} agent(s)...")
        workspaces = []
        procs = []
        start_time = time.time()

        # Measure initial resource usage
        cpu_before = None
        mem_before = None
        if PSUTIL_AVAILABLE:
            proc_self = psutil.Process(os.getpid())
            cpu_before = psutil.cpu_percent(interval=None)
            mem_before = psutil.virtual_memory().used / (1024 * 1024)  # MB

        for i in range(n_agents):
            ws = create_temp_workspace()
            workspaces.append(ws)
            agent_id = f"bench-scale-{n_agents}-{i}-{int(time.time())}"

            env = os.environ.copy()
            env["SYNTH_AUTO_INPUT"] = "1"
            env["SYNTH_TOOL_CYCLES"] = "20"
            env["SYNTH_WORK_TIME"] = "0.05"
            env["SYNTH_THINK_TIME"] = "0.05"
            env["SYNTH_OUTPUT_KB"] = "5"
            env["SYNTH_PERMISSION_PROMPTS"] = "0"

            p = subprocess.Popen(
                [AGENTVIZ_RUN_CMD, "run", "-w", ws, "-i", agent_id, "synthetic",
                 "python3", SYNTH_AGENT_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            procs.append(p)

        # Wait for all to finish (timeout 60s)
        deadline = time.time() + 60
        for p in procs:
            remaining = max(1, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        elapsed = time.time() - start_time

        # Measure resource usage after
        cpu_delta = None
        mem_delta = None
        if PSUTIL_AVAILABLE and cpu_before is not None:
            cpu_after = psutil.cpu_percent(interval=0.1)
            mem_after = psutil.virtual_memory().used / (1024 * 1024)
            cpu_delta = round(cpu_after, 1)
            mem_delta = round(mem_after - mem_before, 1)

        for ws in workspaces:
            cleanup_workspace(ws)

        level_result = {
            "agents": n_agents,
            "total_time_sec": round(elapsed, 2),
            "per_agent_time_sec": round(elapsed / n_agents, 2) if n_agents > 0 else 0,
        }
        if cpu_delta is not None:
            level_result["cpu_percent"] = cpu_delta
            level_result["memory_delta_mb"] = mem_delta

        results["levels"].append(level_result)
        print(f"    Completed in {level_result['total_time_sec']}s "
              f"({level_result['per_agent_time_sec']}s/agent)")

    return results


# ---------------------------------------------------------------------------
# d) Reliability Test
# ---------------------------------------------------------------------------
def run_reliability_test():
    """
    Launch agent, kill with SIGINT after 3s, check final state.
    Repeat 10 times, report success rate.
    """
    print("\n--- Reliability Test ---")
    attempts = 10
    successes = 0

    for attempt in range(attempts):
        workspace = create_temp_workspace()
        agent_id = f"bench-reliability-{attempt}-{int(time.time())}"

        env = os.environ.copy()
        env["SYNTH_AUTO_INPUT"] = "1"
        env["SYNTH_TOOL_CYCLES"] = "100"
        env["SYNTH_WORK_TIME"] = "0.1"
        env["SYNTH_THINK_TIME"] = "0.1"
        env["SYNTH_OUTPUT_KB"] = "5"
        env["SYNTH_PERMISSION_PROMPTS"] = "0"

        proc = subprocess.Popen(
            [AGENTVIZ_RUN_CMD, "run", "-w", workspace, "-i", agent_id, "synthetic",
             "python3", SYNTH_AGENT_SCRIPT],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        # Let it run for 3 seconds then kill
        time.sleep(3)
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Check backend for final state
        final_state = None
        try:
            import urllib.request
            url = f"{SERVER_URL}/agents/{agent_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                # Backend returns {"agent": {...}, "events": [...]}
                agent_data = data.get("agent", data)
                final_state = agent_data.get("state")
        except Exception:
            pass

        cleanup_workspace(workspace)

        # Any recognized state means the agent was tracked and cleaned up gracefully
        is_success = final_state is not None
        if is_success:
            successes += 1

        status_str = "OK" if is_success else f"FAIL (state={final_state})"
        print(f"  Attempt {attempt+1}/{attempts}: {status_str}")

    success_rate = (successes / attempts) * 100 if attempts > 0 else 0
    results = {
        "status": "ok",
        "attempts": attempts,
        "successes": successes,
        "success_rate_percent": round(success_rate, 1),
    }
    print(f"  Success rate: {results['success_rate_percent']}% ({successes}/{attempts})")
    return results


# ---------------------------------------------------------------------------
# e) Tmux Lifecycle Test
# ---------------------------------------------------------------------------
def run_tmux_lifecycle_test():
    """
    Launch synthetic agent with --tmux-start, verify tmux session + ttyd URL
    are created and cleaned up after agent completes. 5 attempts.
    """
    print("\n--- Tmux Lifecycle Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    import urllib.request
    attempts = 5
    successes = 0
    startup_times = []

    for attempt in range(attempts):
        workspace = create_temp_workspace()
        agent_id = f"bench-tmux-lifecycle-{attempt}-{int(time.time())}"
        ttyd_url = None
        tmux_session = None
        done = False
        session_info_at = None
        start_time = time.time()

        sio = socketio.Client()

        @sio.on("agent_event")
        def on_event(data):
            nonlocal ttyd_url, tmux_session, done, session_info_at
            if data.get("agent_id") != agent_id:
                return
            if data.get("event_type") == "tmux_session_info":
                meta = data.get("metadata", {})
                port = meta.get("ttyd_port")
                if port:
                    ttyd_url = f"http://localhost:{port}"
                tmux_session = meta.get("tmux_session")
                session_info_at = time.time()
            if data.get("event_type") == "agent_stopped":
                done = True

        try:
            sio.connect(SERVER_URL)
        except Exception as e:
            print(f"  Attempt {attempt+1}: ERROR connecting: {e}")
            cleanup_workspace(workspace)
            continue

        env = os.environ.copy()
        env["SYNTH_AUTO_INPUT"] = "1"
        env["SYNTH_TOOL_CYCLES"] = "15"  # enough cycles to keep session alive during checks
        env["SYNTH_WORK_TIME"] = "0.3"   # ~9s total work — outlasts ttyd startup + checks
        env["SYNTH_THINK_TIME"] = "0.1"
        env["SYNTH_OUTPUT_KB"] = "1"
        env["SYNTH_PERMISSION_PROMPTS"] = "0"

        proc = subprocess.Popen(
            [AGENTVIZ_RUN_CMD, "run", "--tmux-start", "-w", workspace, "-i", agent_id,
             "synthetic", "python3", SYNTH_AGENT_SCRIPT],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        # Wait for tmux_session_info (timeout 15s)
        deadline = time.time() + 15
        while tmux_session is None and time.time() < deadline:
            time.sleep(0.2)

        # Give ttyd time to bind its port after Popen (startup race condition)
        if tmux_session is not None:
            time.sleep(1.0)

        # Verify tmux session exists
        tmux_ok = False
        if tmux_session:
            res = subprocess.run(["tmux", "has-session", "-t", tmux_session],
                                 capture_output=True)
            tmux_ok = res.returncode == 0

        # Verify ttyd URL reachable
        ttyd_ok = False
        if ttyd_url:
            try:
                urllib.request.urlopen(ttyd_url, timeout=3)
                ttyd_ok = True
            except Exception:
                pass

        # Wait for agent completion (timeout 30s)
        deadline = time.time() + 30
        while not done and time.time() < deadline:
            time.sleep(0.2)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        time.sleep(1)  # Allow cleanup

        # Verify tmux session cleaned up
        cleanup_ok = True
        if tmux_session:
            res = subprocess.run(["tmux", "has-session", "-t", tmux_session],
                                 capture_output=True)
            cleanup_ok = res.returncode != 0  # Should fail = cleaned up

        # Verify ttyd no longer reachable
        ttyd_cleaned = True
        if ttyd_url:
            try:
                urllib.request.urlopen(ttyd_url, timeout=2)
                ttyd_cleaned = False  # Still reachable = not cleaned up
            except Exception:
                pass

        sio.disconnect()
        cleanup_workspace(workspace)

        ok = tmux_ok and ttyd_ok and cleanup_ok and ttyd_cleaned
        if ok:
            successes += 1
        if session_info_at:
            startup_times.append((session_info_at - start_time) * 1000)

        status = "OK" if ok else f"FAIL (tmux={tmux_ok} ttyd={ttyd_ok} cleanup={cleanup_ok} ttyd_clean={ttyd_cleaned})"
        print(f"  Attempt {attempt+1}/{attempts}: {status}")

    results = {
        "status": "ok",
        "attempts": attempts,
        "successes": successes,
        "success_rate_percent": round((successes / attempts) * 100, 1),
    }
    if startup_times:
        results["startup_time_ms"] = {
            "mean": round(mean(startup_times), 2),
            "p50": round(percentile(startup_times, 50), 2),
        }
    print(f"  Success rate: {results['success_rate_percent']}% ({successes}/{attempts})")
    return results


# ---------------------------------------------------------------------------
# f) Tmux Scalability Test
# ---------------------------------------------------------------------------
def run_tmux_scalability_test():
    """
    Launch 1/2/4/8 concurrent tmux agents, verify all ttyd URLs reachable.
    """
    print("\n--- Tmux Scalability Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    import urllib.request
    scale_levels = [1, 2, 4, 8]
    results = {"status": "ok", "levels": []}

    for n_agents in scale_levels:
        print(f"  Testing with {n_agents} tmux agent(s)...")
        workspaces = []
        procs = []
        agent_ids = []
        ttyd_urls = {}  # agent_id -> url
        start_time = time.time()

        sio = socketio.Client()

        @sio.on("agent_event")
        def on_event(data):
            aid = data.get("agent_id", "")
            if aid not in agent_ids:
                return
            if data.get("event_type") == "tmux_session_info":
                meta = data.get("metadata", {})
                port = meta.get("ttyd_port")
                if port:
                    ttyd_urls[aid] = f"http://localhost:{port}"

        try:
            sio.connect(SERVER_URL)
        except Exception as e:
            print(f"  ERROR: Cannot connect: {e}")
            results["levels"].append({"agents": n_agents, "status": "error", "reason": str(e)})
            continue

        for i in range(n_agents):
            ws = create_temp_workspace()
            workspaces.append(ws)
            agent_id = f"bench-tmux-scale-{n_agents}-{i}-{int(time.time())}"
            agent_ids.append(agent_id)

            env = os.environ.copy()
            env["SYNTH_AUTO_INPUT"] = "1"
            env["SYNTH_TOOL_CYCLES"] = "10"
            env["SYNTH_WORK_TIME"] = "0.05"
            env["SYNTH_THINK_TIME"] = "0.05"
            env["SYNTH_OUTPUT_KB"] = "1"
            env["SYNTH_PERMISSION_PROMPTS"] = "0"

            p = subprocess.Popen(
                [AGENTVIZ_RUN_CMD, "run", "--tmux-start", "-w", ws, "-i", agent_id,
                 "synthetic", "python3", SYNTH_AGENT_SCRIPT],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            procs.append(p)

        # Wait for all ttyd URLs (timeout 20s)
        deadline = time.time() + 20
        while len(ttyd_urls) < n_agents and time.time() < deadline:
            time.sleep(0.3)

        # Verify ttyd URLs reachable
        ttyd_verified = 0
        for aid, url in ttyd_urls.items():
            try:
                urllib.request.urlopen(url, timeout=3)
                ttyd_verified += 1
            except Exception:
                pass

        # Wait for all to finish (timeout 60s)
        deadline = time.time() + 60
        for p in procs:
            remaining = max(1, deadline - time.time())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        elapsed = time.time() - start_time
        sio.disconnect()

        for ws in workspaces:
            cleanup_workspace(ws)

        level_result = {
            "agents": n_agents,
            "total_time_sec": round(elapsed, 2),
            "per_agent_time_sec": round(elapsed / n_agents, 2),
            "ttyd_urls_received": len(ttyd_urls),
            "ttyd_urls_verified": ttyd_verified,
        }
        results["levels"].append(level_result)
        print(f"    Completed in {level_result['total_time_sec']}s  "
              f"ttyd verified: {ttyd_verified}/{n_agents}")

    return results


# ---------------------------------------------------------------------------
# g) Tmux Send-Keys Latency Test
# ---------------------------------------------------------------------------
def run_tmux_send_keys_test():
    """
    Launch 1 tmux agent with permission prompts, measure latency of
    control_send_keys -> state transition round trip.
    """
    print("\n--- Tmux Send-Keys Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    workspace = create_temp_workspace()
    agent_id = f"bench-tmux-keys-{int(time.time())}"
    done = False
    latencies = []
    send_time = None
    waiting = False

    sio = socketio.Client()

    @sio.on("agent_state")
    def on_state(data):
        nonlocal waiting, done, send_time
        if data.get("id") != agent_id:
            return
        state = data.get("state", "")
        if state == "waiting_for_input" and not waiting:
            waiting = True
        elif state != "waiting_for_input" and waiting and send_time is not None:
            # Transitioned away from waiting after we sent keys
            latency = (time.time() - send_time) * 1000
            latencies.append(latency)
            waiting = False
            send_time = None
        if state in ("completed", "stopped"):
            done = True

    @sio.on("agent_event")
    def on_event(data):
        nonlocal done
        if data.get("agent_id") != agent_id and data.get("event_type") == "agent_stopped":
            done = True

    try:
        sio.connect(SERVER_URL)
    except Exception as e:
        print(f"  ERROR: Cannot connect: {e}")
        cleanup_workspace(workspace)
        return {"status": "error", "reason": str(e)}

    env = os.environ.copy()
    env["SYNTH_AUTO_INPUT"] = "0"
    env["SYNTH_TOOL_CYCLES"] = "5"
    env["SYNTH_WORK_TIME"] = "0.1"
    env["SYNTH_THINK_TIME"] = "0.1"
    env["SYNTH_OUTPUT_KB"] = "1"
    env["SYNTH_PERMISSION_PROMPTS"] = "3"

    proc = subprocess.Popen(
        [AGENTVIZ_RUN_CMD, "run", "--tmux-start", "-w", workspace, "-i", agent_id,
         "synthetic", "python3", SYNTH_AGENT_SCRIPT],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )

    deadline = time.time() + 60
    while not done and time.time() < deadline:
        if waiting and send_time is None:
            # Agent is waiting for input, send Enter key
            time.sleep(0.5)  # Brief pause to ensure TUI rendered
            send_time = time.time()
            sio.emit("control_send_keys", {"agent_id": agent_id, "key": "Enter"})
        time.sleep(0.2)

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    sio.disconnect()
    cleanup_workspace(workspace)

    if not latencies:
        print("  WARNING: No key transitions measured")
        return {"status": "no_transitions", "transitions": 0}

    results = {
        "status": "ok",
        "transitions": len(latencies),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "max": round(max(latencies), 2),
            "mean": round(mean(latencies), 2),
        },
    }
    print(f"  Transitions: {results['transitions']}")
    print(f"  Latency p50={results['latency_ms']['p50']}ms  "
          f"p95={results['latency_ms']['p95']}ms  "
          f"max={results['latency_ms']['max']}ms")
    return results


# ---------------------------------------------------------------------------
# h) Tmux Resilience Test
# ---------------------------------------------------------------------------
def run_tmux_resilience_test():
    """
    Launch tmux agent, kill with SIGINT, verify no orphaned tmux sessions
    or ttyd processes remain. 5 attempts.
    """
    print("\n--- Tmux Resilience Test ---")

    if not SOCKETIO_AVAILABLE:
        print("  SKIP: python-socketio not installed")
        return {"status": "skipped", "reason": "socketio not installed"}

    attempts = 5
    successes = 0
    orphaned_sessions = 0

    for attempt in range(attempts):
        workspace = create_temp_workspace()
        agent_id = f"bench-tmux-resilience-{attempt}-{int(time.time())}"
        tmux_session = None
        ttyd_url = None

        sio = socketio.Client()

        @sio.on("agent_event")
        def on_event(data):
            nonlocal tmux_session, ttyd_url
            if data.get("agent_id") != agent_id:
                return
            if data.get("event_type") == "tmux_session_info":
                meta = data.get("metadata", {})
                port = meta.get("ttyd_port")
                if port:
                    ttyd_url = f"http://localhost:{port}"
                tmux_session = meta.get("tmux_session")

        try:
            sio.connect(SERVER_URL)
        except Exception as e:
            print(f"  Attempt {attempt+1}: ERROR connecting: {e}")
            cleanup_workspace(workspace)
            continue

        env = os.environ.copy()
        env["SYNTH_AUTO_INPUT"] = "1"
        env["SYNTH_TOOL_CYCLES"] = "100"
        env["SYNTH_WORK_TIME"] = "0.1"
        env["SYNTH_THINK_TIME"] = "0.1"
        env["SYNTH_OUTPUT_KB"] = "1"
        env["SYNTH_PERMISSION_PROMPTS"] = "0"

        proc = subprocess.Popen(
            [AGENTVIZ_RUN_CMD, "run", "--tmux-start", "-w", workspace, "-i", agent_id,
             "synthetic", "python3", SYNTH_AGENT_SCRIPT],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        # Wait for tmux session to be created
        deadline = time.time() + 15
        while tmux_session is None and time.time() < deadline:
            time.sleep(0.2)

        # Let it run for 3 seconds
        time.sleep(3)

        # Kill with SIGINT
        try:
            proc.send_signal(signal.SIGINT)
        except OSError:
            pass

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        time.sleep(1)  # Allow cleanup

        # Verify tmux session cleaned up
        session_cleaned = True
        if tmux_session:
            res = subprocess.run(["tmux", "has-session", "-t", tmux_session],
                                 capture_output=True)
            if res.returncode == 0:
                session_cleaned = False
                orphaned_sessions += 1
                # Force kill orphaned session
                subprocess.run(["tmux", "kill-session", "-t", tmux_session],
                               capture_output=True)

        # Verify ttyd port no longer reachable
        import urllib.request
        ttyd_cleaned = True
        if ttyd_url:
            try:
                urllib.request.urlopen(ttyd_url, timeout=2)
                ttyd_cleaned = False
            except Exception:
                pass

        # Check backend has a final state
        final_state = None
        try:
            url = f"{SERVER_URL}/agents/{agent_id}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                agent_data = data.get("agent", data)
                final_state = agent_data.get("state")
        except Exception:
            pass

        sio.disconnect()
        cleanup_workspace(workspace)

        ok = session_cleaned and ttyd_cleaned and final_state is not None
        if ok:
            successes += 1

        status = "OK" if ok else f"FAIL (session_clean={session_cleaned} ttyd_clean={ttyd_cleaned} state={final_state})"
        print(f"  Attempt {attempt+1}/{attempts}: {status}")

    results = {
        "status": "ok",
        "attempts": attempts,
        "successes": successes,
        "success_rate_percent": round((successes / attempts) * 100, 1),
        "orphaned_sessions": orphaned_sessions,
    }
    print(f"  Success rate: {results['success_rate_percent']}% ({successes}/{attempts})")
    print(f"  Orphaned sessions: {orphaned_sessions}")
    return results


# ---------------------------------------------------------------------------
# i) Frontend Benchmarks (Playwright)
# ---------------------------------------------------------------------------
FRONTEND_URL = "http://localhost:3000"
FRONTEND_BENCHMARK_SCRIPT = os.path.join(os.path.dirname(__file__), "frontend_benchmark.ts")


def run_frontend_benchmarks():
    """
    Run frontend Playwright benchmarks if the React dev server is available.
    Parses structured JSON from the script's stdout.
    """
    print("\n--- Frontend Benchmarks ---")

    # Check if frontend dev server is running
    try:
        import urllib.request
        urllib.request.urlopen(FRONTEND_URL, timeout=3)
    except Exception:
        print("  SKIP: Frontend dev server not running at", FRONTEND_URL)
        print("  Start it with: cd frontend && npm start")
        return {"status": "skipped", "reason": f"frontend not running at {FRONTEND_URL}"}

    # Check script exists
    if not os.path.exists(FRONTEND_BENCHMARK_SCRIPT):
        print(f"  SKIP: {FRONTEND_BENCHMARK_SCRIPT} not found")
        return {"status": "skipped", "reason": "script not found"}

    print("  Running Playwright benchmarks (this may take a few minutes)...")

    try:
        result = subprocess.run(
            ["npx", "tsx", FRONTEND_BENCHMARK_SCRIPT],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )
    except subprocess.TimeoutExpired:
        print("  ERROR: Frontend benchmarks timed out after 5 minutes")
        return {"status": "error", "reason": "timeout"}
    except FileNotFoundError:
        print("  SKIP: npx/tsx not found. Install with: npm install -g tsx")
        return {"status": "skipped", "reason": "npx/tsx not found"}

    # Parse JSON from stdout markers
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if result.returncode != 0:
        print(f"  ERROR: Frontend benchmarks exited with code {result.returncode}")
        if stderr:
            print(f"  stderr: {stderr[:500]}")
        return {"status": "error", "reason": f"exit code {result.returncode}"}

    # Extract JSON between markers
    marker_start = "__FRONTEND_RESULTS_JSON__"
    marker_end = "__FRONTEND_RESULTS_END__"
    start_idx = stdout.find(marker_start)
    end_idx = stdout.find(marker_end)

    if start_idx == -1 or end_idx == -1:
        print("  WARNING: Could not find result markers in output")
        if stdout:
            print(f"  stdout (last 500 chars): {stdout[-500:]}")
        return {"status": "error", "reason": "no result markers in output"}

    json_str = stdout[start_idx + len(marker_start):end_idx].strip()
    try:
        frontend_results = json.loads(json_str)
        print("  Frontend benchmarks completed successfully")
        frontend_results["status"] = "ok"
        return frontend_results
    except json.JSONDecodeError as e:
        print(f"  ERROR: Failed to parse frontend results JSON: {e}")
        return {"status": "error", "reason": f"JSON parse error: {e}"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("AgentViz Benchmark Harness")
    print(f"Started at: {datetime.now().isoformat()}")
    print("=" * 60)

    # Check server is running
    try:
        import urllib.request
        urllib.request.urlopen(f"{SERVER_URL}/agents", timeout=3)
    except Exception as e:
        print(f"\nERROR: Cannot reach AgentViz server at {SERVER_URL}")
        print("Start it with: agentviz server")
        sys.exit(1)

    all_results = {
        "timestamp": datetime.now().isoformat(),
        "server_url": SERVER_URL,
    }

    # Run backend benchmarks
    all_results["backend"] = {
        "latency": run_latency_test(),
        "throughput": run_throughput_test(),
        "scalability": run_scalability_test(),
        "reliability": run_reliability_test(),
    }

    # Run tmux benchmarks if tmux and ttyd are available
    if shutil.which("tmux") and shutil.which("ttyd"):
        all_results["backend"]["tmux"] = {
            "lifecycle": run_tmux_lifecycle_test(),
            "scalability": run_tmux_scalability_test(),
            "send_keys": run_tmux_send_keys_test(),
            "resilience": run_tmux_resilience_test(),
        }
    else:
        all_results["backend"]["tmux"] = {"status": "skipped", "reason": "tmux/ttyd not installed"}

    # Run frontend benchmarks (sequential, after backend)
    all_results["frontend"] = run_frontend_benchmarks()

    # Save results
    results_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(results_dir, f"benchmark_{timestamp}.json")

    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'=' * 60}")
    print(f"Results saved to: {output_path}")
    print(f"{'=' * 60}")

    return all_results


if __name__ == "__main__":
    main()
