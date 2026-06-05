"""
Backward-compatibility shim — do not add new code here.

acquisition/service.py has been split into:
  acquisition/acquisition.py  — AcquisitionCommand, AcquisitionState, Acquisition
  acquisition/worker.py       — AcquisitionWorker

Existing imports of the form
    from omnibci.infrastructure.acquisition.service import AcquisitionCommand, AcquisitionWorker
continue to work through this re-export, but should be migrated to the new
module paths.
"""
from omnibci.infrastructure.acquisition.acquisition import (  # noqa: F401
    AcquisitionCommand,
    AcquisitionState,
    Acquisition,
)
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker  # noqa: F401
