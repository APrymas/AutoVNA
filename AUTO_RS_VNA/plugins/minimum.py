from __future__ import annotations

import argparse
import sys
import time
import warnings
import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass


                                                                               
INPUT_CSV: Optional[str] = None                                 
OUTPUT_DIR: Optional[str] = None                                      
                                                                               
@dataclass
class Measurement:
    frequency_hz: np.ndarray
    s11: np.ndarray
    s21: np.ndarray
    clean_table: pd.DataFrame                                                                              
                                  
                                                                              
def read_vna_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path.resolve()}")

    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    header_index: Optional[int] = None
    separator = ";"
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if "freq" in low and ("s11" in low or "hz" in low):
            header_index = i
            separator = ";" if line.count(";") >= line.count(",") else ","
            break
    if header_index is None:
        raise ValueError("Header not found defining frequency and S11.")

    df = pd.read_csv(path, sep=separator, skiprows=header_index)
    df = df.loc[:, ~df.columns.astype(str).str.match(r"^Unnamed")]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def find_column(df: pd.DataFrame, fragments: tuple[str, ...]) -> str:
    for col in df.columns:
        low = str(col).lower().replace(" ", "")
        if all(fragment.lower().replace(" ", "") in low for fragment in fragments):
            return str(col)
    raise KeyError(
        f"No column containing {fragments}. Available columns: {list(df.columns)}"
    )


def find_column_any(df: pd.DataFrame, variants: tuple[tuple[str, ...], ...]) -> str:
    for fragments in variants:
        try:
            return find_column(df, fragments)
        except KeyError:
            pass
    raise KeyError(
        f"Required column not found. Alternatives checked {variants}. "
        f"Available columns: {list(df.columns)}"
    )


def load_measurement(path: Path) -> Measurement:
    df = read_vna_csv(path)
    freq_col = find_column(df, ("freq",))
    s11_db_col = find_column_any(df, (("db", "trc1", "s11"), ("db", "s11")))
    s11_ang_col = find_column_any(df, (("ang", "trc1", "s11"), ("phase", "s11"), ("ang", "s11")))
    s21_db_col = find_column_any(df, (("db", "trc2", "s21"), ("db", "s21")))
    s21_ang_col = find_column_any(df, (("ang", "trc2", "s21"), ("phase", "s21"), ("ang", "s21")))

    clean = pd.DataFrame({
        "frequency_hz": pd.to_numeric(df[freq_col], errors="coerce"),
        "s11_db": pd.to_numeric(df[s11_db_col], errors="coerce"),
        "s11_phase_deg": pd.to_numeric(df[s11_ang_col], errors="coerce"),
        "s21_db": pd.to_numeric(df[s21_db_col], errors="coerce"),
        "s21_phase_deg": pd.to_numeric(df[s21_ang_col], errors="coerce"),
    }).dropna()
    clean = clean.sort_values("frequency_hz").drop_duplicates("frequency_hz")

    f = clean["frequency_hz"].to_numpy(float)
    s11 = 10.0 ** (clean["s11_db"].to_numpy(float) / 20.0) * np.exp(
        1j * np.deg2rad(clean["s11_phase_deg"].to_numpy(float))
    )
    s21 = 10.0 ** (clean["s21_db"].to_numpy(float) / 20.0) * np.exp(
        1j * np.deg2rad(clean["s21_phase_deg"].to_numpy(float))
    )

    return Measurement(f, s11, s21, clean.loc[:].reset_index(drop=True))

                                                                             
def complex_to_db(x: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-15))


def phase_deg(x: np.ndarray) -> np.ndarray:
    return np.rad2deg(np.unwrap(np.angle(x)))

                                                                              
def choose_csv_interactively(initial: Optional[str] = None) -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        initial_dir = str(Path(initial).expanduser().resolve().parent) if initial else str(Path.cwd())
        selected = filedialog.askopenfilename(
            title="Choose CSV file",
            initialdir=initial_dir,
            filetypes=[("CSV", "*.csv"), ("all files", "*.*")],
        )
        root.destroy()
        return Path(selected) if selected else None
    except Exception as exc:
        print(f"Can not launch window ({exc}).")
        text = input("Input CSV file path or Enter, to cancel: ").strip().strip('"')
        return Path(text) if text else None



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finding minimum value of s11 and s21 for the current sweep"
    )
    parser.add_argument(
        "--input", default=INPUT_CSV,
        help="CSV file, when empty, prompted for a file name",
    )
    parser.add_argument(
        "--output", default=OUTPUT_DIR,
        help="Output directory for results",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.input:
        input_path = Path(args.input).expanduser()
        if not input_path.exists():
            print(f"File not found: {input_path}. Please choose interactively.")
            chosen = choose_csv_interactively(str(input_path))
            if chosen is None:
                print("No file chosen.")
                return 2
            input_path = chosen
    else:
        chosen = choose_csv_interactively()
        if chosen is None:
            print("No file chosen.")
            return 2
        input_path = chosen

    if args.output:
        output_dir = Path(args.output).expanduser()
    else:
        output_dir = input_path.parent / f"{input_path.stem}_results"
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        measurement = load_measurement(input_path)
        print(
            f"Read {len(measurement.frequency_hz)} points: "
            f"{measurement.frequency_hz[0]:.6g} ... "
            f"{measurement.frequency_hz[-1]:.6g} Hz"
        )
        #====================================================
        # The actual processing of data takes place here
        minimum_value = 1e100
        minimum_idx = 0
        for idx,s11_value in enumerate(measurement.s11):
            if np.abs(s11_value) < minimum_value:
                minimum_value = np.abs(s11_value)
                minimum_idx = idx
        
        print(f"Minimum for s11 f={measurement.frequency_hz[minimum_idx]} Hz,  ({complex_to_db(measurement.s11[minimum_idx])} dB)")
        
        minimum_value = 1e100
        minimum_idx = 0
        for idx,s21_value in enumerate(measurement.s21):
            if np.abs(s21_value) < minimum_value:
                minimum_value = np.abs(s21_value)
                minimum_idx = idx
        
        print(f"Minimum for s21 f={measurement.frequency_hz[minimum_idx]} Hz,  ({complex_to_db(measurement.s21[minimum_idx])} dB)")
        #======================================================
        return 0
    except KeyboardInterrupt:
        print("\nAbort.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
