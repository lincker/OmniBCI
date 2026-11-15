/** Configuration operations remain owned by the application across navigation. */
import { useCallback, useEffect, useRef, useState } from "react";
import type { ConfigurationModule } from "../api/plugin-config";
import { ApiError } from "../api/client";
import { useI18n, type TranslationKey, type TranslationParams } from "../i18n";
import { runtimeConfigApi as api, type RuntimePlanRequest, type RuntimePlan,
  type RuntimeStatus, type RuntimeOperation, type ModuleConfigurationRequest, type ModuleActionRequest } from "../api/runtime-config";
import type { ModuleRestoration } from "./useCurrentConfiguration";

export type ConfigurationSection = RuntimePlanRequest["module"];
export const configurationSections: ConfigurationSection[] = ["acquisition", "processing", "stimulation", "actuation", "system"];
export const operationActive = (operation: RuntimeOperation | null | undefined) => operation?.status === "queued" || operation?.status === "applying";
const message = (cause: unknown) => cause instanceof Error ? cause.message : String(cause);
type RequestError = string | { key: TranslationKey; params?: TranslationParams } | null;
type Attempt = {
  reviewing?: boolean; submitting?: boolean; uncertain?: boolean; requestError?: RequestError;
  submission?: { planId?: string; operationId: string }; tracked?: string | null;
  operation?: RuntimeOperation; completed?: RuntimeOperation;
  restoration?: { planId: string; module: ConfigurationModule };
  onApplied?: (operation: RuntimeOperation) => void;
};

export function operationAffects(operation: RuntimeOperation, module: ConfigurationSection) {
  return module === "system" || operation.module === "system"
    || operation.affected_modules.includes("system") || operation.affected_modules.includes(module);
}

export function useRuntimeConfiguration() {
  const { t } = useI18n();
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const statusRef = useRef(status);
  const [plan, setPlan] = useState<RuntimePlan | null>(null);
  const [attempts, setAttempts] = useState<Partial<Record<ConfigurationSection, Attempt>>>({});
  const values = useRef(attempts);
  const [error, setError] = useState<string | null>(null);
  const [completedRestorations, setCompletedRestorations] = useState<Partial<Record<ConfigurationModule, ModuleRestoration>>>({});
  const alive = useRef(true);
  const refreshSequence = useRef(0);
  const update = useCallback((module: ConfigurationSection, changes: Partial<Attempt>) => {
    values.current = { ...values.current, [module]: { ...values.current[module], ...changes } };
    setAttempts(values.current);
  }, []);
  const recordCompletion = useCallback((result: RuntimeOperation) => {
    const attempt = values.current[result.module];
    if (result.result?.configuration_applied === true && attempt?.submission?.operationId === result.operation_id && attempt.onApplied) {
      attempt.onApplied(result);
      update(result.module, { onApplied: undefined });
    }
    if (result.status !== "succeeded") return;
    update(result.module, { completed: result });
    const restoration = values.current[result.module]?.restoration;
    if (restoration && restoration.planId === result.plan_id) {
      const module = restoration.module;
      setCompletedRestorations((previous) => previous[module]?.operationId === result.operation_id ? previous
        : { ...previous, [module]: { module, operationId: result.operation_id, revision: result.target_revisions[module] } });
    }
  }, [update]);
  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    const pending = Object.entries(values.current).filter(([, entry]) => entry.tracked);
    const observed = await Promise.allSettled(pending.map(async ([module, entry]) =>
      [module as ConfigurationSection, await api.operation(entry.tracked!)] as const));
    if (!alive.current) return;
    for (const result of observed) {
      if (result.status !== "fulfilled") continue;
      const [module, operation] = result.value;
      if (values.current[module]?.tracked !== operation.operation_id) continue;
      update(module, { operation, uncertain: false, requestError: null,
        tracked: operationActive(operation) ? operation.operation_id : null });
      recordCompletion(operation);
    }
    try {
      const current = await api.status();
      if (!alive.current || sequence !== refreshSequence.current) return;
      statusRef.current = current; setStatus(current); setError(null);
    } catch (cause) {
      if (alive.current && sequence === refreshSequence.current) setError(message(cause));
    }
  }, [recordCompletion, update]);
  useEffect(() => {
    alive.current = true;
    let mounted = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => { await refresh(); if (mounted) timer = setTimeout(poll, 1000); };
    void poll();
    return () => { mounted = false; alive.current = false; clearTimeout(timer); };
  }, [refresh]);

  const state = (module: ConfigurationSection) => {
    const current = statusRef.current;
    const own = values.current[module] ?? {};
    const operation = own.tracked ? own.operation ?? current?.operations?.[module] ?? null
      : current?.operations?.[module] ?? own.operation ?? null;
    const related = configurationSections.filter((section) => {
      const entry = values.current[section];
      const running = current?.operations?.[section];
      return module === "system" || section === "system" || section === module
        || !!running && operationActive(running) && operationAffects(running, module)
        || !!entry?.operation && operationActive(entry.operation) && operationAffects(entry.operation, module);
    });
    const uncertain = related.some((section) => values.current[section]?.uncertain);
    const inProgress = related.some((section) => {
      const entry = values.current[section];
      return entry?.submitting || entry?.uncertain || operationActive(entry?.operation) || operationActive(current?.operations?.[section]);
    });
    const recoveryRequired = Object.keys(current?.recovery ?? {}).some((section) => module === "system" || section === "system" || section === module);
    const requestError = own.requestError;
    return { operation, completedOperation: own.completed, reviewing: !!own.reviewing, submitting: !!own.submitting,
      uncertain, ownUncertain: !!own.uncertain, recoveryError: current?.recovery?.[module], inProgress, recoveryRequired, blocked: !current?.operations || !current.recovery || inProgress || recoveryRequired || !!error
        || !!current?.errors?.[module] || !!current?.errors?.system,
      requestError: requestError && typeof requestError !== "string" ? t(requestError.key, requestError.params) : requestError ?? null };
  };
  const review = async (body: RuntimePlanRequest) => {
    const module = body.module;
    if (values.current[module]?.reviewing || state(module).blocked) return null;
    update(module, { reviewing: true, requestError: null, submission: undefined, restoration: undefined });
    setPlan(null);
    try { const target = await api.plan(body); setPlan(target); return target; }
    catch (cause) { update(module, { requestError: message(cause) }); await refresh(); return null; }
    finally { update(module, { reviewing: false }); }
  };
  const apply = async (target = plan): Promise<boolean> => {
    if (!target || target.errors.length || values.current[target.module]?.reviewing || state(target.module).blocked) return false;
    const module = target.module;
    const previous = values.current[module]?.submission;
    const submission = previous?.planId === target.plan_id ? previous
      : { planId: target.plan_id, operationId: crypto.randomUUID() };
    update(module, { submitting: true, requestError: null, submission, tracked: submission.operationId, uncertain: true });
    try {
      const result = await api.apply(target.plan_id, submission.operationId);
      update(module, { operation: result, uncertain: false });
      setPlan((current) => current?.plan_id === target.plan_id ? null : current);
      recordCompletion(result);
      if (!operationActive(result)) update(module, { tracked: null });
      await refresh(); return result.status === "succeeded";
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        update(module, { tracked: null, uncertain: false, requestError: { key: "runtime.reloadSelection", params: { error: message(cause) } } });
      } else update(module, { uncertain: true, requestError: { key: "runtime.applyInterrupted" } });
      setPlan((current) => current?.plan_id === target.plan_id ? null : current);
      await refresh(); return false;
    } finally { update(module, { submitting: false }); }
  };
  const execute = async (module: ConfigurationModule, configuration: ModuleConfigurationRequest,
    send: (body: ModuleActionRequest) => Promise<RuntimeOperation | null>,
    onApplied?: (operation: RuntimeOperation) => void): Promise<RuntimeOperation> => {
    if (state(module).blocked || values.current[module]?.reviewing) throw new Error(t("feedback.runtimeUnavailable"));
    const operationId = crypto.randomUUID();
    update(module, { submitting: true, uncertain: true, tracked: operationId,
      submission: { operationId }, restoration: undefined, requestError: null, onApplied });
    try {
      const result = await send({ operation_id: operationId, configuration });
      if (!result || result.operation_id !== operationId || result.module !== module || operationActive(result)) {
        throw new Error(t("runtime.applyInterrupted"));
      }
      update(module, { operation: result, uncertain: false, tracked: null });
      recordCompletion(result);
      return result;
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        update(module, { tracked: null, uncertain: false, requestError: message(cause) });
      } else update(module, { requestError: { key: "runtime.applyInterrupted" } });
      throw cause;
    } finally { update(module, { submitting: false }); }
  };
  const retryUnconfirmed = async (module: ConfigurationSection) => {
    const entry = values.current[module];
    const pending = entry?.submission;
    if (!pending || entry.submitting) return;
    update(module, { submitting: true });
    try {
      let result: RuntimeOperation;
      try { result = await api.operation(pending.operationId); }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause;
        if (!pending.planId) throw cause;
        result = await api.apply(pending.planId, pending.operationId);
      }
      update(module, { operation: result, uncertain: false, requestError: null, tracked: operationActive(result) ? result.operation_id : null });
      recordCompletion(result); await refresh();
    } catch (cause) {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        update(module, { tracked: null, uncertain: false, requestError: { key: "runtime.reloadSelection", params: { error: message(cause) } } });
        await refresh();
      } else update(module, { requestError: message(cause) });
    } finally { update(module, { submitting: false }); }
  };
  return { status, plan, error, state, completedRestorations, inProgress: state("system").inProgress,
    blocked: state("system").blocked, review, apply, execute, refresh, retryUnconfirmed,
    reviewPlan: (target: RuntimePlan, restoreModule?: ConfigurationModule) => {
      if (state(target.module).blocked || values.current[target.module]?.reviewing) return;
      update(target.module, { restoration: restoreModule ? { planId: target.plan_id, module: restoreModule } : undefined,
        requestError: null, submission: undefined });
      setPlan(target);
    }, dismissPlan: () => { if (!plan || !values.current[plan.module]?.submitting) setPlan(null); } };
}

export type RuntimeConfiguration = ReturnType<typeof useRuntimeConfiguration>;
