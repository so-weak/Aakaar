import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

/**
 * Theme system
 * --------------------------------------------------------------------------
 * Themes are pure CSS — variables in src/styles/themes.css scoped to
 * [data-theme="…"]. This module owns three things:
 *   1. The list/metadata of themes available to the user.
 *   2. A React context that flips `data-theme` on <html> and persists the
 *      choice to localStorage.
 *   3. A small chart-palette hook so recharts (which renders inside <svg>
 *      and can't read Tailwind tokens) stays aligned with the active theme.
 */

export type ThemeFamily =
  | "neon-grunge"
  | "skeuomorphic"
  | "retro-futurism"
  | "minimalism"
  | "hdfc";

export type ThemeMode = "light" | "dark";

export type ThemeId = `${ThemeFamily}-${ThemeMode}`;

export interface ThemeMeta {
  id: ThemeId;
  family: ThemeFamily;
  familyLabel: string;
  mode: ThemeMode;
  label: string;
  description: string;
}

export const THEMES: readonly ThemeMeta[] = [
  {
    id: "neon-grunge-dark",
    family: "neon-grunge",
    familyLabel: "Neon Grunge",
    mode: "dark",
    label: "Neon grunge · dark",
    description: "Brutalist offset shadows on a paper-black canvas.",
  },
  {
    id: "neon-grunge-light",
    family: "neon-grunge",
    familyLabel: "Neon Grunge",
    mode: "light",
    label: "Neon grunge · light",
    description: "Same brutalism, inverted onto warm cream paper.",
  },
  {
    id: "skeuomorphic-light",
    family: "skeuomorphic",
    familyLabel: "Skeuomorphism",
    mode: "light",
    label: "Skeuomorphism · light",
    description: "Glossy gradients, beveled controls, glass surfaces.",
  },
  {
    id: "skeuomorphic-dark",
    family: "skeuomorphic",
    familyLabel: "Skeuomorphism",
    mode: "dark",
    label: "Skeuomorphism · dark",
    description: "Brushed dark metal with warm amber dial accents.",
  },
  {
    id: "retro-futurism-dark",
    family: "retro-futurism",
    familyLabel: "Retro Futurism",
    mode: "dark",
    label: "Retro futurism · dark",
    description: "Synthwave horizon: neon magenta, scan-line texture.",
  },
  {
    id: "retro-futurism-light",
    family: "retro-futurism",
    familyLabel: "Retro Futurism",
    mode: "light",
    label: "Retro futurism · light",
    description: "Pastel vaporwave daylight — bubblegum pink + sky cyan.",
  },
  {
    id: "minimalism-light",
    family: "minimalism",
    familyLabel: "Minimalism",
    mode: "light",
    label: "Minimalism · light",
    description: "Pure paper, hairline rules, single warm accent.",
  },
  {
    id: "minimalism-dark",
    family: "minimalism",
    familyLabel: "Minimalism",
    mode: "dark",
    label: "Minimalism · dark",
    description: "Near-black with hairline grey separators.",
  },
  {
    id: "hdfc-light",
    family: "hdfc",
    familyLabel: "HDFC",
    mode: "light",
    label: "HDFC · light",
    description: "Bank-grade paper white, signature red, deep navy ink.",
  },
  {
    id: "hdfc-dark",
    family: "hdfc",
    familyLabel: "HDFC",
    mode: "dark",
    label: "HDFC · dark",
    description: "Vault-night navy with a signal-red pulse.",
  },
] as const;

const DEFAULT_THEME: ThemeId = "neon-grunge-dark";
const STORAGE_KEY = "aakar.theme";

function isThemeId(value: unknown): value is ThemeId {
  return (
    typeof value === "string" &&
    THEMES.some((t) => t.id === value)
  );
}

function readStoredTheme(): ThemeId {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (isThemeId(v)) return v;
  } catch {
    // localStorage may be unavailable (private mode, embedded contexts).
  }
  return DEFAULT_THEME;
}

interface ThemeContextValue {
  theme: ThemeId;
  meta: ThemeMeta;
  setTheme: (id: ThemeId) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeId>(() => readStoredTheme());

  // Apply data-theme to <html> and persist immediately. Doing this in an
  // effect (rather than during render) keeps the call out of strict-mode
  // double-invocation territory.
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.dataset.themeFamily = theme.split("-").slice(0, -1).join("-");
    document.documentElement.dataset.themeMode = theme.endsWith("-light") ? "light" : "dark";
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // ignore storage errors
    }
  }, [theme]);

  const setTheme = useCallback((id: ThemeId) => {
    setThemeState(id);
  }, []);

  const value = useMemo<ThemeContextValue>(() => {
    const meta = THEMES.find((t) => t.id === theme) ?? THEMES[0];
    return { theme, meta, setTheme };
  }, [theme, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return ctx;
}

/* ==========================================================================
 * Chart palette hook
 * --------------------------------------------------------------------------
 * recharts paints into <svg>, where Tailwind classes don't reach. We mirror
 * the active theme's chart variables into a plain JS object once per theme
 * change so chart components have synchronous access to current colours.
 * ======================================================================== */

export interface ChartPalette {
  succeeded: string;
  failed: string;
  paused: string;
  running: string;
  queued: string;
  cancelled: string;
  accent: string;
  pink: string;
  axis: string;
  axisText: string;
  grid: string;
}

const CHART_VARS: Record<keyof ChartPalette, string> = {
  succeeded: "--chart-succeeded",
  failed: "--chart-failed",
  paused: "--chart-paused",
  running: "--chart-running",
  queued: "--chart-queued",
  cancelled: "--chart-cancelled",
  accent: "--chart-accent",
  pink: "--chart-pink",
  axis: "--chart-axis",
  axisText: "--chart-axis-text",
  grid: "--chart-grid",
};

function readPalette(): ChartPalette {
  const styles = getComputedStyle(document.documentElement);
  const palette = {} as ChartPalette;
  (Object.keys(CHART_VARS) as Array<keyof ChartPalette>).forEach((key) => {
    palette[key] = styles.getPropertyValue(CHART_VARS[key]).trim();
  });
  return palette;
}

export function useChartPalette(): ChartPalette {
  const { theme } = useTheme();
  const [palette, setPalette] = useState<ChartPalette>(() => readPalette());

  useEffect(() => {
    setPalette(readPalette());
  }, [theme]);

  return palette;
}
