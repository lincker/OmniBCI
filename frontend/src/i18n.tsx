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
  "pipeline.selectPlugins": "选择数据源与解码器",
  "pipeline.engineStarted": "引擎启动",
  "pipeline.sourceConnected": "数据源连接",
  "pipeline.reading": "数据读取",
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
  "pipeline.selectPlugins": "Select plugins",
  "pipeline.engineStarted": "Start engine",
  "pipeline.sourceConnected": "Connect source",
  "pipeline.reading": "Read data",
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
