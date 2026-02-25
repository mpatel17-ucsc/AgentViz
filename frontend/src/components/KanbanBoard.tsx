import React from 'react';
import { Box } from '@mui/material';
import {
  DndContext,
  DragEndEvent,
  DragOverlay,
  DragStartEvent,
  closestCorners,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import { COLUMNS, Agent, AgentState } from '../types/agent';
import { useAgentStore } from '../hooks/useAgentStore';
import KanbanColumn from './KanbanColumn';
import AgentCard from './AgentCard';
import io from 'socket.io-client';

interface KanbanBoardProps {
  socket: ReturnType<typeof io>;
  hideReady?: boolean;
}

export const KanbanBoard: React.FC<KanbanBoardProps> = ({ socket, hideReady }) => {
  const { agents, getAgentsByState, filters } = useAgentStore();
  const [activeId, setActiveId] = React.useState<string | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8,
      },
    })
  );

  // Get agents for each column with filtering
  const columnAgents = React.useMemo(() => {
    const result: Record<AgentState, Agent[]> = {
      ready: [],
      in_progress: [],
      waiting_for_input: [],
      error: [],
      completed: [],
    };

    Object.values(agents).forEach((agent) => {
      // Apply filters
      if (filters.agentType.length > 0 && !filters.agentType.includes(agent.type)) {
        return;
      }
      if (filters.repo.length > 0 && agent.repo && !filters.repo.includes(agent.repo)) {
        return;
      }
      if (filters.showOnlyNeedsAttention && !agent.needs_attention) {
        return;
      }
      if (filters.hideCompleted && agent.state === 'completed') {
        return;
      }

      result[agent.state].push(agent);
    });

    // Sort each column: needs_attention first, then by last_event_at
    Object.keys(result).forEach((state) => {
      result[state as AgentState].sort((a, b) => {
        if (a.needs_attention !== b.needs_attention) {
          return a.needs_attention ? -1 : 1;
        }
        return b.last_event_at - a.last_event_at;
      });
    });

    return result;
  }, [agents, filters]);

  const handleDragStart = (event: DragStartEvent) => {
    setActiveId(event.active.id as string);
  };

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    setActiveId(null);

    if (!over) return;

    const agentId = active.id as string;
    const targetColumn = over.id as AgentState;
    const agent = agents[agentId];

    if (!agent || agent.state === targetColumn) return;

    // Valid drag-and-drop transitions
    // ERROR -> READY: Retry
    if (agent.state === 'error' && targetColumn === 'ready') {
      console.log(`[DnD] Retry: ${agentId} from ERROR to READY`);
      socket.emit('control_retry', { agent_id: agentId });
    }
    // READY -> IN_PROGRESS: Start task
    else if (agent.state === 'ready' && targetColumn === 'in_progress') {
      console.log(`[DnD] Start task: ${agentId} from READY to IN_PROGRESS`);
      socket.emit('control_start_task', { agent_id: agentId });
    }
    // Invalid transitions are ignored
    else {
      console.log(`[DnD] Invalid transition: ${agent.state} -> ${targetColumn}`);
    }
  };

  const handleDragCancel = () => {
    setActiveId(null);
  };

  const visibleColumns = hideReady ? COLUMNS.filter((c) => c.id !== 'ready') : COLUMNS;

  const activeAgent = activeId ? agents[activeId] : null;
  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCorners}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
      onDragCancel={handleDragCancel}
    >
      <Box
        sx={{
          display: 'flex',
          gap: 2,
          pl: 2,
          pt: 2,
          pb: 2,
          height: '100%',
          overflowX: 'auto',
          '&::-webkit-scrollbar': {
            height: '8px',
          },
          '&::-webkit-scrollbar-thumb': {
            backgroundColor: 'rgba(255,255,255,0.2)',
            borderRadius: '4px',
          },
        }}
      >
        {visibleColumns.map((column) => (
          <KanbanColumn
            key={column.id}
            config={column}
            agents={columnAgents[column.id]}
            socket={socket}
          />
        ))}
        {/* Spacer so the last column's right edge isn't clipped by the scroll container on mobile */}
        <Box sx={{ flexShrink: 0, width: 8 }} />
      </Box>

      <DragOverlay>
        {activeAgent ? <AgentCard agent={activeAgent} isDragging socket={socket} /> : null}
      </DragOverlay>
    </DndContext>
  );
};

export default KanbanBoard;
