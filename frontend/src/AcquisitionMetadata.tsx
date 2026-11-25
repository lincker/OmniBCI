/** Source observations, dimension schema helpers, and experiment expectation comparison. */
import { useI18n, type Translate } from "./i18n";
import type { DataSourceMetadata } from "./api/client";
import { SchemaForm, normalizedParameters, sameParameters, type ConfigurationSchema } from "./SchemaForm";
import { selectionKey, type ModuleActivation } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import type { AcquisitionController } from "./hooks/useAcquisition";
import "./AcquisitionMetadata.css";

const displayValue = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value);
const observedFields = new Set(["sample_rate", "channel_count", "channel_labels"]);

export const connectionParameters = (parameters: Record<string, unknown>) => Object.fromEntries(
  Object.entries(parameters).filter(([key]) => !observedFields.has(key)));

export function isReportedSourceSchema(schema: ConfigurationSchema | null | undefined): boolean {
  const properties = schema?.properties;
  if (!properties) return false;
  const required = new Set(schema.required ?? []);
  const declared = [...observedFields].filter((key) => key in properties);
  return declared.length > 0 && declared.every((key) => !required.has(key));
}

export function stripObservedSourceDefaults(
  schema: ConfigurationSchema | null | undefined,
  defaults: Record<string, unknown>,
): Record<string, unknown> {
  return isReportedSourceSchema(schema) ? connectionParameters(defaults) : defaults;
}

export function acquisitionConnectionSchema(
  schema: ConfigurationSchema | null | undefined,
): ConfigurationSchema | null | undefined {
  if (!schema) return schema;
  return {
    ...schema,
    properties: Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([key]) => !observedFields.has(key))),
  };
}

function supportsChannelLabels(schema: ConfigurationSchema | null | undefined): boolean {
  return !!schema?.properties && "channel_labels" in schema.properties;
}

export function observedParameters(
  metadata: DataSourceMetadata,
  schema: ConfigurationSchema | null | undefined,
  currentParameters: Record<string, unknown>,
): Record<string, unknown> {
  const output = metadata.descriptor!;
  const parameters: Record<string, unknown> = {
    ...connectionParameters(currentParameters),
    sample_rate: output.sample_rate,
    channel_count: output.output_row_count,
  };
  if (supportsChannelLabels(schema)) {
    try {
      const labels: unknown = JSON.parse(metadata.metadata)?.channel_labels;
      if (Array.isArray(labels) && labels.length === output.output_row_count && labels.every((label) => typeof label === "string")) {
        parameters.channel_labels = labels;
      }
    } catch { /* Source text without valid JSON has no importable labels. */ }
  }
  return parameters;
}

export function deriveAcquisitionObservationState({
  schema,
  plugin,
  parameters,
  activation,
  runtime,
  acquisition,
  engineAvailable,
}: {
  schema: ConfigurationSchema | null | undefined;
  plugin: string;
  parameters: Record<string, unknown>;
  activation: ModuleActivation | null;
  runtime: RuntimeConfiguration;
  acquisition: AcquisitionController;
  engineAvailable: boolean;
}) {
  const reportedSource = isReportedSourceSchema(schema);
  const selectedBinding = activation?.bindings.find((item) => item.slot === "acquisition.source");
  const sourceStatus = !engineAvailable || acquisition.error ? null : acquisition.data;
  const runtimeState = runtime.state("acquisition");
  const sourceSelected = !!selectedBinding && runtime.status?.accepted.acquisition === activation?.revision
    && selectionKey(selectedBinding.selection) === plugin
    && sameParameters(selectedBinding.parameters, normalizedParameters(parameters, schema));
  const sourceMatches = sourceSelected && runtime.status?.running === true
    && runtime.status.applied?.acquisition === activation?.revision
    && runtime.status.applied?.engine === runtime.status.accepted.engine;
  const canRead = !!sourceStatus?.connected && !sourceStatus.streaming && sourceMatches && !runtimeState.blocked;
  const visibleMetadata = sourceStatus?.connected && sourceMatches && !runtimeState.blocked
    ? (acquisition.metadata ?? null)
    : null;
  const canUseObserved = canRead && reportedSource && !!visibleMetadata?.descriptor
    && !!visibleMetadata.comparison && visibleMetadata.comparison.status !== "unverified"
    && visibleMetadata.connection_id === sourceStatus?.connection_id
    && visibleMetadata.descriptor.connection_id === visibleMetadata.connection_id
    && visibleMetadata.descriptor.source_id === visibleMetadata.source_id;
  return { reportedSource, sourceStatus, sourceSelected, sourceMatches, canRead, visibleMetadata, canUseObserved };
}

export function renderAcquisitionDimensions({
  schema,
  sourceMode,
  parameters,
  disabled,
  onChange,
  t,
}: {
  schema: ConfigurationSchema | null | undefined;
  sourceMode: string;
  parameters: Record<string, unknown>;
  disabled: boolean;
  onChange: (parameters: Record<string, unknown>) => void;
  t: Translate;
}) {
  if (!schema) return null;
  const properties = Object.fromEntries(Object.entries(schema.properties ?? {}).filter(([key]) => observedFields.has(key)));
  if (Object.keys(properties).length === 0) return null;
  const reportedSource = isReportedSourceSchema(schema);
  const dimensionSchema = { ...schema, properties };
  return <details className="setting-group" open={sourceMode === "new"}>
    <summary>{t(reportedSource ? "profiles.expectedParameters" : "profiles.dimensionDefaults")}</summary>
    <p className="setting-description">{t(reportedSource ? sourceMode === "new" ? "profiles.newSourceHint" : "profiles.expectedParametersHint" : "profiles.dimensionDefaultsHint")}</p>
    {reportedSource ? <dl className="expected-source-parameters">
      <div><dt>{t("metadata.sampleRate")}</dt><dd>{String(parameters.sample_rate ?? t("metadata.notConfirmed"))}</dd></div>
      <div><dt>{t("metadata.outputRows")}</dt><dd>{String(parameters.channel_count ?? t("metadata.notConfirmed"))}</dd></div>
      {supportsChannelLabels(schema) && <div><dt>{t("metadata.channelLabels")}</dt>
        <dd>{Array.isArray(parameters.channel_labels) ? parameters.channel_labels.join(", ") : t("metadata.notConfirmed")}</dd></div>}
    </dl> : <SchemaForm schema={dimensionSchema} value={parameters} disabled={disabled} onChange={onChange} />}
  </details>;
}

export function AcquisitionMetadata({ value, busy, canRead, readHint, onRead, canUseObserved = false, onUseObserved, showComparison = true }: {
  value: DataSourceMetadata | null;
  busy: boolean;
  canRead: boolean;
  readHint: string;
  onRead: () => void;
  canUseObserved?: boolean;
  onUseObserved?: () => void;
  showComparison?: boolean;
}) {
  const { t } = useI18n();
  const descriptor = value?.descriptor;
  const fields: [string, unknown][] = [
    [t("metadata.source"), descriptor?.source_id],
    [t("metadata.sampleRate"), descriptor?.sample_rate],
    [t("metadata.channelCount"), descriptor?.signal_channel_count],
    [t("metadata.outputRows"), descriptor?.output_row_count],
  ];
  const channels = descriptor?.channels ?? [];
  const comparison = value?.comparison;
  const mismatched = comparison?.status === "mismatched";

  return <div className="setting-group acquisition-metadata"><h2>{t("metadata.heading")}</h2>
    <div className="actions"><button disabled={busy || !canRead} onClick={onRead}>{t("metadata.read")}</button></div>
    <p className="setting-description">{readHint}</p>
    {value ? <>
      <pre className="source-metadata-raw" aria-label={t("metadata.raw")}>{value.metadata}</pre>
      {descriptor ? <>
      {showComparison && comparison && <div className="source-expectations">
        <h3>{t("metadata.comparison")}</h3>
        <p role={mismatched ? "alert" : "status"} className={`setting-description${mismatched ? " source-mismatch" : ""}`}>{t(`metadata.comparison.${comparison.status}`)}</p>
        <table aria-label={t("metadata.comparison")}>
          <thead><tr><th>{t("metadata.parameter")}</th><th>{t("metadata.expected")}</th>
            <th>{t(comparison.status === "unverified" ? "metadata.declared" : "metadata.observed")}</th></tr></thead>
          <tbody>{([["sample_rate", "metadata.sampleRate"], ["channel_count", "metadata.outputRows"]] as const).map(([key, label]) =>
            <tr key={key} className={mismatched && comparison[key].expected != null && comparison[key].expected !== comparison[key].actual ? "source-mismatch" : undefined}><th scope="row">{t(label)}</th>
              <td>{comparison[key].expected ?? t("metadata.notConfirmed")}</td><td>{comparison[key].actual}</td></tr>)}</tbody>
        </table>
        {comparison.status !== "unverified" && onUseObserved && <>
          <p className="setting-description">{t("metadata.correctHint")}</p>
          <div className="actions">
            <button disabled={busy || !canUseObserved} onClick={onUseObserved}>{t("metadata.useObserved")}</button>
          </div>
        </>}
      </div>}
      <h3>{t("metadata.adapterOutput")}</h3>
      {descriptor.provenance === "user-declared" && <p className="setting-description">{t("metadata.configured")}</p>}
      <dl className="source-metadata-summary" aria-label={t("metadata.adapterOutput")}>
        {fields.filter(([, field]) => field != null).map(([label, field]) => <div key={label}>
          <dt>{label}</dt><dd>{displayValue(field)}</dd>
        </div>)}
      </dl>
      {descriptor?.output_row_count != null && <p className="setting-description">{t("metadata.outputHint")}</p>}
      {channels.length > 0 && <div className="source-metadata-table"><table aria-label={t("metadata.channelLabels")}>
        <thead><tr><th>{t("metadata.row")}</th><th>{t("metadata.channelId")}</th><th>{t("metadata.channelLabel")}</th>
          <th>{t("metadata.channelType")}</th><th>{t("metadata.unit")}</th></tr></thead>
        <tbody>{channels.map((channel) => <tr key={channel.channel_id}><td>{channel.index + 1}</td><td>{channel.channel_id}</td>
          <td>{channel.name}</td><td>{channel.signal_type}</td><td>{channel.unit ?? "—"}</td></tr>)}</tbody>
      </table></div>}
      </> : <p className="setting-description">{t("metadata.descriptorUnavailable")}</p>}
    </> : <p className="setting-description">{t("metadata.empty")}</p>}
  </div>;
}
