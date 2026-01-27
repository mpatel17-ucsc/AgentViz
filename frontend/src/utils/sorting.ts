import { Agent } from '../types/agent';

/**
 * Sort agents by attention priority and recency.
 * Rules:
 * 1. needs_attention = true agents come first
 * 2. Within each group, sort by last_event_at DESC (newest first)
 */
export function sortByAttentionAndRecency(agents: Agent[]): Agent[] {
  return [...agents].sort((a, b) => {
    // needs_attention first
    if (a.needs_attention !== b.needs_attention) {
      return a.needs_attention ? -1 : 1;
    }
    // Then by last_event_at (newest first)
    return b.last_event_at - a.last_event_at;
  });
}

/**
 * Build subprocess tree structure from flat subprocess map.
 * Returns an array of root-level processes with nested children.
 */
export interface SubprocessNode {
  pid: number;
  parent_pid: number;
  command: string;
  state: 'running' | 'completed' | 'error';
  started_at: number;
  ended_at: number | null;
  exit_code: number | null;
  children: SubprocessNode[];
}

export function buildSubprocessTree(
  subprocesses: Record<number, any>,
  rootPid?: number
): SubprocessNode[] {
  const all = Object.values(subprocesses) as SubprocessNode[];
  const byParent: Record<number, SubprocessNode[]> = {};

  // Group by parent
  all.forEach((proc) => {
    const parent = proc.parent_pid || 0;
    if (!byParent[parent]) {
      byParent[parent] = [];
    }
    byParent[parent].push({ ...proc, children: [] });
  });

  // Build tree recursively
  function attachChildren(node: SubprocessNode): SubprocessNode {
    node.children = (byParent[node.pid] || []).map(attachChildren);
    return node;
  }

  // Find root nodes (those whose parent is the agent's main PID or not in our list)
  const allPids = new Set(all.map((p) => p.pid));
  const roots = all.filter(
    (p) => !allPids.has(p.parent_pid) || p.parent_pid === rootPid
  );

  return roots.map((r) => attachChildren({ ...r, children: [] }));
}

/**
 * Format a timestamp as relative time (e.g., "2m ago", "1h ago")
 */
export function formatRelativeTime(timestamp: number): string {
  const now = Date.now() / 1000;
  const diff = now - timestamp;

  if (diff < 60) {
    return 'just now';
  } else if (diff < 3600) {
    const mins = Math.floor(diff / 60);
    return `${mins}m ago`;
  } else if (diff < 86400) {
    const hours = Math.floor(diff / 3600);
    return `${hours}h ago`;
  } else {
    const days = Math.floor(diff / 86400);
    return `${days}d ago`;
  }
}

/**
 * Format a timestamp as HH:MM
 */
export function formatTime(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * Truncate command string for display
 */
export function truncateCommand(command: string, maxLength: number = 40): string {
  if (command.length <= maxLength) return command;
  return command.slice(0, maxLength - 3) + '...';
}
