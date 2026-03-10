# The canonical server implementation has moved to agentviz/server.py so that it
# is included in the installed Python package and accessible from anywhere.
#
# This file is kept for backwards-compatibility only.  Running uvicorn directly
# against this module still works when invoked from the repo root.

from agentviz.server import *  # noqa: F401,F403
from agentviz.server import socket_app, app, sio  # noqa: F401

import uvicorn

if __name__ == "__main__":
    uvicorn.run("agentviz.server:socket_app", host="127.0.0.1", port=8787, reload=True)
