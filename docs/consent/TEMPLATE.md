# ViFi research data consent (TEMPLATE)

> **Status: template, not a signed form.** This is the master wording the
> founder finalizes and (where required) has reviewed by counsel before any
> subject signs. It is not legal advice. Signed forms contain PII and **NEVER
> enter this repository** (see `docs/DATA_SOP.md`); they are stored per the SOP.
> Fill every `{{ ... }}` placeholder before use.

---

## Study summary (plain language)

ViFi is developing a **contactless vital-signs sensor**: a small 60 GHz radar
device that measures heart rate and breathing from a short distance, without
any wires, cuffs, or skin contact. To teach the system, we record short
sessions where the radar runs while you wear two reference sensors that measure
the "true" values we compare against:

- a **Polar H10 chest strap** (heart rate / ECG), and
- a **Vernier respiration belt** (breathing).

A typical session is about **15-30 minutes**, seated, in a normal room. Some
sessions include light activity (for example a brisk walk or, only if you are
cleared for it, a short burst of exercise) so we capture a range of heart rates.
You set the pace and can stop at any time.

## What is collected

- Radar signal recordings (no camera, no audio, no image of you).
- Heart-rate / ECG and breathing recordings from the two reference sensors.
- Coarse, non-identifying body descriptors used only to analyze how the sensor
  performs across different people: approximate **height, weight, age band, sex,
  and build**. We do **not** record your name, face, voice, or address in the
  dataset.

Each session is stored under a **pseudonymous subject code** (for example
`subj07`), not your name. The link between your name and that code is kept
separately from the data, under restricted access, and is used only to honor a
withdrawal or a re-contact request.

## How your data is used

By signing, you agree that your pseudonymized recordings may be used for:

1. **Building and testing the ViFi sensor**, including training and evaluating
   machine-learning models, now and in the future, including research
   directions not yet defined.
2. **Internal and external collaboration**: sharing with engineers, clinical
   advisors, and research collaborators working on the sensor.
3. *(Optional, opt-in below)* **Publication or release** as part of an academic
   dataset or benchmark, always pseudonymized and without the name-to-code link.

We do **not** sell your data. Because the dataset contains no direct identifiers,
fully de-identified data that has already been shared or published may not be
retractable; this is explained in "Withdrawal" below.

## Optional permissions (initial each that you agree to)

- `____` I agree my pseudonymized data **may be published or shared** as part of
  an academic dataset/benchmark (Use #3 above). *(Leave blank to allow internal
  use only.)*
- `____` ViFi **may contact me again** to ask about an additional voluntary
  session. Preferred channel: `{{ email / phone / other }}` = `____________`.
  *(This is a request each time; it is never an obligation.)*

## Risks and your wellbeing

The radar is low-power and non-contact and poses no known risk. The reference
straps are worn snugly. If a session includes activity, you do only what you are
comfortable with; tell the operator to stop at any sign of dizziness, chest
discomfort, or fatigue. You may pause or end any session for any reason with no
consequence.

## Withdrawal

You may withdraw at any time. To withdraw, contact `{{ contact }}` and quote
your subject code if you have it (or your name; we will look it up via the
restricted link). On withdrawal we will **delete your recordings and your
name-to-code link** from our systems and backups within `{{ N }}` days. Data
that has already been **de-identified and released or published** before your
withdrawal cannot be recalled, because it can no longer be traced back to you.

## Data retention and security

Recordings are stored pseudonymized, encrypted at rest and in transit, with
access limited to the ViFi team and named collaborators. Details are in ViFi's
data-management SOP, available on request. We keep data for as long as it is
useful for sensor development unless you withdraw.

## Voluntary participation

Participation is entirely voluntary. You are not an employee or patient of ViFi
and receive no medical advice or diagnosis from these sessions. The reference
devices are research tools, not a clinical assessment of your health.

---

## Signatures

- Participant name: `____________________________`
- Signature: `____________________`  Date: `__________`
- Operator/witness: `____________________`  Date: `__________`
- Consent version: **{{ consent_version }}** (record this in the capture tracker)

---

*Consent version history (maintained in-repo; the form text, not signatures):*

| version | date | change |
|---|---|---|
| v1 | {{ date }} | initial template |
