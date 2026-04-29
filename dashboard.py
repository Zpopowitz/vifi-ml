"""M5: Streamlit dashboard for the ViFi API.

Run:   streamlit run dashboard.py
Talks to the FastAPI service at $VIFI_API (default http://localhost:8000).

Two tabs:
  - Synthetic   : generate a synthetic IQ window and call /predict
  - Real capture: upload an ESP32-S3 capture.txt + optional H10 hr_log.csv
                  and call /predict/capture for an HR timeline with confidence
                  intervals
"""
from __future__ import annotations

import csv
import io
import os
from typing import Optional

import httpx
import numpy as np
import pandas as pd
import streamlit as st

from data_gen import generate_sample

API_URL = os.environ.get("VIFI_API", "http://localhost:8000")

st.set_page_config(page_title="ViFi", layout="wide")
st.title("ViFi: Contactless HR / RR")
st.caption(f"API endpoint: `{API_URL}`")

# ---------------------------------------------------------------------------
# Health header (also tells us whether the real-capture model is available)
# ---------------------------------------------------------------------------
real_model_available = False
try:
    h = httpx.get(f"{API_URL}/health", timeout=2.0).json()
    real_model_available = bool(h.get("real_model_loaded"))
    real_dir = h.get("real_model_dir", "?")
    line = (
        f"Synthetic model: {h.get('model_version', '?')} | "
        f"HR tol ±{h.get('hr_tol_bpm', '?')} bpm | "
        f"RR tol ±{h.get('rr_tol_bpm', '?')} bpm | "
        f"Real model ({real_dir}): "
        f"{'loaded' if real_model_available else 'NOT loaded'}"
    )
    st.caption(line)
except Exception:
    st.caption("Model status: API unreachable")

tab_real, tab_synth = st.tabs(["Real capture", "Synthetic"])


# ---------------------------------------------------------------------------
# Synthetic tab (legacy)
# ---------------------------------------------------------------------------

with tab_synth:
    st.subheader("Synthetic IQ -> /predict")
    with st.sidebar:
        st.header("Synthetic controls")
        duration = st.slider("Window duration (s)", 5.0, 30.0, 10.0, step=1.0)
        fs = st.slider("Sample rate (Hz)", 50.0, 200.0, 100.0, step=10.0)
        snr = st.slider("SNR (dB)", 0.0, 40.0, 20.0, step=1.0)
        hr_bpm = st.slider("True HR (bpm)", 60.0, 100.0, 75.0, step=1.0)
        rr_bpm = st.slider("True RR (bpm)", 12.0, 30.0, 18.0, step=1.0)
        seed = st.number_input("Seed", value=0, step=1)
        go = st.button("Generate + predict", type="primary", key="synth_go")

    col_a, col_b = st.columns([3, 2])
    if go:
        iq, meta = generate_sample(
            duration_s=duration, fs=fs, hr_bpm=hr_bpm, rr_bpm=rr_bpm,
            snr_db=snr, seed=int(seed),
        )
        with col_a:
            st.caption("Synthetic CSI |IQ|")
            st.line_chart(np.abs(iq))
        payload = {
            "fs": meta.fs,
            "iq_real": iq.real.astype(float).tolist(),
            "iq_imag": iq.imag.astype(float).tolist(),
        }
        try:
            r = httpx.post(f"{API_URL}/predict", json=payload, timeout=10.0)
            r.raise_for_status()
            pred = r.json()
        except Exception as exc:
            st.error(f"API call failed: {exc}")
            pred = None
        if pred is not None:
            with col_b:
                st.metric("HR (bpm)", f"{pred['hr_bpm']:.1f}",
                          delta=f"{pred['hr_bpm'] - hr_bpm:+.1f}")
                st.metric("RR (bpm)", f"{pred['rr_bpm']:.1f}",
                          delta=f"{pred['rr_bpm'] - rr_bpm:+.1f}")
                st.progress(pred["hr_confidence"],
                            text=f"HR confidence {pred['hr_confidence']:.2f}")
                st.progress(pred["rr_confidence"],
                            text=f"RR confidence {pred['rr_confidence']:.2f}")
                st.json(pred)
    else:
        st.info("Use the sidebar to generate a synthetic window and call the API.")


# ---------------------------------------------------------------------------
# Real-capture tab (M4 + real ESP32 path)
# ---------------------------------------------------------------------------

def _parse_hr_log_bytes(raw: bytes) -> Optional[pd.DataFrame]:
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        return None
    df = pd.read_csv(io.StringIO(text))
    if "timestamp_unix" not in df.columns or "hr_bpm" not in df.columns:
        return None
    return df


with tab_real:
    st.subheader("ESP32-S3 capture -> /predict/capture")

    if not real_model_available:
        st.warning(
            "Real-capture model is not loaded on the API. Train one with "
            "`python tools/retrain_on_real.py --pair capture.txt hr_log.csv "
            "--model-dir models_real` and restart the API."
        )

    cap_col, opt_col = st.columns([3, 2])
    with cap_col:
        cap_file = st.file_uploader(
            "Capture file (capture.txt from ESP32-S3)",
            type=["txt", "log"], key="cap_upload",
        )
        hr_file = st.file_uploader(
            "Polar H10 HR log (optional, hr_log.csv) — used to compute MAE",
            type=["csv"], key="hr_upload",
        )

    with opt_col:
        st.markdown("**Inference options**")
        window_s = st.number_input("Window (s)", value=10.0, step=1.0,
                                   min_value=5.0, max_value=60.0,
                                   key="cap_window")
        stride_s = st.number_input("Stride (s)", value=5.0, step=1.0,
                                   min_value=1.0, max_value=30.0,
                                   key="cap_stride")
        fs_resample = st.number_input("Resample fs (Hz)", value=100.0, step=10.0,
                                       min_value=50.0, max_value=400.0,
                                       key="cap_fs")
        packet_rate = st.number_input(
            "Packet rate (Hz, 0 = auto)",
            value=0.0, step=10.0, min_value=0.0, max_value=400.0,
            key="cap_packet_rate",
            help="Override packet rate for captures without timestamps.",
        )
        emit_intervals = st.checkbox("Emit confidence intervals",
                                     value=True, key="cap_intervals")
        max_interval = st.number_input("Suppress when interval >",
                                        value=15.0, step=1.0,
                                        min_value=5.0, max_value=50.0,
                                        key="cap_max_interval")

        st.markdown("**Calibration**")
        cal_mode = st.selectbox("Mode",
                                 ["none", "per_session", "stored"],
                                 index=1, key="cap_cal_mode")
        cal_subject = st.text_input("subject_id (stored only)", value="",
                                     key="cap_cal_subject")
        cal_room = st.text_input("room_id", value="", key="cap_cal_room")
        cal_posture = st.text_input("posture", value="",
                                     key="cap_cal_posture")
        auto_id = st.checkbox("auto-identify subject (fingerprint)",
                              value=False, key="cap_auto_id")

        run_cap = st.button("Score capture", type="primary",
                            key="cap_go", disabled=cap_file is None)

    if run_cap and cap_file is not None:
        capture_text = cap_file.read().decode("utf-8", errors="ignore")
        body = {
            "capture_text": capture_text,
            "packet_rate_hz": packet_rate if packet_rate > 0 else None,
            "window_s": window_s,
            "stride_s": stride_s,
            "fs_resample": fs_resample,
            "calibration": {
                "mode": cal_mode,
                "subject_id": cal_subject or None,
                "room_id": cal_room or None,
                "posture": cal_posture or None,
                "auto_identify": auto_id,
            },
            "emit_intervals": emit_intervals,
            "max_interval_bpm": max_interval,
        }
        try:
            r = httpx.post(f"{API_URL}/predict/capture",
                           json=body, timeout=120.0)
            r.raise_for_status()
            res = r.json()
        except httpx.HTTPStatusError as exc:
            st.error(f"API error {exc.response.status_code}: "
                     f"{exc.response.text}")
            res = None
        except Exception as exc:
            st.error(f"API call failed: {exc}")
            res = None

        if res is not None:
            head_cols = st.columns(4)
            head_cols[0].metric("Windows", res["n_windows"])
            head_cols[1].metric("Suppressed",
                                 f"{res['n_suppressed']} / {res['n_windows']}")
            head_cols[2].metric("Duration", f"{res['duration_s']:.1f} s")
            head_cols[3].metric("Packet rate",
                                 f"{res['packet_rate_hz']:.1f} Hz")

            # Suppression breakdown by reason (Class II safety telemetry)
            by_reason = res.get("n_suppressed_by_reason") or {}
            if by_reason:
                bd_cols = st.columns(len(by_reason))
                reason_labels = {
                    "wide_interval": "Wide CI",
                    "multi_subject": "Multi-subject",
                    "ood": "Out of distribution",
                    "low_quality": "Low quality",
                }
                for i, (reason, count) in enumerate(sorted(by_reason.items())):
                    bd_cols[i].metric(
                        reason_labels.get(reason, reason),
                        count,
                        delta=None,
                    )

            if res.get("audit_log_path"):
                st.caption(f"Audit log: `{res['audit_log_path']}`")

            if res.get("calibration_applied"):
                st.success(f"Calibration: {res['calibration_applied']}")

            sm = res.get("subject_match")
            if sm:
                if sm["matched"]:
                    st.info(
                        f"Subject matched: **{sm['subject_id']}** "
                        f"({sm['posture']}) — confidence "
                        f"{sm['confidence']:.3f}"
                    )
                elif sm["multi_subject_suspected"]:
                    st.warning(f"Multi-subject suspected: {sm['notes']}")
                else:
                    st.warning(f"No match: {sm['notes']}")
                with st.expander("Top fingerprint candidates"):
                    st.dataframe(pd.DataFrame(
                        sm["top_candidates"],
                        columns=["subject", "cosine_similarity"],
                    ))

            windows = res["windows"]
            if not windows:
                st.warning("No windows scored. Check capture length / "
                           "packet rate.")
            else:
                df = pd.DataFrame(windows)

                if hr_file is not None:
                    hr_df = _parse_hr_log_bytes(hr_file.read())
                    if hr_df is None:
                        st.error("hr_log.csv missing 'timestamp_unix' or "
                                 "'hr_bpm' columns")
                    else:
                        hr_unix = hr_df["timestamp_unix"].to_numpy()
                        hr_bpm = hr_df["hr_bpm"].to_numpy(dtype=float)
                        # Map window_start_s onto the H10 timeline by aligning
                        # both to their first timestamp.
                        t_centers = (df["window_start_s"].to_numpy()
                                      + window_s / 2.0)
                        t_centers_unix = hr_unix[0] + t_centers
                        df["hr_true"] = np.interp(
                            t_centers_unix, hr_unix, hr_bpm,
                        )
                        df["error_bpm"] = df["hr_pred"] - df["hr_true"]

                        accepted = df[~df["suppressed"]] if emit_intervals \
                            else df
                        if len(accepted) > 0:
                            mae = float(np.mean(
                                np.abs(accepted["error_bpm"]))
                            )
                            within5 = float(np.mean(
                                np.abs(accepted["error_bpm"]) <= 5.0
                            ))
                            head_cols[3].metric("MAE",
                                                 f"{mae:.2f} bpm")
                            st.caption(
                                f"within ±5 bpm: {within5*100:.1f}% "
                                f"({len(accepted)} accepted windows)"
                            )

                # Timeline plot
                st.subheader("HR timeline")
                plot_df = df.set_index("window_start_s").copy()
                cols_to_plot = ["hr_pred"]
                if "hr_true" in plot_df.columns:
                    cols_to_plot.append("hr_true")
                if emit_intervals and "hr_low" in plot_df.columns:
                    cols_to_plot.extend(["hr_low", "hr_high"])
                st.line_chart(plot_df[cols_to_plot])

                if emit_intervals and "interval_width" in plot_df.columns:
                    st.subheader("Interval width per window")
                    st.bar_chart(plot_df["interval_width"])
                    st.caption(
                        f"Windows with interval > {max_interval:.0f} bpm "
                        f"are suppressed in MAE."
                    )

                # Safety telemetry: rolling fingerprint similarity vs time
                if "fingerprint_similarity" in df.columns and \
                        df["fingerprint_similarity"].notna().any():
                    st.subheader("Multi-subject detector — fingerprint similarity")
                    sim_df = df[["window_start_s", "fingerprint_similarity"]].dropna()
                    sim_df = sim_df.set_index("window_start_s")
                    st.line_chart(sim_df)
                    st.caption(
                        "Cosine similarity of each window's RF fingerprint "
                        "to the calibrated baseline. Drops below 0.55 imply "
                        "a second subject in the field of view; sustained "
                        "low values suppress predictions."
                    )

                # Safety telemetry: Mahalanobis distance vs time
                if "mahalanobis" in df.columns and df["mahalanobis"].notna().any():
                    st.subheader("Out-of-distribution detector — Mahalanobis")
                    m_df = df[["window_start_s", "mahalanobis"]].dropna()
                    m_df = m_df.set_index("window_start_s")
                    st.line_chart(m_df)
                    st.caption(
                        "Squared Mahalanobis distance from training "
                        "distribution. Windows above the chi-square 99% "
                        "threshold are flagged as OOD and suppressed."
                    )

                with st.expander("Per-window table"):
                    st.dataframe(df)
                with st.expander("Raw API response"):
                    st.json({k: v for k, v in res.items() if k != "windows"})
    elif cap_file is None:
        st.info("Upload an ESP32-S3 capture.txt and (optionally) a Polar "
                "H10 hr_log.csv, then press **Score capture**.")
