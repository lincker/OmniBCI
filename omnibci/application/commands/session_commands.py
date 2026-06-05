"""
Session domain commands.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CreateSessionCommand:
    """Create and register a new BCI session."""
    paradigm: Optional[str] = None


@dataclass(frozen=True)
class EndSessionCommand:
    """Mark a session as ended; no new trials may be recorded."""
    session_id: str
