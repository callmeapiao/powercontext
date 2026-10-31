"""SDD context module for AI development assistants.

Public entry point: ``python -m ctxmod --help``.
"""

from .config import Settings
from .context import DevelopmentContext, extract_keywords, resolve_spec_status
from .db import MaterialStore
from .state import TaskState, TaskStateStore, ValidationRecord

__all__ = [
    "DevelopmentContext",
    "MaterialStore",
    "Settings",
    "TaskState",
    "TaskStateStore",
    "ValidationRecord",
    "extract_keywords",
    "resolve_spec_status",
]
