/** Source observations are immutable; manual declarations fill missing facts. */
import type { SourceMetadata, SignalChannel } from "./api/signal-config";
import "./AcquisitionMontage.css";

const positiveInteger = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : 0;
const knownName = (channel?: SignalChannel) => !!channel?.name && channel.name_provenance !== "generated";
const knownType = (channel?: SignalChannel) => !!channel?.signal_type && channel.signal_type !== "unknown";

export function acquisitionMontageErrors(value: SourceMetadata | null): string[] {
  if (!value) return [];
  const errors: string[] = [];
  if (!positiveInteger(value.sample_rate)) errors.push("Enter a positive integer sample rate for the manual montage.");
  if (!value.channels.length) errors.push("Add at least one signal channel to the manual montage.");
  if (new Set(value.channels.map((channel) => channel.name)).size !== value.channels.length) errors.push("Channel names must be unique.");
  value.channels.forEach((channel) => {
    if (!channel.name.trim()) errors.push(`Row ${channel.index + 1}: enter a channel name.`);
    if (channel.position && (channel.position.length !== 3 || !channel.position.every(Number.isFinite) || !channel.coordinate_frame?.trim())) {
      errors.push(`Row ${channel.index + 1}: enter all three finite coordinates and their coordinate frame, or leave the position unknown.`);
    }
  });
  return errors;
}

export function AcquisitionMontage({ observed, value, sourceId, parameters, busy, canRead, readHint, onRead, onChange }: {
  observed?: SourceMetadata | null;
  value: SourceMetadata | null;
  sourceId: string;
  parameters: Record<string, unknown>;
  busy: boolean;
  canRead: boolean;
  readHint: string;
  onRead: () => void;
  onChange: (value: SourceMetadata | null) => void;
}) {
  const configuredRate = positiveInteger(parameters.sample_rate);
  const configuredCount = Math.max(0, positiveInteger(parameters.channel_number) - 1);
  const count = observed?.channels.length ?? value?.channels.length ?? configuredCount;
  const generated = (index: number): SignalChannel => ({ index, name: `CH${index + 1}`, name_provenance: "generated", signal_type: "unknown" });
  const channels = Array.from({ length: count }, (_, index) => value?.channels[index] ?? observed?.channels[index] ?? generated(index));
  const manual = (): SourceMetadata => ({ source_id: sourceId || observed?.source_id || value?.source_id || "",
    sample_rate: observed?.sample_rate ?? value?.sample_rate ?? configuredRate,
    channels: channels.map((channel, index) => value?.channels[index] ? { ...value.channels[index] }
      : { index: channel.index, name: channel.name, signal_type: "unknown",
        ...(channel.name_provenance === "generated" ? { name_provenance: "generated" as const } : {}) }), trigger_row_index: count,
    connection_id: "", native_event: value?.native_event ?? "final-row", provenance: "user-declared",
    ...(value?.original_reference ? { original_reference: value.original_reference } : {}) });
  const changeChannel = (index: number, patch: Partial<SignalChannel>) => {
    const next = manual();
    next.channels[index] = { ...next.channels[index], ...patch };
    for (const key of ["unit", "reference", "coordinate_frame"] as const) {
      if (!next.channels[index][key]) delete next.channels[index][key];
    }
    onChange(next);
  };
  const changePosition = (index: number, axis: number, raw: string) => {
    const channel = channels[index];
    const position = [...(channel.position ?? [NaN, NaN, NaN])];
    position[axis] = raw.trim() ? Number(raw) : NaN;
    changeChannel(index, position.every((coordinate) => Number.isNaN(coordinate))
      ? { position: undefined, coordinate_frame: undefined, position_provenance: undefined }
      : { position, position_provenance: "user-declared" });
  };
  const declaredObservation = observed?.provenance === "user-declared";
  return <div className="setting-group acquisition-montage"><h2>Source montage</h2>
    <div className="actions"><button disabled={busy || !canRead} onClick={onRead}>Read from source</button>
      <button disabled={busy || !value} onClick={() => onChange(null)}>Clear manual declarations</button></div>
    <p className="setting-description">{readHint} Reading updates this saved profile's source information. Save profile also saves manual declarations.</p>
    {observed ? <p className="montage-observation"><strong>{declaredObservation ? "Configured source declaration" : "Saved source observation"}</strong>
      {" · "}{observed.provenance} · {observed.sample_rate} Hz · {observed.channels.length} signal channels · trigger row {observed.trigger_row_index + 1}
      {declaredObservation && <span> · These values were declared in the acquisition configuration; they were not reported by the device.</span>}</p>
      : <p className="setting-description">No source observation is saved. Read the connected source or fill in its missing information below.</p>}
    <p className="setting-description">Reported fields are read-only. Editable fields fill missing information. Coordinates are optional and use meters.</p>
    <div className="montage-declarations">
      <label><span>Sample rate (Hz)</span><input aria-label="Montage sample rate" type="number" min="1" step="1"
        readOnly={!!observed} disabled={busy} value={observed?.sample_rate ?? value?.sample_rate ?? (configuredRate || "")}
        onChange={(event) => onChange({ ...manual(), sample_rate: Number(event.target.value) })} /></label>
      <label><span>Signal channels</span><input aria-label="Montage signal channel count" type="number" min="1" step="1"
        readOnly={!!observed} disabled={busy} value={count || ""} onChange={(event) => {
          const length = Number(event.target.value);
          if (!Number.isSafeInteger(length) || length < 0 || length > 4096) return;
          onChange({ ...manual(), channels: Array.from({ length }, (_, index) => channels[index] ?? generated(index)), trigger_row_index: length });
        }} /></label>
      <label><span>Original reference</span><input aria-label="Montage original reference" placeholder="Unknown" readOnly={!!observed?.original_reference}
        disabled={busy} value={observed?.original_reference ?? value?.original_reference ?? ""}
        onChange={(event) => { const next = manual(); if (event.target.value) next.original_reference = event.target.value; else delete next.original_reference; onChange(next); }} /></label>
    </div>
    <p className="setting-description">Signal channels exclude the normalized final trigger row. Row numbers below start at 1.</p>
    <div className="montage-table"><table><thead><tr><th>Row</th><th>Channel name</th><th>Type</th><th>Unit</th><th>Position x, y, z (m)</th><th>Coordinate frame</th></tr></thead>
      <tbody>{channels.map((channel, index) => {
        const reported = observed?.channels[index];
        const name = knownName(reported) ? reported!.name : channel.name;
        const signalType = knownType(reported) ? reported!.signal_type : channel.signal_type;
        const position = reported?.position ?? channel.position;
        const conflict = (knownName(reported) && channel.name_provenance !== "generated" && reported!.name !== channel.name)
          || (knownType(reported) && channel.signal_type !== "unknown" && reported!.signal_type !== channel.signal_type)
          || (reported?.unit && channel.unit && reported.unit !== channel.unit)
          || (reported?.position && channel.position && (JSON.stringify(reported.position) !== JSON.stringify(channel.position)
            || reported.coordinate_frame !== channel.coordinate_frame));
        return <tr key={index}><td>{index + 1}{conflict && <span className="error"> · Manual declaration conflicts with the source.</span>}</td>
          <td><input aria-label={`Source row ${index + 1} name`} readOnly={knownName(reported)} disabled={busy} value={name}
            onChange={(event) => changeChannel(index, { name: event.target.value, name_provenance: undefined })} /></td>
          <td><select aria-label={`Source row ${index + 1} type`} disabled={busy || knownType(reported)} value={signalType}
            onChange={(event) => changeChannel(index, { signal_type: event.target.value })}>
            {[...new Set(["unknown", "eeg", "eog", "ecg", "emg", "misc", "reference", "ground", signalType])].map((type) => <option key={type} value={type}>{type}</option>)}</select></td>
          <td><input aria-label={`Source row ${index + 1} unit`} placeholder="Unknown" readOnly={!!reported?.unit} disabled={busy}
            value={reported?.unit ?? channel.unit ?? ""} onChange={(event) => changeChannel(index, { unit: event.target.value })} /></td>
          <td><div className="montage-position">{["x", "y", "z"].map((axis, coordinate) => <input key={axis}
            aria-label={`Source row ${index + 1} ${axis} (m)`} type="number" step="any" placeholder={axis} disabled={busy} readOnly={!!reported?.position}
            value={Number.isFinite(position?.[coordinate]) ? position![coordinate] : ""} onChange={(event) => changePosition(index, coordinate, event.target.value)} />)}</div></td>
          <td><input aria-label={`Source row ${index + 1} coordinate frame`} placeholder="Unknown" disabled={busy || !position} readOnly={!!reported?.position}
            value={reported?.coordinate_frame ?? channel.coordinate_frame ?? ""} onChange={(event) => changeChannel(index, { coordinate_frame: event.target.value })} /></td></tr>;
      })}</tbody></table></div>
    {!count && <p className="setting-description">Set the signal channel count to enter a manual montage.</p>}
    {value && <p className="setting-description">Manual declarations: user-declared. Saving does not replace the source observation or apply a runtime change.</p>}
  </div>;
}
