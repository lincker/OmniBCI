/** One console-wide operation tracker; navigation never abandons an apply. */
import { useCallback, useEffect, useRef, useState } from "react";
import type { RevisionMap, ConfigurationModule } from "../api/plugin-config";
import { ApiError } from "../api/client";
import { useI18n, type TranslationKey, type TranslationParams } from "../i18n";
import { runtimeConfigApi as api, type RuntimePlanRequest, type RuntimePlan,
  type RuntimeStatus, type RuntimeOperation } from "../api/runtime-config";

const active = (operation: RuntimeOperation | null) => operation?.status === "queued" || operation?.status === "applying";
const message = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);

export function useRuntimeConfiguration() {
  const { t } = useI18n();
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [operation, setOperation] = useState<RuntimeOperation | null>(null);
  const [plan, setPlan] = useState<RuntimePlan | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | { key: TranslationKey; params?: TranslationParams } | null>(null);
  // Keep our confirmed result even when the server reports a newer global operation.
  const [completedOperation, setCompletedOperation] = useState<RuntimeOperation | null>(null);
  const [completedRestoration, setCompletedRestoration] = useState<{ module: ConfigurationModule; operationId: string; revision: string } | null>(null);
  const restoration = useRef<{ planId: string; module: ConfigurationModule } | null>(null);
  const restoredOperation = useRef<string | null>(null);
  const tracked = useRef<string | null>(null);
  const alive = useRef(true);
  const refreshSequence = useRef(0);
  const submission = useRef<{ planId: string; operationId: string } | null>(null);
  const applying = useRef(false);
  const planning = useRef(false);

  const recordCompletion = useCallback((result: RuntimeOperation) => {
    if (result.status !== "succeeded") return;
    setCompletedOperation(result);
    if (restoration.current && restoration.current.planId === result.plan_id && restoredOperation.current !== result.operation_id) {
      const module = restoration.current.module;
      restoredOperation.current = result.operation_id;
      setCompletedRestoration({ module, operationId: result.operation_id, revision: result.target_revisions[module] });
    }
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

  const review = useCallback(async (body: RuntimePlanRequest) => {
    if (applying.current || planning.current) { setRequestError({ key: "feedback.busy" }); return null; }
    planning.current = true;
    setReviewing(true); setRequestError(null); setPlan(null); submission.current = null;
    try {
      const target = await api.plan(body);
      setPlan(target); return target;
    } catch (cause) { setRequestError(message(cause)); await refresh(); return null; }
    finally { planning.current = false; setReviewing(false); }
  }, [refresh]);

  const apply = async (target = plan, waitForCompletion = false): Promise<boolean> => {
    if (!target || target.errors.length || planning.current || applying.current || submitting || active(operation) || uncertain
        || status?.recovery_required !== false || error) return false;
    applying.current = true; setSubmitting(true); setRequestError(null);
    try {
      const pending = submission.current?.planId === target.plan_id ? submission.current
        : { planId: target.plan_id, operationId: crypto.randomUUID() };
      submission.current = pending; tracked.current = pending.operationId;
      setUncertain(true);
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
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        tracked.current = null; setUncertain(false); setPlan(null);
        setRequestError({ key: "runtime.reloadSelection", params: { error: message(cause) } });
      } else {
        setUncertain(true); setPlan(null);
        setRequestError({ key: "runtime.applyInterrupted" });
      }
      await refresh(); return false;
    } finally { applying.current = false; setSubmitting(false); }
  };

  const applyCurrentSelection = async (body: RuntimePlanRequest): Promise<RevisionMap | null> => {
    if (planning.current || applying.current || submitting || active(operation) || uncertain
        || status?.recovery_required !== false || error) return null;
    planning.current = true;
    setReviewing(true); setRequestError(null); setPlan(null); submission.current = null;
    try {
      const target = await api.plan(body);
      if (target.errors.length) { setRequestError(target.errors.join("; ")); return null; }
      planning.current = false;
      return await apply(target, true) ? { ...target.expected_revisions, ...target.target_revisions } : null;
    } catch (cause) { setRequestError(message(cause)); await refresh(); return null; }
    finally { planning.current = false; setReviewing(false); }
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
  return { status, operation, completedOperation, completedRestoration, plan, reviewing, submitting, uncertain, error,
    requestError: requestError && typeof requestError !== "string" ? t(requestError.key, requestError.params) : requestError,
    inProgress, recoveryRequired, blocked: inProgress || status?.recovery_required !== false || !!error,
    review, reviewPlan: (target: RuntimePlan, restoreModule?: ConfigurationModule) => { if (!applying.current && !planning.current) { restoration.current = restoreModule ? { planId: target.plan_id, module: restoreModule } : null; setPlan(target); setRequestError(null); submission.current = null; } }, apply, applyCurrentSelection, refresh, retryUnconfirmed, dismissPlan: () => {
      if (applying.current) return;
      setPlan(null);
    } };
}

export type RuntimeConfiguration = ReturnType<typeof useRuntimeConfiguration>;
