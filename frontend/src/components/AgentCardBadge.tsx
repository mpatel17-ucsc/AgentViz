import React from 'react';
import { Chip } from '@mui/material';
import WarningIcon from '@mui/icons-material/Warning';
import ErrorIcon from '@mui/icons-material/Error';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import HourglassEmptyIcon from '@mui/icons-material/HourglassEmpty';
import { BadgeType } from '../types/agent';

interface AgentCardBadgeProps {
  badge: BadgeType;
}

const BADGE_CONFIGS: Record<NonNullable<BadgeType>, {
  label: string;
  icon: React.ReactNode;
  color: 'warning' | 'error' | 'success' | 'info';
}> = {
  completed_away: {
    label: 'Completed while away',
    icon: <CheckCircleIcon sx={{ fontSize: 14 }} />,
    color: 'success',
  },
  error_away: {
    label: 'Errored while away',
    icon: <ErrorIcon sx={{ fontSize: 14 }} />,
    color: 'error',
  },
  needs_input: {
    label: 'Needs your input',
    icon: <WarningIcon sx={{ fontSize: 14 }} />,
    color: 'warning',
  },
  stalled: {
    label: 'Stalled',
    icon: <HourglassEmptyIcon sx={{ fontSize: 14 }} />,
    color: 'info',
  },
};

export const AgentCardBadge: React.FC<AgentCardBadgeProps> = ({ badge }) => {
  if (!badge) return null;

  const config = BADGE_CONFIGS[badge];
  if (!config) return null;

  return (
    <Chip
      icon={config.icon as React.ReactElement}
      label={config.label}
      color={config.color}
      size="small"
      sx={{
        height: 20,
        fontSize: '10px',
        fontWeight: 600,
        '& .MuiChip-icon': {
          marginLeft: '4px',
        },
        '& .MuiChip-label': {
          paddingLeft: '4px',
          paddingRight: '8px',
        },
      }}
    />
  );
};

export default AgentCardBadge;
