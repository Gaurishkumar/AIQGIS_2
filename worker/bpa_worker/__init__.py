"""Local AI/GIS worker for AI Building Polygon Automation.

Runs in its own Python environment (never inside QGIS) and serves the plugin
over HTTP on 127.0.0.1. See ``python -m bpa_worker --help``.
"""

import os
import sys

__version__ = "1.0.0"

# Make the shared package importable when running from the source tree.
_COMMON = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "common"))
if os.path.isdir(_COMMON) and _COMMON not in sys.path:
    sys.path.insert(0, _COMMON)
