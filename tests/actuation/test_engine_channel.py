"""
Contract test: the actuation-side peer routes must be the exact mirror image
of the engine-side actuation_peer_config — outbound here == inbound there and
vice versa. Pins the two ends against drifting apart.
"""
from omnibci.shared.events import EventType
from omnibci.infrastructure.actuation.engine_channel import engine_peer_config
from omnibci.infrastructure.neuroengine.gateway.peers import actuation_peer_config


class TestRouteMirror:
    def test_routes_are_mirror_images(self):
        actuation_side = engine_peer_config("127.0.0.1", 8001)
        engine_side = actuation_peer_config("127.0.0.1", 8001)
        assert actuation_side.routes.outbound == engine_side.routes.inbound
        assert actuation_side.routes.inbound == engine_side.routes.outbound

    def test_actuation_dials_out(self):
        from omnibci.infrastructure.ipc.peer_transport import ConnectTransport
        cfg = engine_peer_config("10.0.0.5", 9001)
        assert isinstance(cfg.transport, ConnectTransport)

    def test_command_inbound_feedback_outbound(self):
        cfg = engine_peer_config("127.0.0.1", 8001)
        assert cfg.routes.inbound == frozenset({EventType.ActuationCommand})
        assert cfg.routes.outbound == frozenset({
            EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
        })
