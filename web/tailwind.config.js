/** Dense engineering console palette — neutral surfaces, semantic accents only. */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          0: "#0a0c10", 1: "#0f1218", 2: "#151922", 3: "#1c212c", 4: "#252b38",
        },
        line: { DEFAULT: "#2a3140", strong: "#3a4356" },
        ink: { DEFAULT: "#dbe2ee", dim: "#8f9bb0", faint: "#5d6779" },
        ok: "#4ade80", warn: "#fbbf24", bad: "#f87171",
        info: "#60a5fa", accent: "#a78bfa", live: "#34d399",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "JetBrains Mono", "Menlo", "monospace"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      fontSize: {
        "2xs": ["10px", "14px"], xs: ["11px", "16px"], sm: ["12px", "18px"],
        base: ["13px", "20px"],
      },
    },
  },
  plugins: [],
};
