# ViFi v2 — Demand Thesis for Beat-by-Beat Radar Monitoring

> **Status:** DRAFT for owner sign-off. This answers the open strategic question in
> §0 of `docs/superpowers/plans/2026-05-20-radar-v2-architecture.md` — the one the
> plan deliberately did *not* decide. It is a proposal, not a verdict. The owner
> accepts, rejects, or redirects the wedge below. The three assumptions in §6 are
> the parts only the owner can validate.

---

## 1. The question this answers

The radar plan §0, restated: *beat-by-beat is a capability the 60 GHz radar
unlocks — but who specifically needs contactless per-beat HRV on a still subject,
badly enough that a $90 Polar H10 chest strap isn't fine for them?*

If that question has no honest answer, the plan itself says the move is to reframe
v2 around averaged trend monitoring on radar. So this document either earns
beat-by-beat or it doesn't.

The chest strap is the thing to beat. It is cheap, medical-grade, gives true
per-beat RR intervals today. Contactless only wins where someone **cannot or will
not** wear one. The whole thesis lives in that gap.

## 2. The wedge — narrowest defensible population

**Nocturnal cardiac monitoring of memory-care residents who will not tolerate a
wearable.**

Not "skilled nursing" broadly. Specifically the dementia / Alzheimer's memory-care
unit, at night, while the resident is in bed. This is the population where the
chest-strap objection does not just weaken — it collapses. These residents pull off
anything attached, refuse devices, or are agitated by them. A wearable is not a
worse option here; it is not an option. Contactless is the only modality that runs.

The subject is also, by construction, **still** — asleep, in bed — which is exactly
the regime where mmWave beat detection is demonstrated and where v2's Phase 1–2
work is scoped. The hard part of the technology and the easy part of the customer
overlap. That alignment is the reason to start here.

## 3. Why per-beat is load-bearing, not just averaged

§0's sharpest challenge: averaged HR + RR already serves deterioration trending, so
why pay for per-beat? Three things averaged monitoring cannot do, ranked by how
hard each leans on per-beat:

1. **Paroxysmal AFib detection — irreducibly per-beat.** Atrial fibrillation is
   *defined* by irregularly irregular RR intervals; averaged HR is blind to it.
   Paroxysmal AFib is intermittent, often nocturnal, and badly underdiagnosed in
   the over-80 population (prevalence runs well into double digits), where it is a
   leading, anticoagulable stroke cause. A 24–48 h Holter or a 14-day patch is a
   *snapshot*; intermittent AFib can hide between snapshots. A radar that runs
   **every night for months** has a detection-coverage advantage no strap or Holter
   matches — and that advantage exists *only* because it is contactless and
   adherence-free. This is the strongest single argument in this document.

2. **HRV as an early-warning signal.** Falling heart-rate variability shifts before
   averaged HR moves — an earlier handle on sepsis, autonomic decline, and general
   deterioration than averaged vitals give.

3. **HRV as a non-verbal distress proxy.** In late-stage dementia the patient
   cannot report pain or agitation; autonomic markers in beat-to-beat HRV are a
   researched correlate. Softer claim, secondary pillar — but it compounds with the
   memory-care wedge rather than pointing somewhere else.

Averaged monitoring catches the patient already visibly declining. Per-beat catches
AFib (invisibly), and catches decline earlier. That is the upgrade.

## 4. Why the chest strap genuinely loses here

Three axes, all three lost — not on comfort, on function:

- **Adherence.** The strap monitors only while worn. This population removes it.
  Coverage goes to near-zero exactly when you need months of it.
- **Tolerance.** A strap can itself *cause* the agitation you are trying to detect.
- **Longitudinal coverage.** Even a compliant patient gets a Holter for a day or a
  patch for two weeks. Intermittent paroxysmal AFib needs *continuous months*. The
  contactless monitor is the only thing that delivers that without an adherence
  cost. This is the contactless-specific moat, and it is the same axis as #1 in §3.

## 5. The buyer and willingness to pay

The user is the resident; the **buyer** is the memory-care / SNF operator, or the
value-based-care entity (ACO, Medicare Advantage plan) that bears the cost of the
strokes and ED transfers AFib detection prevents. Those entities already pay for
fall sensors and bed monitors — a per-room contactless cardiac monitor sits in a
budget line that exists. An undiagnosed-AFib stroke is a six-figure event; the
willingness-to-pay math is driven by avoided strokes and avoided transfers, not by
the monitor's BOM. A ~$25–60 v2 sensor (radar plan §6) against that backdrop is not
the constraint — credibility of the AFib signal is.

## 6. Honest weak points — what the owner must validate

This thesis is conditional. It is wrong if any of these fail:

1. **The AFib claim needs the technology to clear Gate 2.** Detecting irregular RR
   from chest *motion* is harder than from a strap's ECG. If Phase 1–2 cannot get
   per-beat IBI clean enough, the AFib pillar falls and only the softer HRV /
   distress pillars remain — which averaged monitoring partly covers. The plan's
   gates already test exactly this; the thesis rides on them.
2. **Owner's market read.** Is memory-care the right beachhead, or does the owner
   see a sharper one (post-surgical discharge, home sleep, infant monitoring)? The
   plan's §0 says this is the owner's call. This document picks memory-care because
   it is where the chest-strap objection is *provably* dead — but the owner has
   customer-contact context this analysis does not.
3. **Regulatory framing.** "AFib screening" vs "AFib diagnostic" are different
   regulatory animals. v2 is positioned as a *screening / trend* tool that flags
   residents for a clinician to confirm — not as a diagnostic device. The owner
   confirms that framing is acceptable to the target buyer.

## 7. Verdict

Beat-by-beat is **earned**, on one wedge: contactless nightly cardiac monitoring of
memory-care residents who cannot wear a strap, with paroxysmal-AFib detection as the
per-beat-only capability that no chest strap or Holter delivers at months-long
coverage. It does not abandon ViFi's founding "why" (catching deterioration between
nursing rounds) — it sharpens it from "averaged trend" to "the trend plus the one
arrhythmia that is both common, dangerous, and otherwise missed."

If the owner cannot get behind this wedge or a sharper substitute, the plan's
fallback stands: reframe v2 as averaged trend monitoring done well on radar, and
drop beat-by-beat as an end in itself.
