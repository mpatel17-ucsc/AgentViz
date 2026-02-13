import React from 'react';
import {
  Drawer,
  Box,
  Typography,
  IconButton,
  Button,
  Chip,
  Divider,
  TextField,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import StopIcon from '@mui/icons-material/Stop';
import ReplayIcon from '@mui/icons-material/Replay';
import TerminalIcon from '@mui/icons-material/Terminal';
import SendIcon from '@mui/icons-material/Send';
import { Agent, AgentEvent, getColumnConfig, PromptOption } from '../types/agent';
import { useAgentStore } from '../hooks/useAgentStore';
import { formatTime, formatRelativeTime } from '../utils/sorting';
import AgentTypeIcon from './AgentTypeIcon';
import SubprocessTree from './SubprocessTree';
import io from 'socket.io-client';

interface DetailDrawerProps {
  socket: ReturnType<typeof io>;
  events: AgentEvent[];
}

// Event type display mapping
const eventTypeMapping: Record<string, { displayName: string; color: string }> = {
  agent_started: { displayName: 'Agent Started', color: 'success' },
  agent_stopped: { displayName: 'Agent Stopped', color: 'default' },
  error: { displayName: 'Error', color: 'error' },
  thinking_start: { displayName: 'Thinking', color: 'info' },
  thinking_end: { displayName: 'Done Thinking', color: 'default' },
  file_created: { displayName: 'File Created', color: 'info' },
  file_modified: { displayName: 'File Modified', color: 'info' },
  file_deleted: { displayName: 'File Deleted', color: 'warning' },
  tool_call: { displayName: 'Tool Call', color: 'primary' },
  waiting_for_input: { displayName: 'Waiting for Input', color: 'warning' },
  user_prompt: { displayName: 'User Prompt', color: 'default' },
  token_usage: { displayName: 'Token Usage', color: 'info' },
  cost_update: { displayName: 'Cost Update', color: 'info' },
  subprocess_started: { displayName: 'Subprocess Started', color: 'primary' },
  subprocess_ended: { displayName: 'Subprocess Ended', color: 'default' },
};

const EventItem: React.FC<{ event: AgentEvent }> = ({ event }) => {
  const mapping = eventTypeMapping[event.event_type] || {
    displayName: event.event_type,
    color: 'default',
  };

  const renderMetadata = () => {
    const { metadata } = event;
    if (!metadata) return null;

    switch (event.event_type) {
      case 'file_modified':
      case 'file_created':
      case 'file_deleted':
        return (
          <Box sx={{ mt: 0.5 }}>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              {metadata.file_path}
              {metadata.lines_added ? ` (+${metadata.lines_added})` : ''}
              {metadata.lines_removed ? ` (-${metadata.lines_removed})` : ''}
            </Typography>
            {metadata.diff && (
              <Box
                component="pre"
                sx={{
                  mt: 1,
                  p: 1,
                  bgcolor: '#1a1a2e',
                  borderRadius: 1,
                  fontSize: '10px',
                  fontFamily: 'monospace',
                  overflow: 'auto',
                  maxHeight: 200,
                  whiteSpace: 'pre-wrap',
                  '& .diff-add': { color: '#4caf50' },
                  '& .diff-remove': { color: '#f44336' },
                }}
              >
                {metadata.diff.split('\n').map((line: string, i: number) => {
                  let className = '';
                  if (line.startsWith('+') && !line.startsWith('+++')) className = 'diff-add';
                  else if (line.startsWith('-') && !line.startsWith('---')) className = 'diff-remove';
                  return (
                    <div key={i} className={className}>
                      {line}
                    </div>
                  );
                })}
              </Box>
            )}
          </Box>
        );
      case 'tool_call':
        return (
          <Typography
            variant="caption"
            sx={{ color: 'text.secondary', fontFamily: 'monospace', display: 'block', mt: 0.5 }}
          >
            $ {metadata.command}
          </Typography>
        );
      case 'waiting_for_input':
        return (
          <Typography variant="caption" sx={{ color: '#f59e0b', display: 'block', mt: 0.5 }}>
            {metadata.prompt}
          </Typography>
        );
      case 'error':
        return (
          <Typography variant="caption" sx={{ color: '#ef4444', display: 'block', mt: 0.5 }}>
            {metadata.error || metadata.message}
          </Typography>
        );
      case 'token_usage':
        return (
          <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mt: 0.5 }}>
            {metadata.model}: {metadata.input_tokens} in / {metadata.output_tokens} out
          </Typography>
        );
      case 'subprocess_started':
      case 'subprocess_ended':
        return (
          <Typography
            variant="caption"
            sx={{ color: 'text.secondary', fontFamily: 'monospace', display: 'block', mt: 0.5 }}
          >
            [{metadata.state}] {metadata.command?.slice(0, 60)}
          </Typography>
        );
      default:
        return null;
    }
  };

  return (
    <Box sx={{ py: 1, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <Chip
          label={mapping.displayName}
          size="small"
          color={mapping.color as any}
          variant="outlined"
          sx={{ height: 20, fontSize: '10px' }}
        />
        <Typography variant="caption" sx={{ color: 'text.disabled', ml: 'auto' }}>
          {formatTime(event.timestamp)}
        </Typography>
      </Box>
      {renderMetadata()}
    </Box>
  );
};

// Events to hide from timeline (high-frequency or internal)
const HIDDEN_EVENT_TYPES = new Set(['prompt_options', 'agent_message', 'terminal_update']);

export const DetailDrawer: React.FC<DetailDrawerProps> = ({ socket, events }) => {
  const { agents, selectedAgentId, drawerOpen, setDrawerOpen, markAgentSeen } = useAgentStore();

  const agent = selectedAgentId ? agents[selectedAgentId] : null;
  const agentEvents = events.filter(
    (e) => e.agent_id === selectedAgentId && !HIDDEN_EVENT_TYPES.has(e.event_type)
  );

  const [inputValue, setInputValue] = React.useState('');

  // Mark agent as seen when drawer opens
  React.useEffect(() => {
    if (drawerOpen && selectedAgentId) {
      markAgentSeen(selectedAgentId);
      socket.emit('mark_agent_seen', { agent_id: selectedAgentId });
    }
  }, [drawerOpen, selectedAgentId, markAgentSeen, socket]);

  // Clear input when agent changes
  React.useEffect(() => {
    setInputValue('');
  }, [selectedAgentId]);

  const handleClose = () => {
    setDrawerOpen(false);
  };

  const handleRetry = () => {
    if (selectedAgentId) {
      socket.emit('control_retry', { agent_id: selectedAgentId });
    }
  };

  const handleCancel = () => {
    if (selectedAgentId) {
      socket.emit('control_cancel', { agent_id: selectedAgentId });
    }
  };

  const handleSendInput = () => {
    if (!selectedAgentId || !inputValue.trim()) return;
    const text = inputValue.trim();
    socket.emit('agent_control', {
      agent_id: selectedAgentId,
      action: 'send_input',
      text,
      append_enter: false,
      enter_sequence: 'cr',
    });
    // Send Enter as an independent control action so terminal UIs treat it as
    // a real keypress rather than part of pasted content.
    setTimeout(() => {
      socket.emit('agent_control', {
        agent_id: selectedAgentId,
        action: 'simulate_enter',
        enter_sequence: 'cr',
      });
    }, 40);
    setInputValue('');
  };

  const handleSelectOption = (opt: PromptOption, index: number) => {
    if (!selectedAgentId) return;
    socket.emit('agent_control', {
      agent_id: selectedAgentId,
      action: 'select_option',
      index,
      selected: { input: opt.input },
      input: opt.input,
    });
  };

  if (!agent) return null;

  const config = getColumnConfig(agent.state);
  const isControllable = agent.wrapper === 'controllable';
  const promptOptions: PromptOption[] = agent.prompt_options || [];
  const showInput = isControllable && (agent.state === 'ready' || agent.state === 'waiting_for_input');
  const showOptions = isControllable && agent.state === 'waiting_for_input' && promptOptions.length > 0;
  if (showOptions) {
    console.log(`[DetailDrawer] showOptions=true for ${agent.id}, ${promptOptions.length} options:`, promptOptions.map(o => o.label));
  }

  return (
    <Drawer
      anchor="right"
      open={drawerOpen}
      onClose={handleClose}
      sx={{
        '& .MuiDrawer-paper': {
          width: 450,
          bgcolor: '#0f0f0f',
          borderLeft: '1px solid rgba(255,255,255,0.1)',
        },
      }}
    >
      {/* Header */}
      <Box
        sx={{
          p: 2,
          borderBottom: '1px solid rgba(255,255,255,0.1)',
          bgcolor: config.bgColor,
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          <AgentTypeIcon type={agent.type} size="large" />
          <Typography variant="h6" sx={{ flex: 1 }}>
            {agent.id}
          </Typography>
          <IconButton onClick={handleClose} size="small">
            <CloseIcon />
          </IconButton>
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          <Chip
            label={config.title}
            size="small"
            sx={{
              bgcolor: config.color,
              color: 'white',
              fontWeight: 600,
              fontSize: '10px',
            }}
          />
          {isControllable && (
            <Chip
              label="CONTROLLABLE"
              size="small"
              color="primary"
              variant="outlined"
              sx={{ fontSize: '9px', height: 20 }}
            />
          )}
          {agent.needs_attention && (
            <Chip label="Needs Attention" size="small" color="warning" sx={{ fontSize: '10px' }} />
          )}
        </Box>

        {agent.repo && (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            {agent.repo}
            {agent.branch && ` / ${agent.branch}`}
          </Typography>
        )}
        {agent.task_summary && (
          <Typography variant="body2" sx={{ mt: 0.5 }}>
            {agent.task_summary}
          </Typography>
        )}

        {/* Control Buttons */}
        <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
          {agent.state === 'error' && (
            <Button
              variant="outlined"
              size="small"
              startIcon={<ReplayIcon />}
              onClick={handleRetry}
              color="warning"
            >
              Retry
            </Button>
          )}
          {agent.state === 'in_progress' && (
            <Button
              variant="outlined"
              size="small"
              startIcon={<StopIcon />}
              onClick={handleCancel}
              color="error"
            >
              Cancel
            </Button>
          )}
        </Box>
      </Box>

      {/* AgentAPI: Prompt Options (when waiting_for_input with detected options) */}
      {showOptions && (
        <Box sx={{ p: 2, borderBottom: '1px solid rgba(255,255,255,0.1)', bgcolor: 'rgba(245, 158, 11, 0.05)' }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600, color: '#f59e0b' }}>
            Select Option
          </Typography>
          <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
            {promptOptions.map((opt, i) => (
              <Button
                key={`${opt.label}-${i}`}
                variant="outlined"
                size="small"
                onClick={() => handleSelectOption(opt, i)}
                sx={{
                  fontSize: '11px',
                  textTransform: 'none',
                  borderColor: 'rgba(245, 158, 11, 0.4)',
                  color: '#f59e0b',
                  '&:hover': {
                    borderColor: '#f59e0b',
                    bgcolor: 'rgba(245, 158, 11, 0.1)',
                  },
                }}
              >
                {opt.label}
              </Button>
            ))}
          </Box>
        </Box>
      )}

      {/* AgentAPI: Input field (when ready or waiting_for_input) */}
      {showInput && (
        <Box sx={{ p: 2, borderBottom: '1px solid rgba(255,255,255,0.1)', bgcolor: 'rgba(59, 130, 246, 0.03)' }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600, display: 'flex', alignItems: 'center', gap: 1 }}>
            <SendIcon fontSize="small" />
            {agent.state === 'ready' ? 'Send New Prompt' : 'Send Input'}
          </Typography>
          <Box sx={{ display: 'flex', gap: 1 }}>
            <TextField
              fullWidth
              size="small"
              multiline
              maxRows={4}
              placeholder={agent.state === 'ready' ? 'Type a task or prompt...' : 'Type your response...'}
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSendInput();
                }
              }}
              sx={{
                '& .MuiOutlinedInput-root': {
                  bgcolor: '#1a1a1a',
                  fontSize: '13px',
                  '& fieldset': { borderColor: 'rgba(255,255,255,0.15)' },
                  '&:hover fieldset': { borderColor: 'rgba(255,255,255,0.3)' },
                  '&.Mui-focused fieldset': { borderColor: '#3b82f6' },
                },
                '& .MuiInputBase-input': { color: '#e0e0e0' },
              }}
            />
            <Button
              variant="contained"
              size="small"
              onClick={handleSendInput}
              disabled={!inputValue.trim()}
              sx={{ minWidth: 60 }}
            >
              Send
            </Button>
          </Box>
          <Typography variant="caption" sx={{ color: 'text.disabled', mt: 0.5, display: 'block' }}>
            Enter to send, Shift+Enter for newline
          </Typography>
        </Box>
      )}

      {/* Subprocess Tree */}
      {Object.keys(agent.subprocesses).length > 0 && (
        <Box sx={{ p: 2, borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600 }}>
            Subprocess Tree
          </Typography>
          <SubprocessTree subprocesses={agent.subprocesses} compact={false} maxVisible={20} />
        </Box>
      )}

      {/* Event Timeline */}
      <Box sx={{ flex: 1, overflow: 'auto', p: 2 }}>
        <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600 }}>
          Event Timeline ({agentEvents.length} events)
        </Typography>

        {agentEvents.length === 0 ? (
          <Typography variant="body2" sx={{ color: 'text.secondary', textAlign: 'center', py: 4 }}>
            No events yet...
          </Typography>
        ) : (
          [...agentEvents].reverse().map((event, index) => (
            <EventItem key={index} event={event} />
          ))
        )}
      </Box>

      {/* Footer with timestamps */}
      <Box
        sx={{
          p: 2,
          borderTop: '1px solid rgba(255,255,255,0.1)',
          bgcolor: 'rgba(0,0,0,0.3)',
        }}
      >
        <Typography variant="caption" sx={{ color: 'text.disabled', display: 'block' }}>
          Started: {formatTime(agent.started_at)} ({formatRelativeTime(agent.started_at)})
        </Typography>
        {agent.completed_at && (
          <Typography variant="caption" sx={{ color: 'text.disabled', display: 'block' }}>
            Completed: {formatTime(agent.completed_at)}
          </Typography>
        )}
        <Typography variant="caption" sx={{ color: 'text.disabled', display: 'block' }}>
          Last activity: {formatRelativeTime(agent.last_event_at)}
        </Typography>
      </Box>
    </Drawer>
  );
};

export default DetailDrawer;
