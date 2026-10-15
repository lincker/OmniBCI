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
            assert await reader.connect() is True
            assert reader.is_connected is True
            assert type(reader.reader_adapter).__name__ == "CNTReaderAdapter"
            metadata = await reader.get_metadata()
            assert isinstance(metadata, str)
            assert "sfreq: 1000.0 Hz" in metadata
            assert "Annotations" in metadata
            descriptor = reader.describe_output(source_id=reader.plugin_type, connection_id="file-connection")
            assert descriptor.signal_channel_count == 9
            assert descriptor.output_row_count == 10
            assert descriptor.sample_rate == 1000
            assert [channel.unit for channel in descriptor.channels] == ["V"] * 9
            assert descriptor.native_event == "annotations"
            assert descriptor.provenance == "file-reported"
            assert reader.file_pointer == 0

            assert await reader.start_stream() is True
            assert reader.is_streaming is True

            chunk = await reader.read_chunk()
            assert chunk.shape == (10, 100)
            assert np.isfinite(chunk).all()
            assert reader.file_pointer == 100
            assert await reader.get_metadata() == metadata
            assert reader.describe_output(source_id=reader.plugin_type, connection_id="file-connection") == descriptor

            assert await reader.stop_stream() is True
            assert reader.is_streaming is False
        finally:
            if reader.is_streaming:
                await reader.stop_stream()
            if reader.is_connected:
                await reader.disconnect()

        assert reader.is_connected is False

    asyncio.run(scenario())
