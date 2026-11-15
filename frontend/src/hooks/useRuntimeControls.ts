/** Own command continuations and feedback independently of the visible page. */
import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { pluginConfigApi, type ModuleActivation, type RevisionMap } from "../api/plugin-config";
import { signalConfigApi } from "../api/signal-config";
import { assertConfigurationRevision } from "../source-lifecycle";
import { useI18n, type TranslationKey } from "../i18n";
import { usePoll } from "./usePoll";
import type { AcquisitionController } from "./useAcquisition";
import type { ConfigurationModule, CurrentConfigurationController } from "./useCurrentConfiguration";
import type { ConfigurationSection, RuntimeConfiguration } from "./useRuntimeConfiguration";

type ActionName = "start" | "stop" | "launch" | "stopStim" | "trigger" | "readCurrent" | "stopRead" | "startProcessing" | "stopProcessing" | "startActuation" | "stopActuation";
type ActionState = { pending: boolean; error: string | null };
const actionModules: Record<ActionName, ConfigurationSection> = {
  start: "system", stop: "system", launch: "stimulation", stopStim: "stimulation", trigger: "stimulation",
  readCurrent: "acquisition", stopRead: "acquisition", startProcessing: "processing", stopProcessing: "processing", startActuation: "actuation", stopActuation: "actuation",
};

export function useRuntimeControls(runtime: RuntimeConfiguration, current: CurrentConfigurationController,
  acquisition: AcquisitionController, refreshEngine: () => Promise<void>) {
  const { t } = useI18n();
  const stimulation = usePoll(api.stimulationStatus);
  const signal = usePoll(signalConfigApi.get);
  const [actions, setActions] = useState<Partial<Record<ActionName, ActionState>>>({});
  const values = useRef(actions);
  const intent = useRef<Partial<Record<ConfigurationSection, number>>>({});
  const [notices, setNotices] = useState<Partial<Record<ConfigurationModule, TranslationKey>>>({});
  const update = (name: ActionName, value: ActionState) => {
    values.current = { ...values.current, [name]: value }; setActions(values.current);
  };
  const pending = (module: ConfigurationSection) => Object.entries(values.current).some(([name, state]) =>
    state.pending && (module === "system" || actionModules[name as ActionName] === module))
    || ((module === "system" || module === "acquisition") && acquisition.pending);
  const run = async (name: ActionName, action: (valid: () => boolean) => Promise<unknown>, stop = false) => {
    const module = actionModules[name];
    if (values.current[name]?.pending || !stop && (pending(module) || values.current.start?.pending || values.current.stop?.pending)) return;
    if (stop) {
      if (module === "system") {
        for (const section of ["system", "acquisition", "processing", "stimulation", "actuation"] as const) intent.current[section] = (intent.current[section] ?? 0) + 1;
        acquisition.cancelPending();
      } else intent.current[module] = (intent.current[module] ?? 0) + 1;
    }
    const token = intent.current[module] ?? 0;
    const valid = () => (intent.current[module] ?? 0) === token;
    update(name, { pending: true, error: null });
    try { await action(valid); }
    catch (cause) { update(name, { pending: true, error: cause instanceof ApiError ? `[${cause.code}] ${cause.message}` : String(cause) }); }
    finally {
      if (module === "system") await Promise.all([acquisition.refresh(), stimulation.refresh(), signal.refresh()]);
      else if (module === "acquisition") await acquisition.refresh();
      else if (module === "stimulation") await Promise.all([stimulation.refresh(), runtime.refresh()]);
      else await Promise.all([refreshEngine(), runtime.refresh(), ...(module === "processing" ? [signal.refresh()] : [])]);
      update(name, { pending: false, error: values.current[name]?.error ?? null });
    }
  };
  const runCurrent = async (module: ConfigurationModule, applyCurrent: boolean, valid: () => boolean,
    action: (revisions: RevisionMap, accepted: ModuleActivation) => Promise<unknown>) => {
    if (!current.modules[module].ready) throw new Error(current.modules[module].error ?? t("run.reason.configurationLoading"));
    let accepted = await pluginConfigApi.module(module);
    if (!valid()) return;
    let expected: RevisionMap = { [module]: accepted.revision, system: runtime.status?.accepted.system ?? null };
    if (current.hasChanges(module, accepted)) {
      if (!applyCurrent) throw new Error(t("run.reason.reconnectCurrentSource"));
      const revisions = await runtime.applyCurrentSelection(current.request(module, accepted));
      if (!revisions || !valid()) return;
      if (expected.system && revisions.system !== expected.system) throw new Error(t("run.reason.selectionChanged"));
      expected = { [module]: revisions[module], system: revisions.system };
      accepted = await pluginConfigApi.module(module);
    }
    if (!valid()) return;
    await assertConfigurationRevision(expected, undefined, !applyCurrent);
    if (valid()) await action(expected, accepted);
  };
  const action = <A extends unknown[],>(name: ActionName, fn: (valid: () => boolean, ...args: A) => Promise<unknown>, stop = false) => ({
    ...(actions[name] ?? { pending: false, error: null }), run: (...args: A) => run(name, (valid) => fn(valid, ...args), stop),
  });
  return { stimulation, signal, notices, pending,
    busy: Object.values(actions).some((state) => state.pending) || acquisition.pending,
    enginePending: !!actions.start?.pending || !!actions.stop?.pending,
    start: action("start", async () => api.engineStart()),
    stop: action("stop", async () => api.engineStop(), true),
    launch: action("launch", async (valid) => {
      setNotices((previous) => ({ ...previous, stimulation: undefined }));
      if (runtime.status?.recovery.stimulation) return api.modulePrepare("stimulation");
      await runCurrent("stimulation", true, valid, async (_revisions, accepted) => {
        if (accepted.settings["STIMULATION.auto_spawn"] === false) {
          const peer = await api.stimulationStatus();
          if (!valid()) return;
          if (!peer) throw new Error(t("run.reason.stimulationUnavailable"));
          if (!peer.engine_connected) {
            await api.modulePrepare("stimulation");
            if (valid()) setNotices((previous) => ({ ...previous, stimulation: "run.externalPrepared" }));
            return;
          }
        }
        if (valid()) await api.stimStart();
      });
    }),
    stopStim: action("stopStim", async () => api.stimStop(), true),
    trigger: action("trigger", async (_valid, count: number, parameters?: number[]) => api.trialStart(count, parameters)),
    readCurrent: action("readCurrent", (valid) => runCurrent("acquisition", false, valid, (revisions) => acquisition.read.run(revisions))),
    stopRead: action("stopRead", async () => acquisition.stopRead.run(), true),
    startProcessing: action("startProcessing", (valid) => runCurrent("processing", true, valid, async () => {
      const status = await api.engineStatus();
      if (!valid()) return;
      if (!status) throw new Error(t("run.reason.engineUnavailable"));
      if (!status.running) await api.engineStart();
      if (valid()) await api.moduleStart("processing");
    })),
    stopProcessing: action("stopProcessing", async () => api.moduleStop("processing"), true),
    startActuation: action("startActuation", async (valid) => {
      setNotices((previous) => ({ ...previous, actuation: undefined }));
      if (runtime.status?.recovery.actuation) return api.modulePrepare("actuation");
      await runCurrent("actuation", true, valid, async (_revisions, accepted) => {
        if (accepted.settings["ACTUATION.auto_spawn"] === false) {
          const status = await api.engineStatus();
          if (!valid()) return;
          if (!status) throw new Error(t("run.reason.engineUnavailable"));
          if (!status.actuation_connected) {
            await api.modulePrepare("actuation");
            if (valid()) setNotices((previous) => ({ ...previous, actuation: "run.externalActuationPrepared" }));
            return;
          }
        }
        if (valid()) await api.moduleStart("actuation");
      });
    }),
    stopActuation: action("stopActuation", async () => api.moduleStop("actuation"), true),
  };
}

export type RuntimeControls = ReturnType<typeof useRuntimeControls>;
