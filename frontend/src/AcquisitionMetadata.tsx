/** Source observations are immutable. */
import { useI18n } from "./i18n";
import type { DataSourceMetadata } from "./api/client";
import "./AcquisitionMetadata.css";

const displayValue = (value: unknown) => typeof value === "string" ? value : JSON.stringify(value);

export function AcquisitionMetadata({ value, busy, canRead, readHint, onRead }: {
  value: DataSourceMetadata | null;
  busy: boolean;
  canRead: boolean;
  readHint: string;
  onRead: () => void;
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

  return <div className="setting-group acquisition-metadata"><h2>{t("metadata.heading")}</h2>
    <div className="actions"><button disabled={busy || !canRead} onClick={onRead}>{t("metadata.read")}</button></div>
    <p className="setting-description">{readHint}</p>
    {value ? <>
      <pre className="source-metadata-raw" aria-label={t("metadata.raw")}>{value.metadata}</pre>
      {descriptor ? <>
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
