"""Read-only HTTP API: metrics, logs, telemetry pulls, discovery.

The server is started by Service.start() and stopped by Service.stop().
Handlers never mutate state, never block the tick loop, and never touch
hardware. Bearer-token authentication is enforced on all endpoints
except the liveness probes (/healthz, /readyz).
"""

from .server import APIServer

__all__ = ["APIServer"]
