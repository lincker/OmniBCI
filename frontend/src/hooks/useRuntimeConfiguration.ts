/** One console-wide operation tracker; navigation never abandons an apply. */
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import { useI18n, type TranslationKey, type TranslationParams } from "../i18n";
import { runtimeConfigApi as api, type RuntimePlanRequest, type RuntimePlan,
  type RuntimeStatus, type RuntimeOperation } from "../api/runtime-config";
import { pluginConfigApi as profilesApi, selectionKey, type PluginProfile } from "../api/plugin-config";
import { sameParameters } from "../SchemaForm";
import { initialProfileReview, normalizedProfileParameters, planIsStale, sameReviewedImpact,
  type ProfileReviewDraft, type ProfileReviewMode, type ProfileReviewState,
  type SavedReviewProfile, type ReviewedRuntimeOperation } from "../profileReview";

const active = (operation: RuntimeOperation | null) => operation?.status === "queued" || operation?.status === "applying";
const message = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);
type ReviewContext = { reviewedPlanId: string; planId: string; request: RuntimePlanRequest; state: ProfileReviewState };
type PendingProfileSave = { context: ReviewContext; mode: "create" | "update"; name: string; beforeIds: Set<string> };

export function useRuntimeConfiguration() {
  const { t } = useI18n();
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [operation, setOperation] = useState<ReviewedRuntimeOperation | null>(null);
  const [plan, setPlan] = useState<RuntimePlan | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | { key: TranslationKey; params?: TranslationParams } | null>(null);
  // Keep our confirmed result even when the server reports a newer global operation.
  const [completedOperation, setCompletedOperation] = useState<ReviewedRuntimeOperation | null>(null);
  const [profileReview, setProfileReview] = useState<ProfileReviewState | null>(null);
  const [savedProfile, setSavedProfile] = useState<SavedReviewProfile | null>(null);
  const tracked = useRef<string | null>(null);
  const alive = useRef(true);
  const refreshSequence = useRef(0);
  const submission = useRef<{ planId: string; operationId: string; reviewedPlanId?: string; profile?: PluginProfile } | null>(null);
  const reviewContext = useRef<ReviewContext | null>(null);
  const pendingProfileSave = useRef<PendingProfileSave | null>(null);
  const applying = useRef(false);

  const recordCompletion = useCallback((result: RuntimeOperation) => {
    if (result.status !== "succeeded") return;
    const pending = submission.current;
    const own = pending && result.plan_id === pending.planId ? pending : null;
    setCompletedOperation(own?.reviewedPlanId ? { ...result, reviewed_plan_id: own.reviewedPlanId,
      ...(own.profile ? { profile: own.profile } : {}) } : result);
    if (own?.reviewedPlanId) setSavedProfile((previous) => previous !== null && previous.reviewed_plan_id === own.reviewedPlanId
      ? { ...previous, applied: true } : previous);
  }, []);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    try {
      const pendingId = tracked.current;
      const observed = pendingId ? await api.operation(pendingId) : null;
      const current = await api.status();
      if (!alive.current || sequence !== refreshSequence.current) return;
      setStatus(current); setOperation(current.operation ?? observed); setError(null);
      if (observed) {
        recordCompletion(observed);
        setUncertain(false); setRequestError(null);
        if (!active(observed)) tracked.current = null;
      }
    } catch (cause) {
      if (alive.current && sequence === refreshSequence.current) setError(message(cause));
    }
  }, [recordCompletion]);

  useEffect(() => {
    alive.current = true;
    let mounted = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refresh();
      if (mounted) timer = setTimeout(poll, 1000);
    };
    void poll();
    return () => { mounted = false; alive.current = false; clearTimeout(timer); };
  }, [refresh]);

  const updateProfileReview = (next: ProfileReviewState) => {
    if (reviewContext.current) reviewContext.current.state = next;
    setProfileReview(next);
  };
  const setProfileReviewMode = (mode: ProfileReviewMode) => {
    const current = reviewContext.current?.state;
    if (!current || applying.current || current.savedProfile || current.saveUncertain || current.mode === "existing"
        || mode === "existing" || (mode === "update" && (!current.draft.profile
          || selectionKey(current.draft.profile.selection) !== selectionKey(current.draft.selection)))) return;
    updateProfileReview({ ...current, mode, name: mode === "update" ? current.draft.profile!.name : current.createName,
      createName: current.mode === "create" ? current.name : current.createName });
  };
  const setProfileReviewName = (name: string) => {
    const current = reviewContext.current?.state;
    if (!current || applying.current || current.savedProfile || current.saveUncertain || current.mode === "existing") return;
    updateProfileReview({ ...current, name, ...(current.mode === "create" ? { createName: name } : {}) });
  };

  const review = useCallback(async (body: RuntimePlanRequest, draft?: ProfileReviewDraft, expectedProfiles: PluginProfile[] = []) => {
    if (pendingProfileSave.current || applying.current) {
      setRequestError({ key: "runtime.profileSaveUncertain" }); return null;
    }
    setReviewing(true); setRequestError(null); setPlan(null); submission.current = null;
    reviewContext.current = null; setProfileReview(null); setSavedProfile(null);
    try {
      const target = await api.plan(body);
      // Plans pin their parameter snapshot. Verify its profiles before exposing Apply.
      const kinds = [...new Set(expectedProfiles.map((profile) => profile.kind))];
      const latest = (await Promise.all(kinds.map((kind) => profilesApi.profiles(kind)))).flat();
      if (expectedProfiles.some((expected) => !latest.some((profile) => profile.profile_id === expected.profile_id
          && profile.revision === expected.revision))) {
        setRequestError({ key: "runtime.profileSaveConflict" }); return null;
      }
      if (draft) {
        const state = initialProfileReview(draft, t("profiles.copyName", { name: draft.profile?.name ?? draft.name }).slice(0, 64));
        reviewContext.current = { reviewedPlanId: target.plan_id, planId: target.plan_id,
          request: structuredClone(body), state };
        setProfileReview(state);
      }
      setPlan(target); return target;
    } catch (cause) { setRequestError(message(cause)); await refresh(); return null; }
    finally { setReviewing(false); }
  }, [refresh, t]);

  const acceptSavedProfile = (context: ReviewContext, profile: PluginProfile) => {
    context.state = { ...context.state, savedProfile: profile, saveUncertain: false };
    setProfileReview(context.state);
    setSavedProfile({ reviewed_plan_id: context.reviewedPlanId, profile, applied: false });
    pendingProfileSave.current = null;
  };
  const reconcileProfileSave = async (pending: PendingProfileSave): Promise<PluginProfile | null> => {
    const { draft } = pending.context.state;
    const parameters = normalizedProfileParameters(draft.parameters, draft.schema);
    const candidates = (await profilesApi.profiles(draft.kind)).filter((item) => item.name === pending.name
      && selectionKey(item.selection) === selectionKey(draft.selection) && sameParameters(item.parameters, parameters)
      && (pending.mode === "update" ? item.profile_id === draft.profile!.profile_id && item.revision > draft.profile!.revision
        : !pending.beforeIds.has(item.profile_id)));
    return candidates.length === 1 ? candidates[0] : null;
  };
  const retryProfileSave = async () => {
    const pending = pendingProfileSave.current;
    if (!pending || applying.current) return;
    applying.current = true; setSubmitting(true);
    try {
      const saved = await reconcileProfileSave(pending);
      if (saved) { acceptSavedProfile(pending.context, saved); setRequestError(null); }
      else setRequestError({ key: "runtime.profileSaveUncertain" });
    } catch { setRequestError({ key: "runtime.profileSaveUncertain" }); }
    finally { applying.current = false; setSubmitting(false); }
  };

  const abandonProfileSave = () => {
    if (!pendingProfileSave.current || applying.current) return;
    pendingProfileSave.current = null; reviewContext.current = null;
    setProfileReview(null); setPlan(null);
    setRequestError({ key: "runtime.profileSaveAbandoned" });
  };

  const apply = async (target = plan, waitForCompletion = false): Promise<boolean> => {
    if (!target || target.errors.length || applying.current || submitting || active(operation) || uncertain
        || pendingProfileSave.current || status?.recovery_required !== false || error) return false;
    applying.current = true; setSubmitting(true); setRequestError(null);
    let phase: "prepare" | "apply" = "prepare";
    let context = reviewContext.current?.planId === target.plan_id ? reviewContext.current : null;
    let profile: PluginProfile | undefined;
    try {
      if (context) {
        // Check the reviewed runtime before making a separate, persistent profile write.
        const current = await api.status();
        setStatus(current);
        if (planIsStale(target, current) || current.recovery_required || active(current.operation)) {
          setRequestError({ key: "runtime.stalePlan" }); return false;
        }
        const state = context.state;
        if (state.mode !== "existing" && (!state.name.trim() || state.name.trim().length > 64 || /[\u0000-\u001f]/.test(state.name))) {
          setRequestError({ key: "runtime.profileNameRequired" }); return false;
        }
        profile = state.savedProfile ?? undefined;
        if (state.mode === "existing") {
          const latest = (await profilesApi.profiles(state.draft.kind))
            .find((item) => item.profile_id === state.draft.profile!.profile_id);
          if (!latest || selectionKey(latest.selection) !== selectionKey(state.draft.selection)
              || !sameParameters(latest.parameters, normalizedProfileParameters(state.draft.parameters, state.draft.schema))) {
            setRequestError({ key: "runtime.profileSaveConflict" }); return false;
          }
          profile = latest;
        }
        if (!profile) {
          const before = await profilesApi.profiles(state.draft.kind);
          const pending: PendingProfileSave = { context, mode: state.mode as "create" | "update",
            name: state.name.trim(), beforeIds: new Set(before.map((item) => item.profile_id)) };
          try {
            profile = state.mode === "update"
              ? await profilesApi.update(state.draft.profile!, pending.name, state.draft.parameters)
              : await profilesApi.create(state.draft.kind, state.draft.selection, pending.name, state.draft.parameters);
          } catch (cause) {
            if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
              setRequestError(cause.status === 409 ? { key: "runtime.profileSaveConflict" } : message(cause));
              return false;
            }
            pendingProfileSave.current = pending;
            context.state = { ...state, saveUncertain: true };
            setProfileReview(context.state);
            try { profile = await reconcileProfileSave(pending) ?? undefined; } catch { /* Keep the unresolved write visible. */ }
            if (!profile) { setRequestError({ key: "runtime.profileSaveUncertain" }); return false; }
          }
          acceptSavedProfile(context, profile);
        }
        const next = await api.plan({ ...context.request, bindings: context.request.bindings.map((binding) =>
          binding.slot === "acquisition.source" ? { slot: binding.slot, profile_id: profile!.profile_id } : binding) });
        context.planId = next.plan_id;
        setPlan(next);
        if (next.errors.length || !sameReviewedImpact(target, next)) {
          setRequestError({ key: "runtime.profilePlanChanged" });
          // A changed target could be a concurrent profile edit; its values were never reviewed.
          if (next.target_revision !== target.target_revision) setPlan(null);
          return false;
        }
        target = next;
      }
      const pending = submission.current?.planId === target.plan_id ? submission.current
        : { planId: target.plan_id, operationId: crypto.randomUUID(),
          ...(context ? { reviewedPlanId: context.reviewedPlanId, profile } : {}) };
      submission.current = pending; tracked.current = pending.operationId;
      phase = "apply"; setUncertain(true);
      let result = await api.apply(pending.planId, pending.operationId);
      setOperation(result); setUncertain(false); setPlan(null);
      const deadline = Date.now() + 30_000;
      while (waitForCompletion && active(result) && alive.current && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 250));
        result = await api.operation(pending.operationId);
        if (alive.current) setOperation(result);
      }
      recordCompletion(result);
      if (!active(result)) tracked.current = null;
      await refresh();
      return result.status === "succeeded";
    } catch (cause) {
      if (phase === "prepare") {
        setRequestError(message(cause));
      } else if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        tracked.current = null; setUncertain(false); setPlan(null);
        setRequestError({ key: "runtime.reloadSelection", params: { error: message(cause) } });
      } else {
        setUncertain(true); setPlan(null);
        setRequestError({ key: "runtime.applyInterrupted" });
      }
      await refresh(); return false;
    } finally { applying.current = false; setSubmitting(false); }
  };

  const applySelection = async (body: RuntimePlanRequest): Promise<boolean> => {
    if (applying.current || submitting || active(operation) || uncertain || pendingProfileSave.current
        || status?.recovery_required !== false || error) return false;
    setReviewing(true); setRequestError(null); setPlan(null); submission.current = null;
    reviewContext.current = null; setProfileReview(null);
    try {
      const target = await api.plan(body);
      if (target.errors.length) { setRequestError(target.errors.join("; ")); return false; }
      return await apply(target, true);
    } catch (cause) { setRequestError(message(cause)); await refresh(); return false; }
    finally { setReviewing(false); }
  };

  const retryUnconfirmed = async () => {
    const pending = submission.current;
    if (!pending || applying.current || submitting) return;
    applying.current = true; setSubmitting(true);
    try {
      let result: RuntimeOperation;
      try { result = await api.operation(pending.operationId); }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
        result = await api.apply(pending.planId, pending.operationId);
      }
      setOperation(result); setUncertain(false); setRequestError(null);
      recordCompletion(result);
      tracked.current = active(result) ? result.operation_id : null;
      await refresh();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        tracked.current = null; setUncertain(false);
        setRequestError({ key: "runtime.reloadSelection", params: { error: message(cause) } });
        await refresh();
      } else setRequestError(message(cause));
    } finally { applying.current = false; setSubmitting(false); }
  };

  const inProgress = submitting || uncertain || active(operation);
  const recoveryRequired = status?.recovery_required === true;
  return { status, operation, completedOperation, plan, reviewing, submitting, uncertain, error,
    profileReview, savedProfile, setProfileReviewMode, setProfileReviewName, retryProfileSave, abandonProfileSave,
    profileSaveNotice: savedProfile ? t(savedProfile.applied ? "runtime.profileSavedApplied" : "runtime.profileSavedPending",
      { name: savedProfile.profile.name }) : null,
    requestError: requestError && typeof requestError !== "string" ? t(requestError.key, requestError.params) : requestError,
    inProgress, recoveryRequired, blocked: inProgress || !!profileReview?.saveUncertain || status?.recovery_required !== false || !!error,
    review, apply, applySelection, refresh, retryUnconfirmed, dismissPlan: () => {
      if (applying.current) return;
      setPlan(null);
      if (!pendingProfileSave.current) { reviewContext.current = null; setProfileReview(null); }
    } };
}

export type RuntimeConfiguration = ReturnType<typeof useRuntimeConfiguration>;
