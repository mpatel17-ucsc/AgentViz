import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { Agent, AgentState, Filters, AgentEvent, Subprocess, BackendState, mapBackendStateToFrontend } from '../types/agent';

interface AgentStore {
  // State
  agents: Record<string, Agent>;
  selectedAgentId: string | null;
  drawerOpen: boolean;
  filters: Filters;
  userLastSeen: number;

  // Actions
  setAgent: (agent: Agent) => void;
  updateAgentState: (agentId: string, state: AgentState) => void;
  updateSubprocess: (agentId: string, subprocess: Subprocess) => void;
  addEvent: (event: AgentEvent) => void;
  selectAgent: (agentId: string | null) => void;
  setDrawerOpen: (open: boolean) => void;
  markAgentSeen: (agentId: string) => void;
  setFilters: (filters: Partial<Filters>) => void;
  clearAgents: () => void;
  deleteAgent: (agentId: string) => void;
  updateUserLastSeen: () => void;

  // Computed
  getAgentsByState: (state: AgentState) => Agent[];
  getFilteredAgents: () => Agent[];
}

const DEFAULT_FILTERS: Filters = {
  agentType: [],
  repo: [],
  branch: [],
  showOnlyNeedsAttention: false,
  hideCompleted: false,
};

export const useAgentStore = create<AgentStore>()(
  persist(
    (set, get) => ({
      // Initial state
      agents: {},
      selectedAgentId: null,
      drawerOpen: false,
      filters: DEFAULT_FILTERS,
      userLastSeen: Date.now(),

      // Set or update an agent
      setAgent: (agent: Agent) => {
        // Validate agent has required fields
        if (!agent.id) {
          console.warn('[Store] Received agent without id, ignoring:', agent);
          return;
        }

        console.log(`[Store] Setting agent ${agent.id} state to ${agent.state}`);

        set((state) => ({
          agents: {
            ...state.agents,
            [agent.id]: agent,
          },
        }));
      },

      // Update agent state
      updateAgentState: (agentId: string, newState: AgentState) => {
        set((state) => {
          const agent = state.agents[agentId];
          if (!agent) return state;

          return {
            agents: {
              ...state.agents,
              [agentId]: {
                ...agent,
                state: newState,
                last_event_at: Date.now() / 1000,
              },
            },
          };
        });
      },

      // Update subprocess
      updateSubprocess: (agentId: string, subprocess: Subprocess) => {
        set((state) => {
          const agent = state.agents[agentId];
          if (!agent) return state;

          return {
            agents: {
              ...state.agents,
              [agentId]: {
                ...agent,
                subprocesses: {
                  ...agent.subprocesses,
                  [subprocess.pid]: subprocess,
                },
              },
            },
          };
        });
      },

      // Add event and update agent accordingly
      addEvent: (event: AgentEvent) => {
        // Validate event has required fields
        if (!event.agent_id) {
          console.warn('[Store] Received event without agent_id, ignoring:', event);
          return;
        }

        // Check if this is a historical event (replayed on page refresh)
        // Historical events should NOT change agent state - the backend already sent
        // the correct current state via agent_state event
        const isHistorical = (event as any).historical === true;

        set((state) => {
          const agent = state.agents[event.agent_id];

          console.log(`[Store] Processing event ${event.event_type} for agent ${event.agent_id}${isHistorical ? ' (historical)' : ''}`);

          // If agent doesn't exist, create it
          if (!agent) {
            const newAgent: Agent = {
              id: event.agent_id,
              type: event.agent_type,
              state: 'ready',  // Start in READY, wait for user prompt
              workspace: event.working_dir,
              branch: null,
              repo: event.working_dir?.split('/').pop() || null,
              task_summary: null,
              pid: null,
              needs_attention: false,
              last_event_at: event.timestamp,
              last_message: 'Waiting for task...',
              error_message: null,
              completed_at: null,
              started_at: event.timestamp,
              subprocesses: {},
              first_seen: event.timestamp,
              user_last_seen: null,
            };

            return {
              agents: {
                ...state.agents,
                [event.agent_id]: newAgent,
              },
            };
          }

          // GUARD: Don't update agents that are already completed (prevents late events from overwriting)
          // Exception: agent_stopped and state_change(stopped) can always update (to handle edge cases)
          if (agent.state === 'completed') {
            const isStoppedEvent = event.event_type === 'agent_stopped' ||
              (event.event_type === 'state_change' && event.metadata?.state === 'stopped');
            if (!isStoppedEvent) {
              console.log(`[Store] Ignoring ${event.event_type} for completed agent ${event.agent_id}`);
              return state; // No change
            }
          }

          // Update existing agent based on event (state comes from backend)
          const updates: Partial<Agent> = {
            last_event_at: event.timestamp,
          };

          switch (event.event_type) {
            case 'state_change':
              // Handle hook-based state changes from backend
              // Skip state changes for historical events - backend already sent correct current state
              if (isHistorical) {
                console.log(`[Store] Skipping state_change for historical event`);
                break;
              }
              const backendState = event.metadata?.state as BackendState;
              if (backendState) {
                const newState = mapBackendStateToFrontend(backendState);

                // Don't override completed state with anything except stopped
                if (agent.state === 'completed' && backendState !== 'stopped') {
                  console.log(`[Store] Ignoring state_change ${backendState} for completed agent`);
                  break;
                }

                updates.state = newState;

                // Update message based on state
                switch (backendState) {
                  case 'starting':
                    updates.last_message = 'Starting session...';
                    break;
                  case 'in_progress':
                    updates.last_message = 'Processing...';
                    break;
                  case 'working':
                    const tool = event.metadata?.tool;
                    updates.last_message = tool
                      ? `Executing: ${tool}`
                      : 'Executing tool...';
                    break;
                  case 'ready':
                    updates.last_message = 'Ready for next task...';
                    break;
                  case 'idle':
                    updates.last_message = 'Idle - waiting for input';
                    break;
                  case 'waiting_for_input':
                    updates.last_message = 'Waiting for approval...';
                    updates.needs_attention = true;
                    break;
                  case 'stopped':
                    updates.last_message = 'Session ended';
                    updates.completed_at = event.timestamp;
                    break;
                }

                console.log(`[Store] State change: ${backendState} -> ${newState} for agent ${event.agent_id}`);
              }
              break;

            case 'waiting_for_input':
              // Skip state changes for historical events
              if (isHistorical) {
                console.log(`[Store] Skipping waiting_for_input state change for historical event`);
                break;
              }
              updates.state = 'waiting_for_input';
              updates.needs_attention = true;
              updates.last_message = `[${event.agent_id.slice(-8)}] ${(event.metadata?.prompt || 'Waiting for input...').slice(0, 100)}`;
              break;
            case 'error':
              // Skip state changes for historical events
              if (isHistorical) {
                console.log(`[Store] Skipping error state change for historical event`);
                break;
              }
              updates.state = 'error';
              updates.error_message = event.metadata?.error || event.metadata?.message || 'Unknown error';
              break;
            case 'task_completed':
              // Skip state changes for historical events
              if (isHistorical) {
                console.log(`[Store] Skipping task_completed state change for historical event`);
                break;
              }
              updates.state = 'ready';
              updates.last_message = 'Ready for next task...';
              break;
            case 'agent_started':
              // Skip state changes for historical events
              if (isHistorical) {
                console.log(`[Store] Skipping agent_started state change for historical event`);
                break;
              }
              updates.state = 'ready';
              updates.last_message = 'Ready for task...';
              break;
            case 'agent_stopped':
              // Skip state changes for historical events
              if (isHistorical) {
                console.log(`[Store] Skipping agent_stopped state change for historical event`);
                break;
              }
              // Process exited - mark as completed
              updates.state = 'completed';
              updates.completed_at = event.timestamp;
              updates.last_message = event.metadata?.return_code === 0
                ? 'Completed successfully'
                : `Exited with code ${event.metadata?.return_code || 'unknown'}`;
              break;
            case 'user_prompt':
              if (!agent.task_summary && event.metadata?.prompt && event.metadata.prompt !== '[user input]') {
                const prompt = event.metadata.prompt;
                updates.task_summary = prompt.length > 100 ? prompt.slice(0, 100) + '...' : prompt;
              }
              break;
            case 'file_modified':
            case 'file_created':
              const filePath = event.metadata?.file_path || 'file';
              const fileName = filePath.split('/').pop() || filePath;
              updates.last_message = `[${event.agent_id.slice(-8)}] Modified: ${fileName}`;
              break;
            case 'tool_call':
              const toolName = event.metadata?.tool_name || '';
              const command = event.metadata?.command || '';
              updates.last_message = toolName
                ? `[${event.agent_id.slice(-8)}] ${toolName}: ${command.slice(0, 30)}`
                : `[${event.agent_id.slice(-8)}] Running: ${command.slice(0, 40)}`;
              break;
            case 'tool_completed':
              // Tool finished, but agent may still be processing
              updates.last_message = `[${event.agent_id.slice(-8)}] Tool completed`;
              break;
            case 'code_generation':
              updates.last_message = `[${event.agent_id.slice(-8)}] Generated code (${event.metadata?.output_tokens || 0} tokens)`;
              break;
            case 'thinking_start':
              updates.last_message = `[${event.agent_id.slice(-8)}] Thinking...`;
              break;
            case 'subprocess_started':
              if (event.metadata?.pid) {
                updates.subprocesses = {
                  ...agent.subprocesses,
                  [event.metadata.pid]: {
                    pid: event.metadata.pid,
                    parent_pid: event.metadata.parent_pid || agent.pid || 0,
                    command: event.metadata.command || '',
                    state: 'running',
                    started_at: event.metadata.started_at || event.timestamp,
                    ended_at: null,
                    exit_code: null,
                  },
                };
              }
              break;
            case 'subprocess_ended':
              if (event.metadata?.pid && agent.subprocesses[event.metadata.pid]) {
                updates.subprocesses = {
                  ...agent.subprocesses,
                  [event.metadata.pid]: {
                    ...agent.subprocesses[event.metadata.pid],
                    state: event.metadata.state || 'completed',
                    ended_at: event.metadata.ended_at || event.timestamp,
                    exit_code: event.metadata.exit_code ?? null,
                  },
                };
              }
              break;
            default:
              break;
          }

          return {
            agents: {
              ...state.agents,
              [event.agent_id]: {
                ...agent,
                ...updates,
              },
            },
          };
        });
      },

      // Select an agent (opens drawer)
      selectAgent: (agentId: string | null) => {
        set({
          selectedAgentId: agentId,
          drawerOpen: agentId !== null,
        });
      },

      // Set drawer open state
      setDrawerOpen: (open: boolean) => {
        set({ drawerOpen: open });
        if (!open) {
          set({ selectedAgentId: null });
        }
      },

      // Mark agent as seen
      markAgentSeen: (agentId: string) => {
        set((state) => {
          const agent = state.agents[agentId];
          if (!agent) return state;

          return {
            agents: {
              ...state.agents,
              [agentId]: {
                ...agent,
                user_last_seen: Date.now() / 1000,
              },
            },
          };
        });
      },

      // Update filters
      setFilters: (newFilters: Partial<Filters>) => {
        set((state) => ({
          filters: {
            ...state.filters,
            ...newFilters,
          },
        }));
      },

      // Clear all agents
      clearAgents: () => {
        set({ agents: {}, selectedAgentId: null, drawerOpen: false });
      },

      // Delete a specific agent
      deleteAgent: (agentId: string) => {
        set((state) => {
          const { [agentId]: deleted, ...remaining } = state.agents;
          return {
            agents: remaining,
            selectedAgentId: state.selectedAgentId === agentId ? null : state.selectedAgentId,
            drawerOpen: state.selectedAgentId === agentId ? false : state.drawerOpen,
          };
        });
      },

      // Update user last seen timestamp
      updateUserLastSeen: () => {
        set({ userLastSeen: Date.now() });
      },

      // Get agents by state (sorted by attention + recency)
      getAgentsByState: (state: AgentState) => {
        const { agents, filters } = get();
        let filtered = Object.values(agents).filter((a) => a.state === state);

        // Apply filters
        if (filters.agentType.length > 0) {
          filtered = filtered.filter((a) => filters.agentType.includes(a.type));
        }
        if (filters.repo.length > 0) {
          filtered = filtered.filter((a) => a.repo && filters.repo.includes(a.repo));
        }
        if (filters.showOnlyNeedsAttention) {
          filtered = filtered.filter((a) => a.needs_attention);
        }

        // Sort: needs_attention first, then by last_event_at (newest first)
        filtered.sort((a, b) => {
          if (a.needs_attention !== b.needs_attention) {
            return a.needs_attention ? -1 : 1;
          }
          return b.last_event_at - a.last_event_at;
        });

        return filtered;
      },

      // Get all filtered agents
      getFilteredAgents: () => {
        const { agents, filters } = get();
        let filtered = Object.values(agents);

        if (filters.agentType.length > 0) {
          filtered = filtered.filter((a) => filters.agentType.includes(a.type));
        }
        if (filters.repo.length > 0) {
          filtered = filtered.filter((a) => a.repo && filters.repo.includes(a.repo));
        }
        if (filters.showOnlyNeedsAttention) {
          filtered = filtered.filter((a) => a.needs_attention);
        }
        if (filters.hideCompleted) {
          filtered = filtered.filter((a) => a.state !== 'completed');
        }

        return filtered;
      },
    }),
    {
      name: 'agentviz-storage-v2',
      partialize: (state) => ({
        filters: state.filters,
        userLastSeen: state.userLastSeen,
      }),
    }
  )
);
