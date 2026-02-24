import React, { useState, useEffect } from 'react';
import {
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  TextField,
  ToggleButtonGroup,
  ToggleButton,
  Box,
  Typography,
  Collapse,
  Alert,
  CircularProgress,
} from '@mui/material';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import CodeIcon from '@mui/icons-material/Code';
import TerminalIcon from '@mui/icons-material/Terminal';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import ExpandLessIcon from '@mui/icons-material/ExpandLess';
import io from 'socket.io-client';

interface LaunchAgentDialogProps {
  open: boolean;
  onClose: () => void;
  socket: ReturnType<typeof io>;
  prefillWorkspace?: string;
  prefillType?: string;
}

type AgentType = 'codex' | 'gemini-cli' | 'claude-code' | 'terminal';

const AGENT_TYPE_OPTIONS: Array<{ value: AgentType; label: string; icon: React.ReactNode; color: string }> = [
  { value: 'claude-code', label: 'Claude Code', icon: <AutoAwesomeIcon fontSize="small" />, color: '#d97706' },
  { value: 'gemini-cli', label: 'Gemini CLI', icon: <SmartToyIcon fontSize="small" />, color: '#4285f4' },
  { value: 'codex', label: 'Codex', icon: <CodeIcon fontSize="small" />, color: '#10a37f' },
  { value: 'terminal', label: 'Empty Terminal', icon: <TerminalIcon fontSize="small" />, color: '#6b7280' },
];

export const LaunchAgentDialog: React.FC<LaunchAgentDialogProps> = ({
  open,
  onClose,
  socket,
  prefillWorkspace = '',
  prefillType = '',
}) => {
  const [agentType, setAgentType] = useState<AgentType>('claude-code');
  const [workspace, setWorkspace] = useState('');
  const [command, setCommand] = useState('');
  const [showCommand, setShowCommand] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Pre-fill when dialog opens (for fork)
  useEffect(() => {
    if (open) {
      setWorkspace(prefillWorkspace || '');
      if (prefillType && AGENT_TYPE_OPTIONS.some((o) => o.value === prefillType)) {
        setAgentType(prefillType as AgentType);
      } else {
        setAgentType('claude-code');
      }
      setCommand('');
      setShowCommand(false);
      setError(null);
      setLoading(false);
    }
  }, [open, prefillWorkspace, prefillType]);

  const handleLaunch = () => {
    if (!workspace.trim()) {
      setError('Workspace path is required');
      return;
    }

    setLoading(true);
    setError(null);

    const onResult = (result: { success: boolean; error?: string; agent_id?: string }) => {
      setLoading(false);
      if (result.success) {
        onClose();
      } else {
        setError(result.error || 'Launch failed');
      }
    };

    // One-time listener for the result
    socket.once('launch_result', onResult);

    if (agentType === 'terminal') {
      socket.emit('launch_terminal', { workspace: workspace.trim() });
    } else {
      socket.emit('launch_agent', {
        agent_type: agentType,
        workspace: workspace.trim(),
        command: command.trim() || undefined,
      });
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !loading) {
      handleLaunch();
    }
  };

  return (
    <Dialog
      open={open}
      onClose={loading ? undefined : onClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{
        sx: { bgcolor: '#111111', border: '1px solid rgba(255,255,255,0.1)' },
      }}
    >
      <DialogTitle sx={{ pb: 1 }}>Launch Agent</DialogTitle>
      <DialogContent>
        {/* Agent type selector */}
        <Typography variant="caption" sx={{ color: 'text.secondary', mb: 1, display: 'block' }}>
          Agent Type
        </Typography>
        <ToggleButtonGroup
          value={agentType}
          exclusive
          onChange={(_, val) => val && setAgentType(val)}
          size="small"
          sx={{ mb: 2, flexWrap: 'wrap', gap: 0.5 }}
        >
          {AGENT_TYPE_OPTIONS.map((opt) => (
            <ToggleButton
              key={opt.value}
              value={opt.value}
              sx={{
                px: 1.5,
                py: 0.75,
                border: '1px solid rgba(255,255,255,0.15) !important',
                borderRadius: '6px !important',
                gap: 0.5,
                color: agentType === opt.value ? opt.color : 'text.secondary',
                bgcolor: agentType === opt.value ? `${opt.color}18` : 'transparent',
                '&.Mui-selected': {
                  color: opt.color,
                  bgcolor: `${opt.color}18`,
                  borderColor: `${opt.color}80 !important`,
                },
              }}
            >
              {opt.icon}
              <Typography variant="caption" sx={{ fontWeight: 600 }}>
                {opt.label}
              </Typography>
            </ToggleButton>
          ))}
        </ToggleButtonGroup>

        {/* Workspace path */}
        <TextField
          label="Workspace Path"
          placeholder="/path/to/your/repo"
          value={workspace}
          onChange={(e) => setWorkspace(e.target.value)}
          onKeyDown={handleKeyDown}
          fullWidth
          size="small"
          required
          sx={{ mb: 1 }}
          autoFocus={!prefillWorkspace}
        />

        {/* Optional command (only for coding agents) */}
        {agentType !== 'terminal' && (
          <>
            <Button
              size="small"
              onClick={() => setShowCommand((v) => !v)}
              startIcon={showCommand ? <ExpandLessIcon /> : <ExpandMoreIcon />}
              sx={{ mb: 1, color: 'text.secondary', textTransform: 'none', px: 0 }}
            >
              {showCommand ? 'Hide' : 'Override'} command
            </Button>
            <Collapse in={showCommand}>
              <TextField
                label="Command override"
                placeholder={agentType === 'claude-code' ? 'claude' : agentType === 'gemini-cli' ? 'gemini' : 'codex'}
                value={command}
                onChange={(e) => setCommand(e.target.value)}
                onKeyDown={handleKeyDown}
                fullWidth
                size="small"
                sx={{ mb: 1 }}
                helperText="Leave blank to use the default command for this agent type"
              />
            </Collapse>
          </>
        )}

        {/* Error */}
        {error && (
          <Alert severity="error" sx={{ mt: 1 }}>
            {error}
          </Alert>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={onClose} disabled={loading} color="inherit">
          Cancel
        </Button>
        <Button
          onClick={handleLaunch}
          disabled={loading || !workspace.trim()}
          variant="contained"
          startIcon={loading ? <CircularProgress size={16} /> : null}
        >
          {loading ? 'Launching…' : 'Launch'}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default LaunchAgentDialog;
