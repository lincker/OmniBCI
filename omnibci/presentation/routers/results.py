"""Current decoded results without a persisted experiment lifecycle."""

from fastapi import APIRouter, Depends

from omnibci.application.services.results_service import ResultsService
from omnibci.presentation.dependencies import get_results_service
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.results import ResultsResponse

router = APIRouter(prefix="/results", tags=["Results"])


@router.get("", response_model=ApiResponse[ResultsResponse], summary="Get live results for the current engine generation")
def get_results(svc: ResultsService = Depends(get_results_service)):
    return ApiResponse(data=svc.snapshot())
