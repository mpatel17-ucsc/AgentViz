// src/App.tsx (minor update if needed, but your original seems fine; adding explicit entries for clarity)

import React, { useEffect, useState } from 'react';
import io from 'socket.io-client';
import {
  AppBar,
  Toolbar,
  Typography,
  Chip,
  Box,
  Paper,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  CssBaseline,
  IconButton,
  Tooltip,
} from '@mui/material';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import RefreshIcon from '@mui/icons-material/Refresh';
import DeleteIcon from '@mui/icons-material/Delete';

const socket = io('http://localhost:8787', {
  reconnection: true,
  reconnectionDelay: 1000,
  reconnectionAttempts: 10
});

interface AgentEvent {
  agent_id: string;
  agent_type: string;
  timestamp: number;
  event_type: string;
  working_dir: string;
  metadata: any;
}

interface Agent {
  id: string;
  type: string;
  status: 'running' | 'stopped' | 'error';
  workspace: string;
  pid: number;
  events: AgentEvent[];
  thinking: boolean;
}

const theme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#5b9bd5' },
    background: {
      default: '#0a0a0a',
      paper: '#111111',
    },
    text: {
      primary: '#e0e0e0',
      secondary: '#a0a0a0',
    },
    divider: '#222222',
    success: { main: '#4caf50' },
    error: { main: '#f44336' },
    info: { main: '#2196f3' },
    warning: { main: '#ff9800' },
  },
  typography: {
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
    h5: { fontWeight: 600 },
    h6: { fontWeight: 600 },
  },
  components: {
    MuiPaper: {
      styleOverrides: {
        root: {
          borderRadius: 12,
          border: '1px solid #222',
          backgroundImage: 'none',
        },
      },
    },
  },
});

const eventTypeMapping: Record<string, { displayName: string; important: boolean }> = {
  // Agent lifecycle
  'agent_started': { displayName: 'Agent Started', important: true },
  'agent_stopped': { displayName: 'Agent Stopped', important: true },
  'error': { displayName: 'Error', important: true },

  // Thinking/activity
  'thinking_start': { displayName: 'Thinking Started', important: true },
  'thinking_end': { displayName: 'Thinking Ended', important: true },

  // File operations (BaseAdapter + OTEL)
  'file_created': { displayName: 'File Created', important: true },
  'file_modified': { displayName: 'File Modified', important: true },
  'file_deleted': { displayName: 'File Deleted', important: true },
  'file_operation': { displayName: 'File Operation', important: true },
  'lines_changed': { displayName: 'Lines Changed', important: true },

  // Tool execution
  'tool_call': { displayName: 'Tool Executed', important: true },
  'tool_approval': { displayName: 'Tool Approval', important: true },
  'tool_result_metadata': { displayName: 'Tool Result', important: false },

  // API/Token usage
  'code_generation': { displayName: 'Code Generated', important: true },
  'token_usage': { displayName: 'Token Usage', important: true },
  'cost_update': { displayName: 'Cost Update', important: true },

  // Session lifecycle
  'session_started': { displayName: 'Session Started', important: true },
  'session_ended': { displayName: 'Session Ended', important: true },
  'session_summary': { displayName: 'Session Summary', important: true },

  // User interaction
  'waiting_for_input': { displayName: 'Waiting for Input', important: true },
  'user_prompt': { displayName: 'User Prompt', important: false },
};

const renderPayload = (event: AgentEvent) => {
    const { metadata } = event;
    if (!metadata) return null;

    switch (event.event_type) {
      case 'file_created':
      case 'file_modified':
      case 'file_deleted':
      case 'file_operation':
        return (
          <Box sx={{ mt: 0.5 }}>
            <Typography variant="body2">
              File: {metadata.file_path} {metadata.size_bytes ? `(${metadata.size_bytes} bytes)` : ''}
              {metadata.lines_added ? ` (+${metadata.lines_added} lines)` : ''}
              {metadata.lines_removed ? ` (-${metadata.lines_removed} lines)` : ''}
              {metadata.operation_type ? ` [${metadata.operation_type}]` : ''}
              {metadata.programming_language ? ` (${metadata.programming_language})` : ''}
            </Typography>
            {/* Show git diff if available */}
            {metadata.diff && (
              <Box
                component="pre"
                sx={{
                  mt: 1,
                  p: 1.5,
                  bgcolor: '#1a1a2e',
                  borderRadius: 1,
                  fontSize: '11px',
                  fontFamily: 'monospace',
                  overflow: 'auto',
                  maxHeight: '300px',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  '& .diff-add': { color: '#4caf50', bgcolor: 'rgba(76, 175, 80, 0.1)' },
                  '& .diff-remove': { color: '#f44336', bgcolor: 'rgba(244, 67, 54, 0.1)' },
                  '& .diff-header': { color: '#2196f3' },
                }}
              >
                {metadata.diff.split('\n').map((line: string, i: number) => {
                  let className = '';
                  if (line.startsWith('+') && !line.startsWith('+++')) className = 'diff-add';
                  else if (line.startsWith('-') && !line.startsWith('---')) className = 'diff-remove';
                  else if (line.startsWith('@@') || line.startsWith('diff') || line.startsWith('index')) className = 'diff-header';
                  return <div key={i} className={className}>{line}</div>;
                })}
              </Box>
            )}
            {/* Show content preview for new files without git diff */}
            {!metadata.diff && metadata.content_preview && (
              <Box
                component="pre"
                sx={{
                  mt: 1,
                  p: 1.5,
                  bgcolor: '#1a1a2e',
                  borderRadius: 1,
                  fontSize: '11px',
                  fontFamily: 'monospace',
                  overflow: 'auto',
                  maxHeight: '200px',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  color: '#a0a0a0',
                }}
              >
                {metadata.content_preview}
              </Box>
            )}
          </Box>
        );
      case 'lines_changed':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Type: {metadata.type} | Count: {metadata.count} | Function: {metadata.function_name}
          </Typography>
        );
      case 'code_generation':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Model: {metadata.model} | Input Tokens: {metadata.input_tokens} | Output Tokens: {metadata.output_tokens}
          </Typography>
        );
      case 'token_usage':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Type: {metadata.type} | Total: {metadata.total} | Model: {metadata.model}
          </Typography>
        );
      case 'session_summary':
        return (
          <Typography variant="body2" sx={{ mt: 0.5, whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(metadata.attributes, null, 2)}
          </Typography>
        );
      case 'tool_call':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Tool: {metadata.tool_name} | Command: {metadata.command}
          </Typography>
        );
      case 'waiting_for_input':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Prompt: {metadata.prompt}
          </Typography>
        );
      case 'tool_approval':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Tool: {metadata.tool_name} | {metadata.approved ? '✓ Approved' : '✗ Denied'}
          </Typography>
        );
      case 'cost_update':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Cost: ${metadata.cost?.toFixed(4) || '0.0000'}
          </Typography>
        );
      case 'session_started':
      case 'session_ended':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            {metadata.conversation_id ? `Session: ${metadata.conversation_id}` : 'Session event'}
          </Typography>
        );
      case 'user_prompt':
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            Prompt: {metadata.prompt}
          </Typography>
        );
      case 'error':
        return (
          <Typography variant="body2" sx={{ mt: 0.5, color: 'error.main' }}>
            {metadata.error || metadata.message || 'Unknown error'}
          </Typography>
        );
      default:
        return (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            {JSON.stringify(metadata, null, 2)}
          </Typography>
        );
    }
};

const processAndFilterEvents = (events: AgentEvent[]): AgentEvent[] => {
  const filtered = events.filter(event => {
    const mapping = eventTypeMapping[event.event_type];
    return mapping?.important ?? true;
  });

  // Deduplicate consecutive identical events (thinking, waiting_for_input)
  const dedupedEvents: AgentEvent[] = [];
  const seenWaitingPrompts = new Set<string>();

  for (let i = 0; i < filtered.length; i++) {
    const event = filtered[i];
    const prev = i > 0 ? filtered[i - 1] : null;

    // Skip consecutive duplicate thinking events
    if (prev && event.event_type.startsWith('thinking') && event.event_type === prev.event_type) {
      continue;
    }

    // Deduplicate waiting_for_input by prompt content
    if (event.event_type === 'waiting_for_input') {
      const prompt = event.metadata?.prompt?.toLowerCase()?.trim()?.substring(0, 200) || '';
      if (seenWaitingPrompts.has(prompt)) {
        continue; // Skip duplicate waiting prompt
      }
      seenWaitingPrompts.add(prompt);

      // Clear old prompts after we see a different event type (new context)
      if (prev && prev.event_type !== 'waiting_for_input') {
        seenWaitingPrompts.clear();
        seenWaitingPrompts.add(prompt);
      }
    }

    dedupedEvents.push(event);
  }

  return dedupedEvents;
};

// Local storage keys
const STORAGE_KEY = 'agentviz_agents';
const SELECTED_AGENT_KEY = 'agentviz_selected_agent';

function App() {
  const [agents, setAgents] = useState<Record<string, Agent>>(() => {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? JSON.parse(stored) : {};
  });
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(() => localStorage.getItem(SELECTED_AGENT_KEY));

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(agents));
  }, [agents]);

  useEffect(() => {
    if (selectedAgentId) {
      localStorage.setItem(SELECTED_AGENT_KEY, selectedAgentId);
    } else {
      localStorage.removeItem(SELECTED_AGENT_KEY);
    }
  }, [selectedAgentId]);

  useEffect(() => {
    const handleEvent = (event: AgentEvent) => {
      setAgents((prevAgents) => {
        const agentId = event.agent_id;
        const existingAgent = prevAgents[agentId] || {
          id: agentId,
          type: event.agent_type,
          status: 'running',
          workspace: event.working_dir,
          pid: 0, // Placeholder, update if available
          events: [],
          thinking: false,
        };

        const newEvents = [...existingAgent.events, event];

        let newStatus = existingAgent.status;
        let newThinking = existingAgent.thinking;

        if (event.event_type === 'agent_stopped') {
          newStatus = 'stopped';
        } else if (event.event_type === 'error') {
          newStatus = 'error';
        } else if (event.event_type === 'thinking_start') {
          newThinking = true;
        } else if (event.event_type === 'thinking_end') {
          newThinking = false;
        }

        return {
          ...prevAgents,
          [agentId]: {
            ...existingAgent,
            events: newEvents,
            status: newStatus,
            thinking: newThinking,
          },
        };
      });
    };

    socket.on('agent_event', handleEvent);

    return () => {
      socket.off('agent_event', handleEvent);
    };
  }, []);

  const selectedAgent = selectedAgentId ? agents[selectedAgentId] : null;
  const displayedEvents = selectedAgent ? processAndFilterEvents(selectedAgent.events) : [];

  const getEventColor = (eventType: string) => {
    switch (eventType) {
      case 'agent_started':
      case 'task_completed':
        return 'success';
      case 'agent_stopped':
      case 'thinking_end':
        return 'secondary';
      case 'file_created':
      case 'file_modified':
      case 'file_operation':
        return 'info';
      case 'file_deleted':
        return 'warning';
      case 'tool_call':
      case 'command_execution':
      case 'program_execution':
        return 'primary';
      case 'error':
      case 'agent_error':
        return 'error';
      default:
        return 'default';
    }
  };

 

  const handleRefresh = () => {
    window.location.reload();
  };

  const handleClearAll = () => {
    setAgents({});
    setSelectedAgentId(null);
  };

  const handleDeleteAgent = (agentId: string) => {
    setAgents((prev) => {
      const newAgents = { ...prev };
      delete newAgents[agentId];
      return newAgents;
    });
    if (selectedAgentId === agentId) {
      setSelectedAgentId(null);
    }
  };

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
        <AppBar position="static" sx={{ borderBottom: '1px solid #222' }}>
          <Toolbar>
            <Typography variant="h6" component="div" sx={{ flexGrow: 1 }}>
              AgentViz
            </Typography>
            <Tooltip title="Refresh">
              <IconButton color="inherit" onClick={handleRefresh}>
                <RefreshIcon />
              </IconButton>
            </Tooltip>
            <Tooltip title="Clear All Agents">
              <IconButton color="inherit" onClick={handleClearAll}>
                <DeleteIcon />
              </IconButton>
            </Tooltip>
          </Toolbar>
        </AppBar>

        <Box sx={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
          <Paper
            sx={{
              width: 280,
              borderRight: '1px solid #222',
              overflowY: 'auto',
              minHeight: 0,
              '&::-webkit-scrollbar': { width: '6px' },
              '&::-webkit-scrollbar-thumb': { backgroundColor: '#555', borderRadius: '3px' },
            }}
          >
            <Typography variant="h6" sx={{ p: 2, borderBottom: '1px solid #222' }}>
              Agents ({Object.keys(agents).length})
            </Typography>
            <List>
              {Object.values(agents).map((agent) => (
                <ListItem
                  key={agent.id}
                  disablePadding
                  secondaryAction={
                    <IconButton edge="end" aria-label="delete" onClick={() => handleDeleteAgent(agent.id)}>
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  }
                  sx={{ pr: 6 }}
                >
                  <ListItemButton
                    selected={selectedAgentId === agent.id}
                    onClick={() => setSelectedAgentId(agent.id)}
                  >
                    <ListItemText
                      primary={agent.id}
                      secondary={
                        <>
                          <Typography component="span" variant="body2" color="text.primary">
                            {agent.type}
                          </Typography>
                          {` • ${agent.status}`}
                          {agent.thinking && ' • Thinking...'}
                        </>
                      }
                    />
                  </ListItemButton>
                </ListItem>
              ))}
            </List>
          </Paper>

          <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
            {selectedAgent ? (
                <Box
                  sx={{
                    flex: 1,
                    p: 2,
                    display: 'flex',
                    flexDirection: 'column',
                    overflow: 'hidden',
                    minHeight: 0,
                  }}
                >
                  <Typography variant="h6" gutterBottom>
                    Event Timeline ({displayedEvents.length} events)
                  </Typography>

                  <Box
                    sx={{
                      flex: 1,
                      overflowY: 'auto',
                      minHeight: 0,
                      '&::-webkit-scrollbar': { width: '6px' },
                      '&::-webkit-scrollbar-thumb': {
                        backgroundColor: '#555',
                        borderRadius: '3px',
                      },
                    }}
                  >
                    {displayedEvents.length === 0 ? (
                      <Typography color="text.secondary" sx={{ mt: 4, textAlign: 'center' }}>
                        No events received yet...
                      </Typography>
                    ) : (
                      displayedEvents
                        .slice()
                        .reverse()
                        .map((event, index) => (
                          <ListItem
                            key={index}
                            disablePadding
                            sx={{
                              mb: 1.5,
                              p: 1.5,
                              borderRadius: 1,
                              bgcolor: 'rgba(255,255,255,0.03)',
                            }}
                          >
                            <ListItemText
                              primary={
                                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                                  <Chip
                                    size="small"
                                    label={eventTypeMapping[event.event_type]?.displayName ?? event.event_type}
                                    color={getEventColor(event.event_type) as 'primary' | 'secondary' | 'success' | 'error' | 'info' | 'warning' | 'default'}
                                    variant="outlined"
                                  />
                                  <Typography variant="body2" color="text.secondary">
                                    {new Date(event.timestamp * 1000).toLocaleTimeString()}
                                  </Typography>
                                </Box>
                              }
                              secondary={renderPayload(event)}
                            />
                          </ListItem>
                        ))
                    )}
                  </Box>
                </Box>
              ) : (
                <Paper
                  sx={{
                    flex: 1,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    minHeight: 0,
                  }}
                >
                  <Typography variant="h6" color="text.secondary">
                    Select an agent to view its timeline
                  </Typography>
                </Paper>
              )}
          </Box>
        </Box>
      </Box>
    </ThemeProvider>
  );
}

export default App;