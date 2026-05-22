# Implementation Plan — Remove the synthetic model as a serving model

Branch: `feat/live-monitoring-stack` (continues the SP1 work)
Predecessor: `2a517fb` (live stack already real-model-only)

## Why

User directive: "I never want the synthetic model anywhere. I don't want the
fake numbers, just the real numbers." The synthetic model must never produce
a served prediction — not in the live stack, not in `inference_worker.py`,
not in any `api.py` endpoint.

## What changes (and what doesn't)

- **Removed as a *serving* concept**: `SyntheticModelBundle`, the
  `--model synthetic` choice on the inference worker, the synthetic-by-default
  in `api.py`, and the `/predict`-family endpoints' synthetic path.
- **Kept (out of scope)**:
  - `data_gen.py` — the *data* generator, used by DSP unit tests for
    deterministic synthetic CSI. Not a model.
  - `radar/synth.py` — radar synthetic generator; current radar work
    depends on it (board hasn't shipped).
  - `train.py` — kept as a *test-fixture builder* (CI uses it to build
    `models/` for tests of the inference pipeline). It is no longer "the
    synthetic model trainer for serving" — its output is loaded by the test
    suite as a `RealModelBundle`-shaped fixture, not by any product code as
    a separate "synthetic" thing.

## Design — one model concept

There is ONE model concept post-change: `RealModelBundle` (kept; renamed in
spirit but the class name stays for diff size). `create_app(model_dir)` takes
ONE directory. `vifi-inference` loads it. In production it points at
`models_real/`. In tests/CI it points at the fixture dir CI builds with
`train.py` (currently `models/`).

The `RealModelBundle` already handles absent optional features
(`has_quantiles()`, `has_ood_detector()` return False) — so a fixture model
without quantiles/OOD works as the single bundle.

---

## Phase 1 — `inference_worker.py`

1. Drop `synthetic` from `--model` choices. Either remove `--model` entirely
   (cleanest) or keep it but only accept `real` (transitional). Pick removal.
2. `_load_model()` loses its `if model == "synthetic"` branch; always loads
   the real model dir (env override via `VIFI_REAL_MODEL_DIR`, default
   `models_real/`).
3. Update the worker docstring + the error-message hint about `train.py`.
4. `deploy/systemd/vifi-inference.service`: drop `--model real` (no longer
   meaningful when there's only one model).
5. Tests: `test_inference_worker.py`, `test_worker_metrics.py`. Drop any
   `--model synthetic`; tests must point the worker at a fixture model dir
   (build with `train.py` into a `tmp_path` or use the CI-built `models/`).

**Verify:** `pytest -q tests/test_inference_worker.py tests/test_worker_metrics.py`
green; `ruff` + `mypy` clean on `inference_worker.py`.

---

## Phase 2 — `api_internals/bundles.py`

1. Delete `SyntheticModelBundle` and `load_synthetic_models` /
   `_load_synthetic_models_impl`.
2. Keep `RealModelBundle` as the single bundle. Rename in a comment only —
   leave class name to keep the diff small and external callers stable.
3. `_check_pca_k_compat` — keep, used by `RealModelBundle.load()`.

**Verify:** `python -c "from api_internals.bundles import RealModelBundle"` —
imports clean.

---

## Phase 3 — `api.py` + `api_internals/routes_*.py`

1. `create_app(model_dir, real_model_dir=None)` → `create_app(model_dir)`.
   The single arg IS the model dir. Default to `Path("models_real")`.
2. `HealthResponse`: drop `synthetic_model_*` fields. Keep
   `model_version`, `hr_tol_bpm`, `rr_tol_bpm`, `feature_names`,
   `real_model_loaded`, `real_model_dir`, `real_model_metadata` (rename to
   `model_loaded`, `model_dir`, `model_metadata` for cleanliness — the
   "real" prefix is redundant when there's only one).
3. `routes_meta.py`: `/health` returns the single-model view; `/readyz`
   checks the one bundle.
4. `routes_predict.py`: `/predict`, `/predict/csi` — were synthetic. Make
   them use the same single bundle (so `/predict` now serves real numbers).
   `/predict/capture` already uses the real bundle. `/predict/demo` — if it
   was a synthetic-only canned response, delete it; if it's a real demo
   path, keep.
5. `routes_stubs.py`: any synthetic stubs gone.
6. The single model's absence: `/predict`, `/predict/csi`, `/predict/capture`
   return 503 with a clear message ("real model not found — see
   docs/STATUS.md sync from laptop"). The app still boots (`/health` /
   `/readyz` work) so the operator can diagnose.

**Verify:** `ruff` + `mypy` clean; the import surface still works:
`python -c "from api import create_app; create_app('models')"` does not raise
even when only one model dir is present.

---

## Phase 4 — Tests

Files that exercise the synthetic-model serving path:

- `tests/test_api.py` (8 hits) — uses `create_app(Path("models"))` + asserts
  `synthetic_model_loaded`. Switch to single-bundle assertions
  (`model_loaded`); the existing CI-built `models/` dir feeds the single
  bundle.
- `tests/test_inference_worker.py` (6) — see Phase 1.
- `tests/test_worker_metrics.py` (4) — see Phase 1.
- `tests/test_model_pca_metadata.py` (1) — drop synthetic-specific paths.
- `tests/test_compose_e2e.py` (2), `tests/test_docker_compose.py` (1) —
  compose stack assertions, light touch.
- `tests/test_roadmap.py` (2) — `/roadmap` route may reference synthetic in
  its response; align with the single-model story.

The CI fixture is whatever `train.py -n 500` produces. That dir is loaded by
the single bundle. The wording in tests changes from "the synthetic model" to
"the test fixture model"; functionally the same artifact.

**Verify:** `pytest -m "not e2e"` — all 571 tests still pass (modulo any
re-numbering from removed tests).

---

## Phase 5 — CI

`.github/workflows/ci.yml` step "Train synthetic models (needed by some
tests): python train.py -n 500" — rename the step to "Build the test fixture
model" (functionally unchanged; just stops calling it "synthetic models").

**Verify:** the GitHub Actions YAML still parses.

---

## Phase 6 — Docs

- `CLAUDE.md`: "Training: `train.py` (baseline)" → keep, but note the output
  is a test fixture, not a serving model. Update the line: "Training:
  `train.py` (test fixture model from synthetic data; not a serving model)".
- `README.md` / `RESULTS.md` — only if they describe the synthetic model as a
  serving thing. Grep first; touch lightly.
- `CHANGELOG.md` [Unreleased]: add a "Removed" section.

---

## Phase 7 — Gauntlet + commit + push

1. `ruff==0.6.9 check` + `format --check` clean.
2. `mypy` strict modules clean.
3. `pytest -m "not e2e"` green.
4. Commit with a clear "removed" message; push.

---

## Done criteria

- `grep -r "synthetic" --include="*.py" .` returns ONLY: `data_gen.py`,
  `radar/synth.py`, comments in `train.py` explaining "synthetic input data
  generates a test-fixture model," and tests for `data_gen.py` / radar synth.
  Nothing in `api.py`, `inference_worker.py`, `api_internals/`, or in any
  serving / `/predict*` / `/health` surface mentions a synthetic model.
- `SyntheticModelBundle` is gone.
- CI is green.
- The live stack still works (re-running `setup_live_stack.sh` deploys it).
