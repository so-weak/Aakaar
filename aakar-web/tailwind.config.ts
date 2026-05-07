import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          50: "#fbf8ee",
          100: "#efe8d1",
          200: "#d8cfb6",
          300: "#aaa18f",
          400: "#817a6e",
          500: "#646058",
          600: "#4d4a45",
          700: "#393734",
          800: "#242422",
          900: "#161614",
          950: "#0b0b0a",
        },
        accent: {
          50: "#faffd8",
          100: "#f2ff9e",
          200: "#e8ff58",
          300: "#d9fb1d",
          400: "#bde600",
          500: "#9dc300",
          600: "#7a9900",
          700: "#5d7306",
          800: "#48570b",
          900: "#39470d",
        },
        signal: {
          pink: "#ff3b93",
          cyan: "#16d9ff",
          paper: "#f4edd7",
          black: "#090908",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Helvetica",
          "Arial",
          "sans-serif",
        ],
        mono: [
          "JetBrains Mono",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
} satisfies Config;
