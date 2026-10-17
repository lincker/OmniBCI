"""
Session domain commands.
"""
from dataclasses import dataclass
from typing import Optional

from omnibci.domain.session import validate_subject_id


@dataclass(frozen=True)
class CreateSessionCommand:
    """Create and register a new BCI session."""
    paradigm: Optional[str] = None
    continuation_of: Optional[str] = None
    config_revision: Optional[str] = None
    subject_id: Optional[str] = None

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)


@dataclass(frozen=True)
class EndSessionCommand:
    """Mark a session as ended; no new trials may be recorded."""
    session_id: str
    interruption_reason: Optional[str] = None
