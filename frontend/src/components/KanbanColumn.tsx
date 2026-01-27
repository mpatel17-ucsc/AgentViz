import React from 'react';
import { Box, Paper, Typography, Badge } from '@mui/material';
import { useDroppable } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { motion } from 'framer-motion';
import { Agent, ColumnConfig } from '../types/agent';
import AgentCard from './AgentCard';

interface KanbanColumnProps {
  config: ColumnConfig;
  agents: Agent[];
}

export const KanbanColumn: React.FC<KanbanColumnProps> = ({ config, agents }) => {
  const { setNodeRef, isOver } = useDroppable({
    id: config.id,
  });

  // Count agents needing attention
  const needsAttentionCount = agents.filter((a) => a.needs_attention).length;

  return (
    <Paper
      ref={setNodeRef}
      sx={{
        flex: 1,
        minWidth: 280,
        maxWidth: 350,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: config.bgColor,
        border: isOver ? `2px dashed ${config.color}` : '1px solid rgba(255,255,255,0.1)',
        borderRadius: 2,
        overflow: 'hidden',
        transition: 'border 0.2s ease',
      }}
    >
      {/* Column Header */}
      <Box
        sx={{
          p: 1.5,
          borderBottom: '1px solid rgba(255,255,255,0.1)',
          bgcolor: 'rgba(0,0,0,0.2)',
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Box
            sx={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              bgcolor: config.color,
            }}
          />
          <Typography
            variant="subtitle2"
            sx={{
              fontWeight: 700,
              fontSize: '11px',
              letterSpacing: '0.5px',
              color: config.color,
              textTransform: 'uppercase',
            }}
          >
            {config.title}
          </Typography>
          <Badge
            badgeContent={agents.length}
            color="default"
            sx={{
              '& .MuiBadge-badge': {
                bgcolor: 'rgba(255,255,255,0.15)',
                color: 'text.secondary',
                fontSize: '10px',
                height: 18,
                minWidth: 18,
              },
            }}
          />
          {needsAttentionCount > 0 && (
            <Box
              sx={{
                ml: 'auto',
                px: 1,
                py: 0.25,
                borderRadius: 1,
                bgcolor: config.id === 'error' ? 'rgba(239, 68, 68, 0.3)' : 'rgba(245, 158, 11, 0.3)',
                color: config.id === 'error' ? '#ef4444' : '#f59e0b',
              }}
            >
              <Typography variant="caption" sx={{ fontSize: '10px', fontWeight: 600 }}>
                {needsAttentionCount} need attention
              </Typography>
            </Box>
          )}
        </Box>
      </Box>

      {/* Cards Container */}
      <Box
        sx={{
          flex: 1,
          overflowY: 'auto',
          p: 1,
          '&::-webkit-scrollbar': {
            width: '6px',
          },
          '&::-webkit-scrollbar-thumb': {
            backgroundColor: 'rgba(255,255,255,0.2)',
            borderRadius: '3px',
          },
        }}
      >
        <SortableContext
          items={agents.map((a) => a.id)}
          strategy={verticalListSortingStrategy}
        >
          {agents.length === 0 ? (
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: 100,
                border: '2px dashed rgba(255,255,255,0.1)',
                borderRadius: 2,
              }}
            >
              <Typography variant="caption" sx={{ color: 'text.disabled' }}>
                No agents
              </Typography>
            </Box>
          ) : (
            agents.map((agent) => (
              <motion.div
                key={agent.id}
                layout
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.2 }}
              >
                <AgentCard agent={agent} />
              </motion.div>
            ))
          )}
        </SortableContext>
      </Box>
    </Paper>
  );
};

export default KanbanColumn;
