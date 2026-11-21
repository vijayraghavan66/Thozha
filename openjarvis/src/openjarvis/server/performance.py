"""Content-free performance span logger shared by the Jarvis request path."""

import logging
import sys

PERF_LOGGER = logging.getLogger("openjarvis.performance")
# The server's product loggers may be set to WARNING. Keep this narrow
# telemetry channel enabled at INFO without increasing unrelated log volume.
PERF_LOGGER.setLevel(logging.INFO)
if not PERF_LOGGER.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    PERF_LOGGER.addHandler(_handler)
    PERF_LOGGER.propagate = False
