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
        }
        agent_events_store[agent_id] = []
    return agent_store[agent_id]


def extract_repo_name(working_dir: str) -> Optional[str]:
    """Extract repository name from working directory path"""
    if not working_dir:
        return None
    parts = working_dir.rstrip("/").split("/")
    return parts[-1] if parts else None


def transition_agent_state(agent: dict, event_type: str, metadata: dict) -> Optional[str]:
    """
    Apply state transition rules based on event type.
    Returns the new state if changed, None otherwise.
    """
    old_state = agent["state"]
    new_state = old_state

    # Events that indicate user has started interacting with the agent
    # Note: user_input_received removed - we don't want every keystroke to change state
    user_activity_events = {
        "user_prompt", "user_resumed"
    }

    # Events that indicate agent is actively working
    work_activity_events = {
        "file_created", "file_modified", "file_deleted",
        "tool_call", "subprocess_started", "code_generation",
        "thinking_start"
    }

    # State transition rules
    if event_type == "agent_started":
        # Agent just started - it's in READY state waiting for user's first task
        new_state = AgentState.READY.value
        agent["needs_attention"] = False
        agent["last_message"] = "Ready for task..."

    elif event_type in user_activity_events:
        # User provided input - go to IN_PROGRESS
        new_state = AgentState.IN_PROGRESS.value
        agent["needs_attention"] = False

    elif event_type == "waiting_for_input":
        new_state = AgentState.WAITING_FOR_INPUT.value
        agent["needs_attention"] = True
        agent["last_message"] = metadata.get("prompt", "Waiting for input...")[:200]

    elif event_type == "error":
        new_state = AgentState.ERROR.value
        agent["needs_attention"] = True
        agent["error_message"] = metadata.get("error") or metadata.get("message", "Unknown error")

    elif event_type == "agent_stopped":
        # Agent session has ended (Ctrl+C or exit)
        # This is COMPLETED regardless of exit code - session is over
        new_state = AgentState.COMPLETED.value
        agent["completed_at"] = time.time()
        return_code = metadata.get("return_code", 0)
        if return_code != 0:
            agent["error_message"] = f"Exited with code {return_code}"

    elif event_type == "task_completed":
        # Agent finished a task but is still running, waiting for next task
        new_state = AgentState.READY.value
        agent["needs_attention"] = False
        agent["last_message"] = "Ready for next task..."

    elif event_type in work_activity_events:
        # If we're in WAITING_FOR_INPUT or READY and see work activity, go to IN_PROGRESS
        if old_state in (AgentState.WAITING_FOR_INPUT.value, AgentState.READY.value):
            new_state = AgentState.IN_PROGRESS.value
            agent["needs_attention"] = False

    elif event_type == "thinking_end":
        # Thinking ended but no activity - might still be waiting
        pass

    # Update state if changed
    if new_state != old_state:
        agent["state"] = new_state
        return new_state

    return None


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
        # Legacy tool_call events also track subprocesses
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
    # Send historical events
    for agent_id, events in agent_events_store.items():
        for event in events[-100:]:  # Last 100 events per agent
            await sio.emit('agent_event', event, to=sid)
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

    print(f"[BACKEND] Received event: {event_type} from agent_id={agent_id} (type={agent_type})")

    if not agent_id:
        return

    # Get or create agent
    agent = get_or_create_agent(agent_id, agent_type, working_dir)

    # Update last event timestamp and message
    agent["last_event_at"] = time.time()

    # Extract task summary from first user_prompt
    if event_type == "user_prompt" and not agent["task_summary"]:
        prompt = metadata.get("prompt", "")
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
    new_state = transition_agent_state(agent, event_type, metadata)

    # Store the event
    agent_events_store[agent_id].append(data)

    # Limit events per agent to prevent memory issues
    if len(agent_events_store[agent_id]) > 1000:
        agent_events_store[agent_id] = agent_events_store[agent_id][-1000:]

    # Broadcast event to all clients (with agent_id for filtering)
    print(f"[BACKEND] Broadcasting event {event_type} for agent_id={agent_id}")
    await sio.emit('agent_event', data)

    # If state changed, broadcast state update for THIS SPECIFIC agent only
    if new_state:
        print(f"[BACKEND] Agent {agent_id} transitioned to {new_state}")
        await sio.emit('agent_state_change', {
            "agent_id": agent_id,
            "old_state": old_state if 'old_state' in dir() else agent["state"],
            "new_state": new_state,
            "timestamp": time.time(),
        })
        # Only send THIS agent's state, not all agents
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
    """Mark an agent as seen by user (clears 'while away' badges)"""
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


@app.get("/dashboard")
def get_dashboard():
    """
    Returns all agents grouped by state with sorting.
    Agents are sorted by: needs_attention DESC, last_event_at DESC
    """
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

    # Sort each group: needs_attention first, then by last_event_at (newest first)
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
    """Mark agent as viewed (clears 'while away' badges)"""
    if agent_id in agent_store:
        agent_store[agent_id]["user_last_seen"] = time.time()
        return {"message": f"Marked agent {agent_id} as seen"}
    return {"error": "Agent not found"}, 404


@app.post("/agents/{agent_id}/retry")
def retry_agent(agent_id: str):
    """Retry a failed agent (moves from ERROR to READY)"""
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
