# Codebase evaluation: vifi-ml (2026-06-09)

Branch: `feat/loso-trainer-weighting`. Scope: full repo, ~43k lines excluding docs/site.
Method: six parallel review agents (radar subsystem, CSI pipeline, platform/API,
security infrastructure, test suite, CI gauntlet); the four highest-impact claims
were independently re-verified against the source before inclusion.

## Bottom line

The codebase is in better shape than most pre-seed hardware-ML repos: CI is green
(666/666 tests, ruff and mypy clean), the DSP tests assert real numerical behavior
instead of "doesn't crash," the audit chain uses correct cryptography, and the bus
architecture is genuinely sensor-agnostic. But the review surfaced one finding that
changes the operational picture: **the live radar inference stack has never published
a single heart rate reading, and nothing told you.** Everything else is fixable
engineering debt; that one is a silent product failure.

## The critical finding (independently verified)

`deploy/systemd/vifi-radar-collector.service:15` runs `--source usb`, and its own
comment calls that "the production path." But the USB path
(`tools/radar_collector.py:285`) parses TLV type 302, which is the board's post-FFT
**magnitude** output: 128 range bins, real-valued. The inference worker
(`tools/radar_inference_worker.py:184`) silently drops every frame whose sample
count isn't 256. So 100% of live frames are discarded and `run_once` always returns
None. Zero radar HR has ever reached the bus from the systemd stack.

Two layers of wrongness, same root cause:

1. **Frame size mismatch** means everything is dropped today (silent zero output).
2. **Even if the count matched**, magnitude data has no phase, and the entire
   pipeline (DACM phase extraction) requires complex IQ. The only path that delivers
   IQ is the FTDI SPI path used for research captures. If the size filter ever
   passed, you'd get plausible-looking but physiologically meaningless HR numbers,
   which is worse than nothing for a medical device.

Business translation: the bench results (the r=+0.56 tracking, the 3.0 bpm oracle)
are real because they come from FTDI captures. But the "live stack with radar" you
can demo from systemd is a Potemkin pipeline. The fix is mechanical (point the
service at the FTDI source, plumb the FTDI URL into live.env), but it must happen
before any pilot conversation where someone watches the dashboard.

## Other verified high-severity findings

**The CSI calibration workflow is dead code that CI can't see.**
`tools/calibrate_subject.py:53` imports `utc_now_iso` from `calibration.py`, which
has no such symbol. Every invocation dies with ImportError at startup. CI is green
because no test imports that script. This is the walk-in-detection /
subject-identification story, currently non-functional.

**Every `/api/v1/*` route silently drops its permission checks.** The aliasing loop
at `api.py:808-822` copies path, handler, and response model but not the route's
scope dependencies (verified: the tuple has four elements, dependencies absent).
Today it's latent because all keys get wildcard scope, but the moment a restricted
clinician key is issued under SP7, that key can hit `/api/v1/identify` and
`/api/v1/predict` regardless of its scopes.

**Fail-open security defaults.** If the Pi's env file is ever missing (SD card
reflash, corruption), the API boots with `VIFI_AUTH_MODE=none` (fully open) and the
audit log writes `pseudo-dev:<real_subject_id>` (plaintext identity with a
decorative prefix). Neither failure stops boot; both are just log lines on a
headless device in someone's home. For a patient-monitoring company, defaults must
fail closed: default auth mode to `api_key` and `VIFI_REQUIRE_PSEUDO` to true, so a
misconfigured device refuses to start instead of running naked.

**The CSI inference worker can die from one bad window.** `tools/inference_worker.py`
has no per-stride exception handler in its main loop; a NaN feature vector or an SVD
non-convergence kills the process, and the unACKed messages then re-crash it in a
loop on restart. Systemd restarts mask this until they don't. One try/except per
stride fixes it.

**Train/eval hygiene in the CSI retrainer.** `tools/retrain_on_real.py` splits
train/val at the window level with 50%-overlapping windows, so its printed val MAE
is optimistic (the LOSO 13.90 number is fine; it was computed correctly elsewhere).
It also re-implements feature building instead of calling the canonical function,
which silently diverges the moment PCA suppression is enabled. CSI is
maintenance-mode so this isn't urgent, but it should be fixed before any retrain.

**Auth-mode flip breaks the dashboard.** Static assets (`styles.css`, `js/*`) aren't
in `PUBLIC_PATHS`, so enabling `api_key` mode 401s the stylesheet and scripts before
the login overlay can render. SP7 hardening as currently planned would brick the UI.

## What's genuinely strong

- **Radar DSP tests** assert known-signal-in, known-frequency-out with tight
  numerical tolerances (FFT peak at exact bin, DACM phase error < 0.02 rad,
  displacement correlation > 0.97). That's the right way to test signal processing.
- **The radar inference worker test file** runs a full end-to-end loop on an
  in-memory bus with synthetic physiology and asserts HR within 6 bpm, apnea timing,
  presence transitions. Thirteen tests, production-grade.
- **Audit chain**: parameterized SQL, real HMAC chaining, constant-time key
  comparison, Fernet authenticated encryption, nine dedicated test files including
  tamper and deletion detection. One gap: the standalone `audit_verify.py` CLI
  doesn't pass the chain-state store, so it can't detect trailing truncation; an
  investigator would get "OK" on a tampered log.
- **The new commit** (subject + HR-bin weighting, `75a6079`) is mathematically
  correct with well-chosen isolation tests. Two small hardening items:
  `_hr_bin(NaN)` silently lands in the elevated bin, and the Viterbi decoder crashes
  on an empty candidate window (both currently shielded by caller-side filters,
  both one-line guards).
- **CI gauntlet on this branch**: ruff, mypy, pytest all pass. The
  vulture/shellcheck/pip-check red exit codes are all inherited from main (12
  false-positive-shaped vulture hits, 4 info-level shellcheck notes, one
  streamlit-vs-pandas-3 conflict in the env).

## Four-pillar summary

1. **Architecture and debt**: The bus abstraction is clean and the sensor-agnostic
   design is paying off exactly as intended. The debt concentration is in `tools/`
   scripts that duplicate library logic (retrainer, calibration) and in the untested
   training harnesses under `tools/spi_debug/`, which are now the company-gate code
   path with zero CI coverage.
2. **Code quality / DRY**: Two confirmed cases of copy-instead-of-call (feature
   building in the retrainer and calibrator) that have already produced one real
   divergence risk and one dead script.
3. **Robustness**: The pattern across findings is consistent: failures are silent.
   Frames dropped silently, workers die silently, auth opens silently,
   pseudonymization degrades silently. The single highest-leverage engineering
   principle to adopt repo-wide is "fail loudly or fail closed."
4. **Performance / infra**: No cloud-bill risks (it's a Pi). One real-time
   inefficiency worth profiling: `select_best_rx` computes the winning antenna's
   displacement twice (~4x the needed filter work per window).

## Complete prioritized work list (all 33 findings)

**Implementation status (2026-06-09): all 33 items landed on branch
`fix/eval-findings-2026-06-09`.** Full gauntlet green after the work: 775
passed / 0 failed (109 net-new tests), ruff, mypy, vulture, shellcheck,
pip check, and docker build all exit 0. Three findings from implementation:

- **Item 26's premise was wrong.** `np.hanning` IS the symmetric Hann,
  numerically identical to `scipy.signal.windows.hann(n, sym=True)` (verified
  to 1 ULP across n=5..1000). There was never a leakage discrepancy between
  `preprocess.py` and `rr_dsp.py`. Resolution: unified on the scipy import
  with zero numerical change, plus a test pinning the equivalence. No
  periodic-window switch (that WOULD have shifted features under the
  deployed model).
- **Item 12's bug was real and material.** Recomputing RR labels from raw
  force over the 10 local v2 captures changed labels in 3 sessions; worst
  case 15/269 labels in `founder/session_20260520T014522Z` with a max error
  of 70.3 brpm. Corrected labels written alongside originals
  (`rr_labels_recomputed_v2.csv`); originals untouched.
- **Item 1 exposed a missing dependency.** `pyftdi` was not in
  requirements.txt; now pinned (0.57.2, capture extra). It must be installed
  in the Pi venv before the FTDI collector unit starts.

Deploy follow-ups required when this branch ships (fail-closed defaults are
the point, but they bite unconfigured hosts): the Pi service env must set
`VIFI_AUTH_MODE` and `VIFI_REQUIRE_PSEUDO` explicitly, file-based API keys
that should see the patient census need the new `read:rooms` scope, and
`VIFI_RADAR_FTDI_URL` must be set for the radar collector.

Every action item from the review, in priority order. Tiers: 1-6 product-breaking
or pilot-blocking, 7-15 correctness and data integrity, 16-23 security hardening
before SP7, 24-33 quality debt and hygiene.

### Tier 1: product-breaking / pilot-blocking

1. **Switch the radar collector to the FTDI source.**
   `deploy/systemd/vifi-radar-collector.service:15` runs `--source usb`, which
   delivers 128-bin magnitude data the worker drops 100% of. The live radar stack
   has never published an HR reading. Point the unit at `--source ftdi`, plumb
   `VIFI_RADAR_FTDI_URL` into live.env.
2. **Add an "IQ is purely real" guard in the radar DSP.**
   `radar/dsp.py` (extract_displacement): if a window arrives with all-zero
   imaginary parts, DACM phase is meaningless but still produces plausible HR
   numbers. Log a loud warning so the magnitude-vs-IQ failure mode can never be
   silent again.
3. **Fail-closed auth default.**
   `security.py:84-94`: `VIFI_AUTH_MODE` defaults to `none`. A Pi that loses its
   env file boots wide open in a patient's home. Default to `api_key`; config
   validation already refuses to start with no keys, so misconfiguration becomes
   a boot failure instead of an open API.
4. **Fail-closed pseudonymization default.**
   `pseudonymize.py:78-110` plus `.env.example`: with no salt and
   `VIFI_REQUIRE_PSEUDO=false` (the default), audit logs record
   `pseudo-dev:<real_subject_id>`, plaintext identity with a label. Default
   `VIFI_REQUIRE_PSEUDO` to true in both code and `.env.example`.
5. **Forward `dependencies` in the `/api/v1` alias loop.**
   `api.py:808-822` copies routes without their scope checks, so every versioned
   endpoint silently loses authorization granularity. Extract
   `route.dependencies` and pass it to `add_api_route`. Add a regression test.
6. **Add static assets to the auth allowlist.**
   `security.py:67-81`: `styles.css`, `js/*`, and fonts are not in
   `PUBLIC_PATHS`, so flipping to `api_key` mode 401s the assets before the login
   overlay can render and bricks the dashboard. Allow by extension or prefix.

### Tier 2: correctness and data integrity

7. **Per-stride try/except in the CSI inference worker.**
   `tools/inference_worker.py:475-478`: one NaN window or SVD non-convergence
   kills the process, and unACKed messages re-crash it in a loop on restart.
   Wrap `run_once` and `_publish_rr` per stride; log, count, continue. Also catch
   `LinAlgError` in `rr_dsp.decompose()` to match `subtract_top_components`.
8. **Same per-stride guard pattern in the radar inference worker.**
   Audit `tools/radar_inference_worker.py`'s loop for the same single-exception
   kill path and apply the same containment.
9. **Fix the dead calibration script.**
   `tools/calibrate_subject.py:53` imports `utc_now_iso`, which does not exist in
   `calibration.py`; every invocation dies with ImportError. Fix the import, and
   add at least an import-smoke test over `tools/` so dead scripts can't pass CI.
10. **Retrainer must call the canonical feature builder.**
    `tools/retrain_on_real.py:102-107` (and `tools/calibrate_subject.py:92-97`)
    re-implement envelope building and skip PCA suppression, guaranteeing
    train/serve skew the moment `VIFI_PCA_COMPONENTS_REMOVED` is set. Replace the
    inline blocks with `build_envelope_from_amps()`. This also fixes the OOD
    detector being fitted in the wrong feature space (`retrain_on_real.py:257`).
11. **Session-boundary train/val splits in the retrainer.**
    `tools/retrain_on_real.py:200-205` splits 50%-overlapping windows randomly,
    so printed val MAE is systematically optimistic. Assign whole sessions to
    train or val, matching the LOSO protocol.
12. **Fix the RR ground-truth parabolic guard, then recompute labels.**
    `rr_logger.py:145-148` lacks the inverted-parabola rejection that
    `preprocess._parabolic_interp` has, occasionally corrupting the live-derived
    RR reference. Adopt the same guard, then recompute RR labels offline from the
    raw 10 Hz force data for all v2-schema captures. No re-capture needed.
13. **Pass the chain-state store to `audit_verify.py`.**
    `tools/audit_verify.py:69` calls `verify_chain` without the SQLite store, so
    a truncated audit log verifies as OK. A forensic tool that cannot detect
    truncation is theater. Load the store from the audit dir and pass it.
14. **Smoke tests for the two training harnesses.**
    `tools/spi_debug/radar_track_accuracy.py` and `radar_train_hr_selector.py`
    are the company-gate code path and have zero tests. Add a synthetic-capture
    smoke test that runs each end-to-end and asserts `groups`/`truths`/`x`/`y`
    stay length-aligned through the candidate-extraction loop.
15. **Guard `_hr_bin` and `viterbi_decode` against bad input.**
    `radar/hr_selector.py:143-147`: `_hr_bin(NaN)` silently lands in the elevated
    bin; raise on non-finite input. `radar/hr_selector.py:211-232`: empty
    candidate windows crash backtracking; guard and document the precondition.
    Add boundary tests pinning the 90/120/150 bpm bin edges.

### Tier 3: security hardening (before SP7 / pilot)

16. **Remove `/docs`, `/redoc`, `/openapi.json` from `PUBLIC_PATHS`.**
    `security.py:67-81`, `api.py:712-727`: in `api_key` mode the full API schema
    is currently readable unauthenticated. Default `VIFI_EXPOSE_DOCS` to false.
17. **Bind worker Prometheus metrics to localhost.**
    `observability.py:267`: `start_http_server` binds 0.0.0.0 with patient IDs as
    metric labels; any LAN client can enumerate patients. Pass `addr=127.0.0.1`.
18. **Sanitize the `X-Request-Id` header.**
    `security.py:434-438`: unvalidated caller input is echoed into response
    headers and log lines. Strip to `[\w\-]`, cap at 64 chars.
19. **Validate the WebSocket `patient_id` query parameter.**
    `api_internals/websocket.py:54`: the raw string is interpolated into Redis
    stream key names, letting a caller subscribe to arbitrary streams. Allowlist
    regex `^[a-zA-Z0-9_\-]{1,64}$`, close with 1008 on violation.
20. **Add a scope check to `/api/v1/rooms`.**
    `api_internals/routes_rooms.py:96-116`: any valid key can enumerate all
    monitored patients and their last-activity timestamps. Require a scope.
21. **Fix `.env.example` audit fsync contradiction.**
    `.env.example` sets `VIFI_AUDIT_FSYNC=false` while the code default is true
    for power-loss durability. Anyone copying the example silently trades away
    audit durability. Set it to true with a comment.
22. **`/health` should report degraded when the model fails to load.**
    `api_internals/routes_meta.py:49` returns `status: "ok"` with
    `model_loaded: false`; monitors keying on status miss the failure. Return
    `"degraded"`.
23. **Make the Trivy container scan blocking.**
    `.github/workflows/ci.yml:246` has `exit-code: '0'`, so HIGH/CRITICAL CVEs in
    the base image never fail CI. Flip to blocking with a tracked `.trivyignore`.

### Tier 4: quality debt and hygiene

24. **Early stopping for the quantile models.**
    `tools/train_quantile_models.py:149,164`: quantile fits get no eval_set, so
    all 400 trees always grow and the CI bounds overfit on 3 sessions. Pass
    `eval_set` and `early_stopping_rounds` like the mean model.
25. **Type the model bundle properly.**
    `tools/inference_worker.py:129`: `hr_model: object` hides API breakage until
    the hot path throws AttributeError. Use `XGBRegressor` or a `predict`
    Protocol. Same pass: fix `quality.py:121`'s wrong return annotation
    (`float` returned where `np.ndarray` is declared).
26. **Standardize the FFT window function.**
    CORRECTED: the original finding claimed `np.hanning` is periodic; it is
    not. Both paths already used the identical symmetric Hann. Resolved by
    unifying `rr_dsp.py` on the scipy `hann` import (zero numerical change)
    with a test pinning the equivalence.
27. **Honest annotation in `rr_logger.update`.**
    `rr_logger.py:119`: parameter typed `float` but None-checked. Annotate
    `Optional[float]` so a future refactor can't silently pass None.
28. **Deduplicate the radar best-RX displacement computation.**
    `radar/dsp.py:345-385`: `select_best_rx` computes and discards displacement
    for all three antennas, then `extract_displacement` recomputes the winner
    (~4x the needed filter work per window). Return the computed result. Profile
    on the Pi first to confirm it matters at the 2 s stride.
29. **Log degenerate circle fits.**
    `radar/dsp.py:273-275`: `kasa_circle_fit` silently returns radius 0 when the
    fit is degenerate (expected at low SNR). Emit a debug log and a
    `circle_fit_ok` flag on `DspInfo` so quality gating can see it.
30. **Bound `InMemoryBus._seq_within_ms` growth.**
    `modules/bus/memory.py:45`: the timestamp-to-sequence dict grows without
    bound in long-running dev/test processes. Trim like the per-topic cap.
31. **Add a WebSocket disconnect test.**
    `tests/test_api_stream.py` covers only the happy path. A subscription leak on
    client disconnect (every browser refresh) would accumulate silently; pin the
    cleanup behavior with a test.
32. **Strengthen the weak selector tests.**
    `tests/test_radar_hr_selector.py`: add a 2-window Viterbi test where
    continuity contradicts per-window argmax (the single-window test cannot fail),
    a combined group-plus-bin imbalance case for `balanced_sample_weights`, and
    the missing mean-1.0 normalization assert in the HR-bin totals test.
33. **Clean the inherited red CI exit codes.**
    Vulture whitelist for the 12 known false positives, the two SC2015 rewrites
    plus SC2016 directives in `tools/enable_security_mode.sh` and
    `tools/live_stack.sh`, remove the dead `logging.basicConfig` at `api.py:71`,
    and resolve the env-level streamlit-vs-pandas-3 conflict (pin pandas <3 or
    bump streamlit).

Tier 1 is roughly a day of work combined. None of this changes the strategic
picture: the radar path is still data-bound, and the dataset plus learned
peak-selector remains the company gate. But land item 1 before anyone watches a
live demo, because right now the live radar stack is a convincing-looking pipeline
that outputs nothing.
