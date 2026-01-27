import React, { useState } from 'react';
import { Box, Typography, Collapse, IconButton, Tooltip } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorIcon from '@mui/icons-material/Error';
import { Subprocess } from '../types/agent';
import { buildSubprocessTree, SubprocessNode, truncateCommand } from '../utils/sorting';

interface SubprocessTreeProps {
  subprocesses: Record<number, Subprocess>;
  compact?: boolean;
  maxVisible?: number;
}

const StateIcon: React.FC<{ state: string }> = ({ state }) => {
  switch (state) {
    case 'running':
      return <PlayArrowIcon sx={{ fontSize: 12, color: '#3b82f6' }} />;
    case 'completed':
      return <CheckCircleIcon sx={{ fontSize: 12, color: '#22c55e' }} />;
    case 'error':
      return <ErrorIcon sx={{ fontSize: 12, color: '#ef4444' }} />;
    default:
      return null;
  }
};

interface TreeNodeProps {
  node: SubprocessNode;
  depth: number;
  compact: boolean;
}

const TreeNode: React.FC<TreeNodeProps> = ({ node, depth, compact }) => {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasChildren = node.children.length > 0;

  const command = compact ? truncateCommand(node.command, 30) : truncateCommand(node.command, 50);

  return (
    <Box sx={{ ml: depth * 1.5 }}>
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.5,
          py: 0.25,
          '&:hover': {
            bgcolor: 'rgba(255,255,255,0.05)',
          },
        }}
      >
        {hasChildren ? (
          <IconButton
            size="small"
            onClick={() => setExpanded(!expanded)}
            sx={{ p: 0, width: 16, height: 16 }}
          >
            {expanded ? (
              <ExpandLessIcon sx={{ fontSize: 12 }} />
            ) : (
              <ExpandMoreIcon sx={{ fontSize: 12 }} />
            )}
          </IconButton>
        ) : (
          <Box sx={{ width: 16, display: 'flex', justifyContent: 'center' }}>
            <Box
              sx={{
                width: 4,
                height: 4,
                borderRadius: '50%',
                bgcolor: 'text.secondary',
              }}
            />
          </Box>
        )}

        <StateIcon state={node.state} />

        <Tooltip title={node.command} arrow>
          <Typography
            variant="caption"
            sx={{
              fontFamily: 'monospace',
              fontSize: '10px',
              color: node.state === 'running' ? 'primary.main' : 'text.secondary',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {command}
          </Typography>
        </Tooltip>

        {!compact && (
          <Typography
            variant="caption"
            sx={{ fontSize: '9px', color: 'text.disabled', ml: 'auto' }}
          >
            pid:{node.pid}
          </Typography>
        )}
      </Box>

      {hasChildren && (
        <Collapse in={expanded}>
          {node.children.map((child) => (
            <TreeNode key={child.pid} node={child} depth={depth + 1} compact={compact} />
          ))}
        </Collapse>
      )}
    </Box>
  );
};

export const SubprocessTree: React.FC<SubprocessTreeProps> = ({
  subprocesses,
  compact = false,
  maxVisible = 5,
}) => {
  const [showAll, setShowAll] = useState(false);

  const tree = buildSubprocessTree(subprocesses);

  if (tree.length === 0) {
    return null;
  }

  // Count running subprocesses
  const runningCount = Object.values(subprocesses).filter((s) => s.state === 'running').length;
  const totalCount = Object.keys(subprocesses).length;

  // For compact view, show limited items
  const visibleNodes = showAll ? tree : tree.slice(0, maxVisible);
  const hasMore = tree.length > maxVisible;

  return (
    <Box
      sx={{
        mt: 1,
        pt: 1,
        borderTop: '1px solid rgba(255,255,255,0.1)',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
        <Typography
          variant="caption"
          sx={{ fontSize: '10px', color: 'text.secondary', fontWeight: 600 }}
        >
          Subprocesses
        </Typography>
        {runningCount > 0 && (
          <Typography
            variant="caption"
            sx={{
              fontSize: '9px',
              color: '#3b82f6',
              bgcolor: 'rgba(59, 130, 246, 0.2)',
              px: 0.5,
              py: 0.125,
              borderRadius: 0.5,
            }}
          >
            {runningCount} running
          </Typography>
        )}
        <Typography variant="caption" sx={{ fontSize: '9px', color: 'text.disabled' }}>
          ({totalCount} total)
        </Typography>
      </Box>

      {visibleNodes.map((node) => (
        <TreeNode key={node.pid} node={node} depth={0} compact={compact} />
      ))}

      {hasMore && !showAll && (
        <Typography
          variant="caption"
          sx={{
            fontSize: '10px',
            color: 'primary.main',
            cursor: 'pointer',
            '&:hover': { textDecoration: 'underline' },
          }}
          onClick={() => setShowAll(true)}
        >
          +{tree.length - maxVisible} more...
        </Typography>
      )}
    </Box>
  );
};

export default SubprocessTree;
