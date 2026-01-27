import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { Agent, AgentState, Filters, AgentEvent, Subprocess } from '../types/agent';

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

        set((state) => {
          const agent = state.agents[event.agent_id];

          console.log(`[Store] Processing event ${event.event_type} for agent ${event.agent_id}`);

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

          // Update existing agent based on event
          const updates: Partial<Agent> = {
            last_event_at: event.timestamp,
          };

          // Update based on event type
          switch (event.event_type) {
            case 'waiting_for_input':
              updates.state = 'waiting_for_input';
              updates.needs_attention = true;
              updates.last_message = `[${event.agent_id.slice(-8)}] ${(event.metadata?.prompt || 'Waiting for input...').slice(0, 100)}`;
              break;
            case 'error':
              updates.state = 'error';
              updates.needs_attention = true;
              updates.error_message = event.metadata?.error || event.metadata?.message || 'Unknown error';
              break;
            case 'agent_stopped':
              // Agent session ended (Ctrl+C or exit) - mark as COMPLETED
              updates.state = 'completed';
              updates.completed_at = event.timestamp;
              const returnCode = event.metadata?.return_code ?? 0;
              if (returnCode !== 0) {
                updates.error_message = `Exited with code ${returnCode}`;
              }
              break;
            case 'task_completed':
              // Agent finished a task but is still running - go to READY/IDLE
              updates.state = 'ready';
              updates.needs_attention = false;
              updates.last_message = 'Ready for next task...';
              break;
            case 'agent_started':
              // Agent just started - it's in READY state waiting for user's first task
              updates.state = 'ready';
              updates.needs_attention = false;
              updates.last_message = 'Ready for task...';
              break;
            case 'user_prompt':
              if (!agent.task_summary) {
                const prompt = event.metadata?.prompt || '';
                updates.task_summary = prompt.length > 100 ? prompt.slice(0, 100) + '...' : prompt;
              }
              break;
            case 'file_modified':
            case 'file_created':
              // Include agent_id in message for debugging
              updates.last_message = `[${event.agent_id.slice(-8)}] Modified: ${event.metadata?.file_path || 'file'}`;
              // Work activity - transition from waiting/ready to in_progress
              if (agent.state === 'waiting_for_input' || agent.state === 'ready') {
                updates.state = 'in_progress';
                updates.needs_attention = false;
              }
              break;
            case 'tool_call':
              updates.last_message = `[${event.agent_id.slice(-8)}] Running: ${(event.metadata?.command || '').slice(0, 40)}`;
              // Work activity - transition from waiting/ready to in_progress
              if (agent.state === 'waiting_for_input' || agent.state === 'ready') {
                updates.state = 'in_progress';
                updates.needs_attention = false;
              }
              break;
            case 'code_generation':
              updates.last_message = `[${event.agent_id.slice(-8)}] Generated code (${event.metadata?.output_tokens || 0} tokens)`;
              // Work activity - transition from waiting/ready to in_progress
              if (agent.state === 'waiting_for_input' || agent.state === 'ready') {
                updates.state = 'in_progress';
                updates.needs_attention = false;
              }
              break;
            case 'thinking_start':
              updates.last_message = `[${event.agent_id.slice(-8)}] Thinking...`;
              if (agent.state === 'waiting_for_input' || agent.state === 'ready') {
                updates.state = 'in_progress';
                updates.needs_attention = false;
              }
              break;
            case 'user_prompt':
              // User provided a prompt - transition to in_progress
              if (agent.state === 'waiting_for_input' || agent.state === 'ready') {
                updates.state = 'in_progress';
                updates.needs_attention = false;
              }
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
      name: 'agentviz-storage',
      partialize: (state) => ({
        agents: state.agents,
        filters: state.filters,
        userLastSeen: state.userLastSeen,
      }),
    }
  )
);
