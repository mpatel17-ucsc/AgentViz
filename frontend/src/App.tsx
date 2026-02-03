import React, { useEffect, useState, useCallback } from 'react';
import io from 'socket.io-client';
import {
  AppBar,
  Toolbar,
  Typography,
  Box,
  CssBaseline,
  IconButton,
  Tooltip,
  Badge,
  Chip,
} from '@mui/material';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import DeleteIcon from '@mui/icons-material/Delete';
import NotificationsIcon from '@mui/icons-material/Notifications';
import { useAgentStore } from './hooks/useAgentStore';
import { AgentEvent, Agent } from './types/agent';
import KanbanBoard from './components/KanbanBoard';
import FilterBar from './components/FilterBar';
import DetailDrawer from './components/DetailDrawer';

// Socket connection
const socket = io('http://localhost:8787', {
  reconnection: true,
  reconnectionDelay: 1000,
  reconnectionAttempts: 10,
});

// Dark theme
const theme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#3b82f6' },
    secondary: { main: '#6b7280' },
    success: { main: '#22c55e' },
    error: { main: '#ef4444' },
    warning: { main: '#f59e0b' },
    info: { main: '#3b82f6' },
    background: {
      default: '#0a0a0a',
      paper: '#111111',
    },
    text: {
      primary: '#e0e0e0',
      secondary: '#a0a0a0',
    },
    divider: '#222222',
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
          backgroundImage: 'none',
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          fontWeight: 500,
        },
      },
    },
  },
});

function App() {
  const {
    agents,
    setAgent,
    addEvent,
    clearAgents,
    updateUserLastSeen,
  } = useAgentStore();

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [connected, setConnected] = useState(false);

  // Count agents needing attention
  const needsAttentionCount = Object.values(agents).filter((a) => a.needs_attention).length;
  const totalAgents = Object.keys(agents).length;

  // Handle socket events
  useEffect(() => {
    // Connection events
    socket.on('connect', () => {
      console.log('[Socket] Connected');
      setConnected(true);
    });

    socket.on('disconnect', () => {
      console.log('[Socket] Disconnected');
      setConnected(false);
    });

    // Agent state updates from backend
    socket.on('agent_state', (agent: Agent) => {
      console.log(`[Socket] Agent state update: id=${agent.id}, state=${agent.state}, type=${agent.type}`);
      if (agent.id) {
        setAgent(agent);
      } else {
        console.error('[Socket] Received agent_state without id!', agent);
      }
    });

    // Agent events
    socket.on('agent_event', (event: AgentEvent) => {
      console.log(`[Socket] Event: ${event.event_type} from agent_id=${event.agent_id} (type=${event.agent_type})`);
      if (event.agent_id) {
        setEvents((prev) => [...prev, event]);
        addEvent(event);
      } else {
        console.error('[Socket] Received event without agent_id!', event);
      }
    });

    // State change notifications
    socket.on('agent_state_change', (data: { agent_id: string; old_state: string; new_state: string }) => {
      console.log('[Socket] State change:', data.agent_id, data.old_state, '->', data.new_state);
    });

    // Cleanup
    return () => {
      socket.off('connect');
      socket.off('disconnect');
      socket.off('agent_state');
      socket.off('agent_event');
      socket.off('agent_state_change');
    };
  }, [setAgent, addEvent]);

  // Update user last seen on visibility change
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.hidden) {
        // User left - save timestamp
        updateUserLastSeen();
      }
    };

    const handleBeforeUnload = () => {
      updateUserLastSeen();
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, [updateUserLastSeen]);

  const handleClearAll = useCallback(() => {
    clearAgents();
    setEvents([]);
    // Also clear on backend
    fetch('http://localhost:8787/agents', { method: 'DELETE' });
  }, [clearAgents]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
        {/* App Bar */}
        <AppBar
          position="static"
          elevation={0}
          sx={{
            borderBottom: '1px solid rgba(255,255,255,0.1)',
            bgcolor: '#0d0d0d',
          }}
        >
          <Toolbar variant="dense">
            <Typography
              variant="h6"
              component="div"
              sx={{ fontWeight: 700, letterSpacing: '-0.5px' }}
            >
              AgentViz
            </Typography>

            <Chip
              label={connected ? 'Connected' : 'Disconnected'}
              size="small"
              color={connected ? 'success' : 'error'}
              sx={{ ml: 2, height: 22 }}
            />

            <Box sx={{ flex: 1 }} />

            {/* Agent count */}
            <Typography variant="body2" sx={{ color: 'text.secondary', mr: 2 }}>
              {totalAgents} agent{totalAgents !== 1 ? 's' : ''}
            </Typography>

            {/* Needs attention badge */}
            {needsAttentionCount > 0 && (
              <Tooltip title={`${needsAttentionCount} agents need attention`}>
                <Badge badgeContent={needsAttentionCount} color="warning" sx={{ mr: 2 }}>
                  <NotificationsIcon sx={{ color: '#f59e0b' }} />
                </Badge>
              </Tooltip>
            )}

            <Tooltip title="Clear All Agents">
              <IconButton color="inherit" onClick={handleClearAll} size="small">
                <DeleteIcon />
              </IconButton>
            </Tooltip>
          </Toolbar>
        </AppBar>

        {/* Filter Bar */}
        <FilterBar />

        {/* Kanban Board */}
        <Box sx={{ flex: 1, overflow: 'hidden' }}>
          <KanbanBoard socket={socket} />
        </Box>

        {/* Detail Drawer */}
        <DetailDrawer socket={socket} events={events} />
      </Box>
    </ThemeProvider>
  );
}

export default App;
