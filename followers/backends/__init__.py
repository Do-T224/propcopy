"""Firm-specific FollowerBackend implementations.

Importing this package registers every backend into ``followers.backend.BACKENDS``.
Each submodule keeps its heavy imports local, so importing the package does
not require optional dependencies such as pythonnet.
"""

from __future__ import annotations

from . import ninjatrader  # noqa: F401  (registration side effects)

__all__ = ["ninjatrader"]
