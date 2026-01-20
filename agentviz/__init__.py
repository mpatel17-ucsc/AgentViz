import sys
from .monitor import Monitor

print(f"[AgentViz Debug] Loading agentviz package from: {__file__}", file=sys.stderr)

__all__ = ["Monitor"]
