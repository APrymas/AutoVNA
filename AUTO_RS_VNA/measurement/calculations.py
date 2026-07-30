from __future__ import annotations

import numpy as np


def complex_to_db(data: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(data), 1e-15))


def phase_deg(data: np.ndarray, unwrap: bool = False) -> np.ndarray:
    angle = np.angle(data)
    if unwrap:
        angle = np.unwrap(angle)
    return np.rad2deg(angle)


def gamma_to_impedance(gamma: np.ndarray | complex, z0: float = 50.0):
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return z0 * (1.0 + gamma) / (1.0 - gamma)



def vswr_from_s11(s11: np.ndarray, clip: float | None = 100.0) -> np.ndarray:
    mag = np.abs(s11)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = (1.0 + mag) / np.maximum(1.0 - mag, 1e-15)
    value = np.where(mag >= 1.0, np.inf, value)
    if clip is not None:
        value = np.minimum(value, clip)
    return value


def nearest_index(freq: np.ndarray, frequency_hz: float) -> int:
    if len(freq) == 0:
        return 0
    return int(np.argmin(np.abs(freq - frequency_hz)))
