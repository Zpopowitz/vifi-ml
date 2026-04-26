"""M5: Streamlit dashboard for the ViFi API.

Run:   streamlit run dashboard.py
Talks to the FastAPI service at $VIFI_API (default http://localhost:8000).
"""
from __future__ import annotations

import os

import httpx
import numpy as np
import streamlit as st

from data_gen import generate_sample

API_URL = os.environ.get("VIFI_API", "http://localhost:8000")

st.set_page_config(page_title="ViFi", layout="wide")
st.title("ViFi: Contactless HR / RR from Synthetic CSI")

with st.sidebar:
    st.header("Signal controls")
    duration = st.slider("Window duration (s)", 5.0, 30.0, 10.0, step=1.0)
    fs = st.slider("Sample rate (Hz)", 50.0, 200.0, 100.0, step=10.0)
    snr = st.slider("SNR (dB)", 0.0, 40.0, 20.0, step=1.0)
    hr_bpm = st.slider("True HR (bpm)", 60.0, 100.0, 75.0, step=1.0)
    rr_bpm = st.slider("True RR (bpm)", 12.0, 30.0, 18.0, step=1.0)
    seed = st.number_input("Seed", value=0, step=1)
    go = st.button("Generate + Predict", type="primary")

st.caption(f"API endpoint: `{API_URL}`")

col_a, col_b = st.columns([3, 2])

if go:
    iq, meta = generate_sample(
        duration_s=duration, fs=fs, hr_bpm=hr_bpm, rr_bpm=rr_bpm,
        snr_db=snr, seed=int(seed),
    )
    with col_a:
        st.subheader("Synthetic CSI (|IQ|)")
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
            st.subheader("Prediction")
            st.metric("HR (bpm)", f"{pred['hr_bpm']:.1f}",
                      delta=f"{pred['hr_bpm'] - hr_bpm:+.1f}")
            st.metric("RR (bpm)", f"{pred['rr_bpm']:.1f}",
                      delta=f"{pred['rr_bpm'] - rr_bpm:+.1f}")
            st.progress(pred["hr_confidence"], text=f"HR confidence {pred['hr_confidence']:.2f}")
            st.progress(pred["rr_confidence"], text=f"RR confidence {pred['rr_confidence']:.2f}")
            st.json(pred)
else:
    st.info("Use the sidebar to generate a synthetic window and call the API.")

try:
    h = httpx.get(f"{API_URL}/health", timeout=2.0).json()
    st.caption(f"Model: {h.get('model_version', '?')} | "
               f"HR tol ±{h.get('hr_tol_bpm', '?')} bpm | "
               f"RR tol ±{h.get('rr_tol_bpm', '?')} bpm")
except Exception:
    st.caption("Model status: API unreachable")
