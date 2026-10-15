import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { desktopBridge } from "./desktop";
import { en as shellEn, zh as shellZh } from "./locales/shell";
import { en as profilesEn, zh as profilesZh } from "./locales/profiles";
import { en as runtimeEn, zh as runtimeZh } from "./locales/runtime";
import { en as setupEn, zh as setupZh } from "./locales/setup";
import { en as guideEn, zh as guideZh } from "./locales/guide";

export type Language = "zh" | "en";

const zh = {
  ...shellZh,
  ...profilesZh,
  ...runtimeZh,
  ...setupZh,
  ...guideZh,
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
  "control.nTrials": "试次数",
  "control.targetCue": "目标提示",
} as const;

export type TranslationKey = keyof typeof zh;
export type TranslationParams = Record<string, string | number>;
export type Translate = (key: TranslationKey, params?: TranslationParams) => string;

const en: Record<TranslationKey, string> = {
  ...shellEn,
  ...profilesEn,
  ...runtimeEn,
  ...setupEn,
  ...guideEn,
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

function detectLanguage(): Language {
  if (typeof window === "undefined") return "en";
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

export function translate(language: Language, key: TranslationKey, params?: TranslationParams): string {
  return format(resources[language][key], params);
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
    (key, params) => translate(language, key, params),
    [language],
  );

  useEffect(() => {
    document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
    document.title = t("app.title");
    desktopBridge()?.setLanguage(language);
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
  const key = Object.prototype.hasOwnProperty.call(PLUGIN_NAME_KEYS, pluginType) ? PLUGIN_NAME_KEYS[pluginType] : undefined;
  return key ? t(key) : fallback;
}

export function translateControlLabel(
  controlName: string,
  fallback: string,
  t: Translate,
): string {
  const key = Object.prototype.hasOwnProperty.call(CONTROL_LABEL_KEYS, controlName) ? CONTROL_LABEL_KEYS[controlName] : undefined;
  return key ? t(key) : fallback;
}
