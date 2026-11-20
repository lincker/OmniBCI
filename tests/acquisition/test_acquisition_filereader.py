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
        reader = FileReader(parameters={"data_file_path": str(_CNT_FIXTURE)})

        try:
            assert await reader.connect() is None
            assert reader.reader_adapter is not None
            assert type(reader.reader_adapter).__name__ == "CNTReaderAdapter"
            metadata = await reader.observe()
            assert isinstance(metadata.native_text, str)
            assert "sfreq: 1000.0 Hz" in metadata.native_text
            assert "Annotations" in metadata.native_text
            descriptor = metadata.signal_info
            assert descriptor.signal_channel_count == 9
            assert descriptor.output_row_count == 10
            assert descriptor.sample_rate == 1000
            assert [channel.unit for channel in descriptor.channels] == ["V"] * 9
            assert metadata.native_event == "annotations"
            assert metadata.provenance == "file-reported"
            assert reader.file_pointer == 0

            assert await reader.start_stream() is None
            assert reader._is_streaming is True

            chunk = await reader.read_chunk()
            assert chunk.shape == (10, 100)
            assert np.isfinite(chunk).all()
            assert reader.file_pointer == 100
            with pytest.raises(RuntimeError, match="Stop"):
                await reader.observe()

            assert await reader.stop_stream() is None
            assert reader._is_streaming is False
        finally:
            if reader._is_streaming:
                await reader.stop_stream()
            if reader.reader_adapter is not None:
                await reader.disconnect()

        assert reader.reader_adapter is None

    asyncio.run(scenario())
