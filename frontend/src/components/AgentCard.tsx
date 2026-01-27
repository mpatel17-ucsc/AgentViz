import React from 'react';
import { Box, Paper, Typography, CircularProgress } from '@mui/material';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorIcon from '@mui/icons-material/Error';
import PauseCircleIcon from '@mui/icons-material/PauseCircle';
import { Agent, getAgentBadge, getColumnConfig } from '../types/agent';
import { formatRelativeTime, formatTime } from '../utils/sorting';
import AgentTypeIcon from './AgentTypeIcon';
import AgentCardBadge from './AgentCardBadge';
import SubprocessTree from './SubprocessTree';
import { useAgentStore } from '../hooks/useAgentStore';

interface AgentCardProps {
  agent: Agent;
  isDragging?: boolean;
}

const StateIndicator: React.FC<{ state: Agent['state'] }> = ({ state }) => {
  switch (state) {
    case 'in_progress':
      return (
        <CircularProgress
          size={16}
          thickness={4}
          sx={{ color: '#3b82f6' }}
        />
      );
    case 'completed':
      return <CheckCircleIcon sx={{ fontSize: 16, color: '#22c55e' }} />;
    case 'error':
      return <ErrorIcon sx={{ fontSize: 16, color: '#ef4444' }} />;
    case 'waiting_for_input':
      return <PauseCircleIcon sx={{ fontSize: 16, color: '#f59e0b' }} />;
    case 'ready':
    default:
      return (
        <Box
          sx={{
            width: 12,
            height: 12,
            borderRadius: '50%',
            bgcolor: '#6b7280',
          }}
        />
      );
  }
};

export const AgentCard: React.FC<AgentCardProps> = ({ agent, isDragging }) => {
  const { userLastSeen, selectAgent } = useAgentStore();
  const config = getColumnConfig(agent.state);
  const badge = getAgentBadge(agent, userLastSeen);

  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
  } = useSortable({ id: agent.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  const handleClick = () => {
    selectAgent(agent.id);
  };

  // Card background based on state
  const getBgColor = () => {
    switch (agent.state) {
      case 'in_progress':
        return 'rgba(59, 130, 246, 0.08)';
      case 'waiting_for_input':
        return 'rgba(245, 158, 11, 0.12)';
      case 'error':
        return 'rgba(239, 68, 68, 0.12)';
      case 'completed':
        return 'rgba(34, 197, 94, 0.08)';
      default:
        return 'rgba(107, 114, 128, 0.08)';
    }
  };

  // Border style for attention states
  const getBorderStyle = () => {
    if (agent.state === 'waiting_for_input') {
      return '2px solid rgba(245, 158, 11, 0.6)';
    }
    if (agent.state === 'error') {
      return '2px solid rgba(239, 68, 68, 0.5)';
    }
    if (agent.state === 'in_progress') {
      return '1px solid rgba(59, 130, 246, 0.3)';
    }
    return '1px solid rgba(255, 255, 255, 0.1)';
  };

  const hasSubprocesses = Object.keys(agent.subprocesses).length > 0;

  return (
    <Paper
      ref={setNodeRef}
      style={style}
      {...attributes}
      {...listeners}
      onClick={handleClick}
      sx={{
        p: 1.5,
        mb: 1,
        cursor: 'grab',
        bgcolor: getBgColor(),
        border: getBorderStyle(),
        borderRadius: 2,
        minHeight: 120,
        maxHeight: hasSubprocesses ? 200 : 150,
        overflow: 'hidden',
        opacity: isDragging ? 0.5 : 1,
        transition: 'all 0.2s ease',
        '&:hover': {
          transform: 'translateY(-2px)',
          boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
        },
        '&:active': {
          cursor: 'grabbing',
        },
        // Pulsing border for in_progress
        ...(agent.state === 'in_progress' && {
          animation: 'pulse 2s infinite',
          '@keyframes pulse': {
            '0%': { borderColor: 'rgba(59, 130, 246, 0.3)' },
            '50%': { borderColor: 'rgba(59, 130, 246, 0.6)' },
            '100%': { borderColor: 'rgba(59, 130, 246, 0.3)' },
          },
        }),
      }}
    >
      {/* Header: Type icon, name, state indicator */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
        <AgentTypeIcon type={agent.type} size="small" />
        <Box sx={{ flex: 1, overflow: 'hidden' }}>
          <Typography
            variant="subtitle2"
            sx={{
              fontWeight: 600,
              fontSize: '12px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {agent.id}
          </Typography>
          {/* Debug: Show agent type for clarity */}
          <Typography
            variant="caption"
            sx={{ fontSize: '9px', color: 'text.disabled' }}
          >
            {agent.type}
          </Typography>
        </Box>
        <StateIndicator state={agent.state} />
      </Box>

      {/* Badge if applicable */}
      {badge && (
        <Box sx={{ mb: 0.5 }}>
          <AgentCardBadge badge={badge} />
        </Box>
      )}

      {/* Repo & task summary */}
      <Box sx={{ mb: 0.5 }}>
        {agent.repo && (
          <Typography
            variant="caption"
            sx={{ color: 'text.secondary', fontSize: '10px', display: 'block' }}
          >
            {agent.repo}
            {agent.branch && ` / ${agent.branch}`}
          </Typography>
        )}
        {agent.task_summary && (
          <Typography
            variant="caption"
            sx={{
              color: 'text.primary',
              fontSize: '10px',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
            }}
          >
            {agent.task_summary}
          </Typography>
        )}
      </Box>

      {/* Last message / error */}
      {agent.state === 'error' && agent.error_message ? (
        <Typography
          variant="caption"
          sx={{
            color: '#ef4444',
            fontSize: '10px',
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {agent.error_message}
        </Typography>
      ) : agent.last_message ? (
        <Typography
          variant="caption"
          sx={{
            color: 'text.secondary',
            fontSize: '10px',
            fontFamily: 'monospace',
            display: '-webkit-box',
            WebkitLineClamp: 1,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
          }}
        >
          {agent.last_message}
        </Typography>
      ) : null}

      {/* Subprocess tree (compact, inline) */}
      {hasSubprocesses && (
        <SubprocessTree subprocesses={agent.subprocesses} compact maxVisible={3} />
      )}

      {/* Footer: timestamp */}
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'flex-end',
          mt: 'auto',
          pt: 0.5,
        }}
      >
        <Typography variant="caption" sx={{ color: 'text.disabled', fontSize: '9px' }}>
          {agent.state === 'completed' && agent.completed_at
            ? `Finished at ${formatTime(agent.completed_at)}`
            : formatRelativeTime(agent.last_event_at)}
        </Typography>
      </Box>
    </Paper>
  );
};

export default AgentCard;
