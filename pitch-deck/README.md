# ViFi YC Pitch Deck

16-slide pitch deck for the YC S26 application. Generated from a Claude Design handoff (claude.ai/design) and updated with current project numbers.

## View it

```bash
# from repo root
cd pitch-deck
python3 -m http.server 8000
# open http://localhost:8000/ViFi%20YC%20Pitch%20Deck.html
```

The deck loads React, ReactDOM, and Babel from a CDN, so it runs as plain HTML in any modern browser. No build step.

## Navigation

- Arrow keys: next/previous slide
- `S`: toggle speaker notes
- `F`: fullscreen
- See `deck-stage.js` for the full key map

## Files

| File | Purpose |
|---|---|
| `ViFi YC Pitch Deck.html` | Main entry. Loads slides + speaker notes JSON. |
| `slides.jsx` | Shared primitives: SlideFrame, SlideTitle, MicroLabel, Footer, StatusChip, design tokens. |
| `slides-part1.jsx` | Slides 01-08 (title, problem, headline result, monitors fail, section opener, how it works, capabilities). |
| `slides-part2.jsx` | Slides 08-11 (unit economics, comparison, why now, roadmap). |
| `slides-part3.jsx` | Slides 12-16 (market, traction, origin, team, close). |
| `colors_and_type.css` | Design system (Inter font face via Google Fonts, color tokens, base typography). |
| `deck-stage.js` | `<deck-stage>` Web Component: 1920×1080 fixed canvas, slide navigation, speaker notes overlay. |
| `assets/icons/` | Capability icons (SVG). |
| `assets/illustrations/signal-path.svg` | "How it works" diagram. |
| `assets/logo/` | ViFi wordmark (light + dark variants). **Add manually from the original design bundle** — these are PNGs and aren't in the initial API push. |

## Numbers used in the deck

All numbers below are also in `RESULTS.md`, `ROADMAP.md`, and the YC application notes. Update one, update them all.

| Number | Source |
|---|---|
| **$44 per room** | 2× ESP32-S3-DevKitC ($30) + 2× antennas ($8) + 2× pigtails ($6) |
| **4.15 bpm** | Cross-session HR MAE, leave-one-session-out, sessions 3-5 |
| **3.89 / 4.41 bpm** | Per-fold MAE (holdout 5 / holdout 4) |
| **65-68%** | Windows within ±5 bpm of Polar H10 ground truth |
| **41 tests** | `pytest -v` on main |
| **~1.5 bpm** | PhaseBeat baseline (Wang et al., INFOCOM 2017, $500/node Intel 5300 NIC) |
| **$3,000-$5,000** | Traditional bedside monitor cost (per-bed, hardware only) |
| **920,000 / 18.6M** | U.S. / global hospital beds (AHA 2024, WHO) |

## Before you present

1. Replace `zach@vifi.health` on Slide 16 with the real email.
2. Drop the two PNG logos (`vifi-logo-dark.png` and `vifi-logo-light.png`) into `pitch-deck/assets/logo/`. Source: the original Claude Design bundle URL.
3. Verify the PhaseBeat venue against Google Scholar — some sources cite ICDCS 2017 instead of INFOCOM 2017.
4. Run a screenshot pass: every slide must fit within 1920×1080 without overflow.
5. Update `April 2026` references on Slide 01 and Slide 03 if presenting in a later month.

## Implementation provenance

This deck was originally generated via Claude Design (handoff bundle exported 2026-04-23). The HTML/JSX prototype is the deliverable; if anyone needs to rebuild this in Pitch.com, Keynote, or a slide framework like reveal.js, the design tokens in `slides.jsx` and `colors_and_type.css` are the source of truth for type scale, color, and spacing.
