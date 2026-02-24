import React from 'react';
import { Box, Tooltip } from '@mui/material';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import CodeIcon from '@mui/icons-material/Code';
import TerminalIcon from '@mui/icons-material/Terminal';

interface AgentTypeIconProps {
  type: string;
  size?: 'small' | 'medium' | 'large';
}

const ICON_SIZES = {
  small: 16,
  medium: 20,
  large: 24,
};

const AGENT_CONFIGS: Record<string, { icon: React.ReactNode; color: string; label: string }> = {
  'claude-code': {
    icon: <AutoAwesomeIcon />,
    color: '#d97706', // Amber/orange for Claude
    label: 'Claude Code',
  },
  'gemini-cli': {
    icon: <SmartToyIcon />,
    color: '#4285f4', // Google blue
    label: 'Gemini CLI',
  },
  'codex': {
    icon: <CodeIcon />,
    color: '#10a37f', // OpenAI green
    label: 'Codex',
  },
  'terminal': {
    icon: <TerminalIcon />,
    color: '#6b7280',
    label: 'Terminal',
  },
};

export const AgentTypeIcon: React.FC<AgentTypeIconProps> = ({ type, size = 'medium' }) => {
  const config = AGENT_CONFIGS[type] || {
    icon: <SmartToyIcon />,
    color: '#6b7280',
    label: type,
  };

  const iconSize = ICON_SIZES[size];

  return (
    <Tooltip title={config.label} arrow>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: config.color,
          '& svg': {
            fontSize: iconSize,
          },
        }}
      >
        {config.icon}
      </Box>
    </Tooltip>
  );
};

export default AgentTypeIcon;
