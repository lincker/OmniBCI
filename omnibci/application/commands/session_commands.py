"""
Session domain commands.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CreateSessionCommand:
    """Create and register a new BCI session."""
    paradigm: Optional[str] = None
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None


@dataclass(frozen=True)
class EndSessionCommand:
    """Mark a session as ended; no new trials may be recorded."""
    session_id: str
    interruption_reason: Optional[str] = None
