"""Public exports for ebbmem-langgraph."""

from __future__ import annotations

from ebbmem_langgraph.store import EbbmemStore
from ebbmem_langgraph.tools import (
    create_ebbmem_manage_memory_tool,
    create_ebbmem_reinforce_memory_tool,
    create_ebbmem_search_memory_tool,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "EbbmemStore",
    "create_ebbmem_manage_memory_tool",
    "create_ebbmem_reinforce_memory_tool",
    "create_ebbmem_search_memory_tool",
]

try:
    from ebbmem_langgraph.middleware import EbbmemMemoryMiddleware as EbbmemMemoryMiddleware

    __all__.append("EbbmemMemoryMiddleware")
except ImportError:
    pass
