import { useEffect, useRef } from "react";
import type { RuntimeConfiguration } from "./hooks/useRuntimeConfiguration";
import { useI18n, type TranslationKey } from "./i18n";
import { schemaText } from "./SchemaForm";
import { formatProfileValue, profileParameterChanges } from "./profileReviewDiff";
import { normalizedProfileParameters } from "./profileReview";
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
      && !runtime.requestError && !runtime.uncertain && !runtime.profileSaveNotice && !runtime.profileReview?.saveUncertain) return null;
  return <section className="configuration-status" aria-label={t("runtime.configurationStatus")} aria-live="polite">
    {runtime.profileSaveNotice && <p role="status">{runtime.profileSaveNotice}</p>}
    {runtime.uncertain && <p>{t("runtime.confirming")}</p>}
    {operation?.error && <p role="alert">{operation.error}{operation.rolled_back && t("runtime.previousRestored")}</p>}
    {runtime.recoveryRequired && <p role="alert">{t("runtime.recoveryRequired")}</p>}
    {(runtime.error || runtime.requestError) && <p role="alert">{runtime.requestError ?? runtime.error}</p>}
    {runtime.profileReview?.saveUncertain && <>
      <p id="profile-save-recovery-help">{t("runtime.profileAbandonSaveHelp")}</p>
      <div className="actions"><button disabled={runtime.submitting} onClick={() => void runtime.retryProfileSave()}>{t("runtime.profileCheckSave")}</button>
        <button disabled={runtime.submitting} aria-describedby="profile-save-recovery-help"
          onClick={runtime.abandonProfileSave}>{t("runtime.returnToEditing")}</button></div>
    </>}
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
  const blockedReason = runtimeBlockReason(runtime);
  const profileReview = runtime.profileReview;
  const originalProfile = profileReview?.draft.profile;
  const changes = originalProfile && profileReview
    ? profileParameterChanges(normalizedProfileParameters(originalProfile.parameters, profileReview.draft.schema),
      normalizedProfileParameters(profileReview.draft.parameters, profileReview.draft.schema), profileReview.draft.schema,
      (label) => schemaText(label, t)) : [];
  const invalidName = !!profileReview && profileReview.mode !== "existing" && !profileReview.savedProfile && !profileReview.name.trim();
  const profileApplyLabel: TranslationKey = !profileReview || profileReview.mode === "existing" ? "runtime.applyConfiguration"
    : profileReview.savedProfile ? "runtime.profileApplySaved"
    : profileReview.mode === "update" ? "runtime.profileUpdateApply"
    : originalProfile ? "runtime.profileCreateApply" : "runtime.profileSaveApply";
  const displayValue = (value: unknown, present: boolean, secret: boolean) => !present ? t("runtime.profileNotSet")
    : secret ? t("runtime.profileSecretValue") : formatProfileValue(value);
  const impact: Record<string, TranslationKey> = {
    select_on_start: "feedback.impactNextStart", no_change: "feedback.impactNoChange",
    trial_boundary: "feedback.impactTrial", module: "feedback.impactModule", rebuild: "feedback.impactRebuild",
  };
  return <dialog ref={dialog} className="config-dialog configuration-review" aria-labelledby="configuration-review-title"
    aria-describedby="configuration-review-impact"
    onCancel={(event) => { event.preventDefault(); if (!runtime.submitting) runtime.dismissPlan(); }}>
    <div className="configuration-review-content">
    <h3 id="configuration-review-title">{t("runtime.reviewTitle")}</h3>
    <p id="configuration-review-impact">{t(Object.prototype.hasOwnProperty.call(impact, plan.strategy)
      ? impact[plan.strategy] : "feedback.impactUnknown")}</p>
    {profileReview && <section className="profile-review" aria-labelledby="profile-review-title">
      <h4 id="profile-review-title">{t("runtime.profileReviewTitle")}</h4>
      <p>{!originalProfile ? t("runtime.profileFirstSave") : t(changes.length
        ? "runtime.profileChanged" : "runtime.profileUnchanged", { name: originalProfile.name })}</p>
      {changes.length > 0 && <div className="profile-review-diff-scroll">
        <table className="profile-review-diff">
          <caption>{t("runtime.profileDiff")}</caption>
          <thead><tr><th scope="col">{t("runtime.profileDiffField")}</th>
            <th scope="col">{t("runtime.profileDiffBefore")}</th><th scope="col">{t("runtime.profileDiffAfter")}</th></tr></thead>
          <tbody>{changes.map((change) => <tr key={change.path}>
            <th scope="row">{change.label}</th>
            <td><pre>{displayValue(change.before, change.beforePresent, change.secret)}</pre></td>
            <td><pre>{displayValue(change.after, change.afterPresent, change.secret)}</pre></td>
          </tr>)}</tbody>
        </table>
      </div>}
      {changes.length > 0 && !profileReview.savedProfile && <fieldset className="profile-review-choices"
        disabled={runtime.submitting || profileReview.saveUncertain}>
        <legend>{t("runtime.profileSaveChoice")}</legend>
        <label><input type="radio" name="profile-save-mode" value="create" checked={profileReview.mode === "create"}
          onChange={() => runtime.setProfileReviewMode("create")} />
          <span><strong>{t("runtime.profileCreateChoice")}</strong><small>{t("runtime.profileCreateHint")}</small></span></label>
        <label><input type="radio" name="profile-save-mode" value="update" checked={profileReview.mode === "update"}
          onChange={() => runtime.setProfileReviewMode("update")} />
          <span><strong>{t("runtime.profileUpdateChoice")}</strong><small>{t("runtime.profileUpdateHint", { name: originalProfile?.name ?? "" })}</small></span></label>
      </fieldset>}
      {profileReview.mode !== "existing" && !profileReview.savedProfile && <label className="profile-review-name">
        <span>{t("runtime.profileName")}</span>
        <input value={profileReview.name} maxLength={64} disabled={runtime.submitting || profileReview.saveUncertain}
          aria-invalid={invalidName || undefined} aria-describedby="profile-review-name-help"
          onChange={(event) => runtime.setProfileReviewName(event.target.value)} />
        <small id="profile-review-name-help">{t(invalidName ? "runtime.profileNameRequired" : "runtime.profileNameHint")}</small>
      </label>}
      <p className="action-help">{t("runtime.profileNotVerified")}</p>
    </section>}
    <dl>
      <dt>{t("runtime.applyMethod")}</dt><dd>{Object.prototype.hasOwnProperty.call(strategies, plan.strategy) ? t(strategies[plan.strategy]) : plan.strategy}</dd>
      <dt>{t("runtime.affectedModules")}</dt><dd>{plan.changed_slots.length ? plan.changed_slots.join(", ") : t("runtime.noPluginChanges")}</dd>
      {plan.changed_settings.length > 0 && <><dt>{t("runtime.hostSettings")}</dt><dd>{plan.changed_settings.join(", ")}</dd></>}
      {plan.signal_summary && <><dt>{t("runtime.channelSetup")}</dt><dd>{plan.signal_summary}</dd></>}
      <dt>{t("runtime.targetRevision")}</dt><dd><code>{plan.target_revision ? plan.target_revision.slice(0, 12) : t("run.none")}</code></dd>
    </dl>
    {runtime.profileSaveNotice && <p role="status">{runtime.profileSaveNotice}</p>}
    {runtime.requestError && <p className="error" role="alert">{runtime.requestError}</p>}
    {plan.errors.length > 0 && <ul className="error" role="alert">{plan.errors.map((error) => <li key={error}>{error}</li>)}</ul>}
    {stale && <p className="error" role="alert">{t("runtime.stalePlan")}</p>}
    {blockedReason && <p id="configuration-review-blocked" role="status">{t(blockedReason)}</p>}
    {profileReview?.saveUncertain && <p id="profile-review-recovery-help">{t("runtime.profileAbandonSaveHelp")}</p>}
    </div>
    <div className="actions"><button disabled={runtime.submitting} onClick={runtime.dismissPlan}>{t("runtime.cancel")}</button>
      {profileReview?.saveUncertain && <>
        <button disabled={runtime.submitting} onClick={() => void runtime.retryProfileSave()}>{t("runtime.profileCheckSave")}</button>
        <button disabled={runtime.submitting} aria-describedby="profile-review-recovery-help"
          onClick={runtime.abandonProfileSave}>{t("runtime.returnToEditing")}</button>
      </>}
      <button className="primary" disabled={runtime.blocked || runtime.submitting || stale || plan.errors.length > 0 || invalidName || profileReview?.saveUncertain}
        aria-describedby={blockedReason ? "configuration-review-blocked" : "configuration-review-impact"}
        onClick={() => void runtime.apply()}>{runtime.submitting ? t("runtime.submitting") : t(profileApplyLabel)}</button></div>
  </dialog>;
}
