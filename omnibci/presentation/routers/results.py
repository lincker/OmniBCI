"""Current decoded results without a persisted experiment lifecycle."""

from fastapi import APIRouter, Depends, Query

from omnibci.application.services.results_service import ResultsService
from omnibci.presentation.dependencies import get_results_service
from omnibci.presentation.schemas.common import ApiResponse
from omnibci.presentation.schemas.results import EpochListResponse, ResultsResponse

router = APIRouter(prefix="/results", tags=["Results"])


@router.get("", response_model=ApiResponse[ResultsResponse], summary="Get live results for the current engine generation")
def get_results(svc: ResultsService = Depends(get_results_service)):
    return ApiResponse(data=svc.snapshot())


@router.get("/epochs", response_model=ApiResponse[EpochListResponse],
            summary="List all processed epochs in the current engine run")
def get_epochs(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
               svc: ResultsService = Depends(get_results_service)):
    return ApiResponse(data=svc.epochs(offset=offset, limit=limit))
