import React from 'react';
import {
  Box,
  Chip,
  FormControlLabel,
  Switch,
  Typography,
  Select,
  MenuItem,
  FormControl,
  InputLabel,
  OutlinedInput,
  SelectChangeEvent,
} from '@mui/material';
import FilterListIcon from '@mui/icons-material/FilterList';
import { useAgentStore } from '../hooks/useAgentStore';

export const FilterBar: React.FC = () => {
  const { agents, filters, setFilters } = useAgentStore();

  // Get unique agent types and repos
  const agentTypes = React.useMemo(() => {
    const types = new Set<string>();
    Object.values(agents).forEach((a) => types.add(a.type));
    return Array.from(types);
  }, [agents]);

  const repos = React.useMemo(() => {
    const repoSet = new Set<string>();
    Object.values(agents).forEach((a) => {
      if (a.repo) repoSet.add(a.repo);
    });
    return Array.from(repoSet);
  }, [agents]);

  // Count agents needing attention
  const needsAttentionCount = React.useMemo(() => {
    return Object.values(agents).filter((a) => a.needs_attention).length;
  }, [agents]);

  const handleTypeChange = (event: SelectChangeEvent<string[]>) => {
    const value = event.target.value;
    setFilters({ agentType: typeof value === 'string' ? value.split(',') : value });
  };

  const handleRepoChange = (event: SelectChangeEvent<string[]>) => {
    const value = event.target.value;
    setFilters({ repo: typeof value === 'string' ? value.split(',') : value });
  };

  const handleAttentionToggle = () => {
    setFilters({ showOnlyNeedsAttention: !filters.showOnlyNeedsAttention });
  };

  const handleHideCompletedToggle = () => {
    setFilters({ hideCompleted: !filters.hideCompleted });
  };

  const clearFilters = () => {
    setFilters({
      agentType: [],
      repo: [],
      showOnlyNeedsAttention: false,
      hideCompleted: false,
    });
  };

  const hasActiveFilters =
    filters.agentType.length > 0 ||
    filters.repo.length > 0 ||
    filters.showOnlyNeedsAttention ||
    filters.hideCompleted;

  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 2,
        p: 1.5,
        borderBottom: '1px solid rgba(255,255,255,0.1)',
        bgcolor: 'rgba(0,0,0,0.3)',
        flexWrap: 'wrap',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <FilterListIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
        <Typography variant="body2" sx={{ color: 'text.secondary', fontWeight: 500 }}>
          Filters
        </Typography>
      </Box>

      {/* Agent Type Filter */}
      <FormControl size="small" sx={{ minWidth: 140 }}>
        <InputLabel sx={{ fontSize: '12px' }}>Agent Type</InputLabel>
        <Select
          multiple
          value={filters.agentType}
          onChange={handleTypeChange}
          input={<OutlinedInput label="Agent Type" />}
          renderValue={(selected) => selected.join(', ')}
          sx={{ fontSize: '12px', height: 36 }}
        >
          {agentTypes.map((type) => (
            <MenuItem key={type} value={type} sx={{ fontSize: '12px' }}>
              {type}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {/* Repo Filter */}
      {repos.length > 0 && (
        <FormControl size="small" sx={{ minWidth: 140 }}>
          <InputLabel sx={{ fontSize: '12px' }}>Repository</InputLabel>
          <Select
            multiple
            value={filters.repo}
            onChange={handleRepoChange}
            input={<OutlinedInput label="Repository" />}
            renderValue={(selected) => selected.join(', ')}
            sx={{ fontSize: '12px', height: 36 }}
          >
            {repos.map((repo) => (
              <MenuItem key={repo} value={repo} sx={{ fontSize: '12px' }}>
                {repo}
              </MenuItem>
            ))}
          </Select>
        </FormControl>
      )}

      {/* Needs Attention Toggle */}
      <FormControlLabel
        control={
          <Switch
            size="small"
            checked={filters.showOnlyNeedsAttention}
            onChange={handleAttentionToggle}
            color="warning"
          />
        }
        label={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <Typography variant="body2" sx={{ fontSize: '12px' }}>
              Needs Attention
            </Typography>
            {needsAttentionCount > 0 && (
              <Chip
                label={needsAttentionCount}
                size="small"
                color="warning"
                sx={{ height: 18, fontSize: '10px' }}
              />
            )}
          </Box>
        }
        sx={{ ml: 1 }}
      />

      {/* Hide Completed Toggle */}
      <FormControlLabel
        control={
          <Switch
            size="small"
            checked={filters.hideCompleted}
            onChange={handleHideCompletedToggle}
          />
        }
        label={
          <Typography variant="body2" sx={{ fontSize: '12px' }}>
            Hide Completed
          </Typography>
        }
        sx={{ ml: 1 }}
      />

      {/* Clear Filters */}
      {hasActiveFilters && (
        <Chip
          label="Clear Filters"
          size="small"
          onClick={clearFilters}
          onDelete={clearFilters}
          sx={{ ml: 'auto' }}
        />
      )}
    </Box>
  );
};

export default FilterBar;
