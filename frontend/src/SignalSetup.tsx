/** Channel bindings and rereferencing share one revision-checked setup. */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { signalConfigApi as api, type BipolarOutput, type SignalAssignment, type SignalConfiguration,
  type SignalPreview, type SignalSetup as Setup, type SourceMetadata } from "./api/signal-config";
import { pluginConfigApi } from "./api/plugin-config";
import { ApiError } from "./api/client";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
import "./SignalSetup.css";

const names = (value: string) => value.split(",").map((name) => name.trim()).filter(Boolean);
const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
const emptySetup = (): Setup => ({ schema_version: 1, source_id: "", assignments: [], outputs: [],
  reference: { mode: "original", channels: [] }, excluded_channels: [] });
const assignmentsFor = (source: SourceMetadata): SignalAssignment[] => source.channels.map((channel) => ({
  source_index: channel.index, expected_name: channel.name, electrode: channel.name,
}));
const summary = (setup: Setup | null) => setup
  ? `${setup.outputs.length ? setup.outputs.map((output) => typeof output === "string" ? output : output.name).join(", ") : "All mapped channels"} · ${setup.reference.mode} reference`
  : "Original source channels and reference";

export function SignalSetup({ navigation, runtime, onDirtyChange, onBusyChange }: {
  navigation?: ReactNode;
  runtime: RuntimeConfiguration;
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [config, setConfig] = useState<SignalConfiguration | null>(null);
  const [draft, setDraft] = useState<Setup>(emptySetup);
  const [baseline, setBaseline] = useState<Setup>(emptySetup);
  const [metadata, setMetadata] = useState<SourceMetadata | null>(null);
  const [metadataError, setMetadataError] = useState("");
  const [templates, setTemplates] = useState<string[]>([]);
  const [positions, setPositions] = useState<string[]>([]);
  const [preview, setPreview] = useState<SignalPreview | null>(null);
  const [listInputs, setListInputs] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
  const sequence = useRef(0);
  const dirty = !same(draft, baseline);
  const source = metadata ?? draft.manual_metadata ?? null;
  const assignments = draft.assignments;
  const bipolar = draft.reference.mode === "bipolar";
  const hasSavedChanges = config && !same(config.setup, config.accepted_setup);

  useEffect(() => {
    const id = ++sequence.current;
    void Promise.allSettled([api.get(), api.metadata(), api.templates()]).then((results) => {
      if (id !== sequence.current) return;
      const [saved, observed, availableTemplates] = results;
      const observedSource = observed.status === "fulfilled" ? observed.value
        : saved.status === "fulfilled" ? saved.value.source_metadata ?? null : null;
      setMetadata(observedSource);
      setMetadataError(observed.status === "rejected" ? String(observed.reason) : "");
      if (saved.status === "fulfilled") {
        setConfig(saved.value);
        const next = saved.value.setup ?? (observedSource ? { ...emptySetup(), source_id: observedSource.source_id,
          assignments: assignmentsFor(observedSource) } : { ...emptySetup(), source_id: saved.value.selected_source_id ?? "" });
        setDraft(structuredClone(next)); setBaseline(structuredClone(next));
      } else setError(String(saved.reason));
      if (availableTemplates.status === "fulfilled") setTemplates(availableTemplates.value.templates);
      setBusy(false);
    });
    return () => { sequence.current += 1; };
  }, []);
  useEffect(() => {
    let active = true;
    if (!draft.template) { setPositions([]); return; }
    void api.template(draft.template).then((template) => { if (active) setPositions(Object.keys(template.positions)); })
      .catch((cause) => { if (active) { setPositions([]); setError(String(cause)); } });
    return () => { active = false; };
  }, [draft.template]);
  useEffect(() => {
    let active = true;
    void api.get().then((next) => { if (active) setConfig((previous) => previous ? { ...previous,
      accepted_setup: next.accepted_setup, active_setup: next.active_setup, readiness: next.readiness } : previous); }).catch(() => {});
    return () => { active = false; };
  }, [runtime.status?.accepted_revision, runtime.status?.applied_revision]);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => () => { onDirtyChange(false); onBusyChange(false); }, [onDirtyChange, onBusyChange]);

  const edit = (next: Setup) => { setDraft(next); setPreview(null); setNotice(""); };
  const changeAssignment = (index: number, patch: Partial<SignalAssignment>) => edit({ ...draft,
    assignments: assignments.map((assignment, row) => {
      if (row !== index) return assignment;
      const next = { ...assignment, ...patch };
      if (!next.unit) delete next.unit;
      return next;
    }) });
  const listInput = (key: string, fallback: string[], onChange: (value: string) => void) => ({
    value: listInputs[key] ?? fallback.join(", "),
    onChange: (event: { target: { value: string } }) => {
      setListInputs((previous) => ({ ...previous, [key]: event.target.value }));
      onChange(event.target.value);
    },
  });
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await action(); }
    catch (cause) { setError(cause instanceof ApiError && cause.status === 409
      ? `${cause.message} Your edits are preserved. Reload saved setup before saving again.`
      : cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  };
  const discardThen = (action: () => void) => {
    if (!dirty) { action(); return; }
    setDialog({ title: "Unsaved channel setup", message: "Discard unsaved channel and reference changes?",
      confirmLabel: "Discard changes", onConfirm: () => { setDialog(null); action(); } });
  };
  const reload = () => discardThen(() => void run(async () => {
    const next = await api.get();
    const setup = next.setup ?? { ...emptySetup(), source_id: next.selected_source_id ?? "" };
    setConfig(next); setDraft(structuredClone(setup)); setBaseline(structuredClone(setup)); setPreview(null); setListInputs({});
  }));
  const save = () => void run(async () => {
    if (!config) return;
    const next = await api.save(draft, config.revision);
    const saved = next.setup ?? draft;
    setConfig(next); setDraft(structuredClone(saved)); setBaseline(structuredClone(saved));
    setNotice("Channel setup saved. Review and apply it to change the runtime.");
  });
  const review = () => void run(async () => {
    const activation = await pluginConfigApi.activation();
    if (!activation || activation.bindings.some((binding) => !binding.profile_id)) {
      throw new Error("Save profiles for the selected plugins before applying channel setup.");
    }
    await runtime.review({ bindings: activation.bindings.map((binding) => ({ slot: binding.slot, profile_id: binding.profile_id! })),
      expected_revision: activation.revision, refresh_slots: [], include_host_settings: false, include_signal_setup: true });
  });
  const useObserved = () => {
    if (!metadata) return;
    discardThen(() => { setListInputs({}); edit({ ...emptySetup(), source_id: metadata.source_id, assignments: assignmentsFor(metadata) }); });
  };
  const updatePair = (index: number, patch: Partial<BipolarOutput>) => edit({ ...draft,
    outputs: draft.outputs.map((output, row) => row === index && typeof output !== "string" ? { ...output, ...patch } : output) });

  return <section className="settings-workspace signal-setup" aria-label="Channels and montage">
    <aside className="explorer">{navigation}<div className="explorer-footer"><p>Bind source rows to electrodes, then prepare the ordered signals for processing.</p></div></aside>
    <div className="settings-editor">
      <header className="editor-heading"><div><p className="eyebrow">Setup / Signal</p><h1>Channels and montage</h1>
        <p>Save the mapping, preview the result, then review and apply.</p></div>
        <button disabled={busy} onClick={reload}>Reload saved setup</button></header>
      {error && <p className="inline-message error" role="alert">{error}</p>}
      {notice && <p className="inline-message success" role="status">{notice}</p>}
      <div className="signal-state" aria-label="Channel configuration state">
        <p><strong>Active:</strong> {runtime.status?.running ? summary(config?.active_setup ?? null) : "Engine stopped"}</p>
        <p><strong>Selected:</strong> {summary(config?.accepted_setup ?? null)}</p>
        {hasSavedChanges && <p>Saved channel setup has changes that are not selected.</p>}
      </div>
      <div className="settings-scroll">
        <div className="setting-group"><h2>Source information</h2>
          <div className="actions"><button disabled={busy} onClick={() => void run(async () => {
            try { setMetadata(await api.metadata(true)); setMetadataError(""); setPreview(null); }
            catch (cause) { setMetadata(null); setMetadataError(String(cause)); throw cause; }
          })}>Refresh source metadata</button>
            <button disabled={busy || !metadata} onClick={useObserved}>Use source channels</button></div>
          {metadata ? <p className="setting-description">{metadata.source_id} · {metadata.sample_rate} Hz · {metadata.channels.length} signal channels · trigger row {metadata.trigger_row_index + 1}
            {" · "}{metadata.provenance ?? "Unknown provenance"}{metadata.native_event && ` · ${metadata.native_event}`}</p>
            : <p className="setting-description">Source metadata is unavailable. Read the source or fill missing montage information in its Acquisition profile. Offline preview does not validate a connected device.</p>}
          {metadataError && <p className="setting-description">{metadataError}</p>}
          <label className="setting-row"><span className="setting-label">Source identifier</span>
            <input value={draft.source_id} disabled={busy} onChange={(event) => edit({ ...draft, source_id: event.target.value,
              ...(draft.manual_metadata ? { manual_metadata: { ...draft.manual_metadata, source_id: event.target.value } } : {}) })} /></label>
          <p className="setting-description">This identifies the selected acquisition source. Row numbers below start at 1.</p>
          <p className="setting-description">Acquisition montage is saved with the source profile. Channel selection and rereferencing are configured below.</p>
          {draft.manual_metadata && <p className="setting-description">This setup retains a legacy manual source declaration. New declarations belong to the Acquisition profile.</p>}
        </div>
        <div className="setting-group"><h2>Electrode bindings</h2>
          <label className="setting-row"><span className="setting-label">Electrode template</span>
            <select value={draft.template ?? ""} disabled={busy} onChange={(event) => {
              const { template: _, ...next } = draft; edit(event.target.value ? { ...next, template: event.target.value } : next);
            }}><option value="">No template</option>{draft.template && !templates.includes(draft.template)
              && <option value={draft.template}>{draft.template} (unavailable)</option>}
              {templates.map((template) => <option key={template} value={template}>{template}</option>)}</select></label>
          <p className="setting-description">Templates supply positions only. Choose each electrode explicitly; unlocated channels remain usable.</p>
          <datalist id="signal-electrodes">{positions.map((position) => <option key={position} value={position} />)}</datalist>
          <div className="signal-table"><table><thead><tr><th>Row</th><th>Source label</th><th>Electrode</th><th>Type</th><th>Unit</th><th>Position</th><th>Provenance</th></tr></thead>
            <tbody>{assignments.map((assignment, index) => {
              const channel = source?.channels.find((channel) => channel.index === assignment.source_index);
              return <tr key={assignment.source_index}><td>{assignment.source_index + 1}</td><td>{channel?.name ?? assignment.expected_name ?? "Unknown"}
                {channel && assignment.expected_name && assignment.expected_name !== channel.name
                  && <span className="error"> · Expected {assignment.expected_name}</span>}</td>
                <td><input aria-label={`Row ${assignment.source_index + 1} electrode`} list="signal-electrodes" disabled={busy} value={assignment.electrode}
                  onChange={(event) => changeAssignment(index, { electrode: event.target.value })} /></td>
                <td><select aria-label={`Row ${assignment.source_index + 1} type`} disabled={busy} value={assignment.signal_type ?? channel?.signal_type ?? "unknown"}
                  onChange={(event) => changeAssignment(index, { signal_type: event.target.value })}>
                  {["unknown", "eeg", "eog", "ecg", "emg", "misc", "reference", "ground"].map((type) => <option key={type} value={type}>{type}</option>)}</select></td>
                <td><input aria-label={`Row ${assignment.source_index + 1} unit`} disabled={busy} placeholder="Unknown" value={assignment.unit ?? channel?.unit ?? ""}
                  onChange={(event) => changeAssignment(index, { unit: event.target.value })} /></td>
                <td>{channel?.position ? channel.position_provenance ?? "Source" : positions.includes(assignment.electrode) ? "Template" : "Unknown"}</td>
                <td>{assignment.unit || assignment.signal_type ? "Type/unit declared" : source?.provenance ?? "Unknown"}</td></tr>;
            })}</tbody></table></div>
          {!assignments.length && <p className="setting-description">Use source channels or enter a manual source declaration to create bindings.</p>}
        </div>
        <div className="setting-group"><h2>Algorithm input</h2>
          <label className="setting-row"><span className="setting-label">Original reference</span><input disabled={busy} placeholder={source?.original_reference ?? "Unknown"}
            value={draft.original_reference ?? ""} onChange={(event) => { const { original_reference: _, ...next } = draft;
              edit(event.target.value ? { ...next, original_reference: event.target.value } : next); }} /></label>
          <label className="setting-row"><span className="setting-label">Reference mode</span><select disabled={busy} value={draft.reference.mode}
            onChange={(event) => { const mode = event.target.value as Setup["reference"]["mode"];
              setListInputs((previous) => { const next = { ...previous }; delete next.reference; if (mode === "bipolar" || bipolar) delete next.outputs; return next; });
              edit({ ...draft, reference: { mode, channels: [] }, outputs: mode === "bipolar" || bipolar ? [] : draft.outputs }); }}>
            <option value="original">Keep original reference</option><option value="single">Single sampled channel</option>
            <option value="average">Average of selected channels</option><option value="bipolar">Bipolar pairs</option></select></label>
          {(draft.reference.mode === "single" || draft.reference.mode === "average") && <label className="setting-row"><span className="setting-label">Reference channels</span>
            <input disabled={busy} placeholder="M1, M2" {...listInput("reference", draft.reference.channels,
              (value) => edit({ ...draft, reference: { ...draft.reference, channels: names(value) } }))} /></label>}
          {!bipolar ? <label className="setting-row"><span className="setting-label">Ordered output electrodes</span>
            <input disabled={busy} placeholder="All mapped channels" {...listInput("outputs", draft.outputs.filter((output): output is string => typeof output === "string"),
              (value) => edit({ ...draft, outputs: names(value) }))} /></label>
            : <div className="signal-pairs">{draft.outputs.map((output, index) => typeof output === "string" ? null : <div className="signal-pair" key={index}>
              <input aria-label={`Pair ${index + 1} name`} disabled={busy} placeholder="Output name" value={output.name} onChange={(event) => updatePair(index, { name: event.target.value })} />
              <input aria-label={`Pair ${index + 1} positive`} disabled={busy} placeholder="Positive electrode" value={output.positive} onChange={(event) => updatePair(index, { positive: event.target.value })} />
              <input aria-label={`Pair ${index + 1} negative`} disabled={busy} placeholder="Negative electrode" value={output.negative} onChange={(event) => updatePair(index, { negative: event.target.value })} />
              <button disabled={busy} aria-label={`Remove pair ${index + 1}`} onClick={() => edit({ ...draft, outputs: draft.outputs.filter((_, row) => row !== index) })}>Remove</button>
            </div>)}<button disabled={busy} onClick={() => edit({ ...draft, outputs: [...draft.outputs, { name: "", positive: "", negative: "" }] })}>Add bipolar pair</button></div>}
          <label className="setting-row"><span className="setting-label">Excluded electrodes</span><input disabled={busy} {...listInput("excluded", draft.excluded_channels,
            (value) => edit({ ...draft, excluded_channels: names(value) }))} /></label>
          <p className="setting-description">Lists use comma-separated electrode names. Reference channels may be outside the output list. Rereferencing requires known units and an original reference.</p>
        </div>
        <div className="setting-group"><h2>Mapping preview</h2>
          <button disabled={busy || !draft.source_id} onClick={() => void run(async () => setPreview(await api.preview(draft, metadata ?? undefined)))}>Preview mapping</button>
          {preview && <><p className="setting-description">{preview.plan.source.sample_rate} Hz · {preview.plan.output_channels.length} algorithm channels · trigger preserved</p>
            <ol>{preview.plan.output_channels.map((channel, index) => <li key={channel.name}>{channel.name} · {channel.unit ?? "Unknown unit"} · {channel.reference ?? "Unknown reference"}
              {channel.derivation && ` · ${channel.derivation.join(" − ")}`}
              <span className="setting-description"> · Source rows: {(preview.plan.coefficients[index] ?? []).flatMap((coefficient, row) => coefficient === 0 ? []
                : [`${row + 1} ${preview.plan.source.channels[row].name}`]).join(", ") || "None"}</span></li>)}</ol>
            {preview.warnings.length > 0 && <ul className="signal-warnings">{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
            <p className="setting-description">Preview checks this draft. Starting acquisition validates the selected setup against the connected source.</p></>}
        </div>
      </div>
      <footer className="editor-footer"><span className="save-state">{dirty ? "Unsaved changes" : config?.setup ? "All changes saved" : "No saved channel setup"}</span>
        <div className="actions"><button disabled={busy || !dirty} onClick={() => discardThen(() => { setDraft(structuredClone(baseline)); setPreview(null); setListInputs({}); })}>Discard</button>
          <button className="primary" disabled={busy || !config || (!dirty && !!config.setup) || !draft.source_id} onClick={save}>Save channel setup</button>
          <button disabled={busy || dirty || runtime.blocked || !config?.setup} onClick={review}>Review and apply channel setup</button></div></footer>
    </div>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </section>;
}
