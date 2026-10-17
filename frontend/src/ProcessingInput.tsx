/** Decoder profiles own channel mapping and declarations for unreported channel types. */
import { useI18n, translate, type Translate } from "./i18n";
import { useEffect, useRef, useState } from "react";
import { signalConfigApi as api, type ChannelBinding, type ChannelDescriptor, type ProcessingInput as Input,
  type SignalConfiguration, type SignalPreview } from "./api/signal-config";
import { selectionKey, type PluginDefinition, type PluginSelection } from "./api/plugin-config";
import "./ProcessingInput.css";

const declaredTypes = ["eeg", "eog", "ecg", "emg", "misc"] as const;
const reportedType = (channel?: ChannelDescriptor) => channel?.signal_type && channel.signal_type !== "unknown" ? channel.signal_type : null;
export const emptyProcessingInput = (): Input => ({ schema_version: 2, outputs: [] });

export function processingInputErrors(value: Input | null, t: Translate = (key, params) => translate("en", key, params)): string[] {
  if (!value) return [];
  const errors: string[] = [];
  const names = value.outputs.map((output) => output.name);
  const sources = value.outputs.map((output) => output.source_channel_id);
  if (names.some((name) => !name.trim())) errors.push(t("processing.emptyName"));
  if (new Set(names).size !== names.length) errors.push(t("processing.uniqueNames"));
  if (sources.some((source) => !source.trim())) errors.push(t("processing.sourceError"));
  if (new Set(sources).size !== sources.length) errors.push(t("processing.uniqueSources"));
  if (value.outputs.some((output) => output.signal_type != null && !declaredTypes.includes(output.signal_type))) errors.push(t("processing.typeError"));
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
  const { t } = useI18n();
  const [config, setConfig] = useState<SignalConfiguration | null>(null);
  const [preview, setPreview] = useState<SignalPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(false);
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
        request.current += 1; setPreview(null); setPreviewing(false); setNotice(true);
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
    setConfig(null); setPreview(null); setPreviewing(false); setError(""); setNotice(false);
    void refreshSource();
    const onFocus = () => { void refreshSource(); };
    if (typeof window !== "undefined") window.addEventListener("focus", onFocus);
    return () => {
      sourceRequest.current += 1; request.current += 1;
      if (typeof window !== "undefined") window.removeEventListener("focus", onFocus);
    };
  }, [configurationRevision]);
  useEffect(() => { request.current += 1; setPreview(null); setPreviewing(false); setError(""); }, [value, decoderKey, requirementsKey]);

  const source = config?.acquisition_descriptor;
  const binding = config?.source_binding;
  const channels = source?.channels ?? [];
  const allChannels = !!value && !value.outputs.length;
  const rows: ChannelBinding[] = allChannels ? channels.map((channel) => ({ name: channel.name, source_channel_id: channel.channel_id })) : value?.outputs ?? [];
  const disabled = busy || previewing || refreshing;
  const errors = processingInputErrors(value, t);
  const channelLabel = (channel: ChannelDescriptor) => channels.filter((item) => item.name === channel.name).length > 1
    ? t("processing.channelRow", { name: channel.name, row: channel.index + 1 }) : channel.name;
  const defaultNames = channelRequirements?.default_names ?? [];
  const defaultsAvailable = !!source && defaultNames.every((name) => channels.filter((channel) => channel.name === name).length === 1);
  const edit = (next: Input) => { request.current += 1; setPreview(null); setPreviewing(false); setError(""); onChange(next); };
  const setRows = (outputs: ChannelBinding[]) => { if (outputs.length) edit({ schema_version: 2, outputs }); };
  const updateRow = (index: number, patch: Partial<ChannelBinding>) => setRows(rows.map((row, target) => {
    if (target !== index) return row;
    const next = { ...row, ...patch };
    if (next.signal_type == null) delete next.signal_type;
    return next;
  }));
  const moveRow = (index: number, offset: number) => {
    const next = [...rows]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; setRows(next);
  };
  const sourceProblem = rows.some((row) => {
    const channel = channels.find((item) => item.channel_id === row.source_channel_id);
    return !channel || (row.signal_type && reportedType(channel) && row.signal_type !== channel.signal_type);
  });
  const previewInput = async () => {
    if (!value || !config) return;
    const sequence = ++request.current;
    setPreviewing(true); setError(""); setNotice(false); setPreview(null);
    try {
      const current = await refreshSource();
      if (!current || sequence !== request.current) return;
      if (current.source_revision !== config.source_revision) { setNotice(true); return; }
      const next = await api.preview(value, current.source_revision, decoderSelection);
      if (sequence === request.current) {
        if (next.source_revision === current.source_revision) setPreview(next);
        else { await refreshSource(); if (sequence === request.current) setNotice(true); }
      }
    } catch (cause) { if (sequence === request.current) setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { if (sequence === request.current) setPreviewing(false); }
  };

  return <div className="setting-group processing-input"><h2>{t("processing.heading")}</h2>
    <p className="setting-description">{t("processing.intro")}</p>
    <ol className="processing-flow" aria-label={t("processing.flowLabel")}>
      <li>{t("app.acquisition")}</li><li>{t("processing.flowMapping")}</li><li>{t("processing.flowEpoching")}</li>
      <li>{t("profiles.slotFilters")} <small>{t("processing.flowOptional")}</small></li><li>{t("profiles.slotDecoder")}</li>
    </ol>
    <p className="setting-description">{t("processing.pipelineSettings")}</p>
    <div className="input-source" aria-label={t("processing.sourceInfoLabel")}>
      <p>{t("processing.dataSource", { source: binding ? `${binding.profile_name ?? binding.profile_id ?? binding.source_id} · ${binding.source_id}` : t("processing.noSource") })}</p>
      {source ? <p>{t("processing.sourceSummary", { rate: source.sample_rate, count: channels.length })}</p>
        : config?.source_error ? <p className="inline-message error" role="alert">{config.source_error}</p> : <p>{t("processing.sourceMissing")}</p>}
      <button disabled={busy || refreshing} onClick={() => { setNotice(false); void refreshSource(); }}>{refreshing ? t("processing.refreshing") : t("processing.refresh")}</button>
    </div>
    <div className="channel-requirements" aria-label={t("processing.requirementsLabel")}>
      {channelRequirements ? <>
        {channelRequirements.description && <p>{channelRequirements.description}</p>}
        <p>{t("processing.requirements", { type: channelRequirements.signal_type?.toUpperCase() ?? t("processing.unrestricted"), count: channelRequirements.min_count ?? 1 })}</p>
        <p>{t("processing.requiredNames", { names: channelRequirements.required_names?.join(", ") || t("processing.noRequiredNames") })}</p>
      </> : <p>{t("processing.noRequirements")}</p>}
    </div>
    {!value ? <><p className="setting-description">{t("processing.noInput")}</p>
      <button disabled={disabled} onClick={() => edit(emptyProcessingInput())}>{t("processing.configure")}</button></> : <>
      <div className="mapping-actions"><strong>{allChannels ? t("processing.allChannels") : t("processing.selectedChannels")}</strong>
        <button disabled={disabled || allChannels} onClick={() => edit(emptyProcessingInput())}>{t("processing.allChannelsAction")}</button>
        {defaultNames.length > 0 && <button disabled={disabled || !defaultsAvailable} onClick={() => setRows(defaultNames.map((name) => ({
          name, source_channel_id: channels.find((channel) => channel.name === name)!.channel_id,
        })))}>{t("processing.pluginDefaults")}</button>}
      </div>
      {allChannels && <p className="setting-description">{t("processing.allHint")}</p>}
      {defaultNames.length > 0 && !defaultsAvailable && <p className="setting-description">{t("processing.defaultsUnavailable")}</p>}
      <div className="mapping-table"><table aria-label={t("processing.bindingLabel")}>
        <thead><tr><th>{t("processing.order")}</th><th>{t("processing.source")}</th><th>{t("processing.inputName")}</th><th>{t("processing.channelType")}</th><th>{t("processing.actions")}</th></tr></thead>
        <tbody>{rows.map((row, index) => {
          const channel = channels.find((item) => item.channel_id === row.source_channel_id);
          const knownType = reportedType(channel);
          const conflict = row.signal_type && knownType && row.signal_type !== knownType;
          return <tr key={index}><td>{index + 1}</td>
            <td><select aria-label={t("processing.inputSourceLabel", { row: index + 1 })} disabled={disabled} value={row.source_channel_id}
              onChange={(event) => updateRow(index, { source_channel_id: event.target.value, signal_type: undefined })}>
              {!channel && <option value={row.source_channel_id}>{row.source_channel_id}{t("processing.unavailable")}</option>}
              {channels.map((item) => <option key={item.channel_id} value={item.channel_id}
                disabled={rows.some((current, target) => target !== index && current.source_channel_id === item.channel_id)}>{channelLabel(item)}</option>)}
            </select>{!channel && source && <p className="channel-error">{t("processing.missingSource", { source: row.source_channel_id })}</p>}</td>
            <td><input aria-label={t("processing.inputNameLabel", { row: index + 1 })} disabled={disabled} value={row.name}
              onChange={(event) => updateRow(index, { name: event.target.value })} /></td>
            <td>{knownType ? <><span>{knownType.toUpperCase()}</span><small>{t("processing.reportedType")}</small>
              {row.signal_type && <button disabled={disabled} onClick={() => updateRow(index, { signal_type: undefined })}>{t("processing.useReportedType")}</button>}
              {conflict && <p className="channel-error">{t("processing.typeConflict", { type: row.signal_type!.toUpperCase() })}</p>}</> : <>
              <select aria-label={t("processing.inputTypeLabel", { row: index + 1 })} disabled={disabled} value={row.signal_type ?? ""}
                onChange={(event) => updateRow(index, { signal_type: event.target.value ? event.target.value as ChannelBinding["signal_type"] : undefined })}>
                <option value="">{t("processing.unknownType")}</option>{declaredTypes.map((type) => <option key={type} value={type}>{type.toUpperCase()}</option>)}
              </select><small>{row.signal_type ? t("processing.declaredType") : t("processing.typeHint")}</small>
            </>}</td>
            <td><div className="mapping-row-actions">
              {allChannels && <button disabled={disabled} aria-label={t("processing.useOnlyLabel", { name: channel ? channelLabel(channel) : row.name })}
                onClick={() => setRows([row])}>{t("processing.useOnly")}</button>}
              <button aria-label={t("processing.moveUpLabel", { row: index + 1 })} disabled={disabled || index === 0} onClick={() => moveRow(index, -1)}>↑</button>
              <button aria-label={t("processing.moveDownLabel", { row: index + 1 })} disabled={disabled || index === rows.length - 1} onClick={() => moveRow(index, 1)}>↓</button>
              <button aria-label={t("processing.removeInputLabel", { row: index + 1 })} disabled={disabled || rows.length === 1}
                onClick={() => setRows(rows.filter((_, target) => target !== index))}>{t("processing.remove")}</button>
            </div></td></tr>;
        })}</tbody>
      </table></div>
      <label className="mapping-add"><span>{t("processing.addSource")}</span><select aria-label={t("processing.addSource")} disabled={disabled || !source || rows.length >= channels.length}
        value="" onChange={(event) => { const channel = channels.find((item) => item.channel_id === event.target.value);
          if (channel) setRows([...rows, { name: channel.name, source_channel_id: channel.channel_id }]); }}>
        <option value="" disabled>{t("processing.selectSource")}</option>
        {channels.map((channel) => <option key={channel.channel_id} value={channel.channel_id}
          disabled={rows.some((row) => row.source_channel_id === channel.channel_id)}>{channelLabel(channel)}</option>)}
      </select></label>
      <p className="setting-description">{t("processing.mappingHint")}</p>
      {errors.map((message) => <p className="inline-message error" key={message} role="alert">{message}</p>)}
      <button disabled={disabled || !source || sourceProblem || errors.length > 0} onClick={() => void previewInput()}>{t("processing.preview")}</button>
      {preview && <div className="mapping-preview"><p>{t("processing.previewSummary", { count: preview.plan.output_channels.length })}</p>
        <ol>{preview.plan.output_channels.map((channel, index) => <li key={index}>
          {preview.plan.source.channels[preview.plan.source_indices[index]]?.name} → {channel.name} · {(channel.signal_type ?? "unknown").toUpperCase()}
        </li>)}</ol>
        {preview.warnings.length > 0 && <ul>{preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>}
      </div>}
    </>}
    {notice && <p className="inline-message" role="status">{t("processing.sourceChanged")}</p>}
    {error && <p className="inline-message error" role="alert">{error}</p>}
  </div>;
}
