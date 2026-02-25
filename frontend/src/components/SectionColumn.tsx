import React, { useState } from 'react';
import { Box, Paper, Typography, IconButton, TextField, Tooltip, Badge } from '@mui/material';
import { useDroppable } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { motion } from 'framer-motion';
import EditIcon from '@mui/icons-material/Edit';
import DeleteIcon from '@mui/icons-material/Delete';
import CheckIcon from '@mui/icons-material/Check';
import CloseIcon from '@mui/icons-material/Close';
import { Section, Agent, DEFAULT_SECTION_ID } from '../types/agent';
import { useAgentStore } from '../hooks/useAgentStore';
import AgentCard from './AgentCard';
import io from 'socket.io-client';

interface SectionColumnProps {
  section: Section;
  agents: Agent[];
  socket: ReturnType<typeof io>;
}

export const SectionColumn: React.FC<SectionColumnProps> = ({ section, agents, socket }) => {
  const { renameSection, removeSection } = useAgentStore();
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(section.name);

  const { setNodeRef, isOver } = useDroppable({ id: section.id });

  const isDefault = section.id === DEFAULT_SECTION_ID;
  const canDelete = !isDefault && agents.length === 0;

  const handleEditSave = () => {
    const trimmed = editName.trim();
    if (trimmed && trimmed !== section.name) {
      renameSection(section.id, trimmed);
      const { sections, agentSectionMap } = useAgentStore.getState();
      socket.emit('update_sections', { sections, agentSectionMap });
    } else {
      setEditName(section.name);
    }
    setEditing(false);
  };

  const handleRemoveSection = () => {
    removeSection(section.id);
    const { sections, agentSectionMap } = useAgentStore.getState();
    socket.emit('update_sections', { sections, agentSectionMap });
  };

  const handleEditKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleEditSave();
    if (e.key === 'Escape') {
      setEditName(section.name);
      setEditing(false);
    }
  };

  return (
    <Paper
      sx={{
        flex: 1,
        minWidth: 280,
        maxWidth: 350,
        height: '100%',
        display: 'flex',
        flexDirection: 'column',
        bgcolor: `${section.color}14`,
        border: isOver ? `2px dashed ${section.color}` : '1px solid rgba(255,255,255,0.1)',
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
              bgcolor: section.color,
              flexShrink: 0,
            }}
          />

          {editing ? (
            <>
              <TextField
                value={editName}
                onChange={(e) => setEditName(e.target.value)}
                onBlur={handleEditSave}
                onKeyDown={handleEditKeyDown}
                size="small"
                autoFocus
                variant="standard"
                sx={{
                  flex: 1,
                  '& .MuiInputBase-input': {
                    fontSize: '11px',
                    fontWeight: 700,
                    letterSpacing: '0.5px',
                    color: section.color,
                    py: 0,
                  },
                  '& .MuiInput-underline:before': { borderColor: section.color },
                }}
              />
              <IconButton size="small" onClick={handleEditSave} sx={{ p: 0.25, color: '#22c55e' }}>
                <CheckIcon sx={{ fontSize: 12 }} />
              </IconButton>
              <IconButton
                size="small"
                onClick={() => { setEditName(section.name); setEditing(false); }}
                sx={{ p: 0.25, color: 'text.disabled' }}
              >
                <CloseIcon sx={{ fontSize: 12 }} />
              </IconButton>
            </>
          ) : (
            <>
              <Typography
                variant="subtitle2"
                sx={{
                  fontWeight: 700,
                  fontSize: '11px',
                  letterSpacing: '0.5px',
                  color: section.color,
                  textTransform: 'uppercase',
                  flex: 1,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {section.name}
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
              {!isDefault && (
                <>
                  <Tooltip title="Rename section">
                    <IconButton
                      size="small"
                      onClick={() => { setEditName(section.name); setEditing(true); }}
                      sx={{ p: 0.25, color: 'text.disabled', '&:hover': { color: 'text.secondary' } }}
                    >
                      <EditIcon sx={{ fontSize: 12 }} />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title={agents.length > 0 ? 'Move agents out first' : 'Delete section'}>
                    <span>
                      <IconButton
                        size="small"
                        disabled={!canDelete}
                        onClick={handleRemoveSection}
                        sx={{
                          p: 0.25,
                          color: 'text.disabled',
                          '&:hover': { color: '#ef4444' },
                          '&.Mui-disabled': { color: 'rgba(255,255,255,0.1)' },
                        }}
                      >
                        <DeleteIcon sx={{ fontSize: 12 }} />
                      </IconButton>
                    </span>
                  </Tooltip>
                </>
              )}
            </>
          )}
        </Box>
      </Box>

      {/* Cards Container */}
      <Box
        ref={setNodeRef}
        sx={{
          flex: 1,
          overflowY: 'auto',
          p: 1,
          '&::-webkit-scrollbar': { width: '6px' },
          '&::-webkit-scrollbar-thumb': {
            backgroundColor: 'rgba(255,255,255,0.2)',
            borderRadius: '3px',
          },
        }}
      >
        <SortableContext items={agents.map((a) => a.id)} strategy={verticalListSortingStrategy}>
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
                <AgentCard agent={agent} socket={socket} />
              </motion.div>
            ))
          )}
        </SortableContext>
      </Box>
    </Paper>
  );
};

export default SectionColumn;
