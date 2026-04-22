# Patient Monitoring ML Platform
## Goal: Contactless vitals (HR/RR) from WiFi CSI / SYNTHETIC data → 92% accuracy

MILESTONES:
M1: Environment + SYNTHETIC data generator (CSI-like IQ samples, HR 60-100, RR 12-30)
M2: Preprocessing pipeline (FFT filter 0.5-3Hz)
M3: LSTM/XGBoost model training  
M4: FastAPI prediction service
M5: Docker + Streamlit dashboard
M6: Deploy script (EC2/Ubuntu)

Tech: PyTorch, NumPy, Docker, FastAPI
Data: 100% SYNTHETIC vitals dataset (no hardware)
Accuracy target: 90%+ on test set

Start with: "COMPLETE_MILESTONE_1"
