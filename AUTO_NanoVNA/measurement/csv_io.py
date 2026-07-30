from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from measurement.calculations import complex_to_db, phase_deg, gamma_to_impedance, vswr_from_s11
from measurement.touchstone_io import load_touchstone, save_touchstone_s1p, save_touchstone_s2p


def _field(data_frame: pd.DataFrame, fragments: tuple[str, ...]) -> str | None:
    for column in data_frame.columns:
        normalized = str(column).lower().replace(" ", "")
        if all(fragment.lower().replace(" ", "") in normalized for fragment in fragments):
            return str(column)
    return None


def load_nanovna_csv(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header_index = None
    separator = ";"
    for index, line in enumerate(lines):
        normalized = line.lower()
        if "freq" in normalized and ("s11" in normalized or "s21" in normalized):
            header_index = index
            separator = ";" if line.count(";") >= line.count(",") else ","
            break
    if header_index is None:
        raise ValueError("NanoVNA CSV header was not found.")
    data_frame = pd.read_csv(path, sep=separator, skiprows=header_index)
    data_frame = data_frame.loc[:, ~data_frame.columns.astype(str).str.match(r"^Unnamed")]
    frequency_column = _field(data_frame, ("freq",))
    if not frequency_column:
        raise ValueError("Frequency column is missing.")
    frequency_hz = pd.to_numeric(data_frame[frequency_column], errors="coerce").to_numpy(float)
    result: dict[str, Any] = {"path": path, "frequency_hz": frequency_hz}
    for parameter_name, trace_name in (("s11", "trc1"), ("s21", "trc2")):
        db_column = _field(data_frame, ("db", trace_name, parameter_name))
        phase_column = _field(data_frame, ("ang", trace_name, parameter_name))
        if db_column and phase_column:
            db_values = pd.to_numeric(data_frame[db_column], errors="coerce").to_numpy(float)
            phase_values = pd.to_numeric(data_frame[phase_column], errors="coerce").to_numpy(float)
            result[parameter_name] = 10.0 ** (db_values / 20.0) * np.exp(1j * np.deg2rad(phase_values))
    return result


def load_measurement_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in {".s1p", ".s2p"}:
        return load_touchstone(path)
    return load_nanovna_csv(path)


def save_tdr_csv(path: Path, tdr_data: dict[str, Any]) -> Path:
    tdr_path = path.with_name(path.stem + "_TDR.csv")
    table = pd.DataFrame({
        "time_s": tdr_data["time_s"],
        "distance_m": tdr_data["distance_m"],
        "rho_real": np.real(tdr_data["rho"]),
        "rho_imag": np.imag(tdr_data["rho"]),
        "z_real_ohm": np.real(tdr_data["z_ohm"]),
        "z_imag_ohm": np.imag(tdr_data["z_ohm"]),
    })
    table.to_csv(tdr_path, sep=";", index=False)
    return tdr_path


def save_nanovna_csv(
    path: Path,
    frequency_hz: np.ndarray,
    s11: np.ndarray | None,
    s21: np.ndarray | None,
    mem_s11: np.ndarray | None,
    mem_s21: np.ndarray | None,
    markers: dict[str, int | None],
    fields: dict[str, bool],
    z0: float,
    tdr_data: dict[str, Any] | None = None,
) -> list[Path]:
    path = path.with_suffix(".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    count = len(frequency_hz)
    headers = ["freq[Hz]"]
    columns: list[np.ndarray] = [np.asarray(frequency_hz, dtype=float)]

    def add_column(header: str, values: np.ndarray) -> None:
        headers.append(header)
        columns.append(np.asarray(values))

    if s11 is not None and fields.get("s11_db_phase", True):
        add_column("db:Trc1_S11", complex_to_db(s11))
        add_column("ang:Trc1_S11", phase_deg(s11))
    if s11 is not None and fields.get("memory", True):
        memory = np.zeros(count, dtype=complex) if mem_s11 is None else np.resize(mem_s11, count)
        add_column("db:Mem2[Trc1]_S11", complex_to_db(memory) if mem_s11 is not None else np.zeros(count))
        add_column("ang:Mem2[Trc1]_S11", phase_deg(memory) if mem_s11 is not None else np.zeros(count))
    if s21 is not None and fields.get("s21_db_phase", True):
        add_column("db:Trc2_S21", complex_to_db(s21))
        add_column("ang:Trc2_S21", phase_deg(s21))
    if s21 is not None and fields.get("memory", True):
        memory = np.zeros(count, dtype=complex) if mem_s21 is None else np.resize(mem_s21, count)
        add_column("db:Mem2[Trc2]_S21", complex_to_db(memory) if mem_s21 is not None else np.zeros(count))
        add_column("ang:Mem2[Trc2]_S21", phase_deg(memory) if mem_s21 is not None else np.zeros(count))
    if s11 is not None and fields.get("s11_complex", False):
        add_column("re:S11", s11.real)
        add_column("im:S11", s11.imag)
    if s21 is not None and fields.get("s21_complex", False):
        add_column("re:S21", s21.real)
        add_column("im:S21", s21.imag)
    if s11 is not None and fields.get("z_s11", True):
        impedance = gamma_to_impedance(s11, z0)
        add_column("re:Z_S11", impedance.real)
        add_column("im:Z_S11", impedance.imag)
    if s11 is not None and fields.get("vswr", True):
        add_column("VSWR:S11", vswr_from_s11(s11, clip=None))
    if fields.get("markers", True):
        for marker_name in ("M1", "M2"):
            values = np.zeros(count)
            marker_index = markers.get(marker_name)
            if marker_index is not None and 0 <= int(marker_index) < count:
                values[int(marker_index)] = 1.0
            add_column(f"marker:{marker_name}", values)

    with path.open("w", newline="", encoding="utf-8") as handle:
        handle.write("# Version 1.00\n#\n")
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(headers + [""])
        for row_index in range(count):
            row = []
            for column in columns:
                value = column[row_index]
                if np.iscomplexobj(value):
                    value = value.real
                row.append(f"{float(value):.15E}")
            writer.writerow(row + [""])

    written_paths = [path]
    if fields.get("tdr", False) and tdr_data and len(tdr_data.get("time_s", [])):
        written_paths.append(save_tdr_csv(path, tdr_data))
    return written_paths


def save_measurement_files(
    base_path: Path,
    formats: dict[str, bool],
    frequency_hz: np.ndarray,
    s11: np.ndarray | None,
    s21: np.ndarray | None,
    mem_s11: np.ndarray | None,
    mem_s21: np.ndarray | None,
    markers: dict[str, int | None],
    fields: dict[str, bool],
    z0: float,
    tdr_data: dict[str, Any] | None = None,
) -> list[Path]:
    base_path = base_path.with_suffix("")
    written_paths: list[Path] = []
    if formats.get("csv", True):
        written_paths.extend(save_nanovna_csv(
            base_path.with_suffix(".csv"), frequency_hz, s11, s21, mem_s11, mem_s21,
            markers, fields, z0, tdr_data,
        ))
    elif fields.get("tdr", False) and tdr_data and len(tdr_data.get("time_s", [])):
        written_paths.append(save_tdr_csv(base_path.with_suffix(".csv"), tdr_data))
    if formats.get("s1p", False):
        if s11 is None:
            raise ValueError("S1P export requires S11 data.")
        written_paths.append(save_touchstone_s1p(base_path.with_suffix(".s1p"), frequency_hz, s11, z0))
    if formats.get("s2p", False):
        if s11 is None or s21 is None:
            raise ValueError("S2P export requires S11 and S21 data.")
        written_paths.append(save_touchstone_s2p(base_path.with_suffix(".s2p"), frequency_hz, s11, s21, z0))
    return written_paths
