/** Own command continuations and feedback independently of the visible page. */
import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { signalConfigApi } from "../api/signal-config";
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
  const [notices, setNotices] = useState<Partial<Record<ConfigurationModule, TranslationKey>>>({});
  const update = (name: ActionName, value: ActionState) => {
    values.current = { ...values.current, [name]: value }; setActions(values.current);
  };
  const pending = (module: ConfigurationSection) => Object.entries(values.current).some(([name, state]) =>
    state.pending && (module === "system" || actionModules[name as ActionName] === module))
    || ((module === "system" || module === "acquisition") && acquisition.pending);
  const run = async (name: ActionName, action: () => Promise<unknown>, stop = false) => {
    const module = actionModules[name];
    if (values.current[name]?.pending || !stop && (pending(module) || values.current.start?.pending || values.current.stop?.pending)) return;
    update(name, { pending: true, error: null });
    try { await action(); }
    catch (cause) { update(name, { pending: true, error: cause instanceof ApiError ? `[${cause.code}] ${cause.message}` : String(cause) }); }
    finally {
      if (module === "system") await Promise.all([acquisition.refresh(), stimulation.refresh(), signal.refresh()]);
      else if (module === "acquisition") await acquisition.refresh();
      else if (module === "stimulation") await Promise.all([stimulation.refresh(), runtime.refresh()]);
      else await Promise.all([refreshEngine(), runtime.refresh(), ...(module === "processing" ? [signal.refresh()] : [])]);
      update(name, { pending: false, error: values.current[name]?.error ?? null });
    }
  };
  const startModule = async (module: ConfigurationModule) => {
    if (module === "acquisition") throw new Error("Use Connect to start acquisition.");
    const configuration = current.request(module);
    const result = await runtime.execute(module, configuration,
      module === "stimulation" ? api.stimStart : (body) => api.moduleStart(module, body),
      (operation) => current.acknowledge(module, configuration, operation.target_revisions[module]));
    if (result.status !== "succeeded") throw new Error(result.error ?? "Module startup failed.");
    if (result.result?.action === "prepared" && module !== "processing") {
      setNotices((previous) => ({ ...previous, [module]: module === "stimulation" ? "run.externalPrepared" : "run.externalActuationPrepared" }));
    }
  };
  const action = <A extends unknown[],>(name: ActionName, fn: (...args: A) => Promise<unknown>, stop = false) => ({
    ...(actions[name] ?? { pending: false, error: null }), run: (...args: A) => run(name, () => fn(...args), stop),
  });
  return { stimulation, signal, notices, pending,
    busy: Object.values(actions).some((state) => state.pending) || acquisition.pending,
    enginePending: !!actions.start?.pending || !!actions.stop?.pending,
    start: action("start", async () => api.engineStart()),
    stop: action("stop", async () => api.engineStop(), true),
    launch: action("launch", async () => {
      setNotices((previous) => ({ ...previous, stimulation: undefined }));
      if (runtime.status?.recovery.stimulation) return api.modulePrepare("stimulation");
      await startModule("stimulation");
    }),
    stopStim: action("stopStim", async () => api.stimStop(), true),
    trigger: action("trigger", async (count: number, parameters?: number[]) => api.trialStart(count, parameters)),
    readCurrent: action("readCurrent", async () => {
      const accepted = current.modules.acquisition.activation;
      if (!accepted?.revision || current.hasChanges("acquisition", accepted)) throw new Error(t("run.reason.reconnectCurrentSource"));
      const generation = runtime.status?.generation;
      const connectionId = acquisition.data?.connection_id;
      if (!generation || !connectionId) throw new Error(t("run.reason.notConnected"));
      await acquisition.read.run({ expected_revision: accepted.revision, generation, connection_id: connectionId });
    }),
    stopRead: action("stopRead", async () => acquisition.stopRead.run(), true),
    startProcessing: action("startProcessing", () => startModule("processing")),
    stopProcessing: action("stopProcessing", async () => api.moduleStop("processing"), true),
    startActuation: action("startActuation", async () => {
      setNotices((previous) => ({ ...previous, actuation: undefined }));
      if (runtime.status?.recovery.actuation) return api.modulePrepare("actuation");
      await startModule("actuation");
    }),
    stopActuation: action("stopActuation", async () => api.moduleStop("actuation"), true),
  };
}

export type RuntimeControls = ReturnType<typeof useRuntimeControls>;
