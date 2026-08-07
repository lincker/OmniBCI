"""Integration coverage for FileReader against the checked-in CNT fixture."""

import asyncio
from pathlib import Path

import numpy as np
import pytest

from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader

_CNT_FIXTURE = Path(__file__).parents[1] / "fixture" / "train.cnt"


@pytest.mark.integration
def test_real_cnt_lifecycle_and_first_chunk():
    async def scenario():
        reader = FileReader()
        reader.set_file_path(str(_CNT_FIXTURE))

        try:
            assert await reader.connect() is True
            assert reader.is_connected is True
            assert type(reader.reader_adapter).__name__ == "CNTReaderAdapter"

            assert await reader.start_stream() is True
            assert reader.is_streaming is True

            chunk = await reader.read_chunk()
            assert chunk.shape == (10, 100)
            assert np.isfinite(chunk).all()
            assert reader.file_pointer == 100

            assert await reader.stop_stream() is True
            assert reader.is_streaming is False
        finally:
            if reader.is_streaming:
                await reader.stop_stream()
            if reader.is_connected:
                await reader.disconnect()

        assert reader.is_connected is False

    asyncio.run(scenario())
