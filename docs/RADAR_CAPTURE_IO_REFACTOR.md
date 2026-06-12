# Scope: memory-frugal `radar/capture_io.load_capture`

Status: PROPOSED (gated on founder sign-off; not started).
Owner: next capture-infra change. Author: 2026-06-11.

## Problem

`load_capture` is the one loader for every `radar_cap.pkl`. It holds the entire
capture in RAM three times over:

1. `_payloads()` reads the pickle (`raw`, ~1.6 GB for a 600 s keep-chirps
   capture) and builds a Python list of **all** parsed frame dicts (~3 GB of
   float objects for ~60 k frames).
2. `load_capture` then `np.stack`s a full `slots` array,
   `(n_frames, 4, 256, n_rx)` complex128 ≈ **1.5 GB**, plus a transient copy
   during the stack.

Measured on the dev box (7.5 GB total, ~5 GB free) with `founder/rest_1`
(15,320 frames, 612 s, 1.6 GB pickle): a full-materialize parse peaks **>5 GB
and gets OOM-killed** (`exit 137`). This already truncated a real capture
mid-finalize (the WSL-restart Frankenstein-dir incident, 2026-06-11).

The capture-finalize path is now defended (commit `d087548`: `verify()` streams,
and meta is stamped before the learnability QC). **But `load_capture` itself is
unchanged**, so:

- the **offline pipeline** (LOCO trainer `tools/spi_debug/radar_train_hr_selector.py`,
  the LOSO accuracy gate, any eval over the dataset) still OOMs on a single
  600 s capture, and will get worse processing many subjects back to back;
- the bench **learnability QC** silently no-ops on long captures (its OOM is now
  survived, but it produces no report).

This is the binding constraint on evaluating our own Stage-1 dataset. We
currently cannot run the standard accuracy tool on a 600 s capture; the
2026-06-11 accuracy numbers were produced by a throwaway frugal loader.

## Design

Single file of real change: `radar/capture_io.py`. One call-site param thread:
`radar/windows.py`.

1. **Stream the parse.** Replace the `raw -> full payloads list` materialization
   with a generator that yields one parsed payload at a time and lets each raw
   entry be reclaimed as it is consumed (iterate + `del`, or pop). Never hold
   `raw` and a full `payloads` list simultaneously.
2. **Preallocate, don't stack.** Two cheap passes (or one pass with a count
   probe): determine `n_frames`, format, and `fs`, allocate the target
   `np.empty` cube once, and fill it row by row. Removes the `np.stack`
   transient-doubling (~1.5 GB) entirely.
3. **Make `slots` lazy.** Add `load_capture(path, *, with_slots: bool = True)`.
   `with_slots=False` returns `CaptureData(frames, ts, slots=None, ...)` without
   ever allocating the 1.5 GB slots array. `radar/windows.py` only uses
   `frames` + `ts`, so it passes `with_slots=False` — that alone removes 1.5 GB
   from the trainer / accuracy-gate / QC path. Phase- and angle-sensitive
   consumers keep the default and pay for slots explicitly.

Out of scope (deliberately): a fully streaming windower (`iter_frames` yielding
one frame at a time). Windowing needs random slices `cube[sel]`; the averaged
cube is only ~188 MB, so holding it is fine. Revisit only if multi-capture
batch eval needs several in memory at once.

## Compatibility + invariants (must not change)

- `CaptureData` field shapes/dtypes/semantics identical for both on-disk
  formats (averaged and keep-chirps).
- Keep-chirps grouping rule **unchanged**: consecutive `chirp_slot` 0..3 runs;
  incomplete/misaligned groups discarded, not coerced.
- `legacy_average`, `per_tx_average`, `measured_fps` untouched.
- `frames`, `ts`, `slots`, `keep_chirps` byte-for-byte equal to today's loader.

The trainer's labels (and therefore the shipped selector) are downstream of this
function. A silent change in grouping or averaging changes the training set.
That is the whole risk; the test plan exists to kill it.

## Test plan

1. **Golden equality (synthetic):** for a synthetic keep-chirps fixture AND an
   averaged fixture, assert `np.array_equal` on `frames`, `ts`, and `slots`
   between the old and new loaders.
2. **Golden equality (real, bounded):** truncate `founder/rest_1` to the first
   ~2,000 frames (both loaders fit) and assert identical output.
3. **Grouping edge cases:** dropped-publish gap, trailing partial frame,
   non-monotonic slot run — assert identical discard behavior old vs new.
4. **Memory ceiling (real):** `load_capture('founder/rest_1', with_slots=False)`
   completes with peak RSS **< 2.5 GB** (measured via `/usr/bin/time -v`); with
   `with_slots=True`, **< 3.5 GB**. (Today: OOM-killed > 5 GB.)
5. **End-to-end:** `iter_windows('founder/rest_1')` completes and yields the
   same window count as the frugal prototype (119 at 20 s / 5 s).
6. Full `pytest -m "not e2e"` green; `ruff` + `mypy` (strict set unaffected).

## Acceptance criteria

- A 600 s keep-chirps capture loads and windows end-to-end on the 7.5 GB dev box
  without OOM.
- Trainer / LOSO gate / learnability QC run over a full-length capture.
- Loader output provably identical to today's on every fixture + the bounded
  real slice.

## Effort / risk

~Half a day. One module rewritten, one param threaded, ~6 tests. Risk is
**medium** because it is shared DSP infra on the training path; the golden-
equality tests reduce it to low. Land as its own reviewed PR before the
multi-subject dataset push, so the accuracy gate is usable when the data lands.
