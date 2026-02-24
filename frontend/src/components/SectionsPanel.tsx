import React, { useState } from 'react';
import {
  Box,
  Typography,
  IconButton,
  TextField,
  Button,
  Tooltip,
} from '@mui/material';
import {
  DndContext,
  DragEndEvent,
  DragOverlay,
  DragStartEvent,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import AddIcon from '@mui/icons-material/Add';
import { useAgentStore } from '../hooks/useAgentStore';
import { Agent, DEFAULT_SECTION_ID } from '../types/agent';
import SectionColumn from './SectionColumn';
import AgentCard from './AgentCard';
import io from 'socket.io-client';

interface SectionsPanelProps {
  socket: ReturnType<typeof io>;
}

export const SectionsPanel: React.FC<SectionsPanelProps> = ({ socket }) => {
  const {
    agents,
    filters,
    sections,
    agentSectionMap,
    addSection,
    setAgentSection,
  } = useAgentStore();

  const [activeId, setActiveId] = useState<string | null>(null);
  const [addingSection, setAddingSection] = useState(false);
  const [newSectionName, setNewSectionName] = useState('');

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } })
  );

  // Get ready/idle agents with filters applied
  const readyAgents: Agent[] = React.useMemo(() => {
    let filtered = Object.values(agents).filter((a) => a.state === 'ready');

    if (filters.agentType.length > 0) {
      filtered = filtered.filter((a) => filters.agentType.includes(a.type));
    }
    if (filters.repo.length > 0) {
      filtered = filtered.filter((a) => a.repo && filters.repo.includes(a.repo));
    }
    if (filters.showOnlyNeedsAttention) {
      filtered = filtered.filter((a) => a.needs_attention);
    }

    return filtered;
  }, [agents, filters]);

  // Group agents by section
  const agentsBySection = React.useMemo(() => {
    const result: Record<string, Agent[]> = {};
    sections.forEach((s) => {
      result[s.id] = [];
    });

    readyAgents.forEach((agent) => {
      const sectionId = agentSectionMap[agent.id] || DEFAULT_SECTION_ID;
      // If the section no longer exists, fall back to default
      if (result[sectionId]) {
        result[sectionId].push(agent);
      } else {
        result[DEFAULT_SECTION_ID].push(agent);
      }
    });

    // Sort within each section: needs_attention first, then most recent
    Object.keys(result).forEach((sectionId) => {
      result[sectionId].sort((a, b) => {
        if (a.needs_attention !== b.needs_attention) return a.needs_attention ? -1 : 1;
        return b.last_event_at - a.last_event_at;
      });
    });

    return result;
  }, [readyAgents, sections, agentSectionMap]);

  // Find which section an agent belongs to (by agent id used as droppable/sortable id)
  const findSectionForAgent = (agentId: string): string | null => {
    for (const [sectionId, sectionAgents] of Object.entries(agentsBySection)) {
      if (sectionAgents.some((a) => a.id === agentId)) {
        return sectionId;
      }
    }
    return null;
  };

  const handleDragStart = (event: DragStartEvent) => {
    setActiveId(event.active.id as string);
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveId(null);

    if (!over) return;

    const agentId = active.id as string;
    const overId = over.id as string;

    // Determine target section: over.id could be a section id OR an agent id
    let targetSectionId: string;
    if (agentsBySection[overId] !== undefined) {
      // Dropped on a section droppable
      targetSectionId = overId;
    } else {
      // Dropped on another agent — find that agent's section
      const sectionId = findSectionForAgent(overId);
      if (!sectionId) return;
      targetSectionId = sectionId;
    }

    setAgentSection(agentId, targetSectionId);
  };

  const handleDragCancel = () => {
    setActiveId(null);
  };

  const handleAddSection = () => {
    const name = newSectionName.trim();
    if (name) {
      addSection(name);
    }
    setNewSectionName('');
    setAddingSection(false);
  };

  const handleAddKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleAddSection();
    if (e.key === 'Escape') {
      setNewSectionName('');
      setAddingSection(false);
    }
  };

  const activeAgent = activeId ? agents[activeId] : null;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
      {/* Panel header */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          px: 2,
          py: 1,
          borderBottom: '1px solid rgba(255,255,255,0.06)',
          flexShrink: 0,
          gap: 1,
        }}
      >
        <Typography variant="subtitle2" sx={{ fontWeight: 600, fontSize: '12px', color: 'text.secondary', flex: 1 }}>
          READY / IDLE — {readyAgents.length} agent{readyAgents.length !== 1 ? 's' : ''}
        </Typography>

        {addingSection ? (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <TextField
              value={newSectionName}
              onChange={(e) => setNewSectionName(e.target.value)}
              onKeyDown={handleAddKeyDown}
              placeholder="Section name"
              size="small"
              autoFocus
              variant="outlined"
              sx={{
                '& .MuiInputBase-input': { fontSize: '12px', py: 0.5 },
                '& .MuiOutlinedInput-root': { height: 28 },
              }}
            />
            <Button
              size="small"
              onClick={handleAddSection}
              disabled={!newSectionName.trim()}
              sx={{ minWidth: 0, px: 1, height: 28, fontSize: '11px' }}
            >
              Add
            </Button>
            <Button
              size="small"
              onClick={() => {
                setNewSectionName('');
                setAddingSection(false);
              }}
              color="inherit"
              sx={{ minWidth: 0, px: 1, height: 28, fontSize: '11px', color: 'text.disabled' }}
            >
              Cancel
            </Button>
          </Box>
        ) : (
          <Tooltip title="Add section">
            <IconButton
              size="small"
              onClick={() => setAddingSection(true)}
              sx={{ p: 0.5, color: 'text.disabled', '&:hover': { color: 'text.secondary' } }}
            >
              <AddIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        )}
      </Box>

      {/* Section columns */}
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={handleDragCancel}
      >
        <Box
          sx={{
            flex: 1,
            display: 'flex',
            gap: 2,
            p: 2,
            overflowX: 'auto',
            overflowY: 'hidden',
            alignItems: 'stretch',
            '&::-webkit-scrollbar': { height: '8px' },
            '&::-webkit-scrollbar-thumb': {
              backgroundColor: 'rgba(255,255,255,0.2)',
              borderRadius: '4px',
            },
          }}
        >
          {sections.map((section) => (
            <SectionColumn
              key={section.id}
              section={section}
              agents={agentsBySection[section.id] || []}
              socket={socket}
            />
          ))}
        </Box>

        <DragOverlay>
          {activeAgent ? <AgentCard agent={activeAgent} isDragging socket={socket} /> : null}
        </DragOverlay>
      </DndContext>
    </Box>
  );
};

export default SectionsPanel;
