/**
 * 配置卡 — 分级配置(默认 + 个性化 profile)。
 *
 * 三列语义:默认值 / 本配置覆盖 / 生效值。
 * 选中"默认配置"时编辑写 settings.toml;选中 profile 时编辑写该 profile 文件。
 * engine-restart 键保存后提示停止→启动引擎生效。
 */
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ConfigData, type ParadigmInfo, type ProfileList } from "./api/client";

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

export function ConfigCard() {
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [profiles, setProfiles] = useState<ProfileList | null>(null);
  const [paradigms, setParadigms] = useState<ParadigmInfo[]>([]);
  const [selected, setSelected] = useState<string>(DEFAULT); // 编辑目标
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    const [cfg, profs, paradigmsResp] = await Promise.all([
      api.getConfig().catch(() => null),
      api.listProfiles().catch(() => null),
      api.listParadigms().catch(() => null),
    ]);
    setConfig(cfg);
    setProfiles(profs);
    if (paradigmsResp) setParadigms(paradigmsResp.paradigms);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

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
        setNotice("已保存——停止→启动引擎后生效");
    }, "已保存");
  };

  const saveAsNew = () => {
    if (!config) return;
    const name = window.prompt("新配置名称(中英文、数字、_-、空格):");
    if (!name) return;
    const updates: Record<string, unknown> = {};
    for (const [key, raw] of Object.entries(draft)) {
      const entry = config.entries.find((e) => e.key === key);
      if (entry) updates[key] = parseInput(raw, entry.type);
    }
    if (Object.keys(updates).length === 0) {
      setError("没有改动可保存——先在覆盖列输入差异值");
      return;
    }
    void run(() => api.putProfile(name, updates), `配置 ${name} 已创建`);
  };

  const active = config?.active_profile ?? null;

  return (
    <section className="card card-wide">
      <h2>配置</h2>

      <div className="actions">
        <label className="meta">
          激活:
          <select
            disabled={busy}
            value={active ?? DEFAULT}
            onChange={(e) =>
              void run(
                () =>
                  e.target.value === DEFAULT
                    ? api.deactivateProfile()
                    : api.activateProfile(e.target.value),
                "已切换——引擎相关键在下次启动引擎时生效",
              )
            }
          >
            <option value={DEFAULT}>默认配置</option>
            {profiles?.profiles.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
                {p.description ? ` — ${p.description}` : ""}
              </option>
            ))}
          </select>
        </label>

        <label className="meta">
          编辑:
          <select disabled={busy} value={selected} onChange={(e) => { setSelected(e.target.value); setDraft({}); }}>
            <option value={DEFAULT}>默认配置</option>
            {profiles?.profiles.map((p) => (
              <option key={p.name} value={p.name}>{p.name}</option>
            ))}
          </select>
        </label>

        <button disabled={busy || Object.keys(draft).length === 0} onClick={save}>
          保存
        </button>
        <button disabled={busy || Object.keys(draft).length === 0} onClick={saveAsNew}>
          另存为…
        </button>
        {selected !== DEFAULT && (
          <button
            className="danger"
            disabled={busy}
            onClick={() => void run(() => api.deleteProfile(selected).then(() => setSelected(DEFAULT)), "已删除")}
          >
            删除此配置
          </button>
        )}
      </div>

      {notice && <p className="meta config-notice">{notice}</p>}
      {error && <p className="error action-error">{error}</p>}

      {/* datalist for paradigm plugin_type values */}
      {paradigms.length > 0 && (
        <datalist id="paradigm-options">
          {paradigms.map((p) => (
            <option key={p.plugin_type} value={p.plugin_type}>
              {p.display_name}
            </option>
          ))}
        </datalist>
      )}

      {config && (
        <table>
          <thead>
            <tr>
              <th>键</th>
              <th>说明</th>
              <th>默认值</th>
              <th>{selected === DEFAULT ? "激活覆盖" : `${selected} 覆盖`}</th>
              <th>生效值</th>
              <th>生效方式</th>
              <th>{selected === DEFAULT ? "改默认为" : "改覆盖为"}</th>
            </tr>
          </thead>
          <tbody>
            {config.entries.map((e) => (
              <tr key={e.key}>
                <td><code>{e.key}</code></td>
                <td className="meta">{e.description}</td>
                <td>{show(e.default_value)}</td>
                <td>{show(e.profile_value)}</td>
                <td className="target">{show(e.effective_value)}</td>
                <td className="meta">{e.requires === "hot" ? "即时" : "重启引擎"}</td>
                <td>
                  <input
                    className={draft[e.key] !== undefined ? "dirty" : ""}
                    placeholder={show(
                      selected === DEFAULT ? e.default_value : e.profile_value,
                    )}
                    value={draft[e.key] ?? ""}
                    list={e.key === "STIMULATION.paradigm" && paradigms.length > 0
                      ? "paradigm-options"
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
