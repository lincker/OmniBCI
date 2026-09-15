/**
 * Layered configuration editor for defaults and named profiles.
 *
 * The table distinguishes default, profile override, and effective values.
 * Editing the default scope writes settings.toml; editing a profile writes its
 * profile file. Engine-restart settings display a restart notice after saving.
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ConfigData, type ParadigmInfo, type ProfileList } from "./api/client";
import {
  translateConfigDescription,
  translatePluginName,
  useI18n,
} from "./i18n";

const DEFAULT = "__default__";

function parseInput(raw: string, type: string): unknown {
  if (type === "int") return Number.parseInt(raw, 10);
  if (type === "float") return Number.parseFloat(raw);
  if (type === "bool") return raw === "true";
  if (type === "list[str]")
    return raw.split(",").map((s) => s.trim()).filter((s) => s.length > 0);
  return raw;
}

function show(v: unknown): string {
  if (v == null) return "";
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}

export function ConfigCard({ refreshKey = 0, engineRunning = false }: { refreshKey?: number; engineRunning?: boolean }) {
  const { t } = useI18n();
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [profiles, setProfiles] = useState<ProfileList | null>(null);
  // Plugin-type options keyed by configuration key.
  const [pluginOptions, setPluginOptions] = useState<Record<string, ParadigmInfo[]>>({});
  const [selected, setSelected] = useState<string>(DEFAULT);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [moduleOwnedKeys, setModuleOwnedKeys] = useState<string[]>([]);

  // Configuration key to plugin category.
  const PLUGIN_CATEGORIES: Record<string, string> = {
    "STIMULATION.paradigm":      "paradigm",
    "ACQUISITION.signal_source": "acquisition",
    "ALGORITHM.decoder":         "decoder",
  };

  const reload = useCallback(async () => {
    const [cfg, profs, acquisition, ...pluginResps] = await Promise.all([
      api.getConfig().catch(() => null),
      api.listProfiles().catch(() => null),
      api.getAcquisitionConfig().catch(() => null),
      ...Object.entries(PLUGIN_CATEGORIES).map(([, cat]) =>
        api.listPlugins(cat).catch(() => null)
      ),
    ]);
    setConfig(cfg);
    setProfiles(profs);
    const fields = acquisition?.plugins.find((p) => p.plugin_type === acquisition.effective_plugin_type)?.fields ?? [];
    setModuleOwnedKeys(acquisition?.active_profile_id
      ? ["ACQUISITION.signal_source", ...fields.map((field) => `ACQUISITION.${field.key}`)] : []);
    const options: Record<string, ParadigmInfo[]> = {};
    Object.keys(PLUGIN_CATEGORIES).forEach((key, i) => {
      const resp = pluginResps[i];
      if (resp) options[key] = resp.paradigms;
    });
    setPluginOptions(options);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  const run = async (fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      if (okMsg) setNotice(okMsg);
      setDraft({});
      await reload();
    } catch (e) {
      setError(e instanceof ApiError ? `[${e.code}] ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (!config) return;
    const updates: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(draft)) {
      const entry = config.entries.find((e) => e.key === key);
      if (entry) updates[key] = parseInput(raw, entry.type);
    }
    if (Object.keys(updates).length === 0) return;
    const scope = selected === DEFAULT ? "default" : selected;
    void run(async () => {
      const result = await api.patchConfig(updates, scope);
      if (result?.engine_restart_required)
        setNotice(t("common.savedRestart"));
    }, t("common.saved"));
  };

  const saveAsNew = () => {
    if (!config) return;
    const name = window.prompt(t("config.newProfilePrompt"));
    if (!name) return;
    const updates: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(draft)) {
      const entry = config.entries.find((e) => e.key === key);
      if (entry) updates[key] = parseInput(raw, entry.type);
    }
    if (Object.keys(updates).length === 0) {
      setError(t("config.noChanges"));
      return;
    }
    void run(
      () => api.putProfile(name, updates),
      t("config.profileCreated", { name }),
    );
  };

  const active = config?.active_profile ?? null;

  return (
    <section className="card card-wide">
      <h2>{t("config.title")}</h2>

      <div className="actions">
        <label className="meta">
          {t("config.active")}
          <select
            disabled={busy || engineRunning}
            value={active ?? DEFAULT}
            onChange={(e) =>
              void run(
                () =>
                  e.target.value === DEFAULT
                    ? api.deactivateProfile()
                    : api.activateProfile(e.target.value),
                t("config.switchedRestart"),
              )
            }
          >
            <option value={DEFAULT}>{t("config.default")}</option>
            {profiles?.profiles.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
                {p.description ? ` — ${p.description}` : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="meta">
          {t("config.edit")}
          <select disabled={busy} value={selected} onChange={(e) => { setSelected(e.target.value); setDraft({}); }}>
            <option value={DEFAULT}>{t("config.default")}</option>
            {profiles?.profiles.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
        </label>

        <button disabled={busy || Object.keys(draft).length === 0} onClick={save}>
          {t("config.save")}
        </button>
        <button disabled={busy || Object.keys(draft).length === 0} onClick={saveAsNew}>
          {t("config.saveAs")}
        </button>
        {selected !== DEFAULT && (
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              void run(
                () => api.deleteProfile(selected).then(() => setSelected(DEFAULT)),
                t("config.deleted"),
              )
            }
          >
            {t("config.delete")}
          </button>
        )}
      </div>

      {notice && <p className="meta config-notice">{notice}</p>}
      {!!moduleOwnedKeys.length && <p className="meta">The active acquisition profile overrides its fields below. Edit those values in Acquisition profiles.</p>}
      {error && <p className="error action-error">{error}</p>}

      {/* Datalists for plugin-type configuration keys. */}
      {Object.entries(pluginOptions).map(([key, plugins]) =>
        plugins.length > 0 ? (
          <datalist key={key} id={`plugin-options-${key}`}>
            {plugins.map((p) => (
              <option key={p.plugin_type} value={p.plugin_type}>
                {translatePluginName(p.plugin_type, p.display_name, t)}
              </option>
            ))}
          </datalist>
        ) : null
      )}

      {config && (
        <table>
          <thead>
            <tr>
              <th>{t("config.key")}</th>
              <th>{t("config.description")}</th>
              <th>{t("config.defaultValue")}</th>
              <th>
                {selected === DEFAULT
                  ? t("config.activeOverride")
                  : t("config.profileOverride", { name: selected })}
              </th>
              <th>{t("config.effectiveValue")}</th>
              <th>{t("config.applyMode")}</th>
              <th>
                {selected === DEFAULT
                  ? t("config.changeDefaultTo")
                  : t("config.changeOverrideTo")}
              </th>
            </tr>
          </thead>
          <tbody>
            {config.entries.map((e) => (
              <tr key={e.key}>
                <td><code>{e.key}</code></td>
                <td className="meta">
                  {translateConfigDescription(e.key, e.description, t)}
                </td>
                <td>{show(e.default_value)}</td>
                <td>{show(e.profile_value)}</td>
                <td className="target">{show(e.effective_value)}</td>
                <td className="meta">
                  {e.requires === "hot"
                    ? t("config.hot")
                    : t("config.engineRestart")}
                </td>
                <td>
                  <input
                    disabled={busy || ((selected === DEFAULT || selected === active)
                      && (moduleOwnedKeys.includes(e.key) || (engineRunning && e.key.startsWith("ACQUISITION."))))}
                    className={draft[e.key] !== undefined ? "dirty" : ""}
                    placeholder={show(
                      selected === DEFAULT ? e.default_value : e.profile_value,
                    )}
                    value={draft[e.key] ?? ""}
                    list={pluginOptions[e.key]?.length
                      ? `plugin-options-${e.key}`
                      : undefined}
                    onChange={(ev) =>
                      setDraft((d) => {
                        const next = { ...d };
                        if (ev.target.value === "") delete next[e.key];
                        else next[e.key] = ev.target.value;
                        return next;
                      })
                    }
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
