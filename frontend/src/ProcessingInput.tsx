/** Decoder profiles own the ordered channels and software reference they consume. */
import { useEffect, useRef, useState } from "react";
import { signalConfigApi as api, type BipolarOutput, type ProcessingInput as Input,
  type SignalConfiguration, type SignalPreview, type SignalSetup } from "./api/signal-config";
import "./ProcessingInput.css";

const names = (value: string) => value.split(",").map((name) => name.trim()).filter(Boolean);
export const emptyProcessingInput = (): Input => ({ schema_version: 1, outputs: [],
  reference: { mode: "original", channels: [] }, excluded_channels: [] });
const fromLegacy = (value: SignalSetup): Input => ({ schema_version: 1, outputs: structuredClone(value.outputs),
  reference: structuredClone(value.reference), excluded_channels: [...value.excluded_channels] });

export function processingInputErrors(value: Input | null): string[] {
  if (!value) return [];
  const errors: string[] = [];
  const outputNames = value.outputs.map((output) => typeof output === "string" ? output : output.name);
  if (new Set(outputNames).size !== outputNames.length) errors.push("Algorithm channel names must be unique.");
  if (value.reference.mode === "single" && value.reference.channels.length !== 1) errors.push("Select exactly one reference channel.");
  if (value.reference.mode === "average" && !value.reference.channels.length) errors.push("Select at least one reference channel.");
  if (value.reference.mode === "bipolar" && (!value.outputs.length || value.outputs.some((output) => typeof output === "string"
      || !output.name.trim() || !output.positive.trim() || !output.negative.trim() || output.positive === output.negative))) {
    errors.push("Each bipolar pair needs a name and two different input channels.");
  }
  return errors;
}

export function ProcessingInput({ value, busy, configurationRevision, onChange }: {
  value: Input | null;
  busy: boolean;
  configurationRevision: string;
  onChange: (value: Input) => void;
}) {
  const [config, setConfig] = useState<SignalConfiguration | null>(null);
  const [preview, setPreview] = useState<SignalPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [listInputs, setListInputs] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const request = useRef(0);
  useEffect(() => {
    let active = true;
    request.current += 1; setConfig(null); setPreview(null); setPreviewing(false); setError("");
    void api.get().then((next) => { if (active) setConfig(next); })
      .catch((cause) => { if (active) setError(String(cause)); });
    return () => { active = false; request.current += 1; };
  }, [configurationRevision]);
  useEffect(() => { request.current += 1; setPreview(null); setPreviewing(false); }, [value]);
  const source = config?.source_metadata;
  const legacy = config?.legacy_setup;
  const legacySourceFields = !!legacy && (!!legacy.assignments.length || !!legacy.template || !!legacy.original_reference || !!legacy.manual_metadata);
  const disabled = busy || previewing;
  const edit = (next: Input) => { setPreview(null); setError(""); onChange(next); };
  const listInput = (key: string, fallback: string[], change: (value: string[]) => void) => ({
    value: listInputs[key] !== undefined && JSON.stringify(names(listInputs[key])) === JSON.stringify(fallback)
      ? listInputs[key] : fallback.join(", "),
    onChange: (event: { target: { value: string } }) => {
      setListInputs((previous) => ({ ...previous, [key]: event.target.value })); change(names(event.target.value));
    },
  });
  const updatePair = (index: number, patch: Partial<BipolarOutput>) => value && edit({ ...value,
    outputs: value.outputs.map((output, row) => row === index && typeof output !== "string" ? { ...output, ...patch } : output) });
  const previewInput = async () => {
    if (!value) return;
    const sequence = ++request.current;
    setPreviewing(true); setError(""); setPreview(null);
    try {
      const next = await api.preview(value);
      if (sequence === request.current) {
        setConfig((previous) => previous ? { ...previous, source_metadata: next.plan.source } : previous);
        setPreview(next);
      }
    } catch (cause) { if (sequence === request.current) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (sequence === request.current) setPreviewing(false); }
  };
  return <div className="setting-group processing-input"><h2>Algorithm input</h2>
    <p className="setting-description">Channel selection, order, exclusions, and software reference are saved with this decoder profile. Save the profile, then review and apply it.</p>
    <div className="input-source" aria-label="Acquisition source information">
      {source ? <><p>{source.source_id} · {source.sample_rate} Hz · {source.channels.length} signal channels · trigger row {source.trigger_row_index + 1}</p>
        <p>Available channels: {source.channels.map((channel) => channel.name).join(", ")}</p>
        <p>Original reference: {source.original_reference ?? "Unknown"}</p></>
        : <p>Source information is unavailable. Read the source or fill its missing montage information in the Acquisition profile.</p>}
      <p>Source electrodes, types, units, positions, and original reference are managed in Acquisition. Offline preview does not validate a connected device.</p>
    </div>
    {legacySourceFields && <p className="inline-message" role="note">The previous setup includes source declarations or electrode bindings. Move that information to the Acquisition profile before adopting a decoder input. Import copies only channel selection, exclusions, and software reference.</p>}
    {!value ? <>
      <p className="setting-description">This profile has no saved channel input. {legacy
        ? "The previous global setup remains in use until a decoder input is saved and applied."
        : "It uses the original source channels and reference."}</p>
      {legacy && <p className="setting-description">Previous channel selection: {legacy.outputs.length
        ? legacy.outputs.map((output) => typeof output === "string" ? output : output.name).join(", ") : "All source channels"} · {legacy.reference.mode} reference.</p>}
      <div className="actions"><button disabled={disabled} onClick={() => edit(emptyProcessingInput())}>Configure decoder input</button>
        {legacy && <button disabled={disabled} onClick={() => edit(fromLegacy(legacy))}>Import previous channel selection</button>}</div>
    </> : <>
      <label className="setting-row"><span className="setting-label">Reference mode</span><select disabled={disabled} value={value.reference.mode}
        onChange={(event) => {
          const mode = event.target.value as Input["reference"]["mode"];
          setListInputs({}); edit({ ...value, reference: { mode, channels: [] }, outputs: mode === "bipolar" || value.reference.mode === "bipolar" ? [] : value.outputs });
        }}>
        <option value="original">Keep original reference</option><option value="single">Single sampled channel</option>
        <option value="average">Average of selected channels</option><option value="bipolar">Bipolar pairs</option></select></label>
      {(value.reference.mode === "single" || value.reference.mode === "average") && <label className="setting-row"><span className="setting-label">Reference channels</span>
        <input disabled={disabled} placeholder="M1, M2" {...listInput("reference", value.reference.channels,
          (channels) => edit({ ...value, reference: { ...value.reference, channels } }))} /></label>}
      {value.reference.mode !== "bipolar" ? <label className="setting-row"><span className="setting-label">Ordered output channels</span>
        <input disabled={disabled} placeholder="All source channels" {...listInput("outputs", value.outputs.filter((output): output is string => typeof output === "string"),
          (outputs) => edit({ ...value, outputs }))} /></label>
        : <div>{value.outputs.map((output, index) => typeof output === "string" ? null : <div className="signal-pair" key={index}>
          <input aria-label={`Pair ${index + 1} name`} disabled={disabled} placeholder="Output name" value={output.name} onChange={(event) => updatePair(index, { name: event.target.value })} />
          <input aria-label={`Pair ${index + 1} positive`} disabled={disabled} placeholder="Positive channel" value={output.positive} onChange={(event) => updatePair(index, { positive: event.target.value })} />
          <input aria-label={`Pair ${index + 1} negative`} disabled={disabled} placeholder="Negative channel" value={output.negative} onChange={(event) => updatePair(index, { negative: event.target.value })} />
          <button disabled={disabled} aria-label={`Remove pair ${index + 1}`} onClick={() => edit({ ...value, outputs: value.outputs.filter((_, row) => row !== index) })}>Remove</button>
        </div>)}<button disabled={disabled} onClick={() => edit({ ...value, outputs: [...value.outputs, { name: "", positive: "", negative: "" }] })}>Add bipolar pair</button></div>}
      <label className="setting-row"><span className="setting-label">Excluded channels</span><input disabled={disabled} {...listInput("excluded", value.excluded_channels,
        (excluded_channels) => edit({ ...value, excluded_channels }))} /></label>
      <p className="setting-description">Lists use comma-separated channel names from Acquisition. Reference channels may be outside the output list. Rereferencing requires known units and an original reference.</p>
      <button disabled={disabled || !config || processingInputErrors(value).length > 0} onClick={() => void previewInput()}>Preview mapping</button>
      {preview && <><p className="setting-description">{preview.plan.source.sample_rate} Hz · {preview.plan.output_channels.length} algorithm channels · trigger preserved</p>
        <ol>{preview.plan.output_channels.map((channel, index) => <li key={channel.name}>{channel.name} · {channel.unit ?? "Unknown unit"} · {channel.reference ?? "Unknown reference"}
          {channel.derivation && ` · ${channel.derivation.join(" − ")}`}
          <span className="setting-description"> · Source rows: {(preview.plan.coefficients[index] ?? []).flatMap((coefficient, row) => coefficient === 0 ? []
            : [`${row + 1} ${preview.plan.source.channels[row].name}`]).join(", ") || "None"}</span></li>)}</ol>
        {preview.warnings.length > 0 && <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
        <p className="setting-description">Preview checks this draft. Starting acquisition validates the selected decoder input against the connected source.</p></>}
    </>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
  </div>;
}
