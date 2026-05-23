# Demand Validation Interviews — runbook

Why this exists, who you're talking to, what to ask, what counts as
a signal vs a polite shrug, and how to write up what you learn. Treats
the radar pivot as falsifiable, not foregone.

Background context for the team: `docs/RADAR_DEMAND_THESIS.md` (draft),
`project-demand-validation-gap` memory. The pivot to mmWave radar was
made on technical grounds (CSI is data-bound + cannot do beat-by-beat
on ESP32-S3); the **commercial** grounding is what these interviews
exist to harden.

---

## 1. What you are validating

Pick a primary thesis and a secondary thesis. The interview is engineered
to falsify the primary one cheaply.

**Primary thesis (default):** *In acute-care or post-discharge settings,
clinicians and the institutions that employ them have an unmet need for
continuous, contactless, ambient vital-signs monitoring (HR, HRV, RR,
presence) that does not require a wearable on every patient.*

**Secondary thesis:** *Continuous ambient HRV (not just HR) is a leading
indicator clinicians would act on if they had it cheaply and reliably.*

Anti-theses you should be willing to leave the conversation believing:

- "Pulse oximeters + on-bed sensors already cover this; you're solving a
  non-problem."
- "Yes the need exists, but ECG-grade is the only thing we'd act on,
  which puts you a Class II clearance away from anything we'd buy."
- "Ambient is a privacy / consent non-starter in our wards regardless of
  capability."

A bad interview is one where every clinician politely says it sounds
useful. A *good* interview is one where someone tells you no, with a
reason, that you didn't anticipate. Bias the questions toward forcing
that.

---

## 2. Interviewees

Per the `project-demand-validation-gap` memory:

- **Anchor 1 — the retired hospital chief of staff** (your existing
  contact). Single most-leveraged 30 minutes. They have decade-scale
  pattern-matching on what hospital procurement actually buys vs what
  clinicians ask for at a conference.
- **Anchor 2-3 — currently-practicing clinicians.** Two of: hospitalist,
  emergency-medicine attending, post-acute / SNF medical director,
  cardiology attending, ICU nurse charge nurse, rapid-response team
  lead.
- **Optional 4-5 — adjacent buyers.** Telehealth platform PM, home-care
  agency operations director, value-based-care contracting lead. These
  are who actually writes the check; clinicians shape the spec.

Five interviews is enough to falsify or harden the primary thesis.
More than that without an explicit follow-on question is overinvestment.

---

## 3. Scheduling template

Email or text, 4-6 sentences. Don't pitch the product.

> Subject: 30 min on continuous monitoring — picking your brain
>
> Hi [name], working on something in the contactless vital-signs space
> and the honest part is I don't know yet whether the problem I'm
> solving is actually a problem clinicians want solved or one I just
> find technically interesting. 30 minutes of your time on what's
> broken vs not in how you monitor patients today — no slide deck, no
> pitch — would be a huge help. Any week in the next two? I can do
> any time you're free, on the phone or over Zoom.
>
> Thanks, [you]

Targets: 3-out-of-5 acceptance. If you're getting <50%, your subject
line is reading like fundraising; soften the framing.

---

## 4. Question bank (45-min interview, ~30 min content)

Sequence matters. Do NOT pitch the product before Q14. The earlier
questions exist to fail your thesis cheaply.

### Phase A — what they actually do (5 min, build context)

1. "Walk me through how you actually monitor vital signs on a typical
   inpatient / your typical patient today. What's the current
   workflow? Who reads which numbers when?"
2. "What's the failure mode of the current setup? When does something
   slip through?"
3. "Last time a patient's HR change went unnoticed for too long — what
   was the situation?" *(Forces specifics; if they can't recall an
   instance, that's a signal.)*

### Phase B — desperate specificity (10 min, the heart of the interview)

4. "Who, specifically, on the floor is responsible for catching a HR
   trending up over the last 30 minutes? Is it actively monitored or
   alert-driven?"
5. "What's the most underserved patient cohort in your unit/practice
   for monitoring? Why?"
6. "If I gave you an extra 3 staff members tomorrow, would the answer
   to question 5 change?" *(Tests whether the problem is monitoring
   capability or staffing — if more headcount fixes it, you don't have
   a product.)*
7. "What's currently on the bed / on the patient / in the room? Pulse
   ox, telemetry strips, capnography, sleep mat, anything ambient
   already?"
8. "Last 3 months — was there a near-miss where slower vital-signs
   monitoring caught the change too late?"

### Phase C — the contactless contraposition (5 min)

9. "If the patient didn't have to wear anything — no leads, no chest
   strap, no finger probe — what would that change?"
10. "What's the ceiling on accuracy you'd accept for it to be useful?
    Within 5 bpm? Within 1 bpm? Beat-by-beat HRV?" *(Bound the
    technical spec from the clinical end. This is the single most
    important answer.)*
11. "Are there patient cohorts where you'd specifically want NOT to
    have the patient instrumented? Why?"

### Phase D — willingness, friction, gatekeepers (5 min)

12. "Who in your institution would have to sign off on a new ambient
    sensor in patient rooms? Privacy, ID, biomed, infection control?"
13. "Imagine a system that does [stated capability — keep it dry: HR,
    RR, presence, HRV; contactless; no patient-worn anything] for
    $X / room / month. Would your unit pilot it? Why or why not?"
14. "If you were going to ship this idea against you / your unit, how
    would you do it?" *(This is the question that surfaces the
    objection they won't volunteer politely.)*

### Phase E — your turn to ask anything (5 min)

15. "What did I not ask about that I should have?"
16. "Who else should I talk to?" *(Always ask. Two interviews this
    surfaces become anchors 4 + 5.)*

---

## 5. Listening rubric — what counts as signal

| You hear... | Counts as... | Note |
|---|---|---|
| A specific story with names + dates of a missed deterioration that contactless monitoring might have caught | strong demand signal | weight 3× a generic "it would be useful" |
| A specific cohort named ("our post-cath patients", "step-down ICU", "home-with-CHF") | useful narrowing | bound the wedge |
| "We already have [X]" + a description of X's failure mode | competitive landscape | confirm X actually does what they think; sometimes they overstate it |
| "We'd want it to be ECG-accurate" with no flexibility | regulatory wall | weight against radar-only if heard 3+ times |
| "Privacy / consent is the showstopper" with specifics | adoption wall | clarify whether non-visual (RF-only) sensing changes the answer |
| "Sounds great" with no specifics | polite shrug | weight 0; you got nothing |
| "Sounds great" + an immediate ask about pricing | strong demand signal | weight 2x |
| They volunteer who else to talk to without prompting | strong qualitative signal | the question pulled their thinking forward |
| They redirect you to a different problem ("the real issue is X") | priceless | this is the gold |

---

## 6. Synthesis after each interview

Write within 24 hours, while it's fresh. Append one block per interview
to `docs/HOME_PILOT_LOG.md`-style log:

```markdown
## Interview — [date] — [interviewee role, anonymized OK]

### Demand reality
- One sentence: do they have the problem, hot/warm/cold.
- Specific story they told that anchors this.

### Underserved cohort (if surfaced)
- Cohort name + why.

### Adoption walls
- Who signs off; what's the actual gating constraint.

### Spec ceiling
- Accuracy they need; what failure mode is unacceptable.
- HRV — would they act on it if you gave it to them?

### Anti-radar signals (if any)
- Did anything they said push toward CSI > radar, or away from both?

### Best quote (verbatim)
- The one sentence that captured their actual view.
```

---

## 7. Decision matrix after 5 interviews

After the fifth interview, compile into a one-page sheet:

| | Inpatient acute | Post-acute / SNF | Home / VBC |
|---|---|---|---|
| Demand evidence (0-3) | | | |
| Spec ceiling (HR-only / HRV / ECG-grade) | | | |
| Top adoption wall | | | |
| Best wedge cohort | | | |
| Estimated buyer | | | |
| Estimated procurement cycle | | | |

If a column scores ≥2 on demand with a buyer named and an adoption wall
that radar (not CSI, not ECG patches) plausibly clears, **that column
is the v2 wedge.** If no column scores ≥2, you've learned the pivot was
premature — go back to the technical work with that fact in hand
instead of building toward a market that didn't ask for it.

If the matrix says "post-acute / SNF" wins, that may also change the
*sensor* choice: SNFs are price-sensitive in a way acute hospitals
aren't, and a $40-radar-module-per-room is a different conversation
from a $400 one. The technical work and the commercial work are not
independent — these interviews are how the latter gets into the former.

---

## 8. Anti-patterns

- **Pitching before Phase D.** You will pollute every answer that follows.
- **Asking "would you use this."** Everyone says yes to be polite. Ask
  about past behaviour: *"last time you had this problem, what did you
  actually do?"*
- **Asking only your existing contacts.** Selection bias toward people
  who already think well of you. Ask each anchor for two introductions.
- **Treating one strong interview as a market.** N=1 is an anecdote.
  Five interviews with consistent signal is a thesis.
- **Skipping write-ups.** Memory degrades within a week; what felt
  obvious becomes a vibes-only impression. The synthesis is the
  artifact, not the interview itself.

---

## 9. What "done" looks like

You can fill in `docs/RADAR_DEMAND_THESIS.md`'s outstanding sections
without hedging:

- The wedge cohort, named with confidence (one paragraph).
- The minimum acceptable accuracy spec, sourced to ≥3 interviews.
- The top adoption wall, with a strategy to clear it (or an explicit
  "we will not clear this in v2, so v2 sells around it").
- The buyer, with their procurement cycle length.
- A decision: continue radar v2, pivot back to CSI for a different
  cohort, or shelve both and find the third option.

Until you can do that, the engineering work for SP3-SP7 should not
be deprioritised but should not be staked on a market that hasn't
told you it exists. SP2 stands on its own as a technical-feasibility
proof regardless.
