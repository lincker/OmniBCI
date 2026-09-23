import { useEffect, useRef } from "react";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import "./RuntimeConfiguration.css";

const strategies = {
  select_on_start: "Use on the next engine start",
  no_change: "Keep the current runtime configuration",
  trial_boundary: "Apply after the current trial completes",
  module: "Pause and restart affected modules",
  rebuild: "Pause and rebuild the engine",
};
const labels = { queued: "Waiting", applying: "Applying", succeeded: "Applied",
  failed: "Failed", recovery_required: "Recovery required" };
const revision = (value: string | null | undefined) => value ? value.slice(0, 12) : "None";

export function RuntimeConfigurationStatus({ runtime }: { runtime: RuntimeConfiguration }) {
  const operation = runtime.operation;
  const operationLabel = operation?.status === "succeeded" && operation.result?.outcome === "selected"
    ? "Selected for next start" : operation ? labels[operation.status] : "";
  return <section className="configuration-status" aria-label="Configuration status" aria-live="polite">
    <div className="configuration-revisions">
      <span>Selected <code title={runtime.status?.accepted_revision ?? ""}>{revision(runtime.status?.accepted_revision)}</code></span>
      <span>Applied <code title={runtime.status?.applied_revision ?? ""}>{revision(runtime.status?.applied_revision)}</code></span>
      {operation && <strong>{operationLabel} · {operation.stage.replace(/_/g, " ")}</strong>}
      {!runtime.status && <span>Checking configuration…</span>}
      {runtime.uncertain && <strong>Confirming operation result…</strong>}
    </div>
    {operation?.error && <p role="alert">{operation.error}{operation.rolled_back && " Previous configuration restored."}</p>}
    {runtime.recoveryRequired && <p role="alert">Recovery is required. Only stop controls are available; inspect the reported failure before restarting.</p>}
    {(runtime.error || runtime.requestError) && <p role="alert">{runtime.requestError ?? runtime.error}</p>}
    {runtime.uncertain ? <button disabled={runtime.submitting} onClick={() => void runtime.retryUnconfirmed()}>Check or retry the same operation</button>
      : runtime.error && <button onClick={() => void runtime.refresh()}>Refresh status</button>}
  </section>;
}

export function RuntimeConfigurationReview({ runtime }: { runtime: RuntimeConfiguration }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const plan = runtime.plan;
  useEffect(() => { if (plan) dialog.current?.showModal(); }, [plan]);
  if (!plan) return null;
  const stale = runtime.status !== null && (plan.expected_revision !== runtime.status.accepted_revision
    || plan.running !== runtime.status.running || plan.applied_revision !== runtime.status.applied_revision
    || plan.generation !== runtime.status.generation);
  return <dialog ref={dialog} className="config-dialog configuration-review" aria-labelledby="configuration-review-title"
    onCancel={(event) => { event.preventDefault(); if (!runtime.submitting) runtime.dismissPlan(); }}>
    <h3 id="configuration-review-title">Review configuration change</h3>
    <dl>
      <dt>Apply method</dt><dd>{strategies[plan.strategy]}</dd>
      <dt>Session</dt><dd>{plan.session_effect === "continuation" ? "Create a continuation session after the current session is saved." : "Preserve the current session."}</dd>
      <dt>Affected modules</dt><dd>{plan.changed_slots.length ? plan.changed_slots.join(", ") : "No plugin changes"}</dd>
      {plan.changed_settings.length > 0 && <><dt>Host settings</dt><dd>{plan.changed_settings.join(", ")}</dd></>}
      <dt>Target revision</dt><dd><code>{revision(plan.target_revision)}</code></dd>
    </dl>
    {plan.errors.length > 0 && <ul className="error" role="alert">{plan.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    {stale && <p className="error" role="alert">The runtime or selected configuration changed. Close this preview and review the current selection again.</p>}
    <div className="actions"><button disabled={runtime.submitting} onClick={runtime.dismissPlan}>Cancel</button>
      <button className="primary" disabled={runtime.blocked || stale || plan.errors.length > 0}
        onClick={() => void runtime.apply()}>{runtime.submitting ? "Submitting…" : "Apply configuration"}</button></div>
  </dialog>;
}
