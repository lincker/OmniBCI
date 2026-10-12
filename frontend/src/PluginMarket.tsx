/** Managed package lifecycle, signed repository browsing, and checked profile copies. */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api/client";
import { pluginConfigApi, type PluginProfile } from "./api/plugin-config";
import { pluginMarketApi as api, type InstalledPackage, type MarketPackage,
  type ProfileVersionProposal, type ProfileVersionRequest } from "./api/plugin-market";
import { ConfigDialog, type ConfigDialogProps } from "./ConfigDialog";
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
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState<VersionDraft | null>(null);
  const [dialog, setDialog] = useState<Omit<ConfigDialogProps, "onCancel"> | null>(null);
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

  const run = async (label: string, action: () => Promise<unknown>, success: string) => {
    if (locked) return false;
    setPending(label); setError(""); setNotice("");
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
      throw new Error("Parameters must be a JSON object.");
    return { source_profile_id: value.sourceId, target_version: value.target.version, name: value.name,
      ...(value.custom ? { parameters: parameters as Record<string, unknown> } : {}) };
  };
  const preview = async () => {
    if (!draft || locked) return;
    setPending("Checking profile version…"); setError("");
    try { const proposal = await api.propose(requestFor(draft)); if (alive.current) setDraft({ ...draft, proposal }); }
    catch (cause) { if (alive.current) setError(messageOf(cause)); }
    finally { if (alive.current) setPending(null); }
  };
  const saveVersion = async () => {
    if (!draft?.proposal) return;
    const proposal = draft.proposal;
    const saved = await run("Creating profile…", () => api.createVersionProfile({ ...requestFor(draft),
      expected_source_revision: proposal.source_revision }), "New profile saved. Select it in Setup and review Apply when ready.");
    if (saved) setDraft(null);
  };
  const matches = (text: string) => text.toLowerCase().includes(query.trim().toLowerCase());
  const displayedInstalled = installed.filter((item) => matches(`${item.package_id} ${item.label} ${item.version} ${item.contributions.map((part) => part.kind).join(" ")}`));
  const displayedCatalog = catalog.filter((item) => matches(`${item.package_id} ${item.display_name} ${item.version} ${item.description}`));

  return <div className="settings-workspace plugin-market">
    <aside className="explorer">
      <div className="explorer-heading">Extensions</div>
      <nav className="workspace-navigation" aria-label="Plugin library">
        <button className={`explorer-item ${tab === "installed" ? "selected" : ""}`} aria-current={tab === "installed" ? "page" : undefined}
          onClick={() => { setTab("installed"); setQuery(""); }}><Icon name="plug" /><span>Installed</span><span className="count">{installed.length}</span></button>
        <button className={`explorer-item ${tab === "repository" ? "selected" : ""}`} aria-current={tab === "repository" ? "page" : undefined}
          onClick={() => { setTab("repository"); setQuery(""); }}><Icon name="folder" /><span>Repository</span></button>
        <button className={`explorer-item ${tab === "guide" ? "selected" : ""}`} aria-current={tab === "guide" ? "page" : undefined}
          onClick={() => setTab("guide")}><Icon name="profile" /><span>Decoder developer guide</span></button>
      </nav>
      <div className="explorer-footer"><strong>Exact versions, reusable profiles</strong>
        <p>Install a package, enable its contributions, then select a profile in Setup.</p>
        <p>Existing profiles keep their selected versions.</p>
        {onOpenSetup && <button className="text-button" disabled={locked || draft !== null} onClick={onOpenSetup}>Open Setup<Icon name="chevron" /></button>}
      </div>
    </aside>
    <section className="settings-editor">
      <div className="breadcrumbs">Extensions<Icon name="chevron" />{tab === "guide" ? "Decoder developer guide" : tab === "installed" ? "Installed plugins" : "Signed repository"}</div>
      <header className="editor-heading"><div><p className="eyebrow">Plugin library</p><h1>{tab === "guide" ? "Decoder developer guide" : tab === "installed" ? "Installed plugins" : "Plugin repository"}</h1>
        <p>{tab === "guide" ? "Required files, channel declarations and an executable starter." : "Manage acquisition, processing, stimulation and actuation packages."}</p></div>
        <button disabled={locked} onClick={() => { if (tab === "guide") setTab("installed"); upload.current?.click(); }}><Icon name="plus" />Install ZIP…</button>
        <input ref={upload} type="file" accept=".zip,application/zip" hidden aria-label="Choose plugin ZIP" onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (file) void run(`Installing ${file.name}…`, () => api.upload(file), "Package installed. Enable it to make its contributions available in Setup.");
        }} />
      </header>
      <div className="market-body">
        {tab === "guide" ? <DecoderDeveloperGuide /> : <>
        <div className="market-toolbar"><label className="market-search"><Icon name="search" /><input type="search" aria-label="Search plugins"
          placeholder="Search names, versions or capabilities" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <button disabled={pending !== null || loading || catalogLoading} onClick={() => { void reload(); if (tab === "repository") void loadCatalog(); }}><Icon name="refresh" />Refresh</button></div>
        <p className="meta">Packages contain executable plugin code. Choose a source you trust; installation keeps new versions disabled.</p>
        {runtimeBusy && <p className="market-banner" role="status">Package changes are temporarily unavailable. Check the runtime status above.</p>}
        {pending && <p className="market-banner" role="status">{pending}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {inventoryError && <p className="error" role="alert">Installed packages could not be refreshed: {inventoryError}</p>}
        {notice && <p className="success" role="status">{notice}</p>}
        {draft && <section className="market-version-editor" aria-labelledby="version-profile-title">
          <div className="market-section-heading"><div><h2 id="version-profile-title">Create profile for {draft.target.version}</h2><span className="meta">{draft.target.package_id} · Upgrade or roll back with a separate profile.</span></div>
            <button className="icon-button" aria-label="Close version profile editor" disabled={locked} onClick={() => setDraft(null)}><Icon name="close" /></button></div>
          <label>Source profile<select value={draft.sourceId} disabled={locked} onChange={(event) => openVersion(draft.target, profiles.find((item) => item.profile_id === event.target.value))}>
            {sourcesFor(draft.target).map((item) => <option key={item.profile_id} value={item.profile_id}>{item.name} · {item.selection.version ?? "legacy"}</option>)}</select></label>
          <label>New profile name<input value={draft.name} maxLength={64} disabled={locked} onChange={(event) => updateDraft({ name: event.target.value })} /></label>
          <label className="market-checkbox"><input type="checkbox" checked={draft.custom} disabled={locked} onChange={(event) => updateDraft({ custom: event.target.checked })} />Edit parameter values</label>
          {draft.custom && <label>Parameters (JSON object)<textarea rows={7} value={draft.parameters} spellCheck={false} disabled={locked}
            onChange={(event) => updateDraft({ parameters: event.target.value })} /></label>}
          <p className="meta">Preview validates every value against the target schema. Removed or incompatible parameters require an explicit edit.</p>
          <button disabled={unavailable || !draft.name.trim()} onClick={() => void preview()}>Preview version change</button>
          {draft.proposal && <div className="market-version-preview"><h3>Reviewed profile</h3>
            <p>{draft.proposal.name} · {draft.proposal.selection.version} · Schema {draft.proposal.configuration_version}</p>
            <pre>{JSON.stringify(draft.proposal.parameters, null, 2)}</pre>
            {!draft.proposal.target_enabled && <p className="meta">Enable this target version, then preview again before creating the profile.</p>}
            <button className="primary" disabled={unavailable || !draft.proposal.target_enabled} onClick={() => void saveVersion()}>Create new profile</button>
            <p className="meta">The source profile and current module selections stay unchanged.</p></div>}
        </section>}
        {tab === "installed" ? <div className="market-list" aria-busy={loading}>
          {loading && installed.length === 0 ? <p className="market-empty">Loading installed packages…</p>
            : !inventoryError && displayedInstalled.length === 0 && <p className="market-empty">{query ? "No installed versions match your search." : "No managed packages installed. Install a ZIP or browse the configured repository."}</p>}
          {displayedInstalled.map((item) => {
            const references = item.references ?? [];
            const blocked = references.length > 0;
            return <article className="market-package" key={identity(item)}><div className="market-package-icon"><Icon name="plug" /></div><div className="market-package-detail">
              <div className="market-package-heading"><h2>{item.label}</h2><span className="market-version">{item.version}</span><span className={`market-state ${item.enabled ? "enabled" : ""}`}>{item.enabled ? "Enabled" : "Disabled"}</span></div>
              <p className="market-identity">{item.package_id}</p><p>{item.description || "Plugin contributions available for reusable module profiles."}</p>
              <div className="market-capabilities">{item.contributions.map((part) => <span key={part.selection.contribution_id}>{part.kind.replace(/_/g, " ")} · {part.selection.contribution_id}</span>)}</div>
              {item.error && <p className="error" role="alert">{item.error}</p>}
              {blocked && <details className="market-references"><summary>In use by {references.length} reference{references.length === 1 ? "" : "s"}</summary><ul>
                {references.map((reference, index) => <li key={`${reference.scope}-${reference.identifier}-${index}`}><strong>{reference.scope.replace(/_/g, " ")}</strong> · {profiles.find((profile) => profile.profile_id === reference.identifier)?.name ?? reference.identifier}</li>)}</ul>
                <p className="meta">Replace active bindings and remove saved profiles before disabling or removing this version.</p></details>}
              <div className="actions"><button disabled={unavailable || (item.enabled ? blocked : !item.available)} title={item.enabled && blocked ? "This version is referenced by saved or active configuration." : undefined}
                onClick={() => void run(item.enabled ? "Disabling package…" : "Enabling package…", () => api.setEnabled(item, !item.enabled), item.enabled ? "Package disabled." : "Package enabled. Create and select its profile in Setup.")}>{item.enabled ? "Disable" : "Enable"}</button>
                <button disabled={unavailable || sourcesFor(item).length === 0 || !item.available} onClick={() => openVersion(item)}><Icon name="copy" />Create version profile…</button>
                <button className="text-button danger" disabled={unavailable || item.enabled || blocked} title={item.enabled ? "Disable this version before removing it." : undefined}
                  onClick={() => setDialog({ title: "Remove installed version", message: `Remove ${identity(item)} from this computer?`, confirmLabel: "Remove version", onConfirm: () => {
                    setDialog(null); void run("Removing package…", () => api.remove(item), "Package version removed.");
                  } })}>Remove</button></div>
            </div></article>;
          })}
        </div> : <div className="market-list" aria-busy={catalogLoading}>
          {catalogLoading && <p className="market-empty">Verifying repository catalog…</p>}
          {!catalogLoading && repositoryDisabled ? <div className="market-empty"><h2>Repository unavailable</h2><p>An operator can configure a trusted signed repository for this console.</p><p>You can install a trusted local ZIP from this page.</p><p className="meta">{repositoryError}</p></div>
            : repositoryError ? <p className="error" role="alert">Repository could not be verified: {repositoryError}</p>
            : !catalogLoading && displayedCatalog.length === 0 && <p className="market-empty">{query ? "No repository versions match your search." : "The verified repository has no packages."}</p>}
          {!repositoryError && displayedCatalog.map((item) => {
            const present = installed.some((local) => identity(local) === identity(item));
            return <article className="market-package" key={identity(item)}><div className="market-package-icon"><Icon name="folder" /></div><div className="market-package-detail">
              <div className="market-package-heading"><h2>{item.display_name}</h2><span className="market-version">{item.version}</span></div>
              <p className="market-identity">{item.package_id} · {item.repository_id}</p><p>{item.description}</p>
              <div className="market-capabilities">{item.contributions.map((part) => <span key={part.selection.contribution_id}>{part.kind.replace(/_/g, " ")}</span>)}</div>
              <p className="meta">{(item.size / 1024 / 1024).toFixed(1)} MB{item.platforms.length ? ` · ${item.platforms.join(", ")}` : ""}{item.api_version ? ` · Plugin API ${item.api_version}` : ""}</p>
              <button disabled={unavailable || present || catalogLoading} onClick={() => void run(`Installing ${identity(item)}…`, () => api.install(item), "Verified package installed. Enable it when ready to create a profile.")}>{present ? "Installed" : `Install ${item.version}`}</button>
            </div></article>;
          })}
        </div>}
        </>}
      </div>
    </section>
    {dialog && <ConfigDialog {...dialog} onCancel={() => setDialog(null)} />}
  </div>;
}
