/** History restores create new entries; module changes still use the runtime review. */
import { useEffect, useState } from "react";
import { pluginConfigApi as api, type ConfigurationModule, type ModuleActivation,
  type ModuleHistory, type PluginProfile, type ProfileHistory } from "./api/plugin-config";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { useI18n } from "./i18n";

export function ConfigurationHistory({ module, profile, runtime, onProfileRestored }: {
  module: ConfigurationModule; profile: PluginProfile | null; runtime: RuntimeConfiguration;
  onProfileRestored: (profile: PluginProfile) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<ModuleHistory[]>([]);
  const [versions, setVersions] = useState<ProfileHistory[]>([]);
  const [detail, setDetail] = useState<ModuleActivation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setBusy(true); setError(""); setDetail(null); setEntries([]); setVersions([]);
    void Promise.all([api.moduleHistory(module), profile ? api.profileHistory(profile.profile_id) : Promise.resolve([])])
      .then(([history, revisions]) => { if (active) { setEntries(history); setVersions(revisions); } })
      .catch((cause) => { if (active) setError(String(cause)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [open, module, profile?.profile_id, profile?.revision, runtime.status?.accepted[module], runtime.completedOperation?.operation_id, refresh]);
  const run = async (action: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await action(); } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return <section className="setting-group configuration-history">
    <button aria-expanded={open} onClick={() => setOpen(!open)}>{t("profiles.history")}</button>
    {open && <>
      <h2>{t("profiles.moduleHistory")}</h2>
      <p className="setting-description">{t("profiles.moduleHistoryHint")}</p>
      {error && <p className="error" role="alert">{error}</p>}
      <button disabled={busy} onClick={() => setRefresh((value) => value + 1)}>{t("profiles.reload")}</button>
      {!busy && !entries.length && <p>{t("profiles.noHistory")}</p>}
      <ol>{entries.map((entry) => <li key={entry.entry_id}>
        <time dateTime={entry.saved_at}>{entry.saved_at}</time> · <code>{entry.revision.slice(0, 12)}</code>
        <p>{entry.binding_summary.join(", ")}</p>
        <div className="actions"><button disabled={busy} onClick={() => void run(async () => setDetail(await api.moduleRevision(module, entry.entry_id)))}>{t("profiles.viewHistory")}</button>
          <button disabled={busy || runtime.blocked} onClick={() => void run(async () => runtime.reviewPlan(await api.moduleRevertPlan(module, entry.entry_id), module))}>{t("profiles.revertHistory")}</button></div>
      </li>)}</ol>
      {detail && <pre tabIndex={0}>{JSON.stringify(detail, null, 2)}</pre>}
      {profile && <>
        <h2>{t("profiles.profileHistory", { name: profile.name })}</h2>
        <p className="setting-description">{t("profiles.profileHistoryHint")}</p>
        <ol>{versions.map((version) => <li key={version.revision}>
          <time dateTime={version.saved_at}>{version.saved_at}</time> · {t("profiles.revision", { revision: version.revision })}
          <details><summary>{t("profiles.viewHistory")}</summary><pre>{JSON.stringify(version.profile, null, 2)}</pre></details>
          <button disabled={busy || version.revision === profile.revision} onClick={() => void run(async () => {
            onProfileRestored(await api.profileRevert(profile, version.revision)); setRefresh((value) => value + 1);
          })}>{t("profiles.restoreProfileVersion")}</button>
        </li>)}</ol>
      </>}
    </>}
  </section>;
}
