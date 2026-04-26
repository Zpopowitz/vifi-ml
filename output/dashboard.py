"""ViFi dashboard with single/multi-person toggle and ICA unmixing demo.

Run:
    streamlit run output/dashboard.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import numpy as np
import streamlit as st
from sklearn.decomposition import FastICA

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_gen import generate_sample  # noqa: E402

API_URL = os.environ.get("VIFI_API", "http://localhost:8000")

st.set_page_config(page_title="ViFi", layout="wide")
st.title("ViFi - Contactless HR / RR from WiFi CSI")
st.caption(f"API: `{API_URL}`")

tab_live, tab_multi, tab_ica = st.tabs(
    ["Single subject", "Multi-person (1 vs 2)", "ICA unmixing"]
)


def call_predict(iq: np.ndarray, fs: float) -> dict | None:
    payload = {
        "fs": fs,
        "iq_real": iq.real.astype(float).tolist(),
        "iq_imag": iq.imag.astype(float).tolist(),
    }
    try:
        r = httpx.post(f"{API_URL}/predict", json=payload, timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"API call failed: {exc}")
        return None


# -------------------------------------------------------------------- tab 1
with tab_live:
    with st.sidebar:
        st.header("Single subject")
        duration = st.slider("Window (s)", 5.0, 30.0, 10.0, step=1.0, key="s_dur")
        fs = st.slider("Sample rate (Hz)", 50.0, 200.0, 100.0, step=10.0, key="s_fs")
        snr = st.slider("SNR (dB)", 0.0, 40.0, 20.0, step=1.0, key="s_snr")
        hr_true = st.slider("True HR (bpm)", 60.0, 100.0, 75.0, key="s_hr")
        rr_true = st.slider("True RR (bpm)", 12.0, 30.0, 18.0, key="s_rr")
        seed = st.number_input("Seed", value=0, step=1, key="s_seed")
        go = st.button("Predict", type="primary", key="s_go")

    if go:
        iq, meta = generate_sample(
            duration_s=duration, fs=fs, hr_bpm=hr_true, rr_bpm=rr_true,
            snr_db=snr, seed=int(seed),
        )
        c1, c2 = st.columns([3, 2])
        with c1:
            st.subheader("|CSI| envelope")
            st.line_chart(np.abs(iq))
        pred = call_predict(iq, meta.fs)
        if pred:
            with c2:
                st.metric("HR (bpm)", f"{pred['hr_bpm']:.1f}",
                          delta=f"{pred['hr_bpm'] - hr_true:+.1f}")
                st.metric("RR (bpm)", f"{pred['rr_bpm']:.1f}",
                          delta=f"{pred['rr_bpm'] - rr_true:+.1f}")
                st.progress(pred["hr_confidence"],
                            text=f"HR conf {pred['hr_confidence']:.2f}")
                st.progress(pred["rr_confidence"],
                            text=f"RR conf {pred['rr_confidence']:.2f}")
                st.json(pred)


# -------------------------------------------------------------------- tab 2
with tab_multi:
    st.subheader("Single subject -> two subjects")
    st.caption(
        "CSI captured by one antenna sees a linear mix of everyone in the "
        "room. Flipping the toggle mixes a second subject into the same "
        "envelope - watch the dominant peak pull toward the average and the "
        "confidence drop."
    )

    n_people = st.radio("Subjects in room", [1, 2], horizontal=True)
    col_l, col_r = st.columns(2)
    with col_l:
        hr1 = st.slider("Subject 1 HR", 60.0, 100.0, 72.0, key="m_hr1")
        rr1 = st.slider("Subject 1 RR", 12.0, 30.0, 16.0, key="m_rr1")
    with col_r:
        hr2 = st.slider("Subject 2 HR", 60.0, 100.0, 92.0, key="m_hr2",
                        disabled=n_people == 1)
        rr2 = st.slider("Subject 2 RR", 12.0, 30.0, 26.0, key="m_rr2",
                        disabled=n_people == 1)
    snr = st.slider("SNR (dB)", 0.0, 40.0, 18.0, step=1.0, key="m_snr")
    mix_ratio = st.slider("Subject 2 mix weight", 0.0, 1.0, 0.7, step=0.05,
                          key="m_mix", disabled=n_people == 1,
                          help="How much of subject 2's signal ends up in this antenna.")
    go = st.button("Simulate", type="primary", key="m_go")

    if go:
        iq1, _ = generate_sample(hr_bpm=hr1, rr_bpm=rr1, snr_db=snr, seed=1)
        if n_people == 2:
            iq2, _ = generate_sample(hr_bpm=hr2, rr_bpm=rr2, snr_db=snr, seed=2)
            mixed = iq1 + mix_ratio * iq2
            true_hr_display = f"{hr1:.0f} + {hr2:.0f}"
            true_rr_display = f"{rr1:.0f} + {rr2:.0f}"
        else:
            mixed = iq1
            true_hr_display = f"{hr1:.0f}"
            true_rr_display = f"{rr1:.0f}"

        c1, c2 = st.columns([3, 2])
        with c1:
            st.line_chart(np.abs(mixed))
        pred = call_predict(mixed, 100.0)
        if pred:
            with c2:
                st.metric("Predicted HR", f"{pred['hr_bpm']:.1f} bpm",
                          help=f"Ground truth: {true_hr_display}")
                st.metric("Predicted RR", f"{pred['rr_bpm']:.1f} bpm",
                          help=f"Ground truth: {true_rr_display}")
                st.progress(pred["rr_confidence"],
                            text=f"RR conf {pred['rr_confidence']:.2f}")
                if n_people == 2:
                    st.warning(
                        "Single-antenna CSI cannot separate two subjects. "
                        "Accuracy degrades toward a weighted mean. Fix: 2+ "
                        "antennas + ICA (next tab) or a 4x ESP32 array for AoA."
                    )


# -------------------------------------------------------------------- tab 3
with tab_ica:
    st.subheader("FastICA unmixing demo (2 antennas, 2 subjects)")
    st.caption(
        "Two antennas pick up different linear combinations of the two "
        "subjects' chest-motion envelopes. FastICA recovers the two source "
        "signals; we then predict on each recovered source independently."
    )

    hr_a = st.slider("Subject A HR", 60.0, 100.0, 68.0, key="i_hra")
    rr_a = st.slider("Subject A RR", 12.0, 30.0, 15.0, key="i_rra")
    hr_b = st.slider("Subject B HR", 60.0, 100.0, 95.0, key="i_hrb")
    rr_b = st.slider("Subject B RR", 12.0, 30.0, 26.0, key="i_rrb")
    snr = st.slider("SNR (dB)", 0.0, 40.0, 22.0, step=1.0, key="i_snr")
    go = st.button("Unmix + predict", type="primary", key="i_go")

    if go:
        rng = np.random.default_rng(7)
        iq_a, _ = generate_sample(hr_bpm=hr_a, rr_bpm=rr_a, snr_db=snr, seed=11)
        iq_b, _ = generate_sample(hr_bpm=hr_b, rr_bpm=rr_b, snr_db=snr, seed=22)
        sources = np.stack([np.abs(iq_a), np.abs(iq_b)], axis=1)  # (T, 2)
        # Random 2x2 mixing matrix (two antennas see different combos)
        mixing = rng.uniform(0.3, 1.0, size=(2, 2))
        observed = sources @ mixing.T  # (T, 2)

        ica = FastICA(n_components=2, random_state=0, whiten="unit-variance")
        recovered = ica.fit_transform(observed)  # (T, 2)

        c1, c2 = st.columns(2)
        with c1:
            st.caption("Antenna observations (mixed)")
            st.line_chart(observed, height=200)
        with c2:
            st.caption("FastICA-recovered sources")
            st.line_chart(recovered, height=200)

        # Predict on each recovered source (interpret as 1-D envelope -> make
        # complex by adding zero imag so we can reuse /predict).
        cols = st.columns(2)
        for i, col in enumerate(cols):
            env = recovered[:, i].astype(np.float32)
            # renormalise - ICA outputs are arbitrary scale
            env = env - env.mean()
            env = env / (np.std(env) + 1e-9)
            complex_env = env.astype(np.complex64)
            pred = call_predict(complex_env, 100.0)
            with col:
                st.caption(f"Recovered source #{i + 1}")
                if pred:
                    st.metric("HR", f"{pred['hr_bpm']:.1f} bpm")
                    st.metric("RR", f"{pred['rr_bpm']:.1f} bpm")
        st.info(
            "Real deployment: 4x ESP32 array + AoA + ICA enables per-bed "
            "vitals in a shared ward. Single device is still best-in-class "
            "for 1 patient per room."
        )


# -------------------------------------------------------------------- footer
try:
    h = httpx.get(f"{API_URL}/health", timeout=2.0).json()
    st.caption(
        f"Model: {h.get('model_version', '?')} | "
        f"HR tol ±{h.get('hr_tol_bpm', '?')} bpm | "
        f"RR tol ±{h.get('rr_tol_bpm', '?')} bpm"
    )
except Exception:
    st.caption("Model status: API unreachable")
