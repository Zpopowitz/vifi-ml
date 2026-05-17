# vifi.health — Design Backlog

> Tracked after the Editorial Lab migration (2026-05-16). The site is at
> design-quality 11/10 within the scope of the migrated Astro pages. Items
> below are "12/10" moves — improvements that need to leave the current
> scope (new dependencies, real data exports, build steps, separate assets).
>
> Pick any in any order. Priority annotations are recommendations, not gates.

## High-leverage (do these first)

### 1. Real CSI data in the ambient trace
**Replaces:** the synthesized sinusoidal SVG path in `site/src/pages/index.astro`
(respiration 0.23 Hz + cardiac 1.2 Hz mathematical sum).

**Move to:** an actual short capture of ViFi HR/RR data exported from `data/`.

**Work:**
- Export a 5-second slice of clean HR estimate from a known-good session
- Convert to SVG path data (Python script, then commit the path)
- Or fetch from a JSON endpoint at runtime via a tiny JS module
- Update the ARIA-label to say "actual capture from session NNN"

**Why:** "the trace is real" is the strongest move available. Researchers can
inspect, copy the path, verify the timing — methodological honesty at the
deepest level.

---

### 2. Self-host fonts via `@fontsource` with `size-adjust` fallback
**Replaces:** Google Fonts CDN loads in `site/src/layouts/Layout.astro`.

**Per DESIGN.md spec:** *"All fonts loaded via @fontsource packages in Astro —
self-hosted, no FOUT, no Google Fonts CDN."*

**Work:**
```
npm install @fontsource-variable/fraunces \
            @fontsource/source-serif-4 \
            @fontsource-variable/inter \
            @fontsource/jetbrains-mono
```
- Import in `Layout.astro` instead of the Google CDN `<link>`
- Add `@font-face` declarations with `size-adjust`, `ascent-override`,
  `descent-override` matched to local fallbacks (Georgia for Fraunces,
  system mono for JetBrains Mono) — eliminates layout shift on font load
- Drop `<link rel="preconnect">` to fonts.googleapis.com / fonts.gstatic.com

**Why:** zero FOIT, no privacy footprint, faster LCP, and DESIGN.md compliance.

---

### 3. Real 1200×630 Open Graph social card
**Replaces:** nothing (currently no OG image — link previews are blank).

**Work:**
- Generate a static PNG showing the hero composition: warm cream background,
  the headline, the ambient trace, the colophon
- Either ship a static asset at `site/public/og-image.png` and reference it in
  Layout.astro's OG meta tags
- Or use `@vercel/og` for dynamic generation per page

**Why:** Twitter, LinkedIn, Slack, iMessage previews currently show nothing.
This is the lowest-effort high-visibility item in the backlog.

---

## Real-data / authenticity

### 4. HRV in the synthesized waveform (only if #1 isn't done yet)
The current waveform is too regular — real HR has beat-to-beat variability.
Add small randomized phase jitter per cardiac cycle. Pairs with #1; obsoleted
by #1.

### 5. Citation system properly grounded
The `.cite-link` anchors on "4.15 bpm" and "1/68th the cost" in the lede
currently point to `#proof-heading` (same page). For real academic credibility:
- Link "4.15 bpm" to a specific section on `/results` that names the dataset,
  methodology repo, and exact commit hash
- Add a "Cite this work" block (BibTeX snippet in JetBrains Mono) academics
  can copy
- Consider DOI when one exists

### 6. ASCII diagram styling on the homepage's "How it works" section
The `<pre>` block renders the diagram in JetBrains Mono — works but may
overflow on narrow viewports. Worth adding `overflow-x: auto` if not already,
and considering a SVG re-render for crisper display.

---

## Performance + polish

### 7. Font preload strategy beyond preconnect
After #2 lands, switch to `<link rel="preload" as="font" type="font/woff2"
crossorigin>` for the critical Fraunces variable file (axes used in the H1).
Defer non-critical font loads (e.g., JetBrains Mono only needed for the
footer colophon — preload-on-interaction or below-the-fold lazy).

### 8. View transitions polish
Astro's `<ViewTransitions />` is enabled but no per-page customization.
Add named transitions:
- Hero headline persists smoothly across navigations
- Trace doesn't restart (continuous across nav)
- Nav fades rather than swaps

### 9. Performance baseline + budget
No Lighthouse / Core Web Vitals baseline measured yet. Establish targets:
- LCP < 1.5s
- CLS < 0.05
- INP < 200ms
- Bundle size budget per route
Re-measure after #2 (self-hosted fonts) — LCP should improve significantly.

### 10. Visual regression tests
No tests on the site currently. Add Playwright snapshots for:
- `/` (light + dark mode)
- `/results`
- `/roadmap`
- `/pilots`
- `/contact`
Catches design drift over time. Run on PR via CI.

---

## A11y + edge cases

### 11. Dark mode logo treatment
The monogram uses `currentColor` + `var(--signal)`. In dark mode the WiFi-arc
emerald gets slightly desaturated. Consider a dedicated `--signal-glow`
(#34D399) for the arc in dark mode to make it pop against `#131715`.

### 12. Print stylesheet refinements
Current print CSS strips animations and hides nav. Could add:
- Page numbers via `@page { @bottom-right { content: counter(page); } }`
- Link-as-footnote conversion: `a::after { content: " (" attr(href) ")"; }`
- Header/footer suppression
Useful for researchers who print methodology pages.

### 13. WCAG 2.2 AAA contrast pass
Current AA pass verified. AAA requires 7:1 for body text. May require darkening
`--muted` slightly in light mode or lightening it in dark mode. Worth a
contrast audit if accessibility is a positioning concern (likely it is for
the medical-research-adjacent audience).

---

## Content + IA

### 14. Migrate JSON content for /privacy, /terms, /roadmap to editorial-zen tone
The SubpageLayout is migrated but the content in `src/content/privacy/main.json`,
`src/content/terms/main.json`, `src/content/roadmap/main.json` was written for
the legacy style. Could use editorial rewrites — shorter, more confident
voice, JetBrains Mono code blocks where appropriate.

### 15. /roadmap as a real visual timeline
Currently a list of 3 milestones. Could be a horizontal timeline with the
3 dated stops, the current position marked, dependencies between phases shown.
More compelling than a list for "what's next" questions.

### 16. Formspree IDs configured
`/pilots` and `/contact` render email-fallback panels because
`PUBLIC_FORMSPREE_PILOTS_ID` and `PUBLIC_FORMSPREE_CONTACT_ID` env vars are
unset. Set these in `.env` (local) and Vercel project settings (prod) to
activate the real forms.

---

## Housekeeping

### 17. Delete `/hero-b-live-ecg.html`
The v8 preview file is kept at `site/public/hero-b-live-ecg.html` for diff
comparison against the migrated `index.astro`. Once you've verified the
migration is good, delete the preview.

### 18. Delete `/logo.png`
The legacy PNG logo is at `site/public/logo.png` and no longer referenced
anywhere (SiteHeader now uses the inline monogram SVG). Safe to delete.

### 19. Tailwind config: drop `accentDark` alias
`tailwind.config.mjs` has `accentDark: "#2A7A92"` as a legacy alias preserved
during migration. Grep the codebase for `accentDark` usages and remove the
alias once nothing references it.

---

## Priority recommendations

| Tier | Items | Why |
|---|---|---|
| **Do these next** | #1 (real data), #2 (self-host fonts), #3 (OG image) | Highest authenticity / perf / visibility impact |
| **Before launch** | #16 (Formspree), #17–19 (housekeeping) | Functional gaps + repo hygiene |
| **Q1 after launch** | #5 (citations), #9 (perf baseline), #10 (visual regression) | Compounding-quality moves |
| **Whenever** | #4, #6, #7, #8, #11, #12, #13, #14, #15 | Polish / nice-to-have |

---

*Generated 2026-05-16 from the design review that pushed the site from 7/10 to
11/10. Each item here is a defensible 12/10 move that requires leaving the
"first-pass Astro migration" scope.*
