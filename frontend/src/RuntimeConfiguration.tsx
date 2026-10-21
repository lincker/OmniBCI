import { useEffect, useRef } from "react";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { useI18n, type TranslationKey } from "./i18n";
import "./RuntimeConfiguration.css";

const strategies: Record<string, TranslationKey> = {
  select_on_start: "runtime.strategy.select_on_start",
  no_change: "runtime.strategy.no_change",
  trial_boundary: "runtime.strategy.trial_boundary",
  module: "runtime.strategy.module",
  rebuild: "runtime.strategy.rebuild",
};

export function RuntimeConfigurationStatus({ runtime }: { runtime: RuntimeConfiguration }) {
  const { t } = useI18n();
  const operation = runtime.operation;
  if (!operation?.error && !runtime.recoveryRequired && !runtime.error
      && !runtime.requestError && !runtime.uncertain) return null;
  return <section className="configuration-status" aria-label={t("runtime.configurationStatus")} aria-live="polite">
    {runtime.uncertain && <p>{t("runtime.confirming")}</p>}
    {operation?.error && <p role="alert">{operation.error}{operation.rolled_back && t("runtime.previousRestored")}</p>}
    {runtime.recoveryRequired && <p role="alert">{t("runtime.recoveryRequired")}</p>}
    {(runtime.error || runtime.requestError) && <p role="alert">{runtime.requestError ?? runtime.error}</p>}
    {runtime.uncertain ? <button disabled={runtime.submitting} onClick={() => void runtime.retryUnconfirmed()}>{t("runtime.checkOrRetry")}</button>
      : runtime.error && <button onClick={() => void runtime.refresh()}>{t("runtime.refreshStatus")}</button>}
  </section>;
}

export function RuntimeConfigurationReview({ runtime }: { runtime: RuntimeConfiguration }) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const plan = runtime.plan;
  useEffect(() => { if (plan) dialog.current?.showModal(); }, [plan]);
  if (!plan) return null;
  const stale = runtime.status !== null && (plan.expected_revision !== runtime.status.accepted_revision
    || plan.running !== runtime.status.running || plan.applied_revision !== runtime.status.applied_revision
    || plan.generation !== runtime.status.generation);
  return <dialog ref={dialog} className="config-dialog configuration-review" aria-labelledby="configuration-review-title"
    onCancel={(event) => { event.preventDefault(); if (!runtime.submitting) runtime.dismissPlan(); }}>
    <h3 id="configuration-review-title">{t("runtime.reviewTitle")}</h3>
    <dl>
      <dt>{t("runtime.applyMethod")}</dt><dd>{Object.prototype.hasOwnProperty.call(strategies, plan.strategy) ? t(strategies[plan.strategy]) : plan.strategy}</dd>
      <dt>{t("runtime.affectedModules")}</dt><dd>{plan.changed_slots.length ? plan.changed_slots.join(", ") : t("runtime.noPluginChanges")}</dd>
      {plan.changed_settings.length > 0 && <><dt>{t("runtime.hostSettings")}</dt><dd>{plan.changed_settings.join(", ")}</dd></>}
      {plan.signal_summary && <><dt>{t("runtime.channelSetup")}</dt><dd>{plan.signal_summary}</dd></>}
      <dt>{t("runtime.targetRevision")}</dt><dd><code>{plan.target_revision ? plan.target_revision.slice(0, 12) : t("run.none")}</code></dd>
    </dl>
    {plan.errors.length > 0 && <ul className="error" role="alert">{plan.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    {stale && <p className="error" role="alert">{t("runtime.stalePlan")}</p>}
    <div className="actions"><button disabled={runtime.submitting} onClick={runtime.dismissPlan}>{t("runtime.cancel")}</button>
      <button className="primary" disabled={runtime.blocked || stale || plan.errors.length > 0}
        onClick={() => void runtime.apply()}>{runtime.submitting ? t("runtime.submitting") : t("runtime.applyConfiguration")}</button></div>
  </dialog>;
}
