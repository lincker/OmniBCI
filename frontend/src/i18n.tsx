import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Language = "zh" | "en";

const zh = {
  "app.title": "OmniBCI 控制台",
  "language.selector": "界面语言",
  "language.chinese": "中文",
  "language.english": "English",
  "nav.system": "系统",
  "nav.config": "配置",
  "connection.live": "实时",
  "connection.connecting": "连接中…",
  "connection.reconnecting": "已断开，重连中…",
  "common.unreachable": "不可达",
  "common.select": "— 选择 —",
  "common.saved": "已保存",
  "common.savedRestart": "已保存——停止→启动引擎后生效",
  "common.saving": "保存中…",
  "common.load": "加载",
  "common.yes": "是",
  "engine.title": "引擎",
  "engine.backendUnreachable": "后端不可达：{message}",
  "engine.running": "运行中",
  "engine.acquisitionProcess": "采集进程",
  "engine.processingProcess": "处理进程",
  "engine.stimulationConnected": "刺激已连接",
  "engine.dataSource": "数据源",
  "engine.decoder": "解码器",
  "engine.starting": "启动中…",
  "engine.start": "启动引擎",
  "engine.stop": "停止引擎",
  "acquisition.title": "采集",
  "acquisition.connected": "已连接",
  "acquisition.streaming": "读取中",
  "acquisition.metadata": "数据源 {source} ・ 采样率 {sampleRate} Hz ・ 通道 {channelCount}",
  "acquisition.session": "会话 {sessionId} ・ 范式 {paradigm} ・ {state} ・ 试次 {trialCount}",
  "acquisition.sessionRunning": "进行中",
  "acquisition.sessionEnded": "已结束",
  "acquisition.noActiveSession": "无活动会话",
  "acquisition.sourceConnection": "数据源连接",
  "acquisition.dataReading": "数据读取",
  "acquisition.connect": "连接",
  "acquisition.disconnect": "断开",
  "acquisition.startReading": "开始读取",
  "acquisition.stopReading": "停止读取",
  "stimulation.title": "刺激",
  "stimulation.processRunning": "进程运行",
  "stimulation.engineConnected": "引擎已连接",
  "stimulation.runningParadigm": "运行范式 {paradigm}",
  "stimulation.useConfigured": "— 用配置值 —",
  "stimulation.start": "启动刺激",
  "stimulation.stop": "停止刺激",
  "stimulation.startRequired": "请先启动刺激",
  "stimulation.triggerTrial": "触发试次",
  "effector.title": "控制输出",
  "effector.select": "— 选择控制输出 —",
  "pipeline.selectPlugins": "选择数据源与解码器",
  "pipeline.engineStarted": "引擎启动",
  "pipeline.sourceConnected": "数据源连接",
  "pipeline.reading": "数据读取",
  "results.title": "试次结果",
  "results.empty": "暂无结果",
  "results.target": "目标",
  "results.event": "事件",
  "results.confidence": "置信度",
  "results.forced": "强制",
  "results.time": "时间 (UTC)",
  "acks.title": "刺激回执",
  "acks.empty": "暂无回执",
  "acks.target": "目标 {target}",
  "config.title": "配置",
  "config.active": "激活：",
  "config.edit": "编辑：",
  "config.default": "默认配置",
  "config.save": "保存",
  "config.saveAs": "另存为…",
  "config.delete": "删除此配置",
  "config.deleted": "已删除",
  "config.switchedRestart": "已切换——引擎相关键在下次启动引擎时生效",
  "config.newProfilePrompt": "新配置名称（中英文、数字、_、-、空格）：",
  "config.noChanges": "没有改动可保存——先在覆盖列输入差异值",
  "config.profileCreated": "配置 {name} 已创建",
  "config.key": "键",
  "config.description": "说明",
  "config.defaultValue": "默认值",
  "config.activeOverride": "激活覆盖",
  "config.profileOverride": "{name} 覆盖",
  "config.effectiveValue": "生效值",
  "config.applyMode": "生效方式",
  "config.changeDefaultTo": "改默认为",
  "config.changeOverrideTo": "改覆盖为",
  "config.hot": "即时",
  "config.engineRestart": "重启引擎",
  "plugin.fileReader": "文件回放",
  "plugin.neuroscanScan45": "Neuroscan Scan45",
  "plugin.classic40Cca": "Classic 40 CCA",
  "plugin.classic40Fbcca": "Classic 40 FBCCA",
  "plugin.classic40Ssvep": "Classic 40-target SSVEP",
  "plugin.logEffector": "日志执行器",
  "control.nTrials": "刺激次数",
  "control.targetCue": "目标提示",
  "configDescription.signalSource": "数据源插件 ID",
  "configDescription.dataFilePath": "文件回放数据路径",
  "configDescription.playSpeed": "文件回放速度倍率",
  "configDescription.sampleRate": "采样率 (Hz)",
  "configDescription.channelCount": "通道数",
  "configDescription.decoder": "解码器插件 ID",
  "configDescription.windowDuration": "解码窗长 (s)",
  "configDescription.offsetTime": "触发偏移 (s)",
  "configDescription.baselineDuration": "基线时长 (s)",
  "configDescription.filters": "预处理滤波器链",
  "configDescription.paradigm": "刺激范式插件类型",
  "configDescription.effector": "控制输出插件类型",
  "configDescription.queueSize": "进程间队列容量",
} as const;

export type TranslationKey = keyof typeof zh;
export type TranslationParams = Record<string, string | number>;
export type Translate = (key: TranslationKey, params?: TranslationParams) => string;

const en: Record<TranslationKey, string> = {
  "app.title": "OmniBCI Console",
  "language.selector": "Interface language",
  "language.chinese": "中文",
  "language.english": "English",
  "nav.system": "System",
  "nav.config": "Configuration",
  "connection.live": "Live",
  "connection.connecting": "Connecting…",
  "connection.reconnecting": "Disconnected, reconnecting…",
  "common.unreachable": "Unavailable",
  "common.select": "— Select —",
  "common.saved": "Saved",
  "common.savedRestart": "Saved — stop and restart the engine to apply",
  "common.saving": "Saving…",
  "common.load": "Load",
  "common.yes": "Yes",
  "engine.title": "Engine",
  "engine.backendUnreachable": "Backend unavailable: {message}",
  "engine.running": "Running",
  "engine.acquisitionProcess": "Acquisition process",
  "engine.processingProcess": "Processing process",
  "engine.stimulationConnected": "Stimulation connected",
  "engine.dataSource": "Data source",
  "engine.decoder": "Decoder",
  "engine.starting": "Starting…",
  "engine.start": "Start engine",
  "engine.stop": "Stop engine",
  "acquisition.title": "Acquisition",
  "acquisition.connected": "Connected",
  "acquisition.streaming": "Reading",
  "acquisition.metadata": "Source {source} · Sample rate {sampleRate} Hz · Channels {channelCount}",
  "acquisition.session": "Session {sessionId} · Paradigm {paradigm} · {state} · Trials {trialCount}",
  "acquisition.sessionRunning": "Running",
  "acquisition.sessionEnded": "Ended",
  "acquisition.noActiveSession": "No active session",
  "acquisition.sourceConnection": "Data source connection",
  "acquisition.dataReading": "Data reading",
  "acquisition.connect": "Connect",
  "acquisition.disconnect": "Disconnect",
  "acquisition.startReading": "Start reading",
  "acquisition.stopReading": "Stop reading",
  "stimulation.title": "Stimulation",
  "stimulation.processRunning": "Process running",
  "stimulation.engineConnected": "Engine connected",
  "stimulation.runningParadigm": "Running paradigm: {paradigm}",
  "stimulation.useConfigured": "— Use configured value —",
  "stimulation.start": "Start stimulation",
  "stimulation.stop": "Stop stimulation",
  "stimulation.startRequired": "Start stimulation first",
  "stimulation.triggerTrial": "Trigger trial",
  "effector.title": "Control Output",
  "effector.select": "— Select control output —",
  "pipeline.selectPlugins": "Select plugins",
  "pipeline.engineStarted": "Start engine",
  "pipeline.sourceConnected": "Connect source",
  "pipeline.reading": "Read data",
  "results.title": "Trial Results",
  "results.empty": "No results",
  "results.target": "Target",
  "results.event": "Event",
  "results.confidence": "Confidence",
  "results.forced": "Forced",
  "results.time": "Time (UTC)",
  "acks.title": "Stimulation Acknowledgements",
  "acks.empty": "No acknowledgements",
  "acks.target": "Target {target}",
  "config.title": "Configuration",
  "config.active": "Active:",
  "config.edit": "Edit:",
  "config.default": "Default configuration",
  "config.save": "Save",
  "config.saveAs": "Save as…",
  "config.delete": "Delete configuration",
  "config.deleted": "Deleted",
  "config.switchedRestart": "Switched — engine settings apply on the next engine start",
  "config.newProfilePrompt": "New configuration name (letters, numbers, _, -, and spaces):",
  "config.noChanges": "Nothing to save — enter a different value in the override column first",
  "config.profileCreated": "Configuration {name} created",
  "config.key": "Key",
  "config.description": "Description",
  "config.defaultValue": "Default",
  "config.activeOverride": "Active override",
  "config.profileOverride": "{name} override",
  "config.effectiveValue": "Effective value",
  "config.applyMode": "Applies",
  "config.changeDefaultTo": "Change default to",
  "config.changeOverrideTo": "Change override to",
  "config.hot": "Immediately",
  "config.engineRestart": "Engine restart",
  "plugin.fileReader": "File Playback",
  "plugin.neuroscanScan45": "Neuroscan Scan45",
  "plugin.classic40Cca": "Classic 40 CCA",
  "plugin.classic40Fbcca": "Classic 40 FBCCA",
  "plugin.classic40Ssvep": "Classic 40-target SSVEP",
  "plugin.logEffector": "Log Effector",
  "control.nTrials": "Trial count",
  "control.targetCue": "Target cue",
  "configDescription.signalSource": "Data source plugin ID",
  "configDescription.dataFilePath": "File playback data path",
  "configDescription.playSpeed": "File playback speed multiplier",
  "configDescription.sampleRate": "Sample rate (Hz)",
  "configDescription.channelCount": "Channel count",
  "configDescription.decoder": "Decoder plugin ID",
  "configDescription.windowDuration": "Decoding window length (s)",
  "configDescription.offsetTime": "Trigger offset (s)",
  "configDescription.baselineDuration": "Baseline duration (s)",
  "configDescription.filters": "Preprocessing filter chain",
  "configDescription.paradigm": "Stimulation paradigm plugin type",
  "configDescription.effector": "Control output plugin type",
  "configDescription.queueSize": "Inter-process queue capacity",
};

const resources: Record<Language, Record<TranslationKey, string>> = { zh, en };
const STORAGE_KEY = "omnibci.language";

const PLUGIN_NAME_KEYS: Record<string, TranslationKey> = {
  file_reader: "plugin.fileReader",
  neuroscan_scan45_reader: "plugin.neuroscanScan45",
  THUBCI_SSVEP_classic40CCA: "plugin.classic40Cca",
  THUBCI_SSVEP_classic40FBCCA: "plugin.classic40Fbcca",
  STIMULATION_THUBCI_SSVEP_classic40: "plugin.classic40Ssvep",
  logger: "plugin.logEffector",
};

const CONTROL_LABEL_KEYS: Record<string, TranslationKey> = {
  n_trials: "control.nTrials",
  target_cue: "control.targetCue",
};

const CONFIG_DESCRIPTION_KEYS: Record<string, TranslationKey> = {
  "ACQUISITION.signal_source": "configDescription.signalSource",
  "ACQUISITION.data_file_path": "configDescription.dataFilePath",
  "ACQUISITION.file_reader_play_speed": "configDescription.playSpeed",
  "ACQUISITION.sample_rate": "configDescription.sampleRate",
  "ACQUISITION.channel_number": "configDescription.channelCount",
  "ALGORITHM.decoder": "configDescription.decoder",
  "ALGORITHM.window_duration": "configDescription.windowDuration",
  "ALGORITHM.offset_time": "configDescription.offsetTime",
  "ALGORITHM.baseline_duration": "configDescription.baselineDuration",
  "ALGORITHM.filters": "configDescription.filters",
  "STIMULATION.paradigm": "configDescription.paradigm",
  "ACTUATION.effector": "configDescription.effector",
  "NEUROENGINE.queue_size": "configDescription.queueSize",
};

function detectLanguage(): Language {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "zh" || stored === "en") return stored;
  } catch {
    // Storage can be unavailable in restricted browser contexts.
  }
  return window.navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

function format(template: string, params?: TranslationParams): string {
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    params[key] == null ? match : String(params[key]),
  );
}

interface I18nContextValue {
  language: Language;
  setLanguage: (language: Language) => void;
  t: Translate;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(detectLanguage);

  const setLanguage = useCallback((next: Language) => {
    setLanguageState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // The in-memory selection still works when persistence is unavailable.
    }
  }, []);

  const t = useCallback<Translate>(
    (key, params) => format(resources[language][key], params),
    [language],
  );

  useEffect(() => {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.title = t("app.title");
  }, [language, t]);

  const value = useMemo(
    () => ({ language, setLanguage, t }),
    [language, setLanguage, t],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}

export function translatePluginName(
  pluginType: string,
  fallback: string,
  t: Translate,
): string {
  const key = PLUGIN_NAME_KEYS[pluginType];
  return key ? t(key) : fallback;
}

export function translateControlLabel(
  controlName: string,
  fallback: string,
  t: Translate,
): string {
  const key = CONTROL_LABEL_KEYS[controlName];
  return key ? t(key) : fallback;
}

export function translateConfigDescription(
  configKey: string,
  fallback: string,
  t: Translate,
): string {
  const key = CONFIG_DESCRIPTION_KEYS[configKey];
  return key ? t(key) : fallback;
}
