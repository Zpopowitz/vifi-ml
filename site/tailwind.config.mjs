import typography from "@tailwindcss/typography";

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./src/**/*.{astro,html,js,jsx,md,mdx,ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // DESIGN.md — Editorial Lab palette
        paper:        "#F4EFE4",
        "paper-2":    "#ECE5D5",
        "paper-3":    "#E0D8C4",
        ink:          "#1A1A1A",
        "ink-2":      "#3A3A3A",
        muted:        "#5A5A5A",
        rule:         "#D8D0BF",
        accent:       "#1D5C6E",
        "accent-2":   "#2A7A92",
        "accent-visited": "#3A4F73",
        signal:       "#0E9F66",
        "signal-glow":"#34D399",
        // Legacy alias preserved during incremental migration (other pages still reference)
        accentDark:   "#2A7A92",
      },
      fontFamily: {
        display: ['"Fraunces"', "Georgia", "serif"],
        serif:   ['"Source Serif 4"', "Georgia", "serif"],
        sans:    ['"Inter Tight"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono:    ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      maxWidth: {
        prose: "65ch",
      },
    },
  },
  plugins: [typography],
};
