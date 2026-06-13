"""
/api/v1/plugins — Plugin discovery endpoints.

GET /api/v1/plugins/{category}
    List available plugins for a given category.
    category ∈ {paradigm, decoder, acquisition, effector}
"""
from fastapi import APIRouter

from omnibci.infrastructure.plugins.plugin_catalog import (
    VALID_CATEGORIES,
    list_plugins,
)
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ApiResponse, ErrorCode
from omnibci.presentation.schemas.stimulation import ParadigmsResponse

router = APIRouter(prefix="/plugins", tags=["Plugins"])


@router.get(
    "/{category}",
    response_model=ApiResponse[ParadigmsResponse],
    summary="List plugins by category",
)
def list_plugins_by_category(category: str):
    """Return discovered plugins for *category*.

    Valid categories: ``paradigm``, ``decoder``, ``acquisition``, ``effector``.
    """
    if category not in VALID_CATEGORIES:
        raise ApiError(
            422,
            ErrorCode.VALIDATION_ERROR,
            f"Unknown plugin category {category!r}. "
            f"Valid: {sorted(VALID_CATEGORIES)}",
        )
    plugins = list_plugins(category)
    return ApiResponse(data=ParadigmsResponse(paradigms=plugins))
