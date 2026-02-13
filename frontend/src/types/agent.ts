// Agent State Machine (Kanban columns)
export type AgentState =
  | 'ready'
  | 'in_progress'
  | 'waiting_for_input'
  | 'error'
  | 'completed';

// Backend hook states (more granular)
// These map to specific lifecycle events from agent hooks:
// - Claude Code: SessionStart, UserPromptSubmit, PreToolUse, Stop, PermissionRequest, SessionEnd
// - Gemini CLI: SessionStart, BeforeAgent, BeforeTool, AfterAgent, Notification, SessionEnd
// - Codex CLI: agent-turn-complete (limited hooks available)
export type BackendState =
  | 'starting'      // Session just started (SessionStart)
  | 'thinking'      // Processing prompt, before tools (UserPromptSubmit / BeforeAgent)
  | 'in_progress'   // Generic processing state
  | 'working'       // Executing a tool (PreToolUse / BeforeTool)
  | 'ready'         // Task complete, waiting for input (Stop / AfterAgent / agent-turn-complete)
  | 'waiting_for_input'  // Needs permission/approval (PermissionRequest / Notification)
  | 'idle'          // Idle for extended period
  | 'stopped'       // Session ended (SessionEnd / process exit)
  | 'error';        // Error occurred

// Map backend states to frontend Kanban columns
export function mapBackendStateToFrontend(backendState: BackendState): AgentState {
  switch (backendState) {
    case 'starting':
    case 'thinking':    // Thinking maps to in_progress (agent is working internally)
    case 'in_progress':
    case 'working':
      return 'in_progress';
    case 'ready':
    case 'idle':
      return 'ready';
    case 'waiting_for_input':
      return 'waiting_for_input';
    case 'stopped':
      return 'completed';
    case 'error':
      return 'error';
    default:
      return 'ready';
  }
}

// Subprocess information
export interface Subprocess {
  pid: number;
  parent_pid: number;
  command: string;
  state: 'running' | 'completed' | 'error';
  started_at: number;
  ended_at: number | null;
  exit_code: number | null;
}

// Full agent model
export interface Agent {
  id: string;
  type: string; // 'claude-code', 'gemini-cli', 'codex'
  state: AgentState;
  workspace: string;
  branch: string | null;
  repo: string | null;
  task_summary: string | null;
  pid: number | null;
  needs_attention: boolean;
  last_event_at: number;
  last_message: string | null;
  error_message: string | null;
  completed_at: number | null;
  started_at: number;
  subprocesses: Record<number, Subprocess>;
  first_seen: number;
  user_last_seen: number | null;
  ttyd_url: string | null;
}

// Agent event from socket
export interface AgentEvent {
  agent_id: string;
  agent_type: string;
  timestamp: number;
  event_type: string;
  working_dir: string;
  metadata: Record<string, any>;
}

// Column configuration
export interface ColumnConfig {
  id: AgentState;
  title: string;
  color: string;
  bgColor: string;
}

// Dashboard response from API
export interface DashboardResponse {
  agents_by_state: Record<AgentState, Agent[]>;
  total_agents: number;
  needs_attention_count: number;
}

// Filter state
export interface Filters {
  agentType: string[];
  repo: string[];
  branch: string[];
  showOnlyNeedsAttention: boolean;
  hideCompleted: boolean;
}

// Badge types for "while you were away"
export type BadgeType =
  | 'completed_away'
  | 'error_away'
  | 'needs_input'
  | 'stalled'
  | null;

// Determine badge for an agent
export function getAgentBadge(agent: Agent, userLastSeen: number | null): BadgeType {
  if (!userLastSeen) return null;

  const userLastSeenSeconds = userLastSeen / 1000; // Convert to seconds if in ms

  if (agent.state === 'completed' && agent.completed_at && agent.completed_at > userLastSeenSeconds) {
    return 'completed_away';
  }
  if (agent.state === 'error' && agent.last_event_at > userLastSeenSeconds) {
    return 'error_away';
  }
  if (agent.state === 'waiting_for_input') {
    return 'needs_input';
  }

  return null;
}

// Column definitions
export const COLUMNS: ColumnConfig[] = [
  { id: 'ready', title: 'READY / IDLE', color: '#6b7280', bgColor: 'rgba(107, 114, 128, 0.1)' },
  { id: 'in_progress', title: 'IN PROGRESS', color: '#3b82f6', bgColor: 'rgba(59, 130, 246, 0.1)' },
  { id: 'waiting_for_input', title: 'WAITING FOR INPUT', color: '#f59e0b', bgColor: 'rgba(245, 158, 11, 0.1)' },
  { id: 'error', title: 'ERROR', color: '#ef4444', bgColor: 'rgba(239, 68, 68, 0.1)' },
  { id: 'completed', title: 'COMPLETED', color: '#22c55e', bgColor: 'rgba(34, 197, 94, 0.1)' },
];

// Get column config by state
export function getColumnConfig(state: AgentState): ColumnConfig {
  return COLUMNS.find(c => c.id === state) || COLUMNS[0];
}
