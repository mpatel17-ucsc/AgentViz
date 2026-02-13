import os
import socketio
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List, Optional
from enum import Enum
from pydantic import BaseModel
import time

app = FastAPI()
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
socket_app = socketio.ASGIApp(sio, app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================
# Agent State Machine
# ============================================

class AgentState(str, Enum):
    READY = "ready"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
    ERROR = "error"
    COMPLETED = "completed"


class SubprocessInfo(BaseModel):
    pid: int
    parent_pid: int
    command: str
    state: str  # "running", "completed", "error"
    started_at: float
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None


class AgentInfo(BaseModel):
    id: str
    type: str
    state: AgentState
    workspace: str
    branch: Optional[str] = None
    repo: Optional[str] = None
    task_summary: Optional[str] = None
    pid: Optional[int] = None
    needs_attention: bool = False
    last_event_at: float
    last_message: Optional[str] = None
    error_message: Optional[str] = None
    completed_at: Optional[float] = None
    started_at: float
    subprocesses: Dict[int, dict] = {}
    first_seen: float
    user_last_seen: Optional[float] = None


# ============================================
# In-Memory Storage
# ============================================

# Structure: { agent_id: AgentInfo }
agent_store: Dict[str, dict] = {}

# Structure: { agent_id: [events...] }
agent_events_store: Dict[str, List[dict]] = {}



def get_or_create_agent(agent_id: str, agent_type: str, working_dir: str) -> dict:
    """Get existing agent or create new one with default state"""
    if agent_id not in agent_store:
        now = time.time()
        agent_store[agent_id] = {
            "id": agent_id,
            "type": agent_type,
            "state": AgentState.READY.value,  # Start in READY, wait for user prompt
            "workspace": working_dir,
            "branch": None,
            "repo": extract_repo_name(working_dir),
            "task_summary": None,
            "pid": None,
            "needs_attention": False,
            "last_event_at": now,
            "last_message": "Waiting for task...",
            "error_message": None,
            "completed_at": None,
            "started_at": now,
            "subprocesses": {},
            "first_seen": now,
            "user_last_seen": None,
            "task_started": False,
            "ttyd_url": None,
        }
        agent_events_store[agent_id] = []
    return agent_store[agent_id]


def extract_repo_name(working_dir: str) -> Optional[str]:
    """Extract repository name from working directory path"""
    if not working_dir:
        return None
    parts = working_dir.rstrip("/").split("/")
    return parts[-1] if parts else None


def transition_agent_state(agent: dict, event_type: str, metadata: dict) -> tuple[Optional[str], str]:
    """
    Apply state transition rules based on event type.
    Returns (new_state, old_state) tuple. new_state is None if unchanged.

    State sources (in priority order):
    1. Hook-based state_change events (most reliable - ALWAYS TRUST THESE)
    2. Specific event types (agent_started, agent_stopped, etc.)
    3. Work activity events (file modifications, tool calls)
    """
    old_state = agent["state"]
    new_state = old_state

    # Events that indicate user has started interacting with the agent
    user_activity_events = {
        "user_prompt", "user_resumed"
    }

    # Events that indicate agent is actively working
    work_activity_events = {
        "file_created", "file_modified", "file_deleted", "file_operation",
        "tool_call", "subprocess_started", "code_generation",
        "token_usage",  # API calls indicate work
        "work_activity",  # Generic work activity signal
    }

    # =========================================================================
    # PRIORITY 1: Handle explicit state_change events from hooks
    # THESE ARE THE SOURCE OF TRUTH - ALWAYS TRUST THEM
    # =========================================================================
    if event_type == "state_change":
        hook_state = metadata.get("state", "")
        hook_detail = metadata.get("detail", "")
        source = metadata.get("source", "unknown")

        print(f"[BACKEND] Hook state_change: {hook_state} (detail={hook_detail}, source={source})")

        if hook_state == "starting":
            new_state = AgentState.READY.value
            agent["needs_attention"] = False
            agent["last_message"] = "Session starting..."
            agent["completed_at"] = None
            agent["error_message"] = None

        elif hook_state in ("thinking", "in_progress", "working"):
            # TRUST THE HOOK - if it says working, agent is working
            new_state = AgentState.IN_PROGRESS.value
            agent["needs_attention"] = False
            agent["task_started"] = True  # Hook confirmed work is happening
            agent["completed_at"] = None
            if hook_detail == "thinking":
                agent["last_message"] = "Thinking..."
            elif hook_detail == "tool_executing":
                tool_name = metadata.get("tool_name", "tool")
                agent["last_message"] = f"Executing {tool_name}..."
            else:
                agent["last_message"] = "Working..."

        elif hook_state == "ready":
            # TRUST THE HOOK - if it says ready, agent is ready
            new_state = AgentState.READY.value
            agent["needs_attention"] = False
            agent["task_started"] = False  # Task completed or not started
            agent["last_message"] = "Ready for task..."

        elif hook_state == "waiting_for_input":
            # TRUST THE HOOK - if it says waiting, agent needs input
            new_state = AgentState.WAITING_FOR_INPUT.value
            agent["needs_attention"] = True
            agent["last_message"] = metadata.get("prompt", "Waiting for input...")[:200]

        elif hook_state == "idle":
            # TRUST THE HOOK - idle means ready
            new_state = AgentState.READY.value
            agent["needs_attention"] = False
            agent["task_started"] = False
            agent["last_message"] = "Idle..."

        elif hook_state == "stopped":
            new_state = AgentState.COMPLETED.value
            agent["completed_at"] = time.time()
            agent["needs_attention"] = False
            agent["task_started"] = False
            return_code = metadata.get("return_code", 0)
            if return_code == -2:
                agent["last_message"] = "Interrupted by user (Ctrl+C)"
            elif return_code != 0:
                agent["last_message"] = f"Exited with code {return_code}"
                agent["error_message"] = f"Exited with code {return_code}"
            else:
                agent["last_message"] = "Session ended"

        elif hook_state == "error":
            new_state = AgentState.ERROR.value
            agent["needs_attention"] = True
            agent["error_message"] = metadata.get("error", "Unknown error")

        # Return early for state_change events
        if new_state != old_state:
            agent["state"] = new_state
            return (new_state, old_state)
        return (None, old_state)

    # =========================================================================
    # PRIORITY 2: Handle specific lifecycle events
    # =========================================================================
    elif event_type == "agent_started":
        new_state = AgentState.READY.value
        agent["needs_attention"] = False
        agent["last_message"] = "Ready for task..."
        agent["task_started"] = False
        agent["completed_at"] = None
        agent["error_message"] = None

    elif event_type in user_activity_events:
        # User provided input
        # Only transition to IN_PROGRESS if:
        # 1. We're in READY state (starting a new task), OR
        # 2. We're in WAITING_FOR_INPUT with an active task (responding to prompt)
        #
        # For startup scenarios (login prompts, etc.), the agent should emit
        # state_change(ready) when actually ready, and state_change(working)
        # when work starts. We rely on those hooks, not on user_prompt events.

        if old_state == AgentState.WAITING_FOR_INPUT.value:
            # User responded to a prompt
            if agent.get("task_started"):
                # Active task - return to working
                new_state = AgentState.IN_PROGRESS.value
                agent["needs_attention"] = False
            else:
                # No active task - this was likely a startup prompt
                # Go to READY and wait for hooks to signal actual work
                new_state = AgentState.READY.value
                agent["needs_attention"] = False

        elif old_state == AgentState.READY.value:
            # User starting a new task from READY state
            # Transition to IN_PROGRESS and mark task as started
            new_state = AgentState.IN_PROGRESS.value
            agent["needs_attention"] = False
            agent["task_started"] = True
            agent["completed_at"] = None

    elif event_type == "waiting_for_input":
        # Direct waiting_for_input event (not from hook state_change)
        # This should transition to WAITING_FOR_INPUT
        new_state = AgentState.WAITING_FOR_INPUT.value
        agent["needs_attention"] = True
        agent["last_message"] = metadata.get("prompt", "Waiting for input...")[:200]

    elif event_type == "error":
        new_state = AgentState.ERROR.value
        agent["needs_attention"] = True
        agent["error_message"] = metadata.get("error") or metadata.get("message", "Unknown error")

    elif event_type == "agent_stopped":
        # Handle duplicate agent_stopped events
        if old_state == AgentState.COMPLETED.value:
            current_reason = agent.get("_stop_reason", "")
            new_reason = metadata.get("reason", "exited")
            if new_reason not in ("interrupted", "error") or current_reason in ("interrupted", "error"):
                print(f"[BACKEND] Ignoring duplicate agent_stopped for {agent['id']} (already completed)")
                return (None, old_state)

        new_state = AgentState.COMPLETED.value
        agent["completed_at"] = time.time()
        agent["needs_attention"] = False
        agent["task_started"] = False
        return_code = metadata.get("return_code", 0)
        reason = metadata.get("reason", "exited")
        agent["_stop_reason"] = reason

        if return_code == -2 or reason in ("interrupted", "cleanup", "user_interrupt"):
            agent["last_message"] = "Interrupted by user (Ctrl+C)"
        elif return_code != 0 or reason == "error":
            agent["error_message"] = f"Exited with code {return_code}"
            agent["last_message"] = f"Exited with code {return_code}"
        else:
            agent["last_message"] = "Session ended"

        print(f"[BACKEND] Agent {agent['id']} stopped: {reason} (code={return_code})")

    elif event_type == "task_completed":
        new_state = AgentState.READY.value
        agent["needs_attention"] = False
        agent["last_message"] = "Ready for next task..."
        agent["task_started"] = False

    elif event_type in work_activity_events:
        # Work activity - only transition if task is already started
        # This prevents startup activity from triggering IN_PROGRESS
        if agent.get("task_started"):
            if old_state in (AgentState.READY.value, AgentState.COMPLETED.value):
                new_state = AgentState.IN_PROGRESS.value
                agent["needs_attention"] = False
                agent["completed_at"] = None

    # Update state if changed
    if new_state != old_state:
        agent["state"] = new_state
        return (new_state, old_state)

    return (None, old_state)


def update_subprocess(agent: dict, event_type: str, metadata: dict):
    """Update subprocess tracking for an agent"""
    pid = metadata.get("pid")
    if not pid:
        return

    if event_type == "subprocess_started":
        agent["subprocesses"][pid] = {
            "pid": pid,
            "parent_pid": metadata.get("parent_pid", agent.get("pid", 0)),
            "command": metadata.get("command", ""),
            "state": "running",
            "started_at": metadata.get("started_at", time.time()),
            "ended_at": None,
            "exit_code": None,
        }
    elif event_type == "subprocess_ended":
        if pid in agent["subprocesses"]:
            agent["subprocesses"][pid]["state"] = metadata.get("state", "completed")
            agent["subprocesses"][pid]["ended_at"] = metadata.get("ended_at", time.time())
            agent["subprocesses"][pid]["exit_code"] = metadata.get("exit_code")
    elif event_type == "tool_call":
        if pid not in agent["subprocesses"]:
            agent["subprocesses"][pid] = {
                "pid": pid,
                "parent_pid": agent.get("pid", 0),
                "command": metadata.get("command", ""),
                "state": "running",
                "started_at": time.time(),
                "ended_at": None,
                "exit_code": None,
            }


# ============================================
# Socket.IO Event Handlers
# ============================================

@sio.event
async def connect(sid, environ):
    print(f"Socket.IO client connected: {sid}")
    # Send current agent states to newly connected client
    for agent_id, agent in agent_store.items():
        await sio.emit('agent_state', agent, to=sid)
    # Send historical events (marked as historical so frontend doesn't re-apply state changes)
    for agent_id, events in agent_events_store.items():
        for event in events[-100:]:
            historical_event = {**event, "historical": True}
            await sio.emit('agent_event', historical_event, to=sid)
    print(f"Sent state for {len(agent_store)} agents to {sid}")


@sio.event
async def disconnect(sid):
    print(f"Socket.IO client disconnected: {sid}")


@sio.event
async def agent_event(sid, data: dict):
    """Handle incoming agent events and update state machine"""
    event_type = data.get('event_type')
    agent_id = data.get('agent_id')
    agent_type = data.get('agent_type', 'unknown')
    working_dir = data.get('working_dir', '')
    metadata = data.get('metadata', {})

    # Ignore token_usage events for Claude to avoid spurious transitions
    if event_type == "token_usage" and agent_type in ("claude", "claude-code"):
        return

    print(f"[BACKEND] Received event: {event_type} from agent_id={agent_id} (type={agent_type})")

    if not agent_id:
        return

    # Get or create agent
    agent = get_or_create_agent(agent_id, agent_type, working_dir)

    # Handle tmux_session_info: set ttyd_url, broadcast state, store event, skip state transition
    if event_type == "tmux_session_info":
        ttyd_port = metadata.get("ttyd_port")
        if ttyd_port:
            agent["ttyd_url"] = f"http://localhost:{ttyd_port}"
        agent["last_event_at"] = time.time()
        agent_events_store[agent_id].append(data)
        await sio.emit('agent_event', data)
        await sio.emit('agent_state', agent)
        print(f"[BACKEND] Agent {agent_id} ttyd_url set to {agent['ttyd_url']}")
        return

    # Update last event timestamp
    agent["last_event_at"] = time.time()

    # Capture task summary from user prompts
    if event_type == "user_prompt" and not agent["task_summary"]:
        prompt = metadata.get("prompt", "")
        if prompt and prompt != "[user input]":
            agent["task_summary"] = prompt[:100] + "..." if len(prompt) > 100 else prompt

    # Update last message for display
    if event_type in ("file_modified", "file_created"):
        agent["last_message"] = f"Modified: {metadata.get('file_path', 'file')}"
    elif event_type == "tool_call":
        agent["last_message"] = f"Running: {metadata.get('command', '')[:50]}"
    elif event_type == "thinking_start":
        agent["last_message"] = "Thinking..."
    elif event_type == "code_generation":
        agent["last_message"] = f"Generated code ({metadata.get('output_tokens', 0)} tokens)"

    # Handle subprocess events
    if event_type in ("subprocess_started", "subprocess_ended", "tool_call"):
        update_subprocess(agent, event_type, metadata)

    # Apply state transition
    new_state, old_state = transition_agent_state(agent, event_type, metadata)

    # Store the event
    agent_events_store[agent_id].append(data)

    # Limit events per agent
    if len(agent_events_store[agent_id]) > 1000:
        agent_events_store[agent_id] = agent_events_store[agent_id][-1000:]

    # Broadcast event to all clients
    print(f"[BACKEND] Broadcasting event {event_type} for agent_id={agent_id}")
    await sio.emit('agent_event', data)

    # If state changed, broadcast state update
    if new_state:
        print(f"[BACKEND] Agent {agent_id} transitioned: {old_state} -> {new_state}")
        await sio.emit('agent_state_change', {
            "agent_id": agent_id,
            "old_state": old_state,
            "new_state": new_state,
            "timestamp": time.time(),
        })
        print(f"[BACKEND] Sending agent_state for agent_id={agent['id']}")
        await sio.emit('agent_state', agent)


@sio.event
async def request_history(sid, data: dict):
    """Client can request full history for a specific agent"""
    agent_id = data.get('agent_id')
    if agent_id and agent_id in agent_events_store:
        for event in agent_events_store[agent_id]:
            await sio.emit('agent_event', event, to=sid)


@sio.event
async def mark_agent_seen(sid, data: dict):
    """Mark an agent as seen by user"""
    agent_id = data.get('agent_id')
    if agent_id and agent_id in agent_store:
        agent_store[agent_id]["user_last_seen"] = time.time()
        await sio.emit('agent_state', agent_store[agent_id])


@sio.event
async def control_retry(sid, data: dict):
    """Handle retry request from drag-and-drop"""
    agent_id = data.get('agent_id')
    if agent_id and agent_id in agent_store:
        agent = agent_store[agent_id]
        agent["state"] = AgentState.READY.value
        agent["needs_attention"] = False
        agent["error_message"] = None
        agent["last_event_at"] = time.time()
        await sio.emit('agent_state', agent)
        await sio.emit('agent_state_change', {
            "agent_id": agent_id,
            "old_state": AgentState.ERROR.value,
            "new_state": AgentState.READY.value,
            "timestamp": time.time(),
        })
        print(f"[BACKEND] Agent {agent_id} retry requested, moved to READY")


@sio.event
async def control_start_task(sid, data: dict):
    """Handle start task request from drag-and-drop"""
    agent_id = data.get('agent_id')
    if agent_id and agent_id in agent_store:
        agent = agent_store[agent_id]
        old_state = agent["state"]
        agent["state"] = AgentState.IN_PROGRESS.value
        agent["needs_attention"] = False
        agent["task_started"] = True
        agent["started_at"] = time.time()
        agent["last_event_at"] = time.time()
        await sio.emit('agent_state', agent)
        await sio.emit('agent_state_change', {
            "agent_id": agent_id,
            "old_state": old_state,
            "new_state": AgentState.IN_PROGRESS.value,
            "timestamp": time.time(),
        })
        print(f"[BACKEND] Agent {agent_id} start task requested, moved to IN_PROGRESS")


# ============================================
# REST API Endpoints
# ============================================

@app.get("/")
def read_root():
    return {"Hello": "AgentViz Server", "version": "2.0"}


@app.get("/health")
def health_check():
    """Health check with agent state summary"""
    states = {}
    for state in AgentState:
        states[state.value] = sum(1 for a in agent_store.values() if a["state"] == state.value)
    return {
        "status": "healthy",
        "agents": states,
        "total": len(agent_store)
    }


@app.get("/dashboard")
def get_dashboard():
    """Returns all agents grouped by state with sorting."""
    grouped = {
        AgentState.READY.value: [],
        AgentState.IN_PROGRESS.value: [],
        AgentState.WAITING_FOR_INPUT.value: [],
        AgentState.ERROR.value: [],
        AgentState.COMPLETED.value: [],
    }

    for agent_id, agent in agent_store.items():
        state = agent.get("state", AgentState.IN_PROGRESS.value)
        if state in grouped:
            grouped[state].append(agent)

    for state in grouped:
        grouped[state].sort(
            key=lambda a: (-int(a.get("needs_attention", False)), -a.get("last_event_at", 0))
        )

    return {
        "agents_by_state": grouped,
        "total_agents": len(agent_store),
        "needs_attention_count": sum(1 for a in agent_store.values() if a.get("needs_attention")),
    }


@app.get("/agents")
def get_agents():
    """REST endpoint to get all agents"""
    return {
        "agents": list(agent_store.values()),
        "total_events": {agent_id: len(events) for agent_id, events in agent_events_store.items()}
    }


@app.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    """REST endpoint to get a specific agent"""
    if agent_id in agent_store:
        return {
            "agent": agent_store[agent_id],
            "events": agent_events_store.get(agent_id, []),
        }
    return {"error": "Agent not found"}, 404


@app.get("/agents/{agent_id}/events")
def get_agent_events(agent_id: str):
    """REST endpoint to get events for a specific agent"""
    if agent_id in agent_events_store:
        return {
            "agent_id": agent_id,
            "agent": agent_store.get(agent_id, {}),
            "events": agent_events_store[agent_id]
        }
    return {"error": "Agent not found"}, 404


@app.post("/agents/{agent_id}/mark_seen")
def mark_seen(agent_id: str):
    """Mark agent as viewed"""
    if agent_id in agent_store:
        agent_store[agent_id]["user_last_seen"] = time.time()
        return {"message": f"Marked agent {agent_id} as seen"}
    return {"error": "Agent not found"}, 404


@app.post("/agents/{agent_id}/retry")
def retry_agent(agent_id: str):
    """Retry a failed agent"""
    if agent_id in agent_store:
        agent = agent_store[agent_id]
        if agent["state"] == AgentState.ERROR.value:
            agent["state"] = AgentState.READY.value
            agent["needs_attention"] = False
            agent["error_message"] = None
            agent["last_event_at"] = time.time()
            return {"message": f"Agent {agent_id} moved to READY for retry"}
        return {"error": "Agent is not in ERROR state"}, 400
    return {"error": "Agent not found"}, 404


@app.post("/agents/{agent_id}/cancel")
def cancel_agent(agent_id: str):
    """Cancel a running agent"""
    if agent_id in agent_store:
        agent = agent_store[agent_id]
        if agent["state"] == AgentState.IN_PROGRESS.value:
            agent["state"] = AgentState.ERROR.value
            agent["needs_attention"] = True
            agent["error_message"] = "Cancelled by user"
            agent["last_event_at"] = time.time()
            return {"message": f"Agent {agent_id} cancelled"}
        return {"error": "Agent is not running"}, 400
    return {"error": "Agent not found"}, 404


@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Clear an agent and its events"""
    if agent_id in agent_store:
        del agent_store[agent_id]
        if agent_id in agent_events_store:
            del agent_events_store[agent_id]
        return {"message": f"Deleted agent {agent_id}"}
    return {"error": "Agent not found"}, 404


@app.delete("/agents")
def clear_all_agents():
    """Clear all agents and events"""
    agent_store.clear()
    agent_events_store.clear()
    return {"message": "Cleared all agents"}


@app.get("/debug")
def debug_info():
    """Debug endpoint to show all stored data summary"""
    summary = {}
    for agent_id, agent in agent_store.items():
        events = agent_events_store.get(agent_id, [])
        event_counts = {}
        for event in events:
            event_type = event.get('event_type', 'unknown')
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        summary[agent_id] = {
            "state": agent.get("state"),
            "task_started": agent.get("task_started"),
            "needs_attention": agent.get("needs_attention"),
            "subprocess_count": len(agent.get("subprocesses", {})),
            "total_events": len(events),
            "event_types": event_counts,
            "last_5_events": [
                {
                    "type": e.get('event_type'),
                    "timestamp": e.get('timestamp')
                }
                for e in events[-5:]
            ]
        }
    return {
        "total_agents": len(agent_store),
        "agents": summary
    }


if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="127.0.0.1", port=8787, reload=True)
