/** Decoder profiles own the ordered channels and software reference they consume. */
import { useEffect, useRef, useState } from "react";
import { signalConfigApi as api, type BipolarOutput, type ChannelBinding, type ProcessingInput as Input,
  type SignalConfiguration, type SignalPreview, type SignalSetup } from "./api/signal-config";
import { selectionKey, type PluginDefinition, type PluginSelection } from "./api/plugin-config";
import "./ProcessingInput.css";

const names = (value: string) => value.split(",").map((name) => name.trim()).filter(Boolean);
const outputName = (output: Input["outputs"][number]) => typeof output === "string" ? output : output.name;
const channelBinding = (name: string, source: string): string | ChannelBinding => name === source ? name : { name, source };
const isPair = (output: Input["outputs"][number]): output is BipolarOutput => typeof output !== "string" && "positive" in output;
const sourceChangedMessage = "Acquisition source information changed. Review it, then preview the mapping again.";
export const emptyProcessingInput = (defaultNames: string[] = []): Input => ({ schema_version: 1, outputs: [...defaultNames],
  reference: { mode: "original", channels: [] }, excluded_channels: [] });
const fromLegacy = (value: SignalSetup): Input => ({ schema_version: 1, outputs: structuredClone(value.outputs),
  reference: structuredClone(value.reference), excluded_channels: [...value.excluded_channels] });

export function processingInputErrors(value: Input | null): string[] {
  if (!value) return [];
  const errors: string[] = [];
  const outputNames = value.outputs.map(outputName);
  if (outputNames.some((name) => !name.trim())) errors.push("Input channel names cannot be empty.");
  if (new Set(outputNames).size !== outputNames.length) errors.push("Algorithm channel names must be unique.");
  if (value.reference.mode === "single" && value.reference.channels.length !== 1) errors.push("Select exactly one reference channel.");
  if (value.reference.mode === "average" && !value.reference.channels.length) errors.push("Select at least one reference channel.");
  if (value.reference.mode === "bipolar") {
    if (!value.outputs.length || value.outputs.some((output) => !isPair(output)
        || !output.name.trim() || !output.positive.trim() || !output.negative.trim() || output.positive === output.negative)) {
      errors.push("Each bipolar pair needs a name and two different input channels.");
    }
  } else {
    const sources = value.outputs.map((output) => typeof output === "string" ? output : "source" in output ? output.source : "");
    if (sources.some((name) => !name.trim())) errors.push("Each input needs an Acquisition source channel.");
    if (new Set(sources).size !== sources.length) errors.push("Each Acquisition channel can be bound only once.");
  }
  return errors;
}

export function ProcessingInput({ value, busy, configurationRevision, decoderSelection, channelRequirements, onChange }: {
  value: Input | null;
  busy: boolean;
  configurationRevision: string;
  decoderSelection?: PluginSelection;
  channelRequirements?: PluginDefinition["channel_requirements"];
  onChange: (value: Input) => void;
}) {
  const [config, setConfig] = useState<SignalConfiguration | null>(null);
  const [preview, setPreview] = useState<SignalPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [listInputs, setListInputs] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef(0);
  const sourceRequest = useRef(0);
  const currentConfig = useRef<SignalConfiguration | null>(null);
  const decoderKey = decoderSelection ? selectionKey(decoderSelection) : "";
  const requirementsKey = JSON.stringify(channelRequirements ?? null);
  const refreshSource = async () => {
    const sequence = ++sourceRequest.current;
    setRefreshing(true); setError("");
    try {
      const next = await api.get();
      if (sequence !== sourceRequest.current) return null;
      if (currentConfig.current && currentConfig.current.source_revision !== next.source_revision) {
        request.current += 1; setPreview(null); setPreviewing(false); setNotice(sourceChangedMessage);
      }
      currentConfig.current = next; setConfig(next);
      return next;
    } catch (cause) {
      if (sequence === sourceRequest.current) {
        request.current += 1; setPreview(null); setPreviewing(false);
        currentConfig.current = null; setConfig(null);
        setError(cause instanceof Error ? cause.message : String(cause));
      }
      return null;
    } finally { if (sequence === sourceRequest.current) setRefreshing(false); }
  };
  useEffect(() => {
    request.current += 1; currentConfig.current = null;
    setConfig(null); setPreview(null); setPreviewing(false); setError(""); setNotice("");
    void refreshSource();
    const onFocus = () => { void refreshSource(); };
    if (typeof window !== "undefined") window.addEventListener("focus", onFocus);
    return () => {
      sourceRequest.current += 1; request.current += 1;
      if (typeof window !== "undefined") window.removeEventListener("focus", onFocus);
    };
  }, [configurationRevision]);
  useEffect(() => { request.current += 1; setPreview(null); setPreviewing(false); setError(""); }, [value, decoderKey, requirementsKey]);
  const source = config?.source_metadata;
  const binding = config?.source_binding;
  const legacy = config?.legacy_setup;
  const legacySourceFields = !!legacy && (!!legacy.assignments.length || !!legacy.template || !!legacy.original_reference || !!legacy.manual_metadata);
  const disabled = busy || previewing || refreshing;
  const defaultNames = channelRequirements?.default_names ?? [];
  const edit = (next: Input) => { setPreview(null); setError(""); onChange(next); };
  const listInput = (key: string, fallback: string[], change: (value: string[]) => void) => ({
    value: listInputs[key] !== undefined && JSON.stringify(names(listInputs[key])) === JSON.stringify(fallback)
      ? listInputs[key] : fallback.join(", "),
    onChange: (event: { target: { value: string } }) => {
      setListInputs((previous) => ({ ...previous, [key]: event.target.value })); change(names(event.target.value));
    },
  });
  const updatePair = (index: number, patch: Partial<BipolarOutput>) => value && edit({ ...value,
    outputs: value.outputs.map((output, row) => row === index && isPair(output) ? { ...output, ...patch } : output) });
  const allChannels = !!value && value.reference.mode !== "bipolar" && !value.outputs.length;
  const availableChannels = source?.channels ?? [];
  const selectedOutputs = allChannels
    ? availableChannels.filter((channel) => !value?.excluded_channels.includes(channel.name)).map((channel) => channel.name)
    : value?.outputs ?? [];
  const rows = selectedOutputs.flatMap((output) => isPair(output) ? [] : [{ name: outputName(output), source: typeof output === "string" ? output : output.source }]);
  const requiredNames = channelRequirements?.required_names ?? [];
  const missingRequired = requiredNames.filter((name) => !rows.some((row) => row.name === name));
  const setRows = (next: typeof rows) => value && next.length > 0 && edit({ ...value,
    outputs: next.map((row) => channelBinding(row.name, row.source)) });
  const moveRow = (index: number, offset: number) => {
    const next = [...rows]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; setRows(next);
  };
  const bindRequired = (name: string, sourceName: string) => {
    const existing = rows.findIndex((row) => row.source === sourceName);
    setRows(existing < 0 ? [...rows, { name, source: sourceName }]
      : rows.map((row, index) => index === existing ? { name, source: sourceName } : row));
  };
  const requirements = <div className="channel-requirements" aria-label="Algorithm channel requirements">
    {channelRequirements ? <>
      {channelRequirements.description && <p>{channelRequirements.description}</p>}
      <p>Signal type: {channelRequirements.signal_type ?? "Not restricted"} · Minimum channel count: {channelRequirements.min_count ?? 1}</p>
      <p>Required channels: {requiredNames.join(", ") || "No specific names required"}</p>
      <p>Plugin default channels: {defaultNames.join(", ") || "No named default"}</p>
    </> : <p>This plugin has not declared channel requirements.</p>}
  </div>;
  const previewInput = async () => {
    if (!value || !config) return;
    const sequence = ++request.current;
    setPreviewing(true); setError(""); setNotice(""); setPreview(null);
    try {
      const current = await refreshSource();
      if (!current || sequence !== request.current) return;
      if (current.source_revision !== config.source_revision) { setNotice(sourceChangedMessage); return; }
      const next = await api.preview(value, current.source_revision, decoderSelection);
      if (sequence === request.current) {
        if (next.source_revision === current.source_revision) setPreview(next);
        else { await refreshSource(); if (sequence === request.current) setNotice(sourceChangedMessage); }
      }
    } catch (cause) { if (sequence === request.current) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (sequence === request.current) setPreviewing(false); }
  };
  return <div className="setting-group processing-input"><h2>Processing input</h2>
    <p className="setting-description">Bind Acquisition channels to the names and order the algorithm needs. The bound channels enter Epoch first; filters then process each epoch before decoding.</p>
    <ol className="processing-flow" aria-label="Signal Processing data flow">
      <li>Acquisition</li><li>Channel binding</li><li>Epoch</li><li>Filters <small>(optional)</small></li><li>Decoder</li>
    </ol>
    <p className="setting-description">Bindings and software reference are saved with this decoder profile. Epoch timing and baseline settings remain in the processing pipeline.</p>
    <div className="input-source" aria-label="Acquisition source information">
      <p>Data source: {binding ? `${binding.profile_name ?? binding.profile_id ?? binding.source_id} · ${binding.source_id}` : "No Acquisition source selected"}
        {binding?.profile_revision != null && ` · profile revision ${binding.profile_revision}`}</p>
      {binding && <p>{binding.state === "active" ? "Applied Acquisition source" : "Selected for next engine start"} · {binding.metadata_origin === "connected"
        ? "Connected source information" : binding.metadata_origin === "profile" ? "Saved Acquisition profile information" : "Source information unavailable"}</p>}
      <p>Signal Processing automatically uses the current Acquisition source. Switching Acquisition preserves these bindings for review.</p>
      {source ? <><p>{source.source_id} · {source.sample_rate} Hz · {source.channels.length} signal channels · trigger row {source.trigger_row_index + 1}</p>
        <p>Original reference: {source.original_reference ?? "Unknown"}</p></>
        : config?.source_error ? <p className="inline-message error" role="alert">{config.source_error}</p>
        : <p>Source information is unavailable. Read the source or fill its missing montage information in the Acquisition profile.</p>}
      <p>Source electrodes, types, units, positions, and original reference are managed in Acquisition. Offline preview does not validate a connected device.</p>
      <button disabled={busy || refreshing} onClick={() => { setNotice(""); void refreshSource(); }}>{refreshing ? "Refreshing source…" : "Refresh source"}</button>
    </div>
    {legacySourceFields && <p className="inline-message" role="note">The previous setup includes source declarations or electrode bindings. Move that information to the Acquisition profile before adopting a decoder input. Import copies only channel selection, exclusions, and software reference.</p>}
    {!value ? <>
      {requirements}
      <p className="setting-description">This profile has no saved channel input. {legacy
        ? "The previous global setup remains in use until a decoder input is saved and applied."
        : "It uses the original source channels and reference."}</p>
      {legacy && <p className="setting-description">Previous channel selection: {legacy.outputs.length
        ? legacy.outputs.map((output) => typeof output === "string" ? output : output.name).join(", ") : "All source channels"} · {legacy.reference.mode} reference.</p>}
      <div className="actions"><button disabled={disabled} onClick={() => edit(emptyProcessingInput(defaultNames))}>Configure decoder input</button>
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
      <div className="channel-binding-panels">
        <section className="channel-panel" aria-label="Available Acquisition channels">
          <div className="channel-panel-heading"><h3>Acquisition channels</h3><span>{availableChannels.length} available</span></div>
          <p className="channel-help">Select source channels on the left. Bind and order algorithm inputs on the right.</p>
          {availableChannels.length ? <ul className="source-channel-list">{availableChannels.map((channel) => {
            const used = rows.find((row) => row.source === channel.name);
            const excluded = value.excluded_channels.includes(channel.name);
            const unavailable = ["trigger", "stim", "ground"].includes(channel.signal_type.toLowerCase());
            return <li key={channel.name} className={used ? "is-bound" : ""}>
              <div><strong>{channel.name}</strong><span>Row {channel.index + 1} · {channel.signal_type} · {channel.unit ?? "Unknown unit"}</span>
                {excluded ? <span>Excluded</span> : used && <span>→ {used.name}</span>}</div>
              {value.reference.mode !== "bipolar" && <button disabled={disabled || excluded || unavailable || (!allChannels && (!!used || rows.some((row) => row.name === channel.name)))}
                aria-label={allChannels ? `Use ${channel.name} only` : `Add ${channel.name}`}
                onClick={() => setRows(allChannels ? [{ name: channel.name, source: channel.name }] : [...rows, { name: channel.name, source: channel.name }])}>
                {allChannels ? "Use only" : used ? "Bound" : "Add →"}</button>}
            </li>;
          })}</ul> : <p className="channel-help">Read source information in Acquisition to choose channels.</p>}
          <p className="channel-help">Trigger is preserved separately{source ? ` (row ${source.trigger_row_index + 1})` : ""}; it is not an algorithm input.</p>
        </section>
        <section className="channel-panel" aria-label="Algorithm input bindings">
          <div className="channel-panel-heading"><h3>Algorithm input channels</h3><span>{value.reference.mode === "bipolar" ? value.outputs.length : rows.length} inputs</span></div>
          <p className="channel-help">Algorithm channel names, in processing order. This set enters Epoch before filters and Decoder.</p>
          {requirements}
          {value.reference.mode !== "bipolar" ? <>
            <div className="channel-selection-mode"><strong>{allChannels ? "All source channels" : "Selected channels"}</strong>
              <button disabled={disabled || allChannels} onClick={() => edit({ ...value, outputs: [] })}>Use all channels</button></div>
            {allChannels && <p className="channel-help">All follows the current Acquisition source, except excluded channels. Editing a binding creates an explicit selection.</p>}
            <ol className="target-channel-list">{rows.map((row, index) => {
              const missing = !!source && !availableChannels.some((channel) => channel.name === row.source);
              return <li key={index} className={missing ? "is-missing" : ""}>
                <div className="binding-row-number">{index + 1}</div>
                <div className="binding-row-content"><div className="binding-fields">
                  <label><span>Acquisition source</span><select aria-label={`Input ${index + 1} source`} disabled={disabled} value={row.source}
                    onChange={(event) => setRows(rows.map((current, target) => target === index ? { ...current, source: event.target.value } : current))}>
                    {!availableChannels.some((channel) => channel.name === row.source) && <option value={row.source}>{row.source}{source ? " (unavailable)" : " (unverified)"}</option>}
                    {availableChannels.map((channel) => <option key={channel.name} value={channel.name}
                      disabled={value.excluded_channels.includes(channel.name) || rows.some((current, target) => target !== index && current.source === channel.name)
                        || ["trigger", "stim", "ground"].includes(channel.signal_type.toLowerCase())}>{channel.name}</option>)}
                  </select></label>
                  <span className="binding-arrow" aria-hidden="true">→</span>
                  <label><span>Algorithm input name</span><input aria-label={`Input ${index + 1} name`} disabled={disabled} value={row.name}
                    onChange={(event) => setRows(rows.map((current, target) => target === index ? { ...current, name: event.target.value } : current))} /></label>
                </div>
                <div className="binding-row-actions"><span className="binding-status">{requiredNames.includes(row.name) ? "Required" : row.name !== row.source ? "Named binding" : "Same name"}</span>
                  <button aria-label={`Move input ${index + 1} up`} disabled={disabled || index === 0} onClick={() => moveRow(index, -1)}>↑</button>
                  <button aria-label={`Move input ${index + 1} down`} disabled={disabled || index === rows.length - 1} onClick={() => moveRow(index, 1)}>↓</button>
                  <button aria-label={`Remove input ${index + 1}`} disabled={disabled || rows.length === 1} title={rows.length === 1 ? "Keep at least one input channel." : undefined}
                    onClick={() => setRows(rows.filter((_, target) => target !== index))}>Remove</button></div>
                {missing && <p className="channel-error">Source {row.source} is unavailable. Choose a channel from the current Acquisition source.</p>}
                {value.excluded_channels.includes(row.source) && <p className="channel-error">Source {row.source} is excluded. Review exclusions below.</p>}
                </div>
              </li>;
            })}</ol>
            {missingRequired.length > 0 && <div className="required-channel-slots"><p>Required channels to bind</p>{missingRequired.map((name) => <label key={name}>
              <strong>{name}</strong><select aria-label={`Bind required ${name}`} disabled={disabled || !source} value="" onChange={(event) => bindRequired(name, event.target.value)}>
                <option value="" disabled>Select Acquisition channel…</option>
                {availableChannels.map((channel) => <option key={channel.name} value={channel.name} disabled={value.excluded_channels.includes(channel.name)
                  || ["trigger", "stim", "ground"].includes(channel.signal_type.toLowerCase())}>{channel.name}{rows.some((row) => row.source === channel.name) ? " (rebind)" : ""}</option>)}
              </select></label>)}<p className="channel-help">Rebinding a selected source replaces its input name. Acquisition labels stay unchanged.</p></div>}
            {rows.length === 1 && <p className="channel-help">Keep at least one selected input. Use “Use all channels” to return to All.</p>}
          </> : <div>{value.outputs.map((output, index) => !isPair(output) ? null : <div className="signal-pair" key={index}>
            <input aria-label={`Pair ${index + 1} name`} disabled={disabled} placeholder="Output name" value={output.name} onChange={(event) => updatePair(index, { name: event.target.value })} />
            <input aria-label={`Pair ${index + 1} positive`} disabled={disabled} placeholder="Positive channel" value={output.positive} onChange={(event) => updatePair(index, { positive: event.target.value })} />
            <input aria-label={`Pair ${index + 1} negative`} disabled={disabled} placeholder="Negative channel" value={output.negative} onChange={(event) => updatePair(index, { negative: event.target.value })} />
            <button disabled={disabled} aria-label={`Remove pair ${index + 1}`} onClick={() => edit({ ...value, outputs: value.outputs.filter((_, row) => row !== index) })}>Remove</button>
          </div>)}<button disabled={disabled} onClick={() => edit({ ...value, outputs: [...value.outputs, { name: "", positive: "", negative: "" }] })}>Add bipolar pair</button></div>}
        </section>
      </div>
      {defaultNames.length > 0 && <>
        <button disabled={disabled || value.reference.mode === "bipolar"} onClick={() => {
          edit({ ...value, outputs: [...defaultNames] });
        }}>Use plugin defaults</button>
        {value.reference.mode === "bipolar" && <p className="setting-description">Plugin defaults contain channel names. Choose a non-bipolar reference mode to use them.</p>}
      </>}
      <label className="setting-row"><span className="setting-label">Excluded channels</span><input disabled={disabled} {...listInput("excluded", value.excluded_channels,
        (excluded_channels) => edit({ ...value, excluded_channels }))} /></label>
      <p className="setting-description">Reference and excluded channels use Acquisition names. Reference channels may be outside the selected inputs. Rereferencing requires known units and an original reference. Renaming an input changes its processing name; it does not move an electrode or change Acquisition metadata.</p>
      {processingInputErrors(value).map((message) => <p className="inline-message error" key={message} role="alert">{message}</p>)}
      <button disabled={disabled || !source || processingInputErrors(value).length > 0} onClick={() => void previewInput()}>Preview mapping</button>
      {preview && <><p className="setting-description">{preview.plan.source.sample_rate} Hz · {preview.plan.output_channels.length} Epoch input channels · trigger preserved</p>
        <ol>{preview.plan.output_channels.map((channel, index) => <li key={channel.name}>{channel.name} · {channel.unit ?? "Unknown unit"} · {channel.reference ?? "Unknown reference"}
          {channel.derivation && ` · ${channel.derivation.join(" − ")}`}
          <span className="setting-description"> · Source rows: {(preview.plan.coefficients[index] ?? []).flatMap((coefficient, row) => coefficient === 0 ? []
            : [`${row + 1} ${preview.plan.source.channels[row].name}`]).join(", ") || "None"}</span></li>)}</ol>
        {preview.warnings.length > 0 && <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
        <p className="setting-description">Preview checks this draft. Starting acquisition validates the selected decoder input against the connected source.</p></>}
    </>}
    {notice && <p className="inline-message" role="status">{notice}</p>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
  </div>;
}
