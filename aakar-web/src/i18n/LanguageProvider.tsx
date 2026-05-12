import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import {
  DEFAULT_LANG,
  LANG_CODES,
  RUN_STATUS_TO_LABEL_KEY,
  labelsFor,
} from "@/i18n/labels";
import type { LabelMap, LangCode } from "@/i18n/labels";

/**
 * Language provider — same pattern as ThemeProvider but for label vocabulary.
 *
 *  - Persists the chosen language to localStorage per device.
 *  - Sets `lang` and `data-lang` attributes on <html> so CSS can target
 *    per-script font fallbacks if ever needed.
 *  - Exposes three hooks: useLang, useLabels, useRunStatusLabel.
 */

const STORAGE_KEY = "aakar.lang";

function isLangCode(v: unknown): v is LangCode {
  return typeof v === "string" && (LANG_CODES as readonly string[]).includes(v);
}

function readStored(): LangCode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (isLangCode(v)) return v;
  } catch {
    // localStorage unavailable
  }
  return DEFAULT_LANG;
}

interface LanguageContextValue {
  lang: LangCode;
  setLang: (code: LangCode) => void;
  labels: LabelMap;
}

const LanguageContext = createContext<LanguageContextValue | null>(null);

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<LangCode>(() => readStored());

  useEffect(() => {
    document.documentElement.lang = lang;
    document.documentElement.dataset.lang = lang;
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      // ignore
    }
  }, [lang]);

  const setLang = useCallback((code: LangCode) => {
    setLangState(code);
  }, []);

  const value = useMemo<LanguageContextValue>(() => {
    return { lang, setLang, labels: labelsFor(lang) };
  }, [lang, setLang]);

  return (
    <LanguageContext.Provider value={value}>
      {children}
    </LanguageContext.Provider>
  );
}

export function useLang(): LangCode {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLang must be used within a LanguageProvider");
  return ctx.lang;
}

export function useSetLang(): (code: LangCode) => void {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useSetLang must be used within a LanguageProvider");
  return ctx.setLang;
}

/** Returns the active language's label map. Recomputed on language change. */
export function useLabels(): LabelMap {
  const ctx = useContext(LanguageContext);
  if (!ctx) throw new Error("useLabels must be used within a LanguageProvider");
  return ctx.labels;
}

/** Translate a run.status string ("queued", "running", ...) into the active language. */
export function useRunStatusLabel(): (status: string) => string {
  const labels = useLabels();
  return useCallback(
    (status: string) => {
      const key = RUN_STATUS_TO_LABEL_KEY[status];
      if (!key) return status;
      return labels[key];
    },
    [labels],
  );
}
