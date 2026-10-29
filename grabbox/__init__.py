"""GrabBox - keep any downloadable thing, locally."""

import os as _os
import sys as _sys

#: Prefer a yt-dlp vendored next to this repo (survives sandbox restarts and
#: needs no system install); fall back to whatever is on PATH otherwise.
_VENDOR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "vendor")
if _os.path.isdir(_VENDOR) and _VENDOR not in _sys.path:
    _sys.path.insert(0, _VENDOR)

__version__ = "0.3.0"
__all__ = ["__version__"]
