"""
Unit tests for Query objects and their result DTOs.
"""
from omnibci.application.queries.acquisition_queries import (
    GetAcquisitionStatusQuery,
    AcquisitionStatusResult,
)
from omnibci.application.queries.engine_queries import (
    GetEngineStatusQuery,
    EngineStatusResult,
)


class TestEngineQueries:
    def test_query_is_frozen(self):
        q = GetEngineStatusQuery()
        assert q == GetEngineStatusQuery()

    def test_result_defaults(self):
        r = EngineStatusResult(running=False, acquisition_alive=False, processor_alive=False)
        assert r.running is False
        assert r.extra is None

    def test_result_with_extra(self):
        r = EngineStatusResult(
            running=True,
            acquisition_alive=True,
            processor_alive=True,
            extra={"uptime": 42},
        )
        assert r.extra == {"uptime": 42}


class TestAcquisitionQueries:
    def test_query_equals(self):
        assert GetAcquisitionStatusQuery() == GetAcquisitionStatusQuery()

    def test_result_connected_streaming(self):
        r = AcquisitionStatusResult(connected=True, streaming=True)
        assert r.connected is True
        assert r.streaming is True
        assert r.source is None
        assert r.sample_rate is None

    def test_result_with_source_info(self):
        r = AcquisitionStatusResult(
            connected=True,
            streaming=False,
            source="file_reader",
            sample_rate=1000,
            channel_count=10,
        )
        assert r.source == "file_reader"
        assert r.sample_rate == 1000
        assert r.channel_count == 10
