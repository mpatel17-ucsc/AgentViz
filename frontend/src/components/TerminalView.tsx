import React, { useEffect, useRef, useState, useCallback } from 'react';
import { Box, Typography, Chip } from '@mui/material';
import { Terminal } from 'xterm';
import { FitAddon } from '@xterm/addon-fit';
import 'xterm/css/xterm.css';
import io from 'socket.io-client';

interface TerminalViewProps {
  socket: ReturnType<typeof io>;
  agentId: string;
  agentState: string;
}

/**
 * Live terminal viewer component using xterm.js.
 *
 * Features:
 * - Displays live terminal output from the agent's PTY
 * - Shows terminal history on initial load
 * - Handles idle notifications
 * - Debounces writes for smooth rendering
 * - Preserves ANSI escape codes for colors/formatting
 */
const TerminalView: React.FC<TerminalViewProps> = ({ socket, agentId, agentState }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);
  const fitAddonRef = useRef<FitAddon | null>(null);
  const isInitializedRef = useRef<boolean>(false);

  // Debounce buffer for smooth rendering
  const writeBufferRef = useRef<string>('');
  const writeTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const WRITE_DEBOUNCE_MS = 200;

  // Idle state
  const [isIdle, setIsIdle] = useState(false);
  const [idleSeconds, setIdleSeconds] = useState(0);
  const idleTimerRef = useRef<NodeJS.Timeout | null>(null);

  // Frontend idle detection (backup)
  const lastOutputRef = useRef<number>(Date.now());
  const FRONTEND_IDLE_THRESHOLD_MS = 10000;

  // Check if agent is in a "running" state
  const isAgentRunning = ['in_progress', 'waiting_for_input'].includes(agentState);

  /**
   * Safely fit terminal to container.
   */
  const safeFit = useCallback(() => {
    if (!fitAddonRef.current || !xtermRef.current || !isInitializedRef.current) {
      return;
    }

    try {
      // Check if container has dimensions
      const container = terminalRef.current;
      if (!container || container.clientWidth === 0 || container.clientHeight === 0) {
        return;
      }

      fitAddonRef.current.fit();
    } catch (error) {
      // Ignore fit errors during initialization
      console.debug('Terminal fit error (safe to ignore during init):', error);
    }
  }, []);

  /**
   * Write text to terminal with debouncing for smooth rendering.
   */
  const writeToTerminal = useCallback((text: string) => {
    if (!xtermRef.current || !isInitializedRef.current) return;

    // Reset idle state on new output
    setIsIdle(false);
    lastOutputRef.current = Date.now();

    // Add to buffer
    writeBufferRef.current += text;

    // Clear existing timeout
    if (writeTimeoutRef.current) {
      clearTimeout(writeTimeoutRef.current);
    }

    // Schedule flush
    writeTimeoutRef.current = setTimeout(() => {
      if (xtermRef.current && writeBufferRef.current && isInitializedRef.current) {
        try {
          xtermRef.current.write(writeBufferRef.current);
        } catch (error) {
          console.debug('Terminal write error:', error);
        }
        writeBufferRef.current = '';
      }
    }, WRITE_DEBOUNCE_MS);
  }, []);

  /**
   * Initialize xterm.js terminal.
   */
  useEffect(() => {
    if (!terminalRef.current || !isAgentRunning) return;

    // Reset initialization flag
    isInitializedRef.current = false;

    // Create terminal instance
    const terminal = new Terminal({
      theme: {
        background: '#1a1a2e',
        foreground: '#e0e0e0',
        cursor: '#6366f1',
        cursorAccent: '#1a1a2e',
        selectionBackground: 'rgba(99, 102, 241, 0.3)',
        black: '#21222c',
        red: '#ff5555',
        green: '#50fa7b',
        yellow: '#f1fa8c',
        blue: '#bd93f9',
        magenta: '#ff79c6',
        cyan: '#8be9fd',
        white: '#f8f8f2',
        brightBlack: '#6272a4',
        brightRed: '#ff6e6e',
        brightGreen: '#69ff94',
        brightYellow: '#ffffa5',
        brightBlue: '#d6acff',
        brightMagenta: '#ff92df',
        brightCyan: '#a4ffff',
        brightWhite: '#ffffff',
      },
      fontSize: 12,
      fontFamily: '"JetBrains Mono", "Fira Code", "SF Mono", Monaco, "Cascadia Code", monospace',
      cursorBlink: false,
      scrollback: 5000,
      convertEol: true,
      allowProposedApi: true,
    });

    // Create fit addon
    const fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);

    // Store refs before opening
    xtermRef.current = terminal;
    fitAddonRef.current = fitAddon;

    // Open terminal in container
    terminal.open(terminalRef.current);

    // Mark as initialized after a brief delay to ensure DOM is ready
    const initTimeout = setTimeout(() => {
      isInitializedRef.current = true;
      safeFit();

      // Request terminal history after initialization
      socket.emit('request_terminal_history', { agent_id: agentId });
    }, 100);

    // Handle container resize with ResizeObserver
    let resizeObserver: ResizeObserver | null = null;
    if (terminalRef.current) {
      resizeObserver = new ResizeObserver(() => {
        // Debounce resize
        setTimeout(safeFit, 50);
      });
      resizeObserver.observe(terminalRef.current);
    }

    // Cleanup
    return () => {
      clearTimeout(initTimeout);
      isInitializedRef.current = false;

      if (writeTimeoutRef.current) {
        clearTimeout(writeTimeoutRef.current);
      }

      if (resizeObserver) {
        resizeObserver.disconnect();
      }

      try {
        terminal.dispose();
      } catch (error) {
        console.debug('Terminal dispose error:', error);
      }

      xtermRef.current = null;
      fitAddonRef.current = null;
    };
  }, [agentId, isAgentRunning, socket, safeFit]);

  /**
   * Handle socket events for terminal data.
   */
  useEffect(() => {
    if (!isAgentRunning) return;

    // Handle terminal history response
    const handleHistory = (data: { agent_id: string; history: string[] }) => {
      if (data.agent_id !== agentId || !xtermRef.current || !isInitializedRef.current) return;

      try {
        // Clear terminal and write history
        xtermRef.current.clear();

        if (data.history && data.history.length > 0) {
          // Join lines with newlines and write
          const historyText = data.history.join('\r\n') + '\r\n';
          xtermRef.current.write(historyText);
          // Scroll to bottom
          xtermRef.current.scrollToBottom();
        }
      } catch (error) {
        console.debug('Terminal history write error:', error);
      }
    };

    // Handle live terminal output
    const handleOutput = (data: { agent_id: string; content: string; encoding: string }) => {
      if (data.agent_id !== agentId) return;

      try {
        // Decode base64 content
        let text: string;
        if (data.encoding === 'base64') {
          text = atob(data.content);
        } else {
          text = data.content;
        }

        writeToTerminal(text);
      } catch (error) {
        console.error('Error decoding terminal output:', error);
      }
    };

    // Handle terminal idle notification from backend
    const handleIdle = (data: { agent_id: string; idle_seconds: number }) => {
      if (data.agent_id !== agentId) return;

      setIsIdle(true);
      setIdleSeconds(Math.floor(data.idle_seconds));
    };

    // Register event listeners
    socket.on('terminal_history', handleHistory);
    socket.on('terminal_output', handleOutput);
    socket.on('terminal_idle', handleIdle);

    // Frontend idle detection (backup timer)
    idleTimerRef.current = setInterval(() => {
      const timeSinceOutput = Date.now() - lastOutputRef.current;
      if (timeSinceOutput > FRONTEND_IDLE_THRESHOLD_MS && !isIdle) {
        setIsIdle(true);
        setIdleSeconds(Math.floor(timeSinceOutput / 1000));
      }
    }, 1000);

    // Cleanup
    return () => {
      socket.off('terminal_history', handleHistory);
      socket.off('terminal_output', handleOutput);
      socket.off('terminal_idle', handleIdle);

      if (idleTimerRef.current) {
        clearInterval(idleTimerRef.current);
      }
    };
  }, [socket, agentId, isAgentRunning, writeToTerminal, isIdle]);

  /**
   * Update idle seconds counter while idle.
   */
  useEffect(() => {
    if (!isIdle) return;

    const interval = setInterval(() => {
      const timeSinceOutput = Date.now() - lastOutputRef.current;
      setIdleSeconds(Math.floor(timeSinceOutput / 1000));
    }, 1000);

    return () => clearInterval(interval);
  }, [isIdle]);

  // Don't show terminal for non-running agents
  if (!isAgentRunning) {
    return (
      <Box
        sx={{
          p: 2,
          bgcolor: '#1a1a2e',
          borderRadius: 1,
          textAlign: 'center',
        }}
      >
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Agent not running - no live terminal available
        </Typography>
      </Box>
    );
  }

  return (
    <Box sx={{ position: 'relative' }}>
      {/* Terminal header */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          px: 1,
          py: 0.5,
          bgcolor: '#0d0d1a',
          borderTopLeftRadius: 4,
          borderTopRightRadius: 4,
        }}
      >
        <Typography variant="caption" sx={{ color: 'text.secondary', fontWeight: 500 }}>
          Live Terminal
        </Typography>
        {isIdle && (
          <Chip
            label={`Idle for ${idleSeconds}s`}
            size="small"
            sx={{
              height: 18,
              fontSize: '10px',
              bgcolor: 'rgba(251, 146, 60, 0.2)',
              color: '#fb923c',
              '& .MuiChip-label': { px: 1 },
            }}
          />
        )}
      </Box>

      {/* Terminal container */}
      <Box
        ref={terminalRef}
        sx={{
          height: 300,
          minHeight: 300,
          bgcolor: '#1a1a2e',
          borderBottomLeftRadius: 4,
          borderBottomRightRadius: 4,
          overflow: 'hidden',
          '& .xterm': {
            height: '100%',
            padding: '8px',
          },
          '& .xterm-viewport': {
            overflowY: 'auto !important',
            '&::-webkit-scrollbar': {
              width: '8px',
            },
            '&::-webkit-scrollbar-track': {
              bgcolor: 'transparent',
            },
            '&::-webkit-scrollbar-thumb': {
              bgcolor: 'rgba(255, 255, 255, 0.2)',
              borderRadius: '4px',
              '&:hover': {
                bgcolor: 'rgba(255, 255, 255, 0.3)',
              },
            },
          },
        }}
      />
    </Box>
  );
};

export default TerminalView;
