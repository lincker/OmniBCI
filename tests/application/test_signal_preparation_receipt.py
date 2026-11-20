"""Acquisition and Processing accept the same immutable preparation identity."""
from dataclasses import FrozenInstanceError, replace

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from tests.neuroengine.test_independent_input_boundaries import input_pair


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [
    lambda value: value.pop("processing_revision"),
    lambda value: value.update(processing_revision=""),
    lambda value: value.update(processing_revision=True),
    lambda value: value.update(extra="unowned"),
    lambda value: value.update(plan_fingerprint="z" * 64),
    lambda value: value.update(input_id=" "),
])
async def test_invalid_preparation_never_opens_the_acquisition_gate(monkeypatch, edit):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    edit(receipt)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    assert events and not events[-1].data.ok
    assert not data.has_consumer
    assert data.empty()
    assert acquisition._processing_receipt is None


@pytest.mark.parametrize("edit", [
    lambda value: value.update(source_fingerprint="z" * 64),
    lambda value: value.update(input_id=" "),
])
def test_invalid_preparation_cannot_replace_a_processing_pipeline(monkeypatch, edit):
    _, processor, _, receipt, events = input_pair(monkeypatch)
    original_decoder = processor._decoder
    edit(receipt)
    processor._prepare_signal(CommandRequest("replace", {
        "plan": processor._signal_plan.to_dict(), "receipt": receipt,
    }))
    assert not events[-1].data.ok
    assert processor._decoder is original_decoder


@pytest.mark.asyncio
async def test_prepared_identity_crosses_fifo_as_an_immutable_value(monkeypatch):
    from omnibci.application.events import SignalPreparationReceipt

    acquisition, _, data, payload, events = input_pair(monkeypatch)
    receipt = SignalPreparationReceipt.from_value(payload)
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", payload)))
    marker = data.get_data(0)
    assert marker.receipt == acquisition._processing_receipt == receipt
    assert not events
    payload["input_id"] = "changed-after-admission"
    encoded = receipt.to_dict()
    encoded["connection_id"] = "changed-after-projection"
    assert marker.receipt.input_id == receipt.input_id == "input-one"
    assert receipt.connection_id == "connection-one"
    assert SignalPreparationReceipt.from_value(receipt.to_dict()) == receipt
    with pytest.raises(FrozenInstanceError):
        receipt.input_id = "mutated"
    with pytest.raises(ValueError):
        replace(receipt, processing_revision=None)
    with pytest.raises(ValueError):
        SignalPreparationReceipt.from_value({**receipt.to_dict(), "extra": "unowned"})
