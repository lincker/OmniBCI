import { useEffect, useRef } from "react";
import { configurationSections, type ConfigurationSection, type RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { useI18n, type TranslationKey } from "./i18n";
import "./RuntimeConfiguration.css";

const strategies: Record<string, TranslationKey> = {
  select_on_start: "runtime.strategy.select_on_start",
  no_change: "runtime.strategy.no_change",
  trial_boundary: "runtime.strategy.trial_boundary",
  module: "runtime.strategy.module",
  rebuild: "runtime.strategy.rebuild",
};

export function runtimeBlockReason(runtime: RuntimeConfiguration, module: ConfigurationSection = "engine"): TranslationKey | null {
  const state = runtime.state(module);
  if (!state.blocked) return null;
  if (state.uncertain) return "feedback.confirming";
  if (state.inProgress) return "feedback.applying";
  if (state.recoveryRequired) return "feedback.recovery";
  return "feedback.runtimeUnavailable";
}

/** Routine feedback stays beside the editor actions; failures use the global status. */
export function RuntimeConfigurationProgress({ runtime, module = "engine" }: { runtime: RuntimeConfiguration; module?: ConfigurationSection }) {
  const { t } = useI18n();
  const state = runtime.state(module);
  const operation = state.operation;
  const key = state.uncertain ? "feedback.confirming"
    : state.submitting ? "feedback.submitting"
    : operation?.status === "queued" ? "feedback.queued"
    : operation?.status === "applying" ? operation.stage === "waiting_for_trial_boundary"
      ? "feedback.waitingForTrial" : "feedback.applying"
    : state.reviewing ? "feedback.reviewing" : null;
  return key ? <p className="action-help" role="status">{t(key)}</p> : null;
}

export function RuntimeConfigurationStatus({ runtime }: { runtime: RuntimeConfiguration }) {
  const { t } = useI18n();
  const labels: Record<ConfigurationSection, TranslationKey> = {
    acquisition: "run.acquisition", processing: "run.signalProcessing", stimulation: "run.stimulation", actuation: "run.actuation", engine: "run.engine",
  };
  const states = configurationSections.map((module) => ({ module, ...runtime.state(module) }))
    .filter((state) => state.operation?.error || state.recoveryError || state.requestError || state.ownUncertain
      || typeof state.operation?.result?.warning === "string");
  if (!states.length && !runtime.error) return null;
  return <section className="configuration-status" aria-label={t("runtime.configurationStatus")} aria-live="polite">
    {states.map((state) => <div key={state.module}>
      <strong>{t(labels[state.module])}</strong>
      {state.ownUncertain && <p>{t("runtime.confirming")}</p>}
      {state.operation?.error && <p role="alert">{state.operation.error}{state.operation.rolled_back && t("runtime.previousRestored")}</p>}
      {typeof state.operation?.result?.warning === "string" && <p role="status">{state.operation.result.warning}</p>}
      {state.recoveryError && <p role="alert">{t("runtime.recoveryRequired")} {state.recoveryError}</p>}
      {state.requestError && <p role="alert">{state.requestError}</p>}
      {state.ownUncertain && <button disabled={state.submitting} onClick={() => void runtime.retryUnconfirmed(state.module)}>{t("runtime.checkOrRetry")}</button>}
    </div>)}
    {runtime.error && <><p role="alert">{runtime.error}</p><button onClick={() => void runtime.refresh()}>{t("runtime.refreshStatus")}</button></>}
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
  const state = runtime.state(plan.module);
  const blockedReason = runtimeBlockReason(runtime, plan.module);
  const impact: Record<string, TranslationKey> = {
    select_on_start: "feedback.impactNextStart", no_change: "feedback.impactNoChange",
    trial_boundary: "feedback.impactTrial", module: "feedback.impactModule", rebuild: "feedback.impactRebuild",
  };
  const applyMethod = plan.strategy === "rebuild" && plan.module !== "engine" ? "runtime.strategy.rebuildModule"
    : plan.strategy === "select_on_start" && plan.module === "engine" ? "runtime.strategy.selectEngineOnStart"
    : Object.prototype.hasOwnProperty.call(strategies, plan.strategy) ? strategies[plan.strategy] : null;
  const applyImpact = plan.strategy === "rebuild" && plan.module !== "engine" ? "feedback.impactModuleRestart"
    : plan.strategy === "select_on_start" && plan.module === "engine" ? "feedback.impactNextEngineStart"
    : Object.prototype.hasOwnProperty.call(impact, plan.strategy) ? impact[plan.strategy] : "feedback.impactUnknown";
  return <dialog ref={dialog} className="config-dialog configuration-review" aria-labelledby="configuration-review-title"
    aria-describedby="configuration-review-impact"
    onCancel={(event) => { event.preventDefault(); if (!state.submitting) runtime.dismissPlan(); }}>
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
    {state.requestError && <p className="error" role="alert">{state.requestError}</p>}
    {plan.errors.length > 0 && <ul className="error" role="alert">{plan.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    {stale && <p className="error" role="alert">{t("runtime.stalePlan")}</p>}
    {blockedReason && <p id="configuration-review-blocked" role="status">{t(blockedReason)}</p>}
    </div>
    <div className="actions"><button disabled={state.submitting} onClick={runtime.dismissPlan}>{t("runtime.cancel")}</button>
      <button className="primary" disabled={state.blocked || state.submitting || stale || plan.errors.length > 0}
        aria-describedby={blockedReason ? "configuration-review-blocked" : "configuration-review-impact"}
        onClick={() => void runtime.apply()}>{state.submitting ? t("runtime.submitting") : t("runtime.applyConfiguration")}</button></div>
  </dialog>;
}
