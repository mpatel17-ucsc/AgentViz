import React, { useState, useRef, useEffect } from 'react';
import {
  Drawer,
  Box,
  Typography,
  IconButton,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  useMediaQuery,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import StopIcon from '@mui/icons-material/Stop';
import ReplayIcon from '@mui/icons-material/Replay';
import TerminalIcon from '@mui/icons-material/Terminal';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward';
import KeyboardReturnIcon from '@mui/icons-material/KeyboardReturn';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import { AgentEvent, getColumnConfig } from '../types/agent';
import { useAgentStore } from '../hooks/useAgentStore';
import { formatTime, formatRelativeTime } from '../utils/sorting';
import AgentTypeIcon from './AgentTypeIcon';
import SubprocessTree from './SubprocessTree';
import io from 'socket.io-client';

interface DetailDrawerProps {
  socket: ReturnType<typeof io>;
  events: AgentEvent[];
}

/**
 * Rewrite localhost/127.0.0.1 in a ttyd URL to the actual server hostname.
 * This makes terminal iframes work from any device on the network, not just
 * the machine running the backend.
 */
function resolveTerminalUrl(ttydUrl: string): string {
  const host = window.location.hostname;
  return ttydUrl.replace(/^(https?:\/\/)(localhost|127\.0\.0\.1)/, `$1${host}`);
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
  subagent_started: { displayName: 'Subagent Started', color: 'secondary' },
  subagent_stopped: { displayName: 'Subagent Done', color: 'secondary' },
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

/** Scrollable actions list that auto-scrolls to bottom for running subagents */
const SubagentActions: React.FC<{ actions: { tool: string; detail: string }[]; running: boolean }> = ({ actions, running }) => {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Scroll only the actions container itself — NOT scrollIntoView which
    // propagates up to all ancestor scrollable containers and causes the
    // main drawer body to stutter on every live update.
    if (running && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [actions.length, running]);

  return (
    <Box ref={containerRef} sx={{
      pl: 2.5, mt: 0.5, maxHeight: 120, overflowY: 'auto',
      '&::-webkit-scrollbar': { width: 4 },
      '&::-webkit-scrollbar-thumb': { bgcolor: 'rgba(255,255,255,0.1)', borderRadius: 2 },
    }}>
      {actions.map((action, i) => (
        <Box key={i} sx={{ display: 'flex', gap: 0.75, alignItems: 'baseline' }}>
          <Typography
            variant="caption"
            sx={{
              color: '#6b7280',
              fontSize: '10px',
              fontFamily: 'monospace',
              fontWeight: 600,
              flexShrink: 0,
              minWidth: 48,
            }}
          >
            {action.tool}
          </Typography>
          {action.detail && (
            <Typography
              variant="caption"
              sx={{
                color: 'text.secondary',
                fontSize: '10px',
                fontFamily: 'monospace',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {action.detail}
            </Typography>
          )}
        </Box>
      ))}
    </Box>
  );
};

export const DetailDrawer: React.FC<DetailDrawerProps> = ({ socket, events }) => {
  const { agents, selectedAgentId, drawerOpen, setDrawerOpen, markAgentSeen } = useAgentStore();
  const [terminalDialogOpen, setTerminalDialogOpen] = useState(false);
  const isTouchDevice = useMediaQuery('(pointer: coarse)');

  const agent = selectedAgentId ? agents[selectedAgentId] : null;
  const agentEvents = events.filter((e) => e.agent_id === selectedAgentId);

  // Mark agent as seen when drawer opens
  React.useEffect(() => {
    if (drawerOpen && selectedAgentId) {
      markAgentSeen(selectedAgentId);
      socket.emit('mark_agent_seen', { agent_id: selectedAgentId });
    }
  }, [drawerOpen, selectedAgentId, markAgentSeen, socket]);

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

  const handleSendKey = (key: 'Up' | 'Down' | 'Enter') => {
    if (selectedAgentId) {
      socket.emit('control_send_keys', { agent_id: selectedAgentId, key });
    }
  };

  if (!agent) return null;

  const config = getColumnConfig(agent.state);

  return (
    <>
    <Drawer
      anchor="right"
      open={drawerOpen}
      onClose={handleClose}
      sx={{
        '& .MuiDrawer-paper': {
          width: 450,
          bgcolor: '#0f0f0f',
          borderLeft: '1px solid rgba(255,255,255,0.1)',
          display: 'flex',
          flexDirection: 'column',
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
          {agent.ttyd_url && (
            <Button
              variant="outlined"
              size="small"
              startIcon={<TerminalIcon />}
              onClick={() => setTerminalDialogOpen(true)}
              color="info"
            >
              Open Terminal
            </Button>
          )}
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

      {/* Scrollable body: Subprocess Tree + Subagents + Event Timeline */}
      <Box sx={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>

      {/* Subprocess Tree */}
      {Object.keys(agent.subprocesses).length > 0 && (
        <Box sx={{ p: 2, borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600 }}>
            Subprocess Tree
          </Typography>
          <SubprocessTree subprocesses={agent.subprocesses} compact={false} maxVisible={20} />
        </Box>
      )}

      {/* Subagents (Claude Code Task tool) */}
      {Object.keys(agent.subagents || {}).length > 0 && (
        <Box sx={{ p: 2, borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <Typography variant="subtitle2" sx={{ mb: 1, fontWeight: 600 }}>
            Subagents ({Object.keys(agent.subagents).length})
          </Typography>
          {Object.values(agent.subagents)
            .sort((a, b) => b.started_at - a.started_at)
            .map((sa) => {
              const durationSec = sa.ended_at
                ? Math.round(sa.ended_at - sa.started_at)
                : null;
              return (
                <Box
                  key={sa.id}
                  sx={{ py: 0.75, borderBottom: '1px solid rgba(255,255,255,0.04)', '&:last-child': { borderBottom: 'none' } }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    {sa.state === 'running' ? (
                      <CircularProgress size={10} thickness={5} sx={{ color: '#8b5cf6' }} />
                    ) : (
                      <CheckCircleIcon sx={{ fontSize: 12, color: '#6b7280' }} />
                    )}
                    <Typography variant="caption" sx={{ fontWeight: 600, fontSize: '11px' }}>
                      {sa.agent_type}
                    </Typography>
                    <Typography variant="caption" sx={{ color: 'text.disabled', fontSize: '10px', ml: 'auto' }}>
                      {sa.state === 'running' ? 'running...' : durationSec !== null ? `${durationSec}s` : ''}
                    </Typography>
                  </Box>
                  {/* Tool call actions — scrollable, auto-scrolls to bottom while running */}
                  {sa.actions && sa.actions.length > 0 && (
                    <SubagentActions actions={sa.actions} running={sa.state === 'running'} />
                  )}
                  {/* Final message (only if no actions to show, avoid duplication) */}
                  {sa.last_message && (!sa.actions || sa.actions.length === 0) && (
                    <Typography
                      variant="caption"
                      sx={{
                        color: 'text.secondary',
                        fontSize: '10px',
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        overflow: 'hidden',
                        pl: 2.5,
                        mt: 0.25,
                      }}
                    >
                      {sa.last_message}
                    </Typography>
                  )}
                </Box>
              );
            })}
        </Box>
      )}

      {/* Event Timeline */}
      <Box sx={{ p: 2 }}>
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

      </Box> {/* end scrollable body */}

      {/* Footer with timestamps */}
      <Box
        sx={{
          p: 2,
          borderTop: '1px solid rgba(255,255,255,0.1)',
          bgcolor: 'rgba(0,0,0,0.3)',
          flexShrink: 0,
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

    {/* Terminal Dialog */}
    {agent.ttyd_url && (
      <Dialog
        open={terminalDialogOpen}
        onClose={() => setTerminalDialogOpen(false)}
        keepMounted
        maxWidth={false}
        PaperProps={{
          sx: {
            width: '80vw',
            height: '70vh',
            bgcolor: '#1a1a1a',
            backgroundImage: 'none',
          },
        }}
      >
        <DialogTitle
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            py: 1,
            bgcolor: '#111',
            borderBottom: '1px solid rgba(255,255,255,0.1)',
          }}
        >
          <TerminalIcon sx={{ fontSize: 20 }} />
          <Typography variant="subtitle1" sx={{ flex: 1 }}>
            Terminal - {agent.id}
          </Typography>
          <IconButton
            size="small"
            onClick={() => window.open(resolveTerminalUrl(agent.ttyd_url!), '_blank')}
            title="Open in new tab"
          >
            <OpenInNewIcon sx={{ fontSize: 18 }} />
          </IconButton>
          <IconButton size="small" onClick={() => setTerminalDialogOpen(false)}>
            <CloseIcon sx={{ fontSize: 18 }} />
          </IconButton>
        </DialogTitle>
        <DialogContent sx={{ p: 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
          <iframe
            src={resolveTerminalUrl(agent.ttyd_url!)}
            style={{
              width: '100%',
              flex: 1,
              border: 'none',
              backgroundColor: '#000',
            }}
            title={`Terminal for ${agent.id}`}
          />
          {isTouchDevice && agent.tmux_session && (
            <Box
              sx={{
                display: 'flex',
                justifyContent: 'center',
                gap: 2,
                p: 1.5,
                bgcolor: '#111',
                borderTop: '1px solid rgba(255,255,255,0.1)',
                // Isolate from iframe touch handling: prevent the browser
                // from interpreting taps on these buttons as scroll/pan
                // gestures targeting the adjacent ttyd iframe.
                position: 'relative',
                zIndex: 10,
                touchAction: 'manipulation',
              }}
            >
              <IconButton
                onPointerDown={(e) => { e.preventDefault(); handleSendKey('Up'); }}
                sx={{
                  width: 56,
                  height: 56,
                  bgcolor: 'rgba(255,255,255,0.1)',
                  '&:active': { bgcolor: 'rgba(255,255,255,0.3)' },
                  touchAction: 'manipulation',
                }}
              >
                <ArrowUpwardIcon sx={{ fontSize: 28 }} />
              </IconButton>
              <IconButton
                onPointerDown={(e) => { e.preventDefault(); handleSendKey('Down'); }}
                sx={{
                  width: 56,
                  height: 56,
                  bgcolor: 'rgba(255,255,255,0.1)',
                  '&:active': { bgcolor: 'rgba(255,255,255,0.3)' },
                  touchAction: 'manipulation',
                }}
              >
                <ArrowDownwardIcon sx={{ fontSize: 28 }} />
              </IconButton>
              <IconButton
                onPointerDown={(e) => { e.preventDefault(); handleSendKey('Enter'); }}
                sx={{
                  width: 56,
                  height: 56,
                  bgcolor: 'rgba(59,130,246,0.3)',
                  '&:active': { bgcolor: 'rgba(59,130,246,0.5)' },
                  touchAction: 'manipulation',
                }}
              >
                <KeyboardReturnIcon sx={{ fontSize: 28 }} />
              </IconButton>
            </Box>
          )}
        </DialogContent>
      </Dialog>
    )}
    </>
  );
};

export default DetailDrawer;
