"""Normalize metadata already available from the selected source or profile."""
from collections.abc import Mapping
from math import isfinite

from omnibci.domain.montage import ChannelDescriptor, SourceMetadata, merge_source_montage


def source_metadata(raw, settings: dict, *, source_id: str, connection_id: str,
                    include_manual: bool = True, validate_configuration: bool = True) -> SourceMetadata:
    """Describe one signal matrix with the existing final trigger row convention."""
    info = raw if isinstance(raw, Mapping) else {}
    profile_manual = settings.get("ACQUISITION.manual_montage") if include_manual else None
    if profile_manual is not None:
        manual = SourceMetadata.from_dict(profile_manual)
        declarations = dict(settings)
        declarations.setdefault("ACQUISITION.sample_rate", manual.sample_rate)
        declarations.setdefault("ACQUISITION.channel_number", manual.output_row_count)
        observed = source_metadata(raw, declarations, source_id=source_id, connection_id=connection_id,
                                   include_manual=False, validate_configuration=validate_configuration)
        return merge_source_montage(observed, manual)
    setup = settings.get("SIGNAL.setup") or {}
    manual_value = setup.get("manual_metadata") if include_manual else None
    manual = SourceMetadata.from_dict(manual_value) if manual_value is not None else None
    if manual is not None and manual.source_id != source_id:
        raise ValueError("Manual metadata source_id differs from the selected source")
    declared_rate = settings.get("ACQUISITION.sample_rate")
    observed_rate = info.get("sample_rate")
    rate = observed_rate if observed_rate is not None else declared_rate
    if rate is None and manual is not None:
        rate = manual.sample_rate
    if (isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not isfinite(rate) or rate <= 0 or int(rate) != rate):
        raise ValueError("Source metadata requires a positive integer sample rate")
    if (validate_configuration and observed_rate is not None and declared_rate is not None
            and observed_rate != declared_rate):
        raise ValueError(f"Source sample rate {observed_rate} differs from configured {declared_rate}")
    declared_count = settings.get("ACQUISITION.channel_number")
    observed_count = info.get("output_channel_count")
    count = observed_count if observed_count is not None else declared_count
    if count is None and manual is not None:
        count = manual.output_row_count
    if type(count) is not int or count < 2:
        raise ValueError("Source metadata requires at least one signal row and a final trigger row")
    if (validate_configuration and observed_count is not None and declared_count is not None
            and observed_count != declared_count):
        raise ValueError(f"Source output row count {observed_count} differs from configured {declared_count}")
    if manual is not None and (manual.sample_rate != rate or manual.output_row_count != count):
        raise ValueError("Manual metadata sample rate or output row count differs from the source")
    original_reference = info.get("original_reference")
    if (manual is not None and original_reference and manual.original_reference
            and original_reference != manual.original_reference):
        raise ValueError("Manual original reference differs from the source")
    names = info.get("channel_labels") or (settings.get("ACQUISITION.channel_names") if include_manual else None) or []
    if not isinstance(names, (list, tuple)):
        raise ValueError("Source channel labels must be a sequence")
    if names and len(names) != count - 1:
        raise ValueError(f"Source channel labels must describe exactly {count - 1} signal rows")
    types = info.get("channel_types") or ["unknown"] * (count - 1)
    units = info.get("channel_units") or [None] * (count - 1)
    positions = info.get("channel_positions") or [None] * (count - 1)
    if len(types) != count - 1 or len(units) != count - 1 or len(positions) != count - 1:
        raise ValueError("Source channel types and units must match the signal row count")
    channels = []
    for index in range(count - 1):
        declared = manual.channels[index] if manual is not None else None
        name = names[index] if names else None
        name_provenance = None
        if name is None or (isinstance(name, str) and not name.strip()):
            name = declared.name if declared else f"CH{index + 1}"
            name_provenance = declared.name_provenance if declared else "generated"
        elif declared is not None and name != declared.name:
            raise ValueError(
                f"Source row {index} changed: manual metadata expects {declared.name!r}, got {name!r}"
            )
        position = positions[index] or {}
        if not position and declared is not None and declared.position is not None:
            position = {"position": declared.position, "coordinate_frame": declared.coordinate_frame,
                        "position_provenance": "user-declared"}
        channels.append(ChannelDescriptor(
            index=index, name=name, name_provenance=name_provenance,
            signal_type=types[index] if types[index] != "unknown" else declared.signal_type if declared else "unknown",
            unit=units[index] or (declared.unit if declared else None),
            reference=declared.reference if declared else None,
            **position,
        ))
    return SourceMetadata(
        source_id=source_id, sample_rate=int(rate), connection_id=connection_id,
        channels=tuple(channels),
        trigger_row_index=count - 1,
        original_reference=original_reference or (manual.original_reference if manual else None),
        native_event=info.get("native_event", "final-row"),
        provenance=info.get("provenance", "user-declared"),
    )
