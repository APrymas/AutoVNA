from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import user_data_dir


@dataclass
class CalibrationProfile:
    frequency_hz: np.ndarray
    mobius_a: np.ndarray
    mobius_b: np.ndarray
    mobius_c: np.ndarray
    thru_s21: np.ndarray
    metadata: dict

    def apply(self, s11: Optional[np.ndarray], s21: Optional[np.ndarray]) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
        out11 = s11
        out21 = s21
        if s11 is not None:
            n = min(len(s11), len(self.mobius_a))
            m = s11[:n]
            den = self.mobius_a[:n] - m * self.mobius_c[:n]
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                corrected = (m - self.mobius_b[:n]) / den
            out11 = corrected
        if s21 is not None:
            n = min(len(s21), len(self.thru_s21))
            ref = self.thru_s21[:n]
            safe = np.where(np.abs(ref) < 1e-15, np.nan + 1j * np.nan, ref)
            with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
                out21 = s21[:n] / safe
        return out11, out21


def create_profile(
    frequency_hz: np.ndarray,
    open_s11: np.ndarray,
    short_s11: np.ndarray,
    load_s11: np.ndarray,
    thru_s21: np.ndarray,
    device_info: dict,
) -> CalibrationProfile:
    n = min(len(frequency_hz), len(open_s11), len(short_s11), len(load_s11), len(thru_s21))
    f = np.asarray(frequency_hz[:n], float)
    measured = np.vstack([open_s11[:n], short_s11[:n], load_s11[:n]])
    true = np.asarray([1.0 + 0j, -1.0 + 0j, 0.0 + 0j])
    a = np.empty(n, complex)
    b = np.empty(n, complex)
    c = np.empty(n, complex)
    for i in range(n):
        m = measured[:, i]
        matrix = np.column_stack([true, np.ones(3, complex), -m * true])
        try:
            solution = np.linalg.solve(matrix, m)
        except np.linalg.LinAlgError:
            solution, *_ = np.linalg.lstsq(matrix, m, rcond=None)
        a[i], b[i], c[i] = solution
    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "start_hz": float(f[0]),
        "stop_hz": float(f[-1]),
        "points": int(n),
        "method": "OSL Mobius + THRU normalization",
        "device": device_info,
    }
    return CalibrationProfile(f, a, b, c, np.asarray(thru_s21[:n], complex), metadata)


class CalibrationStore:
    def __init__(self) -> None:
        self.root = user_data_dir() / "calibrations"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key_for(frequency_hz: np.ndarray, device_id: str = "unknown") -> str:
        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (device_id or "unknown"))
        return f"{safe_id}_{int(round(float(frequency_hz[0])))}_{int(round(float(frequency_hz[-1])))}_{len(frequency_hz)}"

    def save(self, profile: CalibrationProfile, device_id: str = "unknown") -> Path:
        folder = self.root / self.key_for(profile.frequency_hz, device_id)
        folder.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            folder / "calibration.npz",
            frequency_hz=profile.frequency_hz,
            mobius_a=profile.mobius_a,
            mobius_b=profile.mobius_b,
            mobius_c=profile.mobius_c,
            thru_s21=profile.thru_s21,
        )
        (folder / "calibration.json").write_text(
            json.dumps(profile.metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return folder

    def load_exact(self, frequency_hz: np.ndarray, device_id: str = "unknown") -> Optional[CalibrationProfile]:
        if len(frequency_hz) < 2:
            return None
        folder = self.root / self.key_for(frequency_hz, device_id)
        npz_path = folder / "calibration.npz"
        meta_path = folder / "calibration.json"
        if not npz_path.exists():
            return None
        try:
            data = np.load(npz_path)
            saved_f = np.asarray(data["frequency_hz"], float)
            if len(saved_f) != len(frequency_hz) or not np.allclose(saved_f, frequency_hz, rtol=0, atol=0.5):
                return None
            metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            return CalibrationProfile(
                saved_f,
                np.asarray(data["mobius_a"], complex),
                np.asarray(data["mobius_b"], complex),
                np.asarray(data["mobius_c"], complex),
                np.asarray(data["thru_s21"], complex),
                metadata,
            )
        except Exception:
            return None
