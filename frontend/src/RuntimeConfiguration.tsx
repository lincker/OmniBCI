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

export function runtimeBlockReason(runtime: RuntimeConfiguration): TranslationKey | null {
  if (!runtime.blocked) return null;
  if (runtime.uncertain) return "feedback.confirming";
  if (runtime.inProgress) return "feedback.applying";
  if (runtime.recoveryRequired) return "feedback.recovery";
  return "feedback.runtimeUnavailable";
}

/** Routine feedback stays beside the editor actions; failures use the global status. */
export function RuntimeConfigurationProgress({ runtime }: { runtime: RuntimeConfiguration }) {
  const { t } = useI18n();
  const operation = runtime.operation;
  const key = runtime.uncertain ? "feedback.confirming"
    : runtime.submitting ? "feedback.submitting"
    : operation?.status === "queued" ? "feedback.queued"
    : operation?.status === "applying" ? operation.stage === "waiting_for_trial_boundary"
      ? "feedback.waitingForTrial" : "feedback.applying"
    : runtime.reviewing ? "feedback.reviewing" : null;
  return key ? <p className="action-help" role="status">{t(key)}</p> : null;
}

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
  const stale = runtime.status !== null && (Object.entries(plan.expected_revisions).some(([module, revision]) => runtime.status!.accepted[module] !== revision)
    || plan.running !== runtime.status.running
    || Object.keys(plan.expected_revisions).some((module) => plan.applied_revisions?.[module] !== runtime.status!.applied?.[module])
    || plan.generation !== runtime.status.generation);
  const blockedReason = runtimeBlockReason(runtime);
  const impact: Record<string, TranslationKey> = {
    select_on_start: "feedback.impactNextStart", no_change: "feedback.impactNoChange",
    trial_boundary: "feedback.impactTrial", module: "feedback.impactModule", rebuild: "feedback.impactRebuild",
  };
  const applyMethod = plan.strategy === "rebuild" && plan.module !== "system" ? "runtime.strategy.rebuildModule"
    : plan.strategy === "select_on_start" && plan.module === "system" ? "runtime.strategy.selectEngineOnStart"
    : Object.prototype.hasOwnProperty.call(strategies, plan.strategy) ? strategies[plan.strategy] : null;
  const applyImpact = plan.strategy === "rebuild" && plan.module !== "system" ? "feedback.impactModuleRestart"
    : plan.strategy === "select_on_start" && plan.module === "system" ? "feedback.impactNextEngineStart"
    : Object.prototype.hasOwnProperty.call(impact, plan.strategy) ? impact[plan.strategy] : "feedback.impactUnknown";
  return <dialog ref={dialog} className="config-dialog configuration-review" aria-labelledby="configuration-review-title"
    aria-describedby="configuration-review-impact"
    onCancel={(event) => { event.preventDefault(); if (!runtime.submitting) runtime.dismissPlan(); }}>
    <div className="configuration-review-content">
    <h3 id="configuration-review-title">{t("runtime.reviewTitle")}</h3>
    <p id="configuration-review-impact">{t(applyImpact)}</p>
    <dl>
      <dt>{t("runtime.applyMethod")}</dt><dd>{applyMethod ? t(applyMethod) : plan.strategy}</dd>
      <dt>{t("runtime.affectedModules")}</dt><dd>{plan.changed_slots.length ? plan.changed_slots.join(", ") : t("runtime.noPluginChanges")}</dd>
      {plan.changed_settings.length > 0 && <><dt>{t("runtime.hostSettings")}</dt><dd>{plan.changed_settings.join(", ")}</dd></>}
      {plan.signal_summary && <><dt>{t("runtime.channelSetup")}</dt><dd>{plan.signal_summary}</dd></>}
      <dt>{t("runtime.targetRevision")}</dt><dd><code>{Object.entries(plan.target_revisions).map(([module, revision]) => `${module}: ${revision.slice(0, 12)}`).join(", ") || t("run.none")}</code></dd>
    </dl>
    {runtime.requestError && <p className="error" role="alert">{runtime.requestError}</p>}
    {plan.errors.length > 0 && <ul className="error" role="alert">{plan.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    {stale && <p className="error" role="alert">{t("runtime.stalePlan")}</p>}
    {blockedReason && <p id="configuration-review-blocked" role="status">{t(blockedReason)}</p>}
    </div>
    <div className="actions"><button disabled={runtime.submitting} onClick={runtime.dismissPlan}>{t("runtime.cancel")}</button>
      <button className="primary" disabled={runtime.blocked || runtime.submitting || stale || plan.errors.length > 0}
        aria-describedby={blockedReason ? "configuration-review-blocked" : "configuration-review-impact"}
        onClick={() => void runtime.apply()}>{runtime.submitting ? t("runtime.submitting") : t("runtime.applyConfiguration")}</button></div>
  </dialog>;
}
