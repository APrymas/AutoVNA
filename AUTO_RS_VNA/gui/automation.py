from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from app.config import user_data_dir


PYTHON_FIELD_TOKENS = {
    "s11": "s11_db_phase",
    "s21": "s21_db_phase",
    "s12": "s12_db_phase",
    "s22": "s22_db_phase",
    "s11ri": "s11_complex",
    "s21ri": "s21_complex",
    "s12ri": "s12_complex",
    "s22ri": "s22_complex",
    "zs11": "z_s11",
    "vswr": "vswr",
    "memory": "memory",
    "markers": "markers",
    "tdr": "tdr",
}

FREQUENCY_UNITS = {
    "hz": 1.0,
    "khz": 1e3,
    "mhz": 1e6,
    "ghz": 1e9,
}


def parse_python_event_line(line: str) -> tuple[int, dict[str, str]] | None:
    match = re.fullmatch(r"\s*nanoOK_(\d+)(?:\|(.*))?\s*", line, flags=re.IGNORECASE)
    if not match:
        return None
    index = int(match.group(1))
    tail = (match.group(2) or "").strip()
    metadata: dict[str, str] = {}
    if tail:
        for position, token in enumerate(tail.split("|")):
            token = token.strip()
            if not token:
                continue
            if "=" in token:
                key, value = token.split("=", 1)
                metadata[key.strip().lower()] = value.strip()
            elif position == 0:
                metadata["name"] = token
    return index, metadata


def parse_frequency_value(value: str, tr=None) -> float:
    match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(hz|khz|mhz|ghz)\s*", value, flags=re.IGNORECASE)
    if not match:
        text = tr("frequency_value_with_unit", "Use a value with a unit: Hz, kHz, MHz or GHz.") if tr else "Use a value with a unit: Hz, kHz, MHz or GHz."
        raise ValueError(text)
    number = float(match.group(1).replace(",", "."))
    return number * FREQUENCY_UNITS[match.group(2).lower()]


def parse_python_control_line(line: str, tr=None) -> tuple[str, Any] | None:
    value = line.strip()
    if re.fullmatch(r"nanoDataMem", value, flags=re.IGNORECASE):
        return "data_mem", None
    range_match = re.fullmatch(r"nanoRange(Start|Stop)\s*:\s*(.+)", value, flags=re.IGNORECASE)
    if range_match:
        side = range_match.group(1).lower()
        return f"range_{side}", parse_frequency_value(range_match.group(2), tr)
    format_match = re.fullmatch(r"nano(CSV|S1P|S2P)(?:-(.+))?", value, flags=re.IGNORECASE)
    if format_match:
        format_name = format_match.group(1).lower()
        tokens = []
        if format_match.group(2):
            tokens = [token.strip().lower() for token in format_match.group(2).split("-") if token.strip()]
        return "format", (format_name, tokens)
    return None


def safe_filename_stem(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    for suffix in (".csv", ".s1p", ".s2p"):
        if text.lower().endswith(suffix):
            text = text[:-len(suffix)]
            break
    text = text.replace("\\", "/").split("/")[-1]
    illegal = '<>:"/\\|?*' if os.name == "nt" else "/\x00"
    for character in illegal:
        text = text.replace(character, "_")
    text = text.strip().strip(".")
    return text or fallback


class AutomationWindow(tk.Toplevel):
    FIELD_LABELS = {
        "s11_db_phase": "S11 dB/faza",
        "s21_db_phase": "S21 dB/faza",
        "s12_db_phase": "S12 dB/faza",
        "s22_db_phase": "S22 dB/faza",
        "s11_complex": "S11 Re/Im",
        "s21_complex": "S21 Re/Im",
        "s12_complex": "S12 Re/Im",
        "s22_complex": "S22 Re/Im",
        "z_s11": "Z z S11",
        "vswr": "VSWR",
        "memory": "Pamięć",
        "markers": "M1/M2",
        "tdr": "TDR (osobny CSV)",
    }
    FORMAT_LABELS = {
        "csv": "CSV",
        "s1p": "Touchstone .s1p",
        "s2p": "Touchstone .s2p",
    }
    METRICS = [
        "S11 min [dB]", "S11 przy M1 [dB]", "S11 przy M2 [dB]", "ΔS11 M2-M1 [dB]",
        "S21 min [dB]", "S21 przy M1 [dB]", "S21 przy M2 [dB]", "ΔS21 M2-M1 [dB]",
        "S12 min [dB]", "S12 przy M1 [dB]", "S12 przy M2 [dB]", "ΔS12 M2-M1 [dB]",
        "S22 min [dB]", "S22 przy M1 [dB]", "S22 przy M2 [dB]", "ΔS22 M2-M1 [dB]",
        "VSWR max", "VSWR przy M1", "VSWR przy M2", "ΔVSWR M2-M1",
        "Re(Z) przy M1 [ohm]", "Re(Z) przy M2 [ohm]", "ΔRe(Z) M2-M1 [ohm]",
        "|Z| przy M1 [ohm]", "|Z| przy M2 [ohm]", "Δ|Z| M2-M1 [ohm]",
        "Δf M2-M1 [Hz]",
    ]

    def __init__(
        self,
        parent,
        default_fields: dict[str, bool],
        default_formats: dict[str, bool],
        default_folder: Path,
        snapshot_callback: Callable[[int, str, Path, dict[str, bool], dict[str, bool]], list[Path]],
        report_callback: Callable[[Path, dict[str, str], list[Path], list[Path]], Path | None],
        status_callback: Callable[[str], None],
        python_command: list[str] | None,
        reference_provider: Callable[[], list[tuple[str, str]]],
        reference_callback: Callable[[str, Path | None], str],
        data_mem_callback: Callable[[], str],
        range_callback: Callable[[float, float, Callable[[], None], Callable[[Exception], None]], None],
        translator=None,
        measurement_callback=None,
        timed_save_callback=None,
    ) -> None:
        super().__init__(parent)
        self.tr = translator
        self.log_file = user_data_dir() / "AutoRS_VNA_automation.log"
        self.title(self._t("auto_title", "Automatyczne pomiary"))
        self.geometry("1040x820")
        self.minsize(820, 620)
        self.snapshot_callback = snapshot_callback
        self.report_callback = report_callback
        self.status_callback = status_callback
        self.python_command = python_command
        self.reference_provider = reference_provider
        self.reference_callback = reference_callback
        self.data_mem_callback = data_mem_callback
        self.range_callback = range_callback
        self.measurement_callback = measurement_callback
        self.timed_save_callback = timed_save_callback
        self.active = False
        self.process: subprocess.Popen | None = None
        self.after_id: str | None = None
        self.deadline_after_id: str | None = None
        self.deadline_monotonic: float | None = None
        self.expected_finish_wall: datetime | None = None
        self.saved_indices: set[int] = set()
        self.saved_files: list[Path] = []
        self.image_paths: list[Path] = []
        self.threshold_hits = 0
        self.threshold_armed = True
        self.last_trigger_time = 0.0
        self.pending_fields_override: dict[str, bool] | None = None
        self.pending_formats_override: dict[str, bool] | None = None
        self.pending_range_start_hz: float | None = None
        self.pending_range_stop_hz: float | None = None
        self.range_change_active = False
        self.range_data_mem_pending = False
        self.queued_python_events: list[tuple[int, str | None, dict[str, bool] | None, dict[str, bool] | None]] = []
        self.series_start_mono: float | None = None
        self.series_start_wall: datetime | None = None
        self.time_measurement_active = False
        self.pending_time_index: int | None = None
        self.pending_time_target_mono: float | None = None
        self.pending_time_target_wall: datetime | None = None
        self.pending_time_started_mono: float | None = None
        self.pending_time_started_wall: datetime | None = None
        self.last_saved_paths: list[Path] = []
        self.last_saved_name = ""
        self.ui_queue: queue.Queue = queue.Queue()
        self.closing = False
        self.count_var = tk.IntVar(value=1)
        self.template_var = tk.StringVar(value="measurement_{i}_{X}_{date}")
        self.x_var = tk.StringVar(value="0")
        self.folder_var = tk.StringVar(value=str(default_folder))
        self.mode_var = tk.StringVar(value="time")
        self.interval_var = tk.DoubleVar(value=5.0)
        self.duration_minutes_var = tk.DoubleVar(value=0.0)
        self.estimated_finish_var = tk.StringVar(value="")
        self.metric_labels = {key: self._metric_label(key) for key in self.METRICS}
        self.metric_display_to_key = {label: key for key, label in self.metric_labels.items()}
        self.metric_var = tk.StringVar(value=self.metric_labels[self.METRICS[0]])
        self.operator_var = tk.StringVar(value="<")
        self.threshold_var = tk.DoubleVar(value=-10.0)
        self.hysteresis_var = tk.DoubleVar(value=0.5)
        self.min_interval_var = tk.DoubleVar(value=1.0)
        self.consecutive_var = tk.IntVar(value=2)
        self.generate_report_var = tk.BooleanVar(value=True)
        self.field_vars = {key: tk.BooleanVar(value=default_fields.get(key, False)) for key in self.FIELD_LABELS}
        self.format_vars = {key: tk.BooleanVar(value=default_formats.get(key, False)) for key in self.FORMAT_LABELS}
        self.reference_var = tk.StringVar(value="")
        self.reference_map: dict[str, str] = {}
        self.description_vars = {
            "study_title": tk.StringVar(value=self._t("default_auto_series_title", "Automatic R&S ZVL13 series")),
            "operator": tk.StringVar(),
            "object": tk.StringVar(),
            "temperature": tk.StringVar(),
            "humidity": tk.StringVar(),
            "line_length_short": tk.StringVar(),
        }
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(25, self._poll_ui_queue)

    def _t(self, key: str, fallback: str) -> str:
        return self.tr(key, fallback) if self.tr else fallback

    def _metric_label(self, key: str) -> str:
        match = re.fullmatch(r"(S11|S21|S12|S22) przy (M1|M2) \[dB\]", key)
        if match:
            return self._t("metric_at_marker", "{parameter} at {marker} {unit}").format(parameter=match.group(1), marker=match.group(2), unit="[dB]")
        match = re.fullmatch(r"(S11|S21|S12|S22) min \[dB\]", key)
        if match:
            return self._t("metric_minimum", "{parameter} min {unit}").format(parameter=match.group(1), unit="[dB]")
        match = re.fullmatch(r"Δ(S11|S21|S12|S22) M2-M1 \[dB\]", key)
        if match:
            return self._t("metric_delta", "Δ{parameter} M2−M1 {unit}").format(parameter=match.group(1), unit="[dB]")
        match = re.fullmatch(r"(VSWR) przy (M1|M2)", key)
        if match:
            return self._t("metric_at_marker", "{parameter} at {marker} {unit}").format(parameter=match.group(1), marker=match.group(2), unit="").strip()
        match = re.fullmatch(r"(Re\(Z\)|\|Z\|) przy (M1|M2) \[ohm\]", key)
        if match:
            return self._t("metric_impedance_at_marker", "{parameter} at {marker} [Ω]").format(parameter=match.group(1), marker=match.group(2))
        match = re.fullmatch(r"Δ(Re\(Z\)|\|Z\|) M2-M1 \[ohm\]", key)
        if match:
            return self._t("metric_impedance_delta", "Δ{parameter} M2−M1 [Ω]").format(parameter=match.group(1))
        if key == "Δf M2-M1 [Hz]":
            return self._t("metric_frequency_delta", "Δf M2−M1 [Hz]")
        return key.replace("ohm", "Ω").replace("M2-M1", "M2−M1")

    def _default_python_code(self) -> str:
        return r'''import serial
import time

SERIAL_PORT = "COM3"
BAUD_RATE = 250000

POSITIONS_X = [68, 84, 100, 122, 138, 154, 172]


def send_gcode(ser, command):
    print(command, flush=True)
    ser.write((command + "\n").encode("utf-8"))
    ser.flush()

    while True:
        response = ser.readline().decode(
            "utf-8", errors="replace"
        ).strip()

        if not response:
            continue

        response_lower = response.lower()

        if response_lower.startswith("ok"):
            return

        if (
            response_lower.startswith("error")
            or response_lower.startswith("!!")
        ):
            raise RuntimeError(
                f"Error {command}: {response}"
            )


def move(ser, command):
    send_gcode(ser, command)
    send_gcode(ser, "M400")


def main():
    ser = None

    try:
        ser = serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
        )

        time.sleep(2)
        ser.reset_input_buffer()

        move(ser, "G28 X Y")
        send_gcode(ser, "G90")
        move(ser, "G1 X50 Y75 Z0")

        print("nanoDataMem", flush=True)

        measurement_number = 1

        for position, x in enumerate(POSITIONS_X, start=1):
            move(ser, f"G1 X{x}")
            time.sleep(2)

            move(ser, "G1 Z-25")
            time.sleep(9)

            filename = f"position{position:02d}"

            print("nanoCSV-s11-s21-zs11", flush=True)
            print("nanoS1P-s11", flush=True)

            print(
                f"nanoOK_{measurement_number}|name={filename}",
                flush=True,
            )
            measurement_number += 1
            time.sleep(2)
            move(ser, "G1 Z0")

        move(ser, "G1 Z0")
        move(ser, "G28 X Y")

        print(
            f"Finished. Executed {measurement_number - 1} measurements.",
            flush=True,
        )

    except serial.SerialException as error:
        print(f"Port error: {error}", flush=True)

    except RuntimeError as error:
        print(error, flush=True)

    finally:
        if ser is not None and ser.is_open:
            ser.close()


if __name__ == "__main__":
    main()
'''

    def _insert_default_code(self) -> None:
        self.code_text.delete("1.0", "end")
        self.code_text.insert("1.0", self._default_python_code())

    def _cancel_deadline(self) -> None:
        if self.deadline_after_id:
            try:
                self.after_cancel(self.deadline_after_id)
            except Exception:
                pass
            self.deadline_after_id = None
        self.deadline_monotonic = None
        self.expected_finish_wall = None

    def _deadline_expired(self) -> bool:
        return self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic

    def _deadline_callback(self) -> None:
        self.deadline_after_id = None
        if self.active:
            self.status_callback(self._t("deadline_reached", "Osiągnięto ustawiony czas zakończenia serii."))
            self._finish("deadline")

    def post_ui(self, function, *args, **kwargs) -> None:
        self.ui_queue.put((function, args, kwargs))

    def _poll_ui_queue(self) -> None:
        if self.closing or not self.winfo_exists():
            return
        try:
            while True:
                function, args, kwargs = self.ui_queue.get_nowait()
                function(*args, **kwargs)
        except queue.Empty:
            pass
        self.after(25, self._poll_ui_queue)

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        window_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window_id, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        data_section = ttk.LabelFrame(body, text=self._t("auto_section_fields", "Zapisywane dane i formaty"), padding=8)
        data_section.pack(fill="x", pady=4)
        for index, (key, label) in enumerate(self.FIELD_LABELS.items()):
            ttk.Checkbutton(data_section, text=self._t(f"save_field_{key}", label), variable=self.field_vars[key]).grid(row=index // 5, column=index % 5, sticky="w", padx=6, pady=2)
        formats_row = (len(self.FIELD_LABELS) + 4) // 5
        formats_frame = ttk.Frame(data_section)
        formats_frame.grid(row=formats_row, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Label(formats_frame, text=self._t("save_formats", "Formaty:")).pack(side="left", padx=(0, 6))
        for key, label in self.FORMAT_LABELS.items():
            ttk.Checkbutton(formats_frame, text=self._t(f"save_format_{key}", label), variable=self.format_vars[key]).pack(side="left", padx=5)
        ttk.Label(data_section, text=self._t("s2p_assumption", ".s2p zapisuje rzeczywiście zmierzone S11, S21, S12 i S22."), wraplength=900).grid(row=formats_row + 1, column=0, columnspan=5, sticky="w", padx=6, pady=(4, 0))

        trigger_section = ttk.LabelFrame(body, text=self._t("auto_section_trigger", "Sposób wyzwalania zapisu"), padding=8)
        trigger_section.pack(fill="both", expand=True, pady=4)
        radio_frame = ttk.Frame(trigger_section)
        radio_frame.pack(fill="x")
        ttk.Radiobutton(radio_frame, text=self._t("trigger_time", "Co określony czas"), value="time", variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=5)
        ttk.Radiobutton(radio_frame, text=self._t("trigger_python", "Wklejony kod Python"), value="python", variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=5)
        ttk.Radiobutton(radio_frame, text=self._t("trigger_threshold", "Przekroczenie wartości"), value="threshold", variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=5)
        self.time_frame = ttk.Frame(trigger_section)
        ttk.Label(self.time_frame, text=self._t("save_every", "Zapis co [s]")).grid(row=0, column=0, sticky="w")
        ttk.Entry(self.time_frame, textvariable=self.interval_var, width=12).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(self.time_frame, text=self._t("time_limit", "Zakończ po [min], 0 = bez limitu")).grid(row=0, column=2, sticky="w", padx=(18, 2))
        ttk.Entry(self.time_frame, textvariable=self.duration_minutes_var, width=12).grid(row=0, column=3, sticky="w", padx=5)
        ttk.Label(self.time_frame, textvariable=self.estimated_finish_var, style="Info.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(5, 0))
        self.python_frame = ttk.Frame(trigger_section)
        python_header = ttk.Frame(self.python_frame)
        python_header.pack(fill="x")
        ttk.Label(python_header, text=self._t("python_instruction", "Komendy muszą być wysłane przez print(..., flush=True). nanoOK_<index>|name=<name> zapisuje pomiar. nanoDataMem ustawia Data → Mem. Zakres: nanoRangeStart:10MHz i nanoRangeStop:100MHz. Jednorazowy zapis: nanoCSV-s11-s21-zs11-memory, nanoS1P-s11 lub nanoS2P-s11-s21-s12-s22. Pola: s11, s21, s12, s22, s11ri, s21ri, s12ri, s22ri, zs11, zs21, vswr, memory, markers, tdr, all. Nie używaj myślników w nazwach; myślnik rozdziela elementy komendy."), wraplength=840, justify="left").pack(side="left", fill="x", expand=True)
        ttk.Button(python_header, text=self._t("insert_python_example", "Wstaw przykład"), command=self._insert_default_code).pack(side="right", padx=4)
        self.code_text = tk.Text(self.python_frame, height=16, wrap="none", font=("Consolas", 9), undo=True)
        self.code_text.pack(fill="both", expand=True, pady=4)
        saved_code_path = user_data_dir() / "automation_code.py"
        try:
            initial_code = saved_code_path.read_text(encoding="utf-8") if saved_code_path.exists() else self._default_python_code()
        except Exception:
            initial_code = self._default_python_code()
        self.code_text.insert("1.0", initial_code)
        self.threshold_frame = ttk.Frame(trigger_section)
        ttk.Label(self.threshold_frame, text=self._t("parameter", "Parametr")).grid(row=0, column=0, sticky="w")
        ttk.Combobox(self.threshold_frame, textvariable=self.metric_var, values=list(self.metric_labels.values()), state="readonly", width=28).grid(row=0, column=1, padx=4)
        ttk.Combobox(self.threshold_frame, textvariable=self.operator_var, values=["<", ">", "<=", ">="], state="readonly", width=5).grid(row=0, column=2, padx=4)
        ttk.Entry(self.threshold_frame, textvariable=self.threshold_var, width=12).grid(row=0, column=3, padx=4)
        ttk.Label(self.threshold_frame, text=self._t("hysteresis", "Histereza")).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(self.threshold_frame, textvariable=self.hysteresis_var, width=12).grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(self.threshold_frame, text=self._t("min_interval", "Min. odstęp [s]")).grid(row=1, column=2, sticky="e")
        ttk.Entry(self.threshold_frame, textvariable=self.min_interval_var, width=12).grid(row=1, column=3, padx=4)
        ttk.Label(self.threshold_frame, text=self._t("consecutive", "Kolejne przekroczenia")).grid(row=2, column=0, sticky="w")
        ttk.Spinbox(self.threshold_frame, from_=1, to=100, textvariable=self.consecutive_var, width=10).grid(row=2, column=1, sticky="w", padx=4)

        self.naming_section = ttk.LabelFrame(body, text=self._t("auto_section_names", "Nazwa i liczba pomiarów"), padding=8)
        self.naming_section.pack(fill="x", pady=4)
        ttk.Label(self.naming_section, text=self._t("file_template", "Szablon")).grid(row=0, column=0, sticky="w")
        ttk.Entry(self.naming_section, textvariable=self.template_var, width=42).grid(row=0, column=1, sticky="ew", padx=5)
        ttk.Label(self.naming_section, text="X").grid(row=0, column=2)
        ttk.Entry(self.naming_section, textvariable=self.x_var, width=10).grid(row=0, column=3, padx=5)
        ttk.Label(self.naming_section, text=self._t("measurement_count", "Liczba pomiarów")).grid(row=0, column=4)
        ttk.Spinbox(self.naming_section, from_=1, to=100000, textvariable=self.count_var, width=10).grid(row=0, column=5, padx=5)
        self.preview_var = tk.StringVar()
        ttk.Label(self.naming_section, textvariable=self.preview_var).grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))
        self.naming_section.columnconfigure(1, weight=1)
        for variable in (self.template_var, self.x_var, self.count_var):
            variable.trace_add("write", lambda *_: self._update_preview())
        for variable in (self.interval_var, self.duration_minutes_var, self.count_var):
            variable.trace_add("write", lambda *_: self._update_estimated_finish())

        self.reference_section = ttk.LabelFrame(body, text=self._t("reference_section", "Źródło pamięci referencyjnej"), padding=8)
        self.reference_section.pack(fill="x", pady=4)
        self.reference_box = ttk.Combobox(self.reference_section, textvariable=self.reference_var, state="readonly", width=54)
        self.reference_box.pack(side="left", fill="x", expand=True)
        ttk.Button(self.reference_section, text=self._t("refresh", "Odśwież"), command=self._refresh_references).pack(side="left", padx=4)
        ttk.Button(self.reference_section, text=self._t("apply_reference", "Ustaw jako Mem"), command=self._apply_reference).pack(side="left", padx=4)
        ttk.Button(self.reference_section, text=self._t("load_reference", "Wczytaj CSV/S1P/S2P"), command=self._load_reference_file).pack(side="left", padx=4)

        conditions_section = ttk.LabelFrame(body, text=self._t("auto_section_conditions", "Warunki badania i zdjęcia"), padding=8)
        conditions_section.pack(fill="x", pady=4)
        display_map = {
            "study_title": self._t("study_title", "Test title"),
            "operator": self._t("operator", "Operator"),
            "object": self._t("object", "Device under test"),
            "temperature": self._t("temperature", "Temperature"),
            "humidity": self._t("humidity", "Humidity"),
            "line_length_short": self._t("line_length_short", "Line length"),
        }
        for row, (label, variable) in enumerate(self.description_vars.items()):
            ttk.Label(conditions_section, text=display_map.get(label, label)).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(conditions_section, textvariable=variable, width=45).grid(row=row, column=1, sticky="ew", padx=5)
        ttk.Label(conditions_section, text=self._t("notes", "Opis / uwagi")).grid(row=len(self.description_vars), column=0, sticky="nw", pady=2)
        self.notes_text = tk.Text(conditions_section, height=4, wrap="word")
        self.notes_text.grid(row=len(self.description_vars), column=1, sticky="ew", padx=5)
        image_frame = ttk.Frame(conditions_section)
        image_frame.grid(row=len(self.description_vars) + 1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(image_frame, text=self._t("add_images", "Dodaj zdjęcia…"), command=self._add_images).pack(side="left")
        ttk.Button(image_frame, text=self._t("clear_images", "Wyczyść zdjęcia"), command=self._clear_images).pack(side="left", padx=4)
        self.images_var = tk.StringVar(value=self._t("no_images", "Brak zdjęć"))
        ttk.Label(image_frame, textvariable=self.images_var).pack(side="left", padx=8)
        ttk.Checkbutton(conditions_section, text=self._t("generate_report_after", "Po zakończeniu wygeneruj raport PDF"), variable=self.generate_report_var).grid(row=len(self.description_vars) + 2, column=0, columnspan=2, sticky="w")
        conditions_section.columnconfigure(1, weight=1)

        folder_section = ttk.LabelFrame(body, text=self._t("auto_section_folder", "Folder plików"), padding=8)
        folder_section.pack(fill="x", pady=4)
        ttk.Entry(folder_section, textvariable=self.folder_var).pack(side="left", fill="x", expand=True)
        ttk.Button(folder_section, text=self._t("choose", "Wybierz…"), command=self._choose_folder).pack(side="left", padx=5)
        self.state_var = tk.StringVar(value=self._t("series_inactive", "Seria nieaktywna"))
        buttons = ttk.Frame(body, padding=(0, 8))
        buttons.pack(fill="x")
        ttk.Label(buttons, textvariable=self.state_var).pack(side="left")
        ttk.Button(buttons, text=self._t("stop_series", "Zatrzymaj"), command=self.stop).pack(side="right", padx=4)
        ttk.Button(buttons, text=self._t("start_series", "Rozpocznij"), command=self.start).pack(side="right", padx=4)
        self._refresh_references()
        self._update_preview()
        self._update_estimated_finish()
        self._mode_changed()

    def _refresh_references(self) -> None:
        items = self.reference_provider()
        self.reference_map = {label: key for key, label in items}
        values = list(self.reference_map)
        self.reference_box["values"] = values
        if self.reference_var.get() not in values:
            self.reference_var.set(values[0] if values else "")

    def _apply_reference(self) -> None:
        key = self.reference_map.get(self.reference_var.get())
        if not key:
            return
        try:
            result = self.reference_callback(key, None)
            self.status_callback(result)
        except Exception as exception:
            messagebox.showerror(self._t("error", "Błąd"), str(exception), parent=self)

    def _load_reference_file(self) -> None:
        filename = filedialog.askopenfilename(parent=self, title=self._t("load_reference", "Wczytaj plik referencyjny"), filetypes=[(self._t("measurement_files", "Measurement files"), "*.csv *.s1p *.s2p"), (self._t("file_type_all", "All files"), "*.*")])
        if not filename:
            return
        try:
            result = self.reference_callback("file", Path(filename))
            self.status_callback(result)
        except Exception as exception:
            messagebox.showerror(self._t("error", "Błąd"), str(exception), parent=self)

    def _mode_changed(self) -> None:
        for frame in (self.time_frame, self.python_frame, self.threshold_frame):
            frame.pack_forget()
        mode = self.mode_var.get()
        if mode == "time":
            self.time_frame.pack(fill="x", pady=8)
            if not self.naming_section.winfo_manager():
                self.naming_section.pack(fill="x", pady=4, before=self.reference_section)
        elif mode == "python":
            self.python_frame.pack(fill="both", expand=True, pady=8)
            self.naming_section.pack_forget()
        else:
            self.threshold_frame.pack(fill="x", pady=8)
            if not self.naming_section.winfo_manager():
                self.naming_section.pack(fill="x", pady=4, before=self.reference_section)

    def _update_preview(self) -> None:
        try:
            name = self._format_name(0)
            extensions = [f".{key}" for key, variable in self.format_vars.items() if variable.get()]
            self.preview_var.set(self._t("preview", "Przykład: {name}").format(name=name + " / " + ", ".join(extensions)))
        except Exception as exception:
            self.preview_var.set(self._t("invalid_template", "Niepoprawny szablon: {error}").format(error=exception))

    def _update_estimated_finish(self) -> None:
        try:
            count = max(1, int(self.count_var.get()))
            interval = max(0.0, float(self.interval_var.get()))
            duration_minutes = max(0.0, float(self.duration_minutes_var.get()))
            seconds = max(0, count - 1) * interval
            if duration_minutes > 0:
                seconds = min(seconds, duration_minutes * 60.0)
            finish = datetime.fromtimestamp(datetime.now().timestamp() + seconds)
            self.estimated_finish_var.set(self._t("estimated_finish", "Przewidywane zakończenie: {time}").format(time=finish.strftime("%Y-%m-%d %H:%M:%S")))
        except Exception:
            self.estimated_finish_var.set("")

    def _format_name(self, index: int, controller_name: str | None = None) -> str:
        fallback = f"measurement_{index}"
        if controller_name and controller_name.strip():
            return safe_filename_stem(controller_name, fallback)
        template = self.template_var.get().strip() or "measurement_{i}_{date}"
        name = template.format(i=index, X=self.x_var.get(), date=datetime.now().strftime("%Y%m%d_%H%M%S"), mode=self.mode_var.get())
        return safe_filename_stem(name, fallback)

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self, initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def _add_images(self) -> None:
        files = filedialog.askopenfilenames(parent=self, title=self._t("select_images_title", "Select images"), filetypes=[(self._t("file_type_images", "Images"), "*.png *.jpg *.jpeg *.webp *.bmp"), (self._t("file_type_all", "All files"), "*.*")])
        for filename in files:
            path = Path(filename)
            if path not in self.image_paths:
                self.image_paths.append(path)
        self.images_var.set(self._t("selected_images", "Selected: {count}").format(count=len(self.image_paths)))

    def _clear_images(self) -> None:
        self.image_paths.clear()
        self.images_var.set(self._t("no_images", "Brak zdjęć"))

    def start(self) -> None:
        if self.active:
            return
        mode = self.mode_var.get()
        try:
            folder = Path(self.folder_var.get()).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            if mode in {"time", "threshold"} and "csv" in self.format_vars:
                self.format_vars["csv"].set(True)
            formats = {key: variable.get() for key, variable in self.format_vars.items()}
            if mode != "python" and not any(formats.values()):
                raise ValueError
            if mode != "python":
                count = int(self.count_var.get())
                if count < 1:
                    raise ValueError
            else:
                count = 0
            interval = float(self.interval_var.get())
            duration_minutes = float(self.duration_minutes_var.get())
            if mode == "time" and (interval <= 0 or duration_minutes < 0):
                raise ValueError
            if mode == "time" and self.measurement_callback is None and self.timed_save_callback is None:
                raise RuntimeError("Brak funkcji wykonującej nowy pomiar VNA. Podmień także gui/main_window.py.")
        except RuntimeError as exception:
            messagebox.showerror(self._t("error", "Błąd"), str(exception), parent=self)
            return
        except Exception:
            messagebox.showerror(self._t("error", "Błąd"), self._t("auto_invalid_settings", "Sprawdź ustawienia, formaty i folder."), parent=self)
            return
        self._cancel_deadline()
        self.active = True
        self.saved_indices.clear()
        self.saved_files.clear()
        self.threshold_hits = 0
        self.threshold_armed = True
        self.last_trigger_time = 0.0
        self.series_start_mono = time.monotonic()
        self.series_start_wall = datetime.now()
        self.time_measurement_active = False
        self.pending_time_index = None
        self.pending_time_target_mono = None
        self.pending_time_target_wall = None
        self.pending_time_started_mono = None
        self.pending_time_started_wall = None
        self.last_saved_paths = []
        self.last_saved_name = ""
        self.pending_fields_override = None
        self.pending_formats_override = None
        self.pending_range_start_hz = None
        self.pending_range_stop_hz = None
        self.range_change_active = False
        self.range_data_mem_pending = False
        self.queued_python_events.clear()
        finish_text = ""
        if mode == "time":
            seconds_by_count = max(0, count - 1) * max(0.0, interval)
            expected_seconds = seconds_by_count
            if duration_minutes > 0:
                duration_seconds = duration_minutes * 60.0
                expected_seconds = min(seconds_by_count, duration_seconds)
                self.deadline_monotonic = self.series_start_mono + duration_seconds
                self.deadline_after_id = self.after(max(1, int(duration_seconds * 1000)), self._deadline_callback)
            self.expected_finish_wall = self.series_start_wall + timedelta(seconds=expected_seconds)
            finish_text = " | " + self._t("estimated_finish", "Przewidywane zakończenie: {time}").format(time=self.expected_finish_wall.strftime("%Y-%m-%d %H:%M:%S"))
        if mode == "python":
            self.state_var.set(f"{self._t('series_active', 'Seria aktywna')}: 0")
        else:
            self.state_var.set(f"{self._t('series_active', 'Seria aktywna')}: 0/{count}{finish_text}")
        self.status_callback(self._t("series_started", "Uruchomiono automatyczną serię pomiarową."))
        if mode == "time":
            self._schedule_time_tick()
        elif mode == "python":
            self._start_python()

    def _schedule_time_tick(self) -> None:
        if not self.active or self.mode_var.get() != "time":
            return
        if self._deadline_expired():
            self._deadline_callback()
            return
        index = len(self.saved_indices)
        if index >= int(self.count_var.get()):
            self._finish("count")
            return
        if self.series_start_mono is None:
            self.series_start_mono = time.monotonic()
        interval = max(0.05, float(self.interval_var.get()))
        target_mono = self.series_start_mono + index * interval
        delay_s = max(0.0, target_mono - time.monotonic())
        self.after_id = self.after(max(1, int(delay_s * 1000.0)), self._time_tick)

    def _time_tick(self) -> None:
        self.after_id = None
        if not self.active or self.mode_var.get() != "time" or self.time_measurement_active:
            return
        if self._deadline_expired():
            self._deadline_callback()
            return
        index = len(self.saved_indices)
        if index >= int(self.count_var.get()):
            self._finish("count")
            return
        interval = max(0.05, float(self.interval_var.get()))
        base_mono = self.series_start_mono if self.series_start_mono is not None else time.monotonic()
        base_wall = self.series_start_wall if self.series_start_wall is not None else datetime.now()
        target_mono = base_mono + index * interval
        target_wall = base_wall + timedelta(seconds=index * interval)
        now = time.monotonic()
        if now + 0.0005 < target_mono:
            self.after_id = self.after(max(1, int((target_mono - now) * 1000.0)), self._time_tick)
            return
        self.pending_time_index = index
        self.pending_time_target_mono = target_mono
        self.pending_time_target_wall = target_wall
        self.pending_time_started_mono = None
        self.pending_time_started_wall = None
        self.time_measurement_active = True
        if self.timed_save_callback is not None:
            fields = {key: variable.get() for key, variable in self.field_vars.items()}
            formats = {key: variable.get() for key, variable in self.format_vars.items()}
            formats = dict(formats)
            formats["csv"] = True
            folder = Path(self.folder_var.get()).expanduser()
            filename_stem = self._format_name(index)
            try:
                self.timed_save_callback(
                    index,
                    filename_stem,
                    folder,
                    fields,
                    formats,
                    target_mono,
                    target_wall,
                    self._time_atomic_completed,
                    self._time_measurement_failed,
                )
            except Exception as exception:
                self._time_measurement_failed(exception)
            return
        if self.measurement_callback is None:
            self._time_measurement_failed(RuntimeError("Brak funkcji wykonującej nowy pomiar VNA."))
            return
        try:
            self.measurement_callback(self._time_measurement_started, self._time_measurement_completed, self._time_measurement_failed)
        except Exception as exception:
            self._time_measurement_failed(exception)

    def _time_atomic_completed(self, result: dict[str, Any]) -> None:
        if not self.active or self.mode_var.get() != "time":
            self.time_measurement_active = False
            return
        index = int(result.get("index", self.pending_time_index if self.pending_time_index is not None else len(self.saved_indices)))
        paths = [Path(path) for path in result.get("paths", [])]
        csv_paths = [path for path in paths if path.suffix.lower() == ".csv" and path.exists()]
        if not csv_paths:
            self.time_measurement_active = False
            self._time_measurement_failed(RuntimeError("Nie zapisano pliku CSV pomiaru."))
            return
        name = str(result.get("name", self._format_name(index)))
        self.last_saved_name = name
        self.last_saved_paths = list(paths)
        self.saved_indices.add(index)
        self.saved_files.extend(path for path in paths if path not in self.saved_files)
        count = int(self.count_var.get())
        self.state_var.set(f"{self._t('series_active', 'Seria aktywna')}: {len(self.saved_indices)}/{count} | {self._t('last_index', 'ostatni indeks')}: {index}")
        self.time_measurement_active = False
        self.status_callback(self._t("auto_index_saved", "Zapisano automatyczny pomiar {index}: {name}").format(index=index, name=name))
        if len(self.saved_indices) >= count:
            self._finish("count")
            return
        self._schedule_time_tick()

    def _time_measurement_started(self, started_mono: float, started_wall: datetime) -> None:
        self.pending_time_started_mono = started_mono
        self.pending_time_started_wall = started_wall

    def _time_measurement_completed(self, read_elapsed_s: float) -> None:
        if not self.active or self.mode_var.get() != "time":
            self.time_measurement_active = False
            return
        index = self.pending_time_index if self.pending_time_index is not None else len(self.saved_indices)
        ok = self._trigger(index, defer_finish=True)
        self.time_measurement_active = False
        if not ok:
            self._finish("save_error")
            return
        if len(self.saved_indices) >= int(self.count_var.get()):
            self._finish("count")
            return
        self._schedule_time_tick()

    def _time_measurement_failed(self, exception: Exception) -> None:
        if not self.time_measurement_active and not self.active:
            return
        index = self.pending_time_index if self.pending_time_index is not None else len(self.saved_indices)
        self.time_measurement_active = False
        self.status_callback(self._t("status_auto_save_error", "Błąd automatycznego zapisu {index}: {error}").format(index=index, error=exception))
        self._finish("measurement_error")

    def _start_python(self) -> None:
        code = self.code_text.get("1.0", "end-1c")
        script = user_data_dir() / "automation_code.py"
        script.write_text(code, encoding="utf-8")
        environment = os.environ.copy()
        environment["NANOVNA_OUTPUT_DIR"] = str(Path(self.folder_var.get()).expanduser())
        process_options: dict[str, Any] = {}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        if not self.python_command:
            self.active = False
            messagebox.showerror(self._t("error_title_python_missing", "Brak Pythona"), self._t("error_python_missing", "Nie znaleziono interpretera Python."), parent=self)
            return
        try:
            self.process = subprocess.Popen([*self.python_command, "-u", str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(Path(self.folder_var.get()).expanduser()), env=environment, bufsize=1, **process_options)
            self._write_log(f"Python automation started: {script}")
        except Exception as exception:
            self.active = False
            messagebox.showerror(self._t("error_title_python_code", "Błąd kodu Python"), str(exception), parent=self)
            return

        def reader() -> None:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                self.post_ui(self._python_line, line.rstrip())
            return_code = self.process.wait()
            self.post_ui(self._python_finished, return_code)

        threading.Thread(target=reader, daemon=True).start()

    def _write_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
        except OSError:
            pass

    def _python_line(self, line: str) -> None:
        self._write_log(f"Python automation output: {line}")
        if not self.active:
            return
        event = parse_python_event_line(line)
        if event:
            index, metadata = event
            controller_name = metadata.get("name") or metadata.get("filename") or metadata.get("nazwa")
            fields_override = None if self.pending_fields_override is None else dict(self.pending_fields_override)
            formats_override = None if self.pending_formats_override is None else dict(self.pending_formats_override)
            if self.range_change_active:
                self.queued_python_events.append((index, controller_name, fields_override, formats_override))
                self.pending_fields_override = None
                self.pending_formats_override = None
                self.status_callback(self._t("python_wait_range", "nanoOK_{index} is waiting for the sweep-range change to finish.").format(index=index))
                return
            if self._trigger(index, controller_name, fields_override, formats_override):
                self.pending_fields_override = None
                self.pending_formats_override = None
            return
        try:
            control = parse_python_control_line(line, self.tr)
        except Exception as exception:
            self.status_callback(self._t("python_command_error", "Błąd komendy Python: {error}").format(error=exception))
            return
        if control is None:
            return
        command, payload = control
        if command == "data_mem":
            if self.range_change_active:
                self.range_data_mem_pending = True
                self.status_callback(self._t("data_mem_wait_range", "nanoDataMem is waiting for a new measurement after the sweep-range change."))
            else:
                self._apply_data_mem()
            return
        if command in {"range_start", "range_stop"}:
            if command == "range_start":
                self.pending_range_start_hz = float(payload)
            else:
                self.pending_range_stop_hz = float(payload)
            self._try_apply_range()
            return
        if command == "format":
            format_name, tokens = payload
            self._apply_format_override(format_name, tokens)

    def _apply_data_mem(self) -> None:
        try:
            result = self.data_mem_callback()
            self.field_vars["memory"].set(True)
            self.status_callback(result)
        except Exception as exception:
            self.status_callback(self._t("data_mem_error", "Błąd nanoDataMem: {error}").format(error=exception))

    def _apply_format_override(self, format_name: str, tokens: list[str]) -> None:
        if self.pending_formats_override is None:
            self.pending_formats_override = {key: False for key in self.FORMAT_LABELS}
        self.pending_formats_override[format_name] = True
        if format_name == "s1p":
            invalid = [token for token in tokens if token not in {"s11"}]
            if invalid:
                self.status_callback(self._t("s1p_only_s11", "nanoS1P obsługuje tylko pole s11."))
            return
        if format_name == "s2p":
            invalid = [token for token in tokens if token not in {"s11", "s21", "s12", "s22"}]
            if invalid:
                self.status_callback(self._t("s2p_fields", "nanoS2P obsługuje pola s11, s21, s12 i s22."))
            return
        if not tokens:
            return
        if self.pending_fields_override is None:
            self.pending_fields_override = {key: False for key in self.FIELD_LABELS}
        for token in tokens:
            if token == "all":
                for key in self.pending_fields_override:
                    self.pending_fields_override[key] = True
                continue
            field_name = PYTHON_FIELD_TOKENS.get(token)
            if field_name is None:
                self.status_callback(self._t("unknown_csv_field", "Nieznane pole nanoCSV: {field}").format(field=token))
                continue
            self.pending_fields_override[field_name] = True
        self.status_callback(self._t("one_shot_format_set", "Ustawiono jednorazowy format następnego nanoOK."))

    def _try_apply_range(self) -> None:
        if self.pending_range_start_hz is None or self.pending_range_stop_hz is None:
            return
        start_hz = self.pending_range_start_hz
        stop_hz = self.pending_range_stop_hz
        self.pending_range_start_hz = None
        self.pending_range_stop_hz = None
        if start_hz <= 0 or stop_hz <= start_hz:
            self.status_callback(self._t("status_range_invalid", "Błędny zakres: stop musi być większy od startu."))
            return
        if self.range_change_active:
            self.status_callback(self._t("status_range_busy", "Zmiana zakresu już trwa."))
            return
        self.range_change_active = True
        self.status_callback(self._t("status_range_setting", "Ustawianie zakresu {start}–{stop} Hz…").format(start=f"{start_hz:g}", stop=f"{stop_hz:g}"))
        try:
            self.range_callback(start_hz, stop_hz, self._range_ready, self._range_failed)
        except Exception as exception:
            self._range_failed(exception)

    def _range_ready(self) -> None:
        self.range_change_active = False
        if self.range_data_mem_pending:
            self.range_data_mem_pending = False
            self._apply_data_mem()
        queued = list(self.queued_python_events)
        self.queued_python_events.clear()
        for index, controller_name, fields_override, formats_override in queued:
            self._trigger(index, controller_name, fields_override, formats_override)
        self.status_callback(self._t("status_range_ready", "Zakres ustawiony i wykonano pierwszy pomiar."))

    def _range_failed(self, exception: Exception) -> None:
        self.range_change_active = False
        self.range_data_mem_pending = False
        self.queued_python_events.clear()
        self.status_callback(self._t("status_range_python_error", "Błąd zmiany zakresu z kodu Python: {error}").format(error=exception))

    def _python_finished(self, return_code: int) -> None:
        self._write_log(f"Python automation finished with exit code {return_code}.")
        if not self.active:
            return
        self._finish("process" if return_code == 0 else f"process_error_{return_code}")

    def on_measurement(self, metrics: dict[str, float], read_elapsed_s: float | None = None, measurement_started_mono: float | None = None) -> None:
        if not self.active or self.mode_var.get() != "threshold":
            return
        if self._deadline_expired():
            self._deadline_callback()
            return
        metric_key = self.metric_display_to_key.get(self.metric_var.get(), self.metric_var.get())
        value = metrics.get(metric_key)
        if value is None or value != value:
            return
        threshold = float(self.threshold_var.get())
        hysteresis = abs(float(self.hysteresis_var.get()))
        operator = self.operator_var.get()
        condition = {"<": value < threshold, "<=": value <= threshold, ">": value > threshold, ">=": value >= threshold}[operator]
        if not self.threshold_armed:
            if operator in {">", ">="} and value < threshold - hysteresis:
                self.threshold_armed = True
            elif operator in {"<", "<="} and value > threshold + hysteresis:
                self.threshold_armed = True
            return
        self.threshold_hits = self.threshold_hits + 1 if condition else 0
        now = time.monotonic()
        if self.threshold_hits < max(1, int(self.consecutive_var.get())):
            return
        if now - self.last_trigger_time < max(0.0, float(self.min_interval_var.get())):
            return
        self.last_trigger_time = now
        self.threshold_hits = 0
        self.threshold_armed = False
        index = len(self.saved_indices)
        ok = self._trigger(index, defer_finish=True)
        if not ok:
            self._finish("save_error")
            return
        if len(self.saved_indices) >= int(self.count_var.get()):
            self._finish("count")

    def _trigger(
        self,
        index: int,
        controller_name: str | None = None,
        fields_override: dict[str, bool] | None = None,
        formats_override: dict[str, bool] | None = None,
        defer_finish: bool = False,
    ) -> bool:
        mode = self.mode_var.get()
        if self._deadline_expired():
            self._deadline_callback()
            return False
        if not self.active or index < 0 or index in self.saved_indices:
            return False
        if mode != "python" and index >= int(self.count_var.get()):
            return False
        fields = fields_override or {key: variable.get() for key, variable in self.field_vars.items()}
        formats = formats_override or {key: variable.get() for key, variable in self.format_vars.items()}
        if mode in {"time", "threshold"}:
            formats = dict(formats)
            formats["csv"] = True
        if not any(formats.values()):
            self.status_callback(self._t("status_auto_save_no_format", "Błąd automatycznego zapisu {index}: nie wybrano formatu.").format(index=index))
            return False
        if formats_override is not None and not formats.get("csv", False):
            fields = dict(fields)
            fields["tdr"] = False
        folder = Path(self.folder_var.get()).expanduser()
        self.last_saved_paths = []
        self.last_saved_name = ""
        try:
            filename_stem = self._format_name(index, controller_name)
            paths = self.snapshot_callback(index, filename_stem, folder, fields, formats)
        except Exception as exception:
            self.status_callback(self._t("status_auto_save_error", "Błąd automatycznego zapisu {index}: {error}").format(index=index, error=exception))
            return False
        self.last_saved_name = filename_stem
        self.last_saved_paths = list(paths)
        self.saved_indices.add(index)
        self.saved_files.extend(paths)
        if mode == "python":
            self.state_var.set(f"{self._t('series_active', 'Seria aktywna')}: {len(self.saved_indices)} | {self._t('last_index', 'ostatni indeks')}: {index}")
        else:
            count = int(self.count_var.get())
            self.state_var.set(f"{self._t('series_active', 'Seria aktywna')}: {len(self.saved_indices)}/{count} | {self._t('last_index', 'ostatni indeks')}: {index}")
            if len(self.saved_indices) >= count and not defer_finish:
                self._finish("count")
        return True

    def _finish(self, reason: str = "count") -> None:
        was_active = self.active
        self.active = False
        if self.after_id:
            try:
                self.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        self._cancel_deadline()
        if reason not in {"process"} and self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        state = f"{self._t('series_finished', 'Seria zakończona')}: {len(self.saved_indices)}"
        if self.mode_var.get() != "python":
            state += f"/{self.count_var.get()}"
        if reason.startswith("process_error_"):
            state += f" | {self._t('process_code', 'kod procesu')} {reason.rsplit('_', 1)[-1]}"
        self.state_var.set(state)
        if was_active and self.generate_report_var.get():
            description = {self._t(key, key): variable.get() for key, variable in self.description_vars.items()}
            description[self._t("notes", "Description / notes")] = self.notes_text.get("1.0", "end-1c").strip()
            description[self._t("series_end_reason", "Series end reason")] = reason
            report_path = Path(self.folder_var.get()).expanduser() / f"{self._t('series_report_filename_prefix', 'series_report')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            try:
                self.report_callback(report_path, description, list(self.image_paths), list(self.saved_files))
            except Exception as exception:
                self.status_callback(f"{self._t('report_error', 'Nie udało się wygenerować raportu serii')}: {exception}")
        self.status_callback(self._t("series_completed", "Automatyczna seria została zakończona."))

    def stop(self) -> None:
        if not self.active and not (self.process and self.process.poll() is None):
            return
        self.active = False
        if self.after_id:
            try:
                self.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        self._cancel_deadline()
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        self.state_var.set(self._t("series_stopped", "Seria zatrzymana przez użytkownika"))
        self.status_callback(self._t("series_stopped_status", "Automatyczna seria została zatrzymana."))

    def _on_close(self) -> None:
        if self.active and not messagebox.askyesno(self._t("close_question", "Zamknąć?"), self._t("close_active_series_question", "Seria jest aktywna. Zatrzymać ją i zamknąć okno?"), parent=self):
            return
        self.closing = True
        self.stop()
        self.destroy()
