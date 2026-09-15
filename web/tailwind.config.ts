import type { Config } from "tailwindcss";

/**
 * A trading operations palette, not a generic SaaS one: a deep neutral ground,
 * one accent, and semantic colours that mean the same thing everywhere --
 * green is filled, amber is waiting, red is blocked.
 */
const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: {
          DEFAULT: "hsl(var(--bg))",
          raised: "hsl(var(--bg-raised))",
          sunken: "hsl(var(--bg-sunken))",
        },
        line: "hsl(var(--line))",
        fg: {
          DEFAULT: "hsl(var(--fg))",
          muted: "hsl(var(--fg-muted))",
          subtle: "hsl(var(--fg-subtle))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          fg: "hsl(var(--accent-fg))",
        },
        long: "hsl(var(--long))",
        short: "hsl(var(--short))",
        warn: "hsl(var(--warn))",
        danger: "hsl(var(--danger))",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      borderRadius: { lg: "0.5rem", md: "0.375rem" },
      keyframes: {
        pulseDot: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.35" },
        },
      },
      animation: { "pulse-dot": "pulseDot 2s ease-in-out infinite" },
    },
  },
  plugins: [],
};
export default config;
