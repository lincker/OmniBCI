from typing import List, Optional
from pydantic import BaseModel


class StartStimulationRequest(BaseModel):
    paradigm: Optional[str] = None


class StartTrialRequest(BaseModel):
    target_index: Optional[int] = None


class ParadigmInfo(BaseModel):
    name: str
    description: Optional[str] = None


class ParadigmsResponse(BaseModel):
    paradigms: List[ParadigmInfo]
