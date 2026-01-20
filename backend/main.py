import socketio
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List
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

# In-memory storage for agent events
# Structure: { agent_id: [events...] }
agent_events_store: Dict[str, List[dict]] = {}
agent_metadata_store: Dict[str, dict] = {}

@sio.event
async def connect(sid, environ):
    print(f"Socket.IO client connected: {sid}")
    # Send all historical events to the newly connected client
    for agent_id, events in agent_events_store.items():
        for event in events:
            await sio.emit('agent_event', event, to=sid)
    print(f"Sent {sum(len(events) for events in agent_events_store.values())} historical events to {sid}")

@sio.event
async def disconnect(sid):
    print(f"Socket.IO client disconnected: {sid}")

@sio.event
async def agent_event(sid, data: dict):
    event_type = data.get('event_type')
    agent_id = data.get('agent_id')
    metadata = data.get('metadata', {})

    # Detailed logging for debugging
    print(f"[BACKEND] Received event: {event_type} from {agent_id}")
    if event_type in ('file_operation', 'file_created', 'file_modified'):
        print(f"[BACKEND]   -> File: {metadata.get('file_path')} ({metadata.get('operation_type', 'unknown')})")
    elif event_type == 'tool_call':
        print(f"[BACKEND]   -> Tool: {metadata.get('tool_name')} / {metadata.get('command')}")
    elif event_type == 'lines_changed':
        print(f"[BACKEND]   -> Lines: {metadata.get('type')} {metadata.get('count')} ({metadata.get('function_name')})")
    elif event_type == 'token_usage':
        print(f"[BACKEND]   -> Tokens: {metadata.get('type')} {metadata.get('total')} ({metadata.get('model')})")

    # Store the event
    if agent_id:
        if agent_id not in agent_events_store:
            agent_events_store[agent_id] = []
            agent_metadata_store[agent_id] = {
                'agent_type': data.get('agent_type'),
                'working_dir': data.get('working_dir'),
                'first_seen': time.time(),
                'last_seen': time.time(),
            }
        
        agent_events_store[agent_id].append(data)
        agent_metadata_store[agent_id]['last_seen'] = time.time()
        
        # Optional: Limit events per agent to prevent memory issues
        # Keep only the last 1000 events per agent
        if len(agent_events_store[agent_id]) > 1000:
            agent_events_store[agent_id] = agent_events_store[agent_id][-1000:]
    
    # Broadcast to all connected clients
    await sio.emit('agent_event', data)

@sio.event
async def request_history(sid, data: dict):
    """Client can request full history for a specific agent"""
    agent_id = data.get('agent_id')
    if agent_id and agent_id in agent_events_store:
        for event in agent_events_store[agent_id]:
            await sio.emit('agent_event', event, to=sid)

@app.get("/")
def read_root():
    return {"Hello": "AgentViz Server"}

@app.get("/agents")
def get_agents():
    """REST endpoint to get all agent metadata"""
    return {
        "agents": agent_metadata_store,
        "total_events": {agent_id: len(events) for agent_id, events in agent_events_store.items()}
    }

@app.get("/agents/{agent_id}/events")
def get_agent_events(agent_id: str):
    """REST endpoint to get events for a specific agent"""
    if agent_id in agent_events_store:
        return {
            "agent_id": agent_id,
            "metadata": agent_metadata_store.get(agent_id, {}),
            "events": agent_events_store[agent_id]
        }
    return {"error": "Agent not found"}, 404

@app.delete("/agents/{agent_id}")
def delete_agent(agent_id: str):
    """Clear events for a specific agent"""
    if agent_id in agent_events_store:
        del agent_events_store[agent_id]
        del agent_metadata_store[agent_id]
        return {"message": f"Deleted agent {agent_id}"}
    return {"error": "Agent not found"}, 404

@app.delete("/agents")
def clear_all_agents():
    """Clear all stored events"""
    agent_events_store.clear()
    agent_metadata_store.clear()
    return {"message": "Cleared all agents"}

@app.get("/debug")
def debug_info():
    """Debug endpoint to show all stored events summary"""
    summary = {}
    for agent_id, events in agent_events_store.items():
        event_counts = {}
        for event in events:
            event_type = event.get('event_type', 'unknown')
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        summary[agent_id] = {
            "total_events": len(events),
            "event_types": event_counts,
            "last_5_events": [
                {
                    "type": e.get('event_type'),
                    "metadata": e.get('metadata'),
                    "timestamp": e.get('timestamp')
                }
                for e in events[-5:]
            ]
        }
    return {
        "total_agents": len(agent_events_store),
        "agents": summary
    }

if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="127.0.0.1", port=8787, reload=True)