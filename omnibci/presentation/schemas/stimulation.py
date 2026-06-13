from typing import List, Optional
from pydantic import BaseModel, computed_field


class StartStimulationRequest(BaseModel):
    paradigm: Optional[str] = None


class StartTrialRequest(BaseModel):
    target_index: Optional[int] = None


class ParadigmInfo(BaseModel):
    """Metadata for a discovered stimulation paradigm.

    Fields
    ------
    id           Machine identifier; unique per plugin (= manifest package.id).
    plugin_type  Registry key used to actually start the paradigm.
    display_name Human-readable label shown in the UI.
    description  Optional long description.
    version      Semver string from the manifest, if present.
    source       "builtin" (ships with app) or "user" (downloaded / self-written).

    Deprecated
    ----------
    name         Alias for ``display_name``; kept for one release cycle.
    """

    id: str
    plugin_type: str
    display_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    source: str = "user"

    @computed_field
    @property
    def name(self) -> str:
        """Deprecated alias for display_name."""
        return self.display_name


class ParadigmsResponse(BaseModel):
    paradigms: List[ParadigmInfo]
