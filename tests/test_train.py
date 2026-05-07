"""Tests for M3 training pipeline. Requires >= 0.92 combined validation accuracy."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from train import HR_TOL_BPM, RR_TOL_BPM, train


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    model_dir = tmp_path_factory.mktemp("models")
    report = train(n_samples=3000, val_frac=0.2, seed=42, model_dir=model_dir)
    return report, model_dir


def test_model_files_exist(trained):
    _, model_dir = trained
    assert (model_dir / "hr_model.json").exists()
    assert (model_dir / "rr_model.json").exists()
    assert (model_dir / "metadata.json").exists()


def test_combined_accuracy_meets_target(trained):
    report, _ = trained
    # Headline: 92% combined (HR within 5 bpm AND RR within 2 bpm)
    assert report.combined_acc >= 0.92, (
        f"combined_acc={report.combined_acc:.3f} HR_acc={report.hr_acc:.3f} "
        f"RR_acc={report.rr_acc:.3f} HR_mae={report.hr_mae:.2f} RR_mae={report.rr_mae:.2f}"
    )


def test_per_target_accuracy(trained):
    report, _ = trained
    assert report.hr_acc >= 0.92
    assert report.rr_acc >= 0.95
    # Sanity: errors bounded
    assert report.hr_mae < HR_TOL_BPM
    assert report.rr_mae < RR_TOL_BPM


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
