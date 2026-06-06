import type { Config } from "tailwindcss";

// Tailwind colors are wired to CSS variables so themes can swap palettes
// without touching component classNames. Each value is an `R G B` triplet
// the `<alpha-value>` form interpolates into, which means utilities like
// `bg-ink-900/70` keep working under every theme.
const cssVarRgb = (name: string) => `rgb(var(${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          50: cssVarRgb("--ink-50"),
          100: cssVarRgb("--ink-100"),
          200: cssVarRgb("--ink-200"),
          300: cssVarRgb("--ink-300"),
          400: cssVarRgb("--ink-400"),
          500: cssVarRgb("--ink-500"),
          600: cssVarRgb("--ink-600"),
          700: cssVarRgb("--ink-700"),
          800: cssVarRgb("--ink-800"),
          900: cssVarRgb("--ink-900"),
          950: cssVarRgb("--ink-950"),
        },
        accent: {
          50: cssVarRgb("--accent-50"),
          100: cssVarRgb("--accent-100"),
          200: cssVarRgb("--accent-200"),
          300: cssVarRgb("--accent-300"),
          400: cssVarRgb("--accent-400"),
          500: cssVarRgb("--accent-500"),
          600: cssVarRgb("--accent-600"),
          700: cssVarRgb("--accent-700"),
          800: cssVarRgb("--accent-800"),
          900: cssVarRgb("--accent-900"),
        },
        signal: {
          pink: cssVarRgb("--signal-pink"),
          cyan: cssVarRgb("--signal-cyan"),
          paper: cssVarRgb("--signal-paper"),
          black: cssVarRgb("--signal-black"),
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)"],
        mono: ["var(--font-mono)"],
        display: ["var(--font-display)"],
      },
      borderRadius: {
        control: "var(--radius-control)",
        card: "var(--radius-card)",
      },
    },
  },
  plugins: [],
} satisfies Config;
