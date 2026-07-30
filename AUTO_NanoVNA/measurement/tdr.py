from __future__ import annotations

from typing import Any

import numpy as np

C0 = 299_792_458.0


def _empty_result() -> dict[str, Any]:
    empty = np.array([], dtype=float)
    return {
        "time_s": empty.copy(),
        "distance_m": empty.copy(),
        "impulse": empty.copy(),
        "rho": empty.copy(),
        "z_ohm": np.array([], dtype=complex),
        "resolution_m": float("nan"),
        "resolution_s": float("nan"),
        "max_unambiguous_distance_m": float("nan"),
        "max_unambiguous_time_s": float("nan"),
        "bandwidth_hz": float("nan"),
        "frequency_step_hz": float("nan"),
        "warning": "",
    }


def _lowpass_window(size: int, name: str) -> np.ndarray:
    if size <= 1 or name == "rectangular":
        return np.ones(max(1, size), dtype=float)
    if name == "kaiser":
        full = np.kaiser(2 * size - 1, 8.6)
        out = full[size - 1 :]
    else:        
        k = np.arange(size, dtype=float)
        out = 0.5 * (1.0 + np.cos(np.pi * k / max(1, size - 1)))
    if out[0] != 0:
        out = out / out[0]
    return out


def calculate_tdr(
    frequency_hz: np.ndarray,
    s11: np.ndarray,
    velocity_factor: float = 0.66,
    window: str = "hann",
    z0: float = 50.0,
    zero_offset_m: float = 0.0,
    oversampling: int = 8,
) -> dict[str, Any]:
    result = _empty_result()
    if len(frequency_hz) < 4 or len(s11) < 4:
        return result

    vf = float(velocity_factor)
    if not np.isfinite(vf) or vf <= 0.0 or vf > 1.0:
        raise ValueError("VF musi być większy od 0 i nie większy od 1.")
    if not np.isfinite(z0) or z0 <= 0:
        raise ValueError("Z0 musi być dodatnie.")

    n = min(len(frequency_hz), len(s11))
    f = np.asarray(frequency_hz[:n], dtype=float)
    gamma = np.asarray(s11[:n], dtype=complex)
    valid = np.isfinite(f) & np.isfinite(gamma.real) & np.isfinite(gamma.imag) & (f >= 0)
    f, gamma = f[valid], gamma[valid]
    if len(f) < 4:
        return result

    order = np.argsort(f)
    f, gamma = f[order], gamma[order]
    f, unique_idx = np.unique(f, return_index=True)
    gamma = gamma[unique_idx]
    if len(f) < 4 or f[-1] <= f[0]:
        return result

    diffs = np.diff(f)
    positive_diffs = diffs[diffs > 0]
    if len(positive_diffs) == 0:
        return result
    df = float(np.median(positive_diffs))
    fmax = float(f[-1])
    bandwidth = float(f[-1] - f[0])

                                                                           
                                                                              
    positive_count = max(4, int(round(fmax / df)) + 1)
    lowpass_f = np.arange(positive_count, dtype=float) * df
    if lowpass_f[-1] < fmax * 0.995:
        positive_count += 1
        lowpass_f = np.arange(positive_count, dtype=float) * df
    lowpass_gamma = (
        np.interp(lowpass_f, f, gamma.real, left=gamma.real[0], right=gamma.real[-1])
        + 1j * np.interp(lowpass_f, f, gamma.imag, left=gamma.imag[0], right=gamma.imag[-1])
    )

    taper = _lowpass_window(len(lowpass_gamma), window)
    spectrum = lowpass_gamma * taper

    minimum_time_points = 2 * (len(spectrum) - 1)
    target = max(1024, max(2, int(oversampling)) * minimum_time_points)
    nfft = int(2 ** np.ceil(np.log2(target)))

    full_impulse = np.fft.irfft(spectrum, n=nfft)
    time_s = np.arange(nfft, dtype=float) / (nfft * df)

                                                                             
                                                                        
                                                                   
    usable = nfft // 2
    negative_time_area = float(np.sum(full_impulse[usable:]))
    impulse = full_impulse[:usable]
    rho_step = negative_time_area + np.cumsum(impulse)
    time_s = time_s[:usable]
    distance_m = time_s * C0 * vf / 2.0 - float(zero_offset_m)

    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        z = float(z0) * (1.0 + rho_step) / (1.0 - rho_step)

    resolution_s = 1.0 / max(2.0 * bandwidth, 1e-30)
    resolution_m = C0 * vf / max(2.0 * bandwidth, 1e-30)
    max_time = 1.0 / max(2.0 * df, 1e-30)
    max_distance = C0 * vf / max(4.0 * df, 1e-30)
    warning = ""
    if f[0] > 0.02 * f[-1]:
        warning = (
            "Pomiar nie zaczyna się blisko DC; początek odpowiedzi TDR jest "
            "ekstrapolowany i może mieć większy błąd."
        )

    return {
        "time_s": time_s,
        "distance_m": distance_m,
        "impulse": impulse,
        "rho": rho_step,
        "z_ohm": z.astype(complex),
        "resolution_m": float(resolution_m),
        "resolution_s": float(resolution_s),
        "max_unambiguous_distance_m": float(max_distance),
        "max_unambiguous_time_s": float(max_time),
        "bandwidth_hz": float(bandwidth),
        "frequency_step_hz": float(df),
        "warning": warning,
    }
