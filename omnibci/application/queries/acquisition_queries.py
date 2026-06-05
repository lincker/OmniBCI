"""
Acquisition read-side queries.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class GetAcquisitionStatusQuery:
    """Return current acquisition / stream state."""


@dataclass
class AcquisitionStatusResult:
    connected: bool
    streaming: bool
    source: Optional[str] = None
    sample_rate: Optional[int] = None
    channel_count: Optional[int] = None
