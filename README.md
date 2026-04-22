# vitalscan-ml

Contactless heart-rate (HR) and respiratory-rate (RR) estimation from
synthetic WiFi-CSI-like IQ samples. Pure-Python pipeline, XGBoost models,
FastAPI service, Streamlit dashboard, Dockerized deploy.

- **Dataset:** 100% synthetic (no hardware)
- **Targets:** HR 60–100 bpm, RR 12–30 bpm
- **Accuracy:** ≥ 92% combined on held-out validation (HR ±5 bpm, RR ±2 bpm)

## Milestones

| # | File | Purpose |
|---|------|---------|
| M1 | `data_gen.py` / `test_data_gen.py` | Synthetic CSI IQ generator (1000+ samples). |
| M2 | `preprocess.py` / `test_preprocess.py` | Detrend + 0.1–3 Hz bandpass + zero-padded FFT features. |
| M3 | `train.py` / `test_train.py` | XGBoost regressors for HR/RR, reports MAE + within-tolerance accuracy. |
| M4 | `api.py` / `test_api.py` | FastAPI `/predict`, `/predict/demo`, `/health`. |
| M5 | `Dockerfile` + `dashboard.py` / `test_build.py` | Container image + Streamlit dashboard. |
| M6 | `deploy.sh` / `test_deploy.sh` | One-shot deploy for EC2/Ubuntu. |

## Quickstart (local)

```bash
pip install -r requirements.txt
python train.py -n 3000          # trains + saves to ./models/
uvicorn api:app --port 8000      # serve
streamlit run dashboard.py       # optional UI on :8501
```

## Quickstart (Docker)

```bash
docker build -t vitalscan .
docker run -p 8000:8000 vitalscan
curl -X POST http://localhost:8000/predict/demo \
     -H 'content-type: application/json' \
     -d '{"hr_bpm":75,"rr_bpm":18,"seed":0}'
```

## Deploy (EC2 / Ubuntu)

```bash
./deploy.sh         # build + run + wait for /health
./deploy.sh logs    # tail logs
./deploy.sh down    # stop + remove
```

## Run all tests

```bash
pytest -v                     # 21 Python tests (M1-M5)
./test_deploy.sh              # M6 static + optional live-docker checks
```

## API

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET  | `/health` | – | model metadata |
| POST | `/predict` | `{fs, iq_real[], iq_imag[]}` | `{hr_bpm, rr_bpm, hr_confidence, rr_confidence, ...}` |
| POST | `/predict/demo` | `{hr_bpm?, rr_bpm?, snr_db?, seed?}` | same as `/predict` |
