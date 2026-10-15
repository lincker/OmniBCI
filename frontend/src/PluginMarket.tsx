/** Managed package lifecycle, signed repository browsing, and checked profile copies. */
import { useI18n, type TranslationKey, type TranslationParams } from "./i18n";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api/client";
import { pluginConfigApi, type PluginProfile } from "./api/plugin-config";
import { pluginMarketApi as api, type InstalledPackage, type MarketPackage,
  type ProfileVersionProposal, type ProfileVersionRequest } from "./api/plugin-market";
import { ConfigDialog } from "./ConfigDialog";
import { DecoderDeveloperGuide } from "./DecoderDeveloperGuide";
import { WorkbenchIcon as Icon } from "./WorkbenchIcon";
import "./PluginMarket.css";

type VersionDraft = { target: InstalledPackage; sourceId: string; name: string; custom: boolean;
  parameters: string; proposal: ProfileVersionProposal | null };
const messageOf = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);
const identity = (item: { package_id: string; version: string }) => `${item.package_id}@${item.version}`;

export function PluginMarket({ runtimeBusy = false, onBusyChange, onDirtyChange, onOpenSetup }: {
  runtimeBusy?: boolean;
  onBusyChange?: (busy: boolean) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onOpenSetup?: () => void;
}) {
  const { t } = useI18n();
  const [tab, setTab] = useState<"installed" | "repository" | "guide">("installed");
  const [query, setQuery] = useState("");
  const [installed, setInstalled] = useState<InstalledPackage[]>([]);
  const [catalog, setCatalog] = useState<MarketPackage[]>([]);
  const [profiles, setProfiles] = useState<PluginProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [inventoryError, setInventoryError] = useState("");
  const [repositoryError, setRepositoryError] = useState("");
  const [repositoryDisabled, setRepositoryDisabled] = useState(false);
  const [pending, setPending] = useState<{ key: TranslationKey; params?: TranslationParams } | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<TranslationKey | "">("");
  const [draft, setDraft] = useState<VersionDraft | null>(null);
  const [dialog, setDialog] = useState<InstalledPackage | null>(null);
  const upload = useRef<HTMLInputElement>(null);
  const alive = useRef(true);
  const locked = pending !== null || runtimeBusy;
  const unavailable = locked || loading || inventoryError !== "";

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { onBusyChange?.(pending !== null); }, [pending, onBusyChange]);
  useEffect(() => { onDirtyChange?.(draft !== null); }, [draft, onDirtyChange]);
  useEffect(() => () => { onBusyChange?.(false); onDirtyChange?.(false); }, [onBusyChange, onDirtyChange]);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const [packages, saved] = await Promise.all([api.installed(), pluginConfigApi.profiles()]);
      if (alive.current) { setInstalled(packages); setProfiles(saved); setInventoryError(""); }
    } catch (cause) { if (alive.current) setInventoryError(messageOf(cause)); }
    finally { if (alive.current) setLoading(false); }
  }, []);
  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true); setRepositoryError(""); setRepositoryDisabled(false);
    try { const packages = await api.catalog(); if (alive.current) setCatalog(packages); }
    catch (cause) {
      if (alive.current) {
        setCatalog([]);
        setRepositoryDisabled(cause instanceof ApiError && cause.status === 503);
        setRepositoryError(messageOf(cause));
      }
    } finally { if (alive.current) { setCatalogLoading(false); setCatalogLoaded(true); } }
  }, []);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (tab === "repository" && !catalogLoaded && !catalogLoading) void loadCatalog(); },
    [tab, catalogLoaded, catalogLoading, loadCatalog]);

  const run = async (label: TranslationKey, action: () => Promise<unknown>, success: TranslationKey, params?: TranslationParams) => {
    if (locked) return false;
    setPending({ key: label, params }); setError(""); setNotice("");
    try {
      await action(); await reload();
      if (alive.current) setNotice(success);
      return true;
    } catch (cause) { if (alive.current) { setError(messageOf(cause)); await reload(); } return false; }
    finally { if (alive.current) setPending(null); }
  };
  const sourcesFor = (target: InstalledPackage) => profiles.filter((profile) =>
    profile.selection.package_id === target.package_id && profile.selection.version !== target.version
    && target.contributions.some((item) => item.kind === profile.kind
      && item.selection.contribution_id === profile.selection.contribution_id));
  const openVersion = (target: InstalledPackage, source = sourcesFor(target)[0]) => {
    if (!source) return;
    setDraft({ target, sourceId: source.profile_id, name: `${source.name} ${target.version}`.slice(0, 64),
      custom: false, parameters: JSON.stringify(source.parameters, null, 2), proposal: null });
    setError(""); setNotice("");
  };
  const updateDraft = (values: Partial<VersionDraft>) => setDraft((current) => current ? { ...current, ...values, proposal: null } : null);
  const requestFor = (value: VersionDraft): ProfileVersionRequest => {
    const parameters: unknown = value.custom ? JSON.parse(value.parameters) : undefined;
    if (value.custom && (parameters === null || typeof parameters !== "object" || Array.isArray(parameters)))
      throw new Error(t("market.parametersError"));
    return { source_profile_id: value.sourceId, target_version: value.target.version, name: value.name,
      ...(value.custom ? { parameters: parameters as Record<string, unknown> } : {}) };
  };
  const preview = async () => {
    if (!draft || locked) return;
    setPending({ key: "market.checking" }); setError("");
    try { const proposal = await api.propose(requestFor(draft)); if (alive.current) setDraft({ ...draft, proposal }); }
    catch (cause) { if (alive.current) setError(messageOf(cause)); }
    finally { if (alive.current) setPending(null); }
  };
  const saveVersion = async () => {
    if (!draft?.proposal) return;
    const proposal = draft.proposal;
    const saved = await run("market.creating", () => api.createVersionProfile({ ...requestFor(draft),
      expected_source_revision: proposal.source_revision }), "market.created");
    if (saved) setDraft(null);
  };
  const kindLabel = (kind: string) => {
    const keys: Record<string, TranslationKey> = { acquisition: "market.kind.acquisition", decoder: "market.kind.decoder", epoch_detector: "market.kind.epoch_detector", filter: "market.kind.filter", paradigm: "market.kind.paradigm", trigger: "market.kind.trigger", effector: "market.kind.effector" };
    return Object.prototype.hasOwnProperty.call(keys, kind) ? t(keys[kind]) : kind.replace(/_/g, " ");
  };
  const referenceScope = (scope: string) => {
    const labels: Record<string, string> = { profile: t("market.scope.profile"), accepted: t("market.scope.accepted"), running: t("market.scope.running") };
    return Object.prototype.hasOwnProperty.call(labels, scope) ? labels[scope] : scope.replace(/_/g, " ");
  };
  const matches = (text: string) => text.toLowerCase().includes(query.trim().toLowerCase());
  const displayedInstalled = installed.filter((item) => matches(`${item.package_id} ${item.label} ${item.version} ${item.contributions.map((part) => `${part.kind} ${kindLabel(part.kind)}`).join(" ")}`));
  const displayedCatalog = catalog.filter((item) => matches(`${item.package_id} ${item.display_name} ${item.version} ${item.description}`));

  return <div className="settings-workspace plugin-market">
    <aside className="explorer">
      <div className="explorer-heading">{t("market.extensions")}</div>
      <nav className="workspace-navigation" aria-label={t("market.library")}>
        <button className={`explorer-item ${tab === "installed" ? "selected" : ""}`} aria-current={tab === "installed" ? "page" : undefined}
          onClick={() => { setTab("installed"); setQuery(""); }}><Icon name="plug" /><span>{t("market.installed")}</span><span className="count">{installed.length}</span></button>
        <button className={`explorer-item ${tab === "repository" ? "selected" : ""}`} aria-current={tab === "repository" ? "page" : undefined}
          onClick={() => { setTab("repository"); setQuery(""); }}><Icon name="folder" /><span>{t("market.repository")}</span></button>
        <button className={`explorer-item ${tab === "guide" ? "selected" : ""}`} aria-current={tab === "guide" ? "page" : undefined}
          onClick={() => setTab("guide")}><Icon name="profile" /><span>{t("market.guide")}</span></button>
      </nav>
      <div className="explorer-footer"><strong>{t("market.versionHeading")}</strong>
        <p>{t("market.installHint")}</p>
        <p>{t("market.versionHint")}</p>
        {onOpenSetup && <button className="text-button" disabled={locked || draft !== null} onClick={onOpenSetup}>{t("market.openSetup")}<Icon name="chevron" /></button>}
      </div>
    </aside>
    <section className="settings-editor">
      <div className="breadcrumbs">{t("market.extensions")}<Icon name="chevron" />{tab === "guide" ? t("market.guide") : tab === "installed" ? t("market.installedHeading") : t("market.signedRepository")}</div>
      <header className="editor-heading"><div><p className="eyebrow">{t("market.library")}</p><h1>{tab === "guide" ? t("market.guide") : tab === "installed" ? t("market.installedHeading") : t("market.repositoryHeading")}</h1>
        <p>{tab === "guide" ? t("market.guideIntro") : t("market.intro")}</p></div>
        <button disabled={locked} onClick={() => { if (tab === "guide") setTab("installed"); upload.current?.click(); }}><Icon name="plus" />{t("market.installZip")}</button>
        <input ref={upload} type="file" accept=".zip,application/zip" hidden aria-label={t("market.zipLabel")} onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (file) void run("market.installing", () => api.upload(file), "market.installedNotice", { name: file.name });
        }} />
      </header>
      <div className="market-body">
        {tab === "guide" ? <DecoderDeveloperGuide /> : <>
        <div className="market-toolbar"><label className="market-search"><Icon name="search" /><input type="search" aria-label={t("market.searchLabel")}
          placeholder={t("market.searchPlaceholder")} value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <button disabled={pending !== null || loading || catalogLoading} onClick={() => { void reload(); if (tab === "repository") void loadCatalog(); }}><Icon name="refresh" />{t("market.refresh")}</button></div>
        <p className="meta">{t("market.trustHint")}</p>
        {runtimeBusy && <p className="market-banner" role="status">{t("market.runtimeBusy")}</p>}
        {pending && <p className="market-banner" role="status">{t(pending.key, pending.params)}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {inventoryError && <p className="error" role="alert">{t("market.inventoryError", { error: inventoryError })}</p>}
        {notice && <p className="success" role="status">{t(notice)}</p>}
        {draft && <section className="market-version-editor" aria-labelledby="version-profile-title">
          <div className="market-section-heading"><div><h2 id="version-profile-title">{t("market.createFor", { version: draft.target.version })}</h2><span className="meta">{draft.target.package_id} · {t("market.separateProfile")}</span></div>
            <button className="icon-button" aria-label={t("market.closeEditor")} disabled={locked} onClick={() => setDraft(null)}><Icon name="close" /></button></div>
          <label>{t("market.sourceProfile")}<select value={draft.sourceId} disabled={locked} onChange={(event) => openVersion(draft.target, profiles.find((item) => item.profile_id === event.target.value))}>
            {sourcesFor(draft.target).map((item) => <option key={item.profile_id} value={item.profile_id}>{item.name} · {item.selection.version ?? t("market.legacy")}</option>)}</select></label>
          <label>{t("market.newProfileName")}<input value={draft.name} maxLength={64} disabled={locked} onChange={(event) => updateDraft({ name: event.target.value })} /></label>
          <label className="market-checkbox"><input type="checkbox" checked={draft.custom} disabled={locked} onChange={(event) => updateDraft({ custom: event.target.checked })} />{t("market.editParameters")}</label>
          {draft.custom && <label>{t("market.parameters")}<textarea rows={7} value={draft.parameters} spellCheck={false} disabled={locked}
            onChange={(event) => updateDraft({ parameters: event.target.value })} /></label>}
          <p className="meta">{t("market.previewHint")}</p>
          <button disabled={unavailable || !draft.name.trim()} onClick={() => void preview()}>{t("market.preview")}</button>
          {draft.proposal && <div className="market-version-preview"><h3>{t("market.reviewed")}</h3>
            <p>{draft.proposal.name} · {draft.proposal.selection.version} · {t("market.schemaVersion", { version: draft.proposal.configuration_version })}</p>
            <pre>{JSON.stringify(draft.proposal.parameters, null, 2)}</pre>
            {!draft.proposal.target_enabled && <p className="meta">{t("market.enablePreview")}</p>}
            <button className="primary" disabled={unavailable || !draft.proposal.target_enabled} onClick={() => void saveVersion()}>{t("market.create")}</button>
            <p className="meta">{t("market.unchanged")}</p></div>}
        </section>}
        {tab === "installed" ? <div className="market-list" aria-busy={loading}>
          {loading && installed.length === 0 ? <p className="market-empty">{t("market.loading")}</p>
            : !inventoryError && displayedInstalled.length === 0 && <p className="market-empty">{query ? t("market.noInstalledMatch") : t("market.noInstalled")}</p>}
          {displayedInstalled.map((item) => {
            const references = item.references ?? [];
            const blocked = references.length > 0;
            return <article className="market-package" key={identity(item)}><div className="market-package-icon"><Icon name="plug" /></div><div className="market-package-detail">
              <div className="market-package-heading"><h2>{item.label}</h2><span className="market-version">{item.version}</span><span className={`market-state ${item.enabled ? "enabled" : ""}`}>{item.enabled ? t("market.enabled") : t("market.disabled")}</span></div>
              <p className="market-identity">{item.package_id}</p><p>{item.description || t("market.defaultDescription")}</p>
              <div className="market-capabilities">{item.contributions.map((part) => <span key={part.selection.contribution_id}>{kindLabel(part.kind)} · {part.selection.contribution_id}</span>)}</div>
              {item.error && <p className="error" role="alert">{item.error}</p>}
              {blocked && <details className="market-references"><summary>{t(references.length === 1 ? "market.oneReference" : "market.references", { count: references.length })}</summary><ul>
                {references.map((reference, index) => <li key={`${reference.scope}-${reference.identifier}-${index}`}><strong>{referenceScope(reference.scope)}</strong> · {profiles.find((profile) => profile.profile_id === reference.identifier)?.name ?? reference.identifier}</li>)}</ul>
                <p className="meta">{t("market.referencesHint")}</p></details>}
              <div className="actions"><button disabled={unavailable || (item.enabled ? blocked : !item.available)} title={item.enabled && blocked ? t("market.referencedTitle") : undefined}
                onClick={() => void run(item.enabled ? "market.disabling" : "market.enabling", () => api.setEnabled(item, !item.enabled), item.enabled ? "market.disabledNotice" : "market.enabledNotice")}>{item.enabled ? t("market.disable") : t("market.enable")}</button>
                <button disabled={unavailable || sourcesFor(item).length === 0 || !item.available} onClick={() => openVersion(item)}><Icon name="copy" />{t("market.createVersionProfile")}</button>
                <button className="text-button danger" disabled={unavailable || item.enabled || blocked} title={item.enabled ? t("market.disableFirst") : undefined}
                  onClick={() => setDialog(item)}>{t("market.remove")}</button></div>
            </div></article>;
          })}
        </div> : <div className="market-list" aria-busy={catalogLoading}>
          {catalogLoading && <p className="market-empty">{t("market.verifying")}</p>}
          {!catalogLoading && repositoryDisabled ? <div className="market-empty"><h2>{t("market.repositoryUnavailable")}</h2><p>{t("market.configureRepository")}</p><p>{t("market.localZip")}</p><p className="meta">{repositoryError}</p></div>
            : repositoryError ? <p className="error" role="alert">{t("market.repositoryError", { error: repositoryError })}</p>
            : !catalogLoading && displayedCatalog.length === 0 && <p className="market-empty">{query ? t("market.noRepositoryMatch") : t("market.noRepositoryPackages")}</p>}
          {!repositoryError && displayedCatalog.map((item) => {
            const present = installed.some((local) => identity(local) === identity(item));
            return <article className="market-package" key={identity(item)}><div className="market-package-icon"><Icon name="folder" /></div><div className="market-package-detail">
              <div className="market-package-heading"><h2>{item.display_name}</h2><span className="market-version">{item.version}</span></div>
              <p className="market-identity">{item.package_id} · {item.repository_id}</p><p>{item.description}</p>
              <div className="market-capabilities">{item.contributions.map((part) => <span key={part.selection.contribution_id}>{kindLabel(part.kind)}</span>)}</div>
              <p className="meta">{(item.size / 1024 / 1024).toFixed(1)} MB{item.platforms.length ? ` · ${item.platforms.join(", ")}` : ""}{item.api_version ? t("market.pluginApi", { version: item.api_version }) : ""}</p>
              <button disabled={unavailable || present || catalogLoading} onClick={() => void run("market.installing", () => api.install(item), "market.verifiedInstalled", { name: identity(item) })}>{present ? t("market.installed") : t("market.installVersion", { version: item.version })}</button>
            </div></article>;
          })}
        </div>}
        </>}
      </div>
    </section>
    {dialog && <ConfigDialog title={t("market.removeTitle")} message={t("market.removeMessage", { identity: identity(dialog) })}
      confirmLabel={t("market.removeConfirm")} onConfirm={() => {
        const item = dialog; setDialog(null); void run("market.removing", () => api.remove(item), "market.removedNotice");
      }} onCancel={() => setDialog(null)} />}
  </div>;
}
