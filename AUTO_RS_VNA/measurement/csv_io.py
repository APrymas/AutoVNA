from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from measurement.calculations import (
    complex_to_db,
    gamma_to_impedance,
    phase_deg,
    vswr_from_s11,
)
from measurement.touchstone_io import load_touchstone, save_touchstone_s1p, save_touchstone_s2p


TRACE_NUMBERS = {"s11": 1, "s21": 2, "s12": 3, "s22": 4}


def _field(data_frame: pd.DataFrame, fragments: tuple[str, ...]) -> str | None:
    for column in data_frame.columns:
        normalized = str(column).lower().replace(" ", "")
        if all(fragment.lower().replace(" ", "") in normalized for fragment in fragments):
            return str(column)
    return None


def load_rs_lab_csv(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header_index, separator = None, ";"
    for index, line in enumerate(lines):
        normalized = line.lower()
        if "freq" in normalized and any(name in normalized for name in ("s11", "s21", "s12", "s22")):
            header_index = index
            separator = ";" if line.count(";") >= line.count(",") else ","
            break
    if header_index is None:
        raise ValueError("Nagłówek CSV z parametrami S nie został znaleziony.")
    data_frame = pd.read_csv(path, sep=separator, skiprows=header_index)
    data_frame = data_frame.loc[:, ~data_frame.columns.astype(str).str.match(r"^Unnamed")]
    frequency_column = _field(data_frame, ("freq",))
    if not frequency_column:
        raise ValueError("Brak kolumny częstotliwości.")
    result: dict[str, Any] = {
        "path": path,
        "frequency_hz": pd.to_numeric(data_frame[frequency_column], errors="coerce").to_numpy(float),
    }
    for name, trace in TRACE_NUMBERS.items():
        db_column = _field(data_frame, ("db", f"trc{trace}", name)) or _field(data_frame, ("db", name))
        phase_column = _field(data_frame, ("ang", f"trc{trace}", name)) or _field(data_frame, ("ang", name))
        real_column = _field(data_frame, ("re", name))
        imag_column = _field(data_frame, ("im", name))
        if db_column and phase_column:
            db = pd.to_numeric(data_frame[db_column], errors="coerce").to_numpy(float)
            phase = pd.to_numeric(data_frame[phase_column], errors="coerce").to_numpy(float)
            result[name] = 10.0 ** (db / 20.0) * np.exp(1j * np.deg2rad(phase))
        elif real_column and imag_column:
            real = pd.to_numeric(data_frame[real_column], errors="coerce").to_numpy(float)
            imag = pd.to_numeric(data_frame[imag_column], errors="coerce").to_numpy(float)
            result[name] = real + 1j * imag
    return result


def load_measurement_file(path: Path) -> dict[str, Any]:
    return load_touchstone(path) if path.suffix.lower() in {".s1p", ".s2p"} else load_rs_lab_csv(path)


def save_tdr_csv(path: Path, tdr_data: dict[str, Any]) -> Path:
    tdr_path = path.with_name(path.stem + "_TDR.csv")
    pd.DataFrame({
        "time_s": tdr_data["time_s"],
        "distance_m": tdr_data["distance_m"],
        "rho_real": np.real(tdr_data["rho"]),
        "rho_imag": np.imag(tdr_data["rho"]),
        "z_real_ohm": np.real(tdr_data["z_ohm"]),
        "z_imag_ohm": np.imag(tdr_data["z_ohm"]),
    }).to_csv(tdr_path, sep=";", index=False)
    return tdr_path


def save_rs_lab_csv(
    path: Path,
    frequency_hz: np.ndarray,
    s11: np.ndarray | None,
    s21: np.ndarray | None,
    s12: np.ndarray | None,
    s22: np.ndarray | None,
    mem_s11: np.ndarray | None,
    mem_s21: np.ndarray | None,
    mem_s12: np.ndarray | None,
    mem_s22: np.ndarray | None,
    markers: dict[str, int | None],
    fields: dict[str, bool],
    z0: float,
    tdr_data: dict[str, Any] | None = None,
) -> list[Path]:
    path = path.with_suffix(".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    frequency = np.asarray(frequency_hz, dtype=float)
    count = len(frequency)
    headers = ["freq[Hz]"]
    columns: list[np.ndarray] = [frequency]

    def add(header: str, values: np.ndarray) -> None:
        headers.append(header)
        columns.append(np.asarray(values))

    traces = {"s11": s11, "s21": s21, "s12": s12, "s22": s22}
    memories = {"s11": mem_s11, "s21": mem_s21, "s12": mem_s12, "s22": mem_s22}
    for name, values in traces.items():
        if values is None:
            continue
        values = np.asarray(values, complex)[:count]
        trace_no = TRACE_NUMBERS[name]
        upper = name.upper()
        if fields.get(f"{name}_db_phase", True):
            add(f"db:Trc{trace_no}_{upper}", complex_to_db(values))
            add(f"ang:Trc{trace_no}_{upper}", phase_deg(values))
        if fields.get("memory", True):
            memory = memories[name]
            if memory is None:
                add(f"db:Mem[Trc{trace_no}]_{upper}", np.zeros(count))
                add(f"ang:Mem[Trc{trace_no}]_{upper}", np.zeros(count))
            else:
                source_memory = np.asarray(memory, complex)
                padded_memory = np.zeros(count, dtype=complex)
                memory_count = min(count, len(source_memory))
                padded_memory[:memory_count] = source_memory[:memory_count]
                add(f"db:Mem[Trc{trace_no}]_{upper}", complex_to_db(padded_memory))
                add(f"ang:Mem[Trc{trace_no}]_{upper}", phase_deg(padded_memory))
        if fields.get(f"{name}_complex", False):
            add(f"re:{upper}", values.real)
            add(f"im:{upper}", values.imag)

    if s11 is not None and fields.get("z_s11", True):
        impedance = gamma_to_impedance(np.asarray(s11, complex)[:count], z0)
        add("re:Z_S11", impedance.real)
        add("im:Z_S11", impedance.imag)
    if s11 is not None and fields.get("vswr", True):
        add("VSWR:S11", vswr_from_s11(np.asarray(s11, complex)[:count], clip=None))
    if fields.get("markers", True):
        for marker_name in ("M1", "M2"):
            values = np.zeros(count)
            marker_index = markers.get(marker_name)
            if marker_index is not None and 0 <= int(marker_index) < count:
                values[int(marker_index)] = 1.0
            add(f"marker:{marker_name}", values)

    usable_count = min([count, *(len(column) for column in columns)]) if columns else 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write("# R&S LAB CSV 1.00\n#\n")
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(headers + [""])
        for row_index in range(usable_count):
            row = [f"{float(np.real(column[row_index])):.15E}" for column in columns]
            writer.writerow(row + [""])

    written = [path]
    if fields.get("tdr", False) and tdr_data and len(tdr_data.get("time_s", [])):
        written.append(save_tdr_csv(path, tdr_data))
    return written


def save_measurement_files(
    base_path: Path,
    formats: dict[str, bool],
    frequency_hz: np.ndarray,
    s11: np.ndarray | None,
    s21: np.ndarray | None,
    s12: np.ndarray | None,
    s22: np.ndarray | None,
    mem_s11: np.ndarray | None,
    mem_s21: np.ndarray | None,
    mem_s12: np.ndarray | None,
    mem_s22: np.ndarray | None,
    markers: dict[str, int | None],
    fields: dict[str, bool],
    z0: float,
    tdr_data: dict[str, Any] | None = None,
) -> list[Path]:
    base_path = base_path.with_suffix("")
    written: list[Path] = []
    if formats.get("csv", True):
        written.extend(save_rs_lab_csv(
            base_path.with_suffix(".csv"), frequency_hz,
            s11, s21, s12, s22,
            mem_s11, mem_s21, mem_s12, mem_s22,
            markers, fields, z0, tdr_data,
        ))
    elif fields.get("tdr", False) and tdr_data and len(tdr_data.get("time_s", [])):
        written.append(save_tdr_csv(base_path.with_suffix(".csv"), tdr_data))
    if formats.get("s1p", False):
        if s11 is None:
            raise ValueError("Eksport S1P wymaga danych S11.")
        written.append(save_touchstone_s1p(base_path.with_suffix(".s1p"), frequency_hz, s11, z0))
    if formats.get("s2p", False):
        if any(value is None for value in (s11, s21, s12, s22)):
            raise ValueError("Eksport S2P wymaga zmierzonych danych S11, S21, S12 i S22.")
        written.append(save_touchstone_s2p(
            base_path.with_suffix(".s2p"), frequency_hz,
            np.asarray(s11), np.asarray(s21), np.asarray(s12), np.asarray(s22), z0,
        ))
    return written
