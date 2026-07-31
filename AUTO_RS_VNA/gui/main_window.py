from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from typing import Any, Optional

import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from app.config import Settings, user_data_dir
from app.i18n import Translator
from app.runtime import python_command
from device.zvl13 import ZVL13
from export.pdf_report import generate_pdf_report
from gui.automation import AutomationWindow
from gui.dialogs import (
    CalibrationModeDialog, ModelOptionsDialog, PluginOptionsDialog,
    RangeDialog, SaveSettingsDialog, TDRSettingsDialog,
)
from measurement.calculations import (
    complex_to_db, phase_deg, gamma_to_impedance,
    vswr_from_s11, nearest_index,
)
from measurement.csv_io import save_measurement_files, load_measurement_file
from measurement.tdr import calculate_tdr


class RSLab:
    def __init__(self, root: tk.Tk, base_dir: Path, installation_dir: Path | None = None) -> None:
        self.root = root
        self.base_dir = base_dir
        self.installation_dir = installation_dir or base_dir
        self.plugin_dir = self.installation_dir / "plugins"
        try:
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
        except OSError:

            self.plugin_dir = Path.home() / "AutoRS_VNA_plugins"
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
        bundled_plugins = self.base_dir / "plugins"
        if bundled_plugins.exists() and bundled_plugins.resolve() != self.plugin_dir.resolve():
            for bundled_plugin in bundled_plugins.glob("*.py"):
                target = self.plugin_dir / bundled_plugin.name
                if not target.exists():
                    try:
                        shutil.copy2(bundled_plugin, target)
                    except OSError:
                        pass
        self.settings = Settings()
        self.tr = Translator(base_dir, self.settings.get("language", "pl"))
        self.log_file = user_data_dir() / "AutoRS_VNA.log"
        self.vna = ZVL13(self.tr)
        self.python_command = python_command(base_dir)
        self.calibration: Optional[dict[str, Any]] = None
        self.connected = False
        self.running = False
        self.worker_busy = False
        self.measurement_pending = False
        self.connecting = False
        self.pending_start_after_connect = False
        self.pending_single_after_connect = False
        self.device_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ZVL13-SCPI")
        self.device_task_lock = threading.Lock()
        self.device_task_count = 0
        self.after_id: Optional[str] = None
        self.plugin_process: Optional[subprocess.Popen[str]] = None
        self.plugin_resume_after = False
        self.plugin_output_dir: Optional[Path] = None

        self.freq = np.array([], float)
        self.raw_s11 = np.array([], complex)
        self.raw_s21 = np.array([], complex)
        self.raw_s12 = np.array([], complex)
        self.raw_s22 = np.array([], complex)
        self.s11 = np.array([], complex)
        self.s21 = np.array([], complex)
        self.s12 = np.array([], complex)
        self.s22 = np.array([], complex)
        self.mem_s11: Optional[np.ndarray] = None
        self.mem_s21: Optional[np.ndarray] = None
        self.mem_s12: Optional[np.ndarray] = None
        self.mem_s22: Optional[np.ndarray] = None
        self.tdr_data: dict[str, np.ndarray] = {}
        self.markers: dict[str, Optional[int]] = {"M1": None, "M2": None}
        self.history: deque[dict[str, np.ndarray]] = deque(maxlen=max(2, int(self.settings.get("uncertainty_samples", 10))))
        self.overlays: list[dict[str, Any]] = []
        self.overlay_artists: list[Any] = []
        self.overlays_dirty = True
        self.auto_window: Optional[AutomationWindow] = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.closing = False
        self.terminal_history: list[str] = []
        self.terminal_history_index = 0

        self.root.title(self.tr("app_title", "AutoRS VNA"))
        self.root.geometry("1200x800")
        self.root.minsize(780, 600)
        self._build_variables()
        self._build_gui()
        self._build_plots()
        self.apply_theme(self.theme_var.get(), persist=False)
        self.apply_language(self.language_var.get(), persist=False)
        self.refresh_ports()
        self.refresh_plugins()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(25, self._poll_ui_queue)


    def post_ui(self, func, *args, **kwargs) -> None:
        self.ui_queue.put((func, args, kwargs))

    @property
    def device_busy(self) -> bool:
        with self.device_task_lock:
            return self.device_task_count > 0

    def _submit_device_task(self, work, on_success=None, on_error=None, task_name: str = "SCPI") -> bool:
        




        if self.closing:
            return False
        with self.device_task_lock:
            self.device_task_count += 1

        try:
            future = self.device_executor.submit(work)
        except Exception:
            with self.device_task_lock:
                self.device_task_count = max(0, self.device_task_count - 1)
            raise

        def completed(done_future: Future) -> None:
            try:
                result = done_future.result()
                self.post_ui(self._device_task_completed, on_success, None, result, task_name)
            except Exception as exc:
                self.post_ui(self._device_task_completed, on_error, exc, None, task_name)

        future.add_done_callback(completed)
        return True

    def _device_task_completed(self, callback, error, result, task_name: str) -> None:
        with self.device_task_lock:
            self.device_task_count = max(0, self.device_task_count - 1)
        if callback is None:
            if error is not None:
                self.set_status(f"{task_name}: {error}")
            return
        try:
            callback(error) if error is not None else callback(result)
        except Exception as exc:
            self.log(f"Callback error in {task_name}: {exc}")

    def _poll_ui_queue(self) -> None:
        if self.closing:
            return
        try:
            while True:
                func, args, kwargs = self.ui_queue.get_nowait()
                try:
                    func(*args, **kwargs)
                except Exception as exc:
                    self.log(f"GUI update error: {exc}")
        except queue.Empty:
            pass
        self.root.after(25, self._poll_ui_queue)

    def _build_variables(self) -> None:
        self.host_var = tk.StringVar(value=self.settings.get("last_host", ""))
        self.tcp_port_var = tk.IntVar(value=int(self.settings.get("tcp_port", 5025)))
        self.interval_var = tk.IntVar(value=int(self.settings.get("refresh_ms", 800)))
        self.enable_s11_var = tk.BooleanVar(value=True)
        self.enable_s21_var = tk.BooleanVar(value=True)
        self.enable_s12_var = tk.BooleanVar(value=False)
        self.enable_s22_var = tk.BooleanVar(value=False)
        self.show_impedance_var = tk.BooleanVar(value=True)
        self.show_vswr_var = tk.BooleanVar(value=True)
        self.show_tdr_var = tk.BooleanVar(value=False)
        self.show_memory_var = tk.BooleanVar(value=True)
        self.active_marker_var = tk.StringVar(value="M1")
        self.theme_var = tk.StringVar(value=self.settings.get("theme", "dark"))
        self.language_var = tk.StringVar(value=self.settings.get("language", "pl"))
        self.z0_var = tk.DoubleVar(value=float(self.settings.get("z0_ohm", 50.0)))
        self.vf_var = tk.DoubleVar(value=float(self.settings.get("velocity_factor", 0.66)))
        self.tdr_window_var = tk.StringVar(value=self.settings.get("tdr_window", "hann"))
        self.tdr_axis_mode_var = tk.StringVar(value=self.settings.get("tdr_axis_mode", "distance"))
        self.tdr_max_distance_var = tk.DoubleVar(value=float(self.settings.get("tdr_max_distance_m", 0.0)))
        self.tdr_zero_offset_var = tk.DoubleVar(value=float(self.settings.get("tdr_zero_offset_m", 0.0)))
        self.tdr_auto_zoom_var = tk.BooleanVar(value=bool(self.settings.get("tdr_auto_zoom", True)))
        self.status_var = tk.StringVar(value=self.tr("not_connected"))
        self.calibration_var = tk.StringVar(value=self.tr("uncalibrated"))
        self.marker_vars = {"M1": tk.StringVar(value="M1: --"), "M2": tk.StringVar(value="M2: --")}
        self.marker_delta_var = tk.StringVar(value=self.tr("no_marker_delta", "Δ(M2−M1): ustaw oba znaczniki"))
        self.tdr_info_var = tk.StringVar(value=self.tr("tdr_info_no_data", "Włącz S11 i TDR, aby obliczyć odpowiedź w dziedzinie czasu."))
        self.terminal_command_var = tk.StringVar()
        self.vf_var.trace_add("write", lambda *_: self.update_plots() if hasattr(self, "line_tdr_rho") else None)
        self.tdr_window_var.trace_add("write", lambda *_: self.update_plots() if hasattr(self, "line_tdr_rho") else None)

    def _build_gui(self) -> None:
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass
        self.widgets: dict[str, Any] = {}
        header = ttk.Frame(self.root, padding=(5, 3))
        header.pack(side="top", fill="x")
        header.columnconfigure(0, weight=3)
        header.columnconfigure(1, weight=2)

        top_bar = ttk.Frame(header)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self.flag_images = {}
        for language, filename in (("pl", "flag_pl.png"), ("en", "flag_en.png")):
            image_path = self.base_dir / "assets" / filename
            if image_path.exists():
                self.flag_images[language] = tk.PhotoImage(file=str(image_path))
        self.calibration_label = ttk.Label(top_bar, textvariable=self.calibration_var, style="Calibration.Bad.TLabel")
        self.calibration_label.pack(side="left", padx=(2, 8))
        language_box = ttk.Frame(top_bar)
        language_box.pack(side="right")
        self.widgets["language_label"] = ttk.Label(language_box, text=self.tr("language", "Język"))
        self.widgets["language_label"].pack(side="left", padx=(0, 3))
        self.language_buttons = {}
        for language in ("pl", "en"):
            button = ttk.Button(language_box, command=lambda value=language: self.language_var.set(value), width=3)
            button.configure(image=self.flag_images[language]) if language in self.flag_images else button.configure(text=language.upper())
            button.pack(side="left", padx=1)
            self.language_buttons[language] = button

        self.device_section = ttk.LabelFrame(header, text=self.tr("device_section", "Urządzenie i pomiar"), padding=(5, 3))
        self.device_section.grid(row=1, column=0, sticky="nsew", padx=(0, 2), pady=2)
        self.widgets["host_label"] = ttk.Label(self.device_section, text="IP / host")
        self.widgets["host_label"].grid(row=0, column=0, sticky="w")
        self.host_entry = ttk.Entry(self.device_section, textvariable=self.host_var, width=20)
        self.host_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=3)
        self.widgets["tcp_port_label"] = ttk.Label(self.device_section, text="TCP")
        self.widgets["tcp_port_label"].grid(row=0, column=3, sticky="e", padx=(5, 1))
        ttk.Entry(self.device_section, textvariable=self.tcp_port_var, width=7).grid(row=0, column=4, sticky="w")
        self.widgets["connect"] = ttk.Button(self.device_section, command=self.toggle_connection)
        self.widgets["connect"].grid(row=0, column=5, padx=1)
        self.widgets["start"] = ttk.Button(self.device_section, command=self.toggle_running)
        self.widgets["start"].grid(row=1, column=0, padx=1, pady=(3, 0), sticky="w")
        self.widgets["single"] = ttk.Button(self.device_section, command=self.single_measurement)
        self.widgets["single"].grid(row=1, column=1, padx=1, pady=(3, 0), sticky="w")
        self.widgets["range"] = ttk.Button(self.device_section, command=self.open_range_dialog)
        self.widgets["range"].grid(row=1, column=2, padx=1, pady=(3, 0), sticky="w")
        self.widgets["calibration"] = ttk.Button(self.device_section, command=self.start_calibration)
        self.widgets["calibration"].grid(row=1, column=3, padx=1, pady=(3, 0), sticky="w")
        self.widgets["refresh_label"] = ttk.Label(self.device_section)
        self.widgets["refresh_label"].grid(row=1, column=4, padx=(5, 1), pady=(3, 0), sticky="e")
        ttk.Entry(self.device_section, textvariable=self.interval_var, width=6).grid(row=1, column=5, padx=1, pady=(3, 0), sticky="w")
        self.device_section.columnconfigure(1, weight=1)

        self.measurement_section = ttk.LabelFrame(header, text=self.tr("measurement_section", "Kanały, pamięć i TDR"), padding=(5, 3))
        self.measurement_section.grid(row=1, column=1, sticky="nsew", padx=(2, 0), pady=2)
        channel_frame = ttk.Frame(self.measurement_section)
        channel_frame.pack(fill="x")
        for name, variable in (("s11", self.enable_s11_var), ("s21", self.enable_s21_var), ("s12", self.enable_s12_var), ("s22", self.enable_s22_var)):
            self.widgets[f"cb_{name}"] = ttk.Checkbutton(channel_frame, text=name.upper(), variable=variable, command=self._channel_changed)
            self.widgets[f"cb_{name}"].pack(side="left", padx=2)
        for key, variable, fallback in (("impedance", self.show_impedance_var, "Z"), ("vswr", self.show_vswr_var, "VSWR"), ("tdr", self.show_tdr_var, "TDR"), ("memory", self.show_memory_var, "Mem")):
            self.widgets[f"cb_{key}"] = ttk.Checkbutton(channel_frame, text=fallback, variable=variable, command=self.update_plots)
            self.widgets[f"cb_{key}"].pack(side="left", padx=2)
        memory_frame = ttk.Frame(self.measurement_section)
        memory_frame.pack(fill="x", pady=(3, 0))
        self.widgets["trace_to_mem"] = ttk.Button(memory_frame, command=self.trace_to_mem)
        self.widgets["trace_to_mem"].pack(side="left", padx=1)
        self.widgets["clear_memory"] = ttk.Button(memory_frame, command=self.clear_memory)
        self.widgets["clear_memory"].pack(side="left", padx=1)
        self.widgets["tdr_settings"] = ttk.Button(memory_frame, command=self.open_tdr_settings)
        self.widgets["tdr_settings"].pack(side="left", padx=(5, 1))

        self.tools_section = ttk.LabelFrame(header, text=self.tr("tools_section", "Pliki i narzędzia"), padding=(5, 3))
        self.tools_section.grid(row=2, column=0, sticky="nsew", padx=(0, 2), pady=2)
        file_actions = ttk.Frame(self.tools_section)
        file_actions.pack(fill="x")
        for key, command in (("save_settings", self.open_save_settings), ("save_csv", self.manual_save_csv), ("auto_measurements", self.open_automation), ("overlay", self.add_overlay), ("clear_overlays", self.clear_overlays), ("report", self.manual_report)):
            self.widgets[key] = ttk.Button(file_actions, command=command)
            self.widgets[key].pack(side="left", padx=1, pady=1)
        self.widgets["theme_label"] = ttk.Label(file_actions, text=self.tr("theme", "Motyw"))
        self.widgets["theme_label"].pack(side="left", padx=(5, 1))
        ttk.Combobox(file_actions, textvariable=self.theme_var, values=["light", "dark"], state="readonly", width=6).pack(side="left", padx=1)

        self.plugins_section = ttk.LabelFrame(header, text=self.tr("plugins", "Wtyczki"), padding=(5, 3))
        self.plugins_section.grid(row=2, column=1, sticky="nsew", padx=(2, 0), pady=2)
        self.plugins_button_frame = ttk.Frame(self.plugins_section)
        self.plugins_button_frame.pack(side="left", fill="x", expand=True)
        self.widgets["refresh_plugins"] = ttk.Button(self.plugins_section, command=self.refresh_plugins)
        self.widgets["refresh_plugins"].pack(side="right", padx=1)

        self.theme_var.trace_add("write", lambda *_: self.apply_theme(self.theme_var.get()))
        self.language_var.trace_add("write", lambda *_: self.apply_language(self.language_var.get()))

        self.marker_bar = ttk.LabelFrame(self.root, text=self.tr("marker"), padding=(5, 3))
        self.marker_bar.pack(side="top", fill="x", padx=5, pady=(1, 2))
        marker_controls = ttk.Frame(self.marker_bar)
        marker_controls.grid(row=0, column=0, sticky="w", padx=(0, 5))
        ttk.Radiobutton(marker_controls, text="M1", value="M1", variable=self.active_marker_var).pack(side="left", padx=1)
        ttk.Radiobutton(marker_controls, text="M2", value="M2", variable=self.active_marker_var).pack(side="left", padx=1)
        self.widgets["clear_markers"] = ttk.Button(marker_controls, command=self.clear_markers)
        self.widgets["clear_markers"].pack(side="left", padx=(4, 1))
        self.marker_m1_label = ttk.Label(self.marker_bar, textvariable=self.marker_vars["M1"], anchor="w", justify="left", style="CompactMarker.TLabel")
        self.marker_m1_label.grid(row=0, column=1, sticky="ew", padx=3)
        self.marker_m2_label = ttk.Label(self.marker_bar, textvariable=self.marker_vars["M2"], anchor="w", justify="left", style="CompactMarker.TLabel")
        self.marker_m2_label.grid(row=0, column=2, sticky="ew", padx=3)
        self.marker_delta_label = ttk.Label(self.marker_bar, textvariable=self.marker_delta_var, style="MarkerDelta.TLabel", anchor="w", justify="left")
        self.marker_delta_label.grid(row=0, column=3, sticky="ew", padx=3)
        self.marker_bar.columnconfigure(1, weight=3)
        self.marker_bar.columnconfigure(2, weight=3)
        self.marker_bar.columnconfigure(3, weight=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=2)
        names = ("sparams", "s12", "s22", "impedance", "smith", "tdr", "terminal")
        self.frames = {name: ttk.Frame(self.notebook) for name in names}
        for frame in self.frames.values():
            self.notebook.add(frame, text="")

        terminal_controls = ttk.Frame(self.frames["terminal"], padding=(6, 5))
        terminal_controls.pack(side="bottom", fill="x")
        self.terminal_entry = ttk.Entry(terminal_controls, textvariable=self.terminal_command_var)
        self.terminal_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.widgets["terminal_send"] = ttk.Button(terminal_controls, text=self.tr("send", "Send"), command=self.send_terminal_command)
        self.widgets["terminal_send"].pack(side="left")
        self.terminal_output = tk.Text(self.frames["terminal"], wrap="word", font=("Consolas", 9), state="disabled")
        terminal_scroll = ttk.Scrollbar(self.frames["terminal"], command=self.terminal_output.yview)
        self.terminal_output.configure(yscrollcommand=terminal_scroll.set)
        self.terminal_output.pack(side="left", fill="both", expand=True)
        terminal_scroll.pack(side="right", fill="y")
        self.terminal_entry.bind("<Return>", lambda _event: self.send_terminal_command())
        self.terminal_entry.bind("<Up>", self._terminal_history_up)
        self.terminal_entry.bind("<Down>", self._terminal_history_down)

        status = ttk.Frame(self.root, padding=(5, 2))
        status.pack(side="bottom", fill="x")
        ttk.Label(status, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)
        self.root.bind("<Configure>", self._update_marker_wrap, add="+")

    def _update_marker_wrap(self, event=None) -> None:
        available = max(540, self.root.winfo_width() - 210)
        self.marker_m1_label.configure(wraplength=max(180, int(available * 0.28)))
        self.marker_m2_label.configure(wraplength=max(180, int(available * 0.28)))
        self.marker_delta_label.configure(wraplength=max(220, int(available * 0.38)))

    def _build_plots(self) -> None:
        self.fig_s = Figure(figsize=(10, 5.5), dpi=100)
        self.ax_s11_db = self.fig_s.add_subplot(221)
        self.ax_s21_db = self.fig_s.add_subplot(222)
        self.ax_s11_phase = self.fig_s.add_subplot(223)
        self.ax_s21_phase = self.fig_s.add_subplot(224)
        self.canvas_s = FigureCanvasTkAgg(self.fig_s, master=self.frames["sparams"])
        self.canvas_s.get_tk_widget().pack(fill="both", expand=True)

        self.fig_s12 = Figure(figsize=(10, 5.5), dpi=100)
        self.ax_s12_db = self.fig_s12.add_subplot(211)
        self.ax_s12_phase = self.fig_s12.add_subplot(212)
        self.canvas_s12 = FigureCanvasTkAgg(self.fig_s12, master=self.frames["s12"])
        self.canvas_s12.get_tk_widget().pack(fill="both", expand=True)

        self.fig_s22 = Figure(figsize=(10, 5.5), dpi=100)
        self.ax_s22_db = self.fig_s22.add_subplot(211)
        self.ax_s22_phase = self.fig_s22.add_subplot(212)
        self.canvas_s22 = FigureCanvasTkAgg(self.fig_s22, master=self.frames["s22"])
        self.canvas_s22.get_tk_widget().pack(fill="both", expand=True)

        self.fig_z = Figure(figsize=(10, 5.5), dpi=100)
        z_grid = self.fig_z.add_gridspec(2, 2, height_ratios=(1.0, 1.05), hspace=0.52, wspace=0.2)
        self.ax_z11_r = self.fig_z.add_subplot(z_grid[0, 0])
        self.ax_z11_x = self.fig_z.add_subplot(z_grid[0, 1])
        self.ax_vswr = self.fig_z.add_subplot(z_grid[1, :])
        self.canvas_z = FigureCanvasTkAgg(self.fig_z, master=self.frames["impedance"])
        self.canvas_z.get_tk_widget().pack(fill="both", expand=True)

        self.fig_smith = Figure(figsize=(7, 5.5), dpi=100)
        self.ax_smith = self.fig_smith.add_subplot(111)
        self.canvas_smith = FigureCanvasTkAgg(self.fig_smith, master=self.frames["smith"])
        self.canvas_smith.get_tk_widget().pack(fill="both", expand=True)

        tdr_header = ttk.Frame(self.frames["tdr"], padding=(8, 5))
        tdr_header.pack(side="top", fill="x")
        ttk.Label(tdr_header, textvariable=self.tdr_info_var, style="Info.TLabel", wraplength=1300).pack(side="left", fill="x", expand=True)
        self.widgets["tdr_settings_tab"] = ttk.Button(tdr_header, command=self.open_tdr_settings)
        self.widgets["tdr_settings_tab"].pack(side="right", padx=4)
        self.fig_tdr = Figure(figsize=(10, 5.5), dpi=100)
        self.ax_tdr_rho = self.fig_tdr.add_subplot(211)
        self.ax_tdr_z = self.fig_tdr.add_subplot(212)
        self.canvas_tdr = FigureCanvasTkAgg(self.fig_tdr, master=self.frames["tdr"])
        self.canvas_tdr.get_tk_widget().pack(fill="both", expand=True)

        for canvas in (self.canvas_s, self.canvas_s12, self.canvas_s22, self.canvas_z, self.canvas_smith):
            canvas.mpl_connect("button_press_event", self.on_plot_click)
        self._initialize_plot_artists()

    def _initialize_plot_artists(self) -> None:
        frequency_axes = (
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_s12_db, self.ax_s12_phase, self.ax_s22_db, self.ax_s22_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
            self.ax_tdr_rho, self.ax_tdr_z,
        )
        for ax in frequency_axes:
            ax.clear()
            ax.grid(True, alpha=0.35)
        for ax, ylabel in (
            (self.ax_s11_db, "dB"), (self.ax_s21_db, "dB"),
            (self.ax_s11_phase, "°"), (self.ax_s21_phase, "°"),
            (self.ax_s12_db, "dB"), (self.ax_s12_phase, "°"),
            (self.ax_s22_db, "dB"), (self.ax_s22_phase, "°"),
        ):
            ax.set_ylabel(ylabel)
        self.ax_z11_r.set_ylabel("Ω")
        self.ax_z11_x.set_ylabel("Ω")
        self.ax_vswr.set_ylabel("VSWR")
        self.ax_tdr_rho.set_ylabel("|ρ|")
        self.ax_tdr_z.set_ylabel("Ω")
        self._apply_plot_language()

        (self.line_s11_db,) = self.ax_s11_db.plot([], [], label="S11", lw=1.6)
        (self.line_s21_db,) = self.ax_s21_db.plot([], [], label="S21", lw=1.6)
        (self.line_s11_phase,) = self.ax_s11_phase.plot([], [], label="S11", lw=1.6)
        (self.line_s21_phase,) = self.ax_s21_phase.plot([], [], label="S21", lw=1.6)
        (self.line_s12_db,) = self.ax_s12_db.plot([], [], label="S12", lw=1.6)
        (self.line_s12_phase,) = self.ax_s12_phase.plot([], [], label="S12", lw=1.6)
        (self.line_s22_db,) = self.ax_s22_db.plot([], [], label="S22", lw=1.6)
        (self.line_s22_phase,) = self.ax_s22_phase.plot([], [], label="S22", lw=1.6)
        self.mem_line_s11_db = self.ax_s11_db.plot([], [], "--", label="Mem S11", alpha=0.65)[0]
        self.mem_line_s21_db = self.ax_s21_db.plot([], [], "--", label="Mem S21", alpha=0.65)[0]
        self.mem_line_s11_phase = self.ax_s11_phase.plot([], [], "--", label="Mem S11", alpha=0.65)[0]
        self.mem_line_s21_phase = self.ax_s21_phase.plot([], [], "--", label="Mem S21", alpha=0.65)[0]
        self.mem_line_s12_db = self.ax_s12_db.plot([], [], "--", label="Mem S12", alpha=0.65)[0]
        self.mem_line_s12_phase = self.ax_s12_phase.plot([], [], "--", label="Mem S12", alpha=0.65)[0]
        self.mem_line_s22_db = self.ax_s22_db.plot([], [], "--", label="Mem S22", alpha=0.65)[0]
        self.mem_line_s22_phase = self.ax_s22_phase.plot([], [], "--", label="Mem S22", alpha=0.65)[0]
        self.line_z11_r = self.ax_z11_r.plot([], [], label="R(S11)", lw=1.5)[0]
        self.line_z11_x = self.ax_z11_x.plot([], [], label="X(S11)", lw=1.5)[0]
        self.line_vswr = self.ax_vswr.plot([], [], label="VSWR", lw=1.5)[0]
        self.line_tdr_rho = self.ax_tdr_rho.plot([], [], lw=1.5)[0]
        self.line_tdr_z = self.ax_tdr_z.plot([], [], lw=1.5)[0]

        self._draw_smith_grid()
        self.line_smith_s11 = self.ax_smith.plot([], [], label="S11", lw=1.5)[0]
        self.line_smith_s22 = self.ax_smith.plot([], [], label="S22", lw=1.3, ls="--")[0]
        self.smith_marker_artists = {
            "M1": self.ax_smith.plot([], [], marker="o", ms=14, mew=2.2, mec="white", mfc="#ff3b30", ls="None", label="M1", zorder=8)[0],
            "M2": self.ax_smith.plot([], [], marker="D", ms=13, mew=2.0, mec="#111111", mfc="#ffd60a", ls="None", label="M2", zorder=8)[0],
        }
        self.smith_marker_labels = {
            "M1": self.ax_smith.annotate("M1", (0, 0), xytext=(9, 9), textcoords="offset points", fontsize=10, fontweight="bold", color="#ff3b30", visible=False, zorder=9),
            "M2": self.ax_smith.annotate("M2", (0, 0), xytext=(9, -15), textcoords="offset points", fontsize=10, fontweight="bold", color="#ffd60a", visible=False, zorder=9),
        }
        self.marker_lines: dict[str, list[Any]] = {"M1": [], "M2": []}
        marker_axes = (
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_s12_db, self.ax_s12_phase, self.ax_s22_db, self.ax_s22_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
        )
        for marker, line_style in (("M1", ":"), ("M2", "--")):
            for ax in marker_axes:
                self.marker_lines[marker].append(ax.axvline(np.nan, ls=line_style, lw=1.1, alpha=0.8))
        self._update_legends()
        for figure in (self.fig_s, self.fig_s12, self.fig_s22, self.fig_tdr, self.fig_smith):
            figure.tight_layout()
        self.fig_z.subplots_adjust(left=0.07, right=0.985, bottom=0.1, top=0.95, wspace=0.2, hspace=0.52)

    def _apply_plot_language(self) -> None:
        frequency = self.tr("axis_frequency_mhz", "Frequency [MHz]")
        distance = self.tr("axis_distance_m", "Distance [m]")
        phase_template = self.tr("phase_title", "{parameter} phase")
        for axis, title in (
            (self.ax_s11_db, "S11 [dB]"),
            (self.ax_s21_db, "S21 [dB]"),
            (self.ax_s11_phase, phase_template.format(parameter="S11")),
            (self.ax_s21_phase, phase_template.format(parameter="S21")),
            (self.ax_s12_db, "S12 [dB]"),
            (self.ax_s12_phase, phase_template.format(parameter="S12")),
            (self.ax_s22_db, "S22 [dB]"),
            (self.ax_s22_phase, phase_template.format(parameter="S22")),
        ):
            axis.set_title(title)
            axis.set_xlabel(frequency)
        self.ax_z11_r.set_title("Re{Z(S11)}")
        self.ax_z11_x.set_title("Im{Z(S11)}")
        self.ax_vswr.set_title(self.tr("vswr_from_s11", "VSWR from S11"))
        self.ax_z11_r.set_xlabel(frequency)
        self.ax_z11_x.set_xlabel(frequency)
        self.ax_vswr.set_xlabel(frequency)
        self.ax_smith.set_title(self.tr("smith_chart_title", "Smith chart"))
        tdr_axis = self.tr("axis_time_ns", "Time [ns]") if self.tdr_axis_mode_var.get() == "time" else distance
        self.ax_tdr_rho.set_title("TDR — |ρ(t)|")
        self.ax_tdr_z.set_title("TDR — Re{Z}")
        self.ax_tdr_rho.set_xlabel(tdr_axis)
        self.ax_tdr_z.set_xlabel(tdr_axis)

    def _draw_smith_grid(self) -> None:
        self.ax_smith.clear()
        self.ax_smith.set_title(self.tr("smith_chart_title", "Smith chart"))
        self.ax_smith.set_xlabel("Re(Γ)")
        self.ax_smith.set_ylabel("Im(Γ)")
        self.ax_smith.set_aspect("equal", adjustable="box")
        theta = np.linspace(0, 2 * np.pi, 600)
        self.ax_smith.plot(np.cos(theta), np.sin(theta), lw=1.0)
        for r in (0, 0.2, 0.5, 1, 2, 5):
            x = np.linspace(-20, 20, 1000)
            z = r + 1j * x
            g = (z - 1) / (z + 1)
            self.ax_smith.plot(g.real, g.imag, lw=0.4, alpha=0.35)
        for x0 in (0.2, 0.5, 1, 2, 5):
            r = np.linspace(0, 20, 1000)
            for sign in (1, -1):
                z = r + 1j * sign * x0
                g = (z - 1) / (z + 1)
                self.ax_smith.plot(g.real, g.imag, lw=0.4, alpha=0.35)
        self.ax_smith.set_xlim(-1.08, 1.08)
        self.ax_smith.set_ylim(-1.08, 1.08)
        self.ax_smith.grid(True, alpha=0.25)

    def _update_legends(self) -> None:
        for ax in (
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_s12_db, self.ax_s12_phase, self.ax_s22_db, self.ax_s22_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr, self.ax_smith,
        ):
            try:
                ax.legend(loc="best", fontsize=8)
            except Exception:
                pass

    def _ensure_indicator_styles(self) -> None:
        if hasattr(self, "_indicator_images"):
            return
        names = (
            "check_off", "check_on", "check_off_disabled", "check_on_disabled",
            "radio_off", "radio_on", "radio_off_disabled", "radio_on_disabled",
        )
        self._indicator_images = {name: tk.PhotoImage(width=16, height=16) for name in names}
        self.style.element_create(
            "AutoRS.Check.indicator", "image", self._indicator_images["check_off"],
            ("disabled", "selected", self._indicator_images["check_on_disabled"]),
            ("disabled", self._indicator_images["check_off_disabled"]),
            ("selected", self._indicator_images["check_on"]),
            border=0, sticky="",
        )
        self.style.element_create(
            "AutoRS.Radio.indicator", "image", self._indicator_images["radio_off"],
            ("disabled", "selected", self._indicator_images["radio_on_disabled"]),
            ("disabled", self._indicator_images["radio_off_disabled"]),
            ("selected", self._indicator_images["radio_on"]),
            border=0, sticky="",
        )
        self.style.layout("TCheckbutton", [(
            "Checkbutton.padding", {"sticky": "nswe", "children": [
                ("AutoRS.Check.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "nswe"}),
            ]},
        )])
        self.style.layout("TRadiobutton", [(
            "Radiobutton.padding", {"sticky": "nswe", "children": [
                ("AutoRS.Radio.indicator", {"side": "left", "sticky": ""}),
                ("Radiobutton.label", {"side": "left", "sticky": "nswe"}),
            ]},
        )])

    @staticmethod
    def _fill_image(image: tk.PhotoImage, color: str, x1: int, y1: int, x2: int, y2: int) -> None:
        image.put(color, to=(x1, y1, x2, y2))

    def _paint_indicator_images(self, palette: dict[str, str]) -> None:
        self._ensure_indicator_styles()
        bg = palette["bg"]
        border = palette["border"]
        accent = palette["accent"]
        disabled_bg = palette["disabled_bg"]
        disabled_fg = palette["disabled_fg"]
        images = self._indicator_images
        for name, image in images.items():
            image.blank()
            self._fill_image(image, bg, 0, 0, 16, 16)
            if "disabled" in name:
                self._fill_image(image, disabled_bg, 1, 1, 15, 15)
        for name in ("check_off", "check_on"):
            image = images[name]
            self._fill_image(image, border, 1, 1, 15, 15)
            self._fill_image(image, bg if name == "check_off" else "#f2f2f2", 2, 2, 14, 14)
        for name in ("check_off_disabled", "check_on_disabled"):
            image = images[name]
            self._fill_image(image, disabled_fg, 1, 1, 15, 15)
            self._fill_image(image, disabled_bg, 2, 2, 14, 14)
        check_points = (
            (4, 4), (5, 5), (6, 6), (7, 7), (8, 8), (9, 9), (10, 10), (11, 11),
            (11, 4), (10, 5), (9, 6), (8, 7), (7, 8), (6, 9), (5, 10), (4, 11),
        )
        for image_name, color in (("check_on", "#202020"), ("check_on_disabled", disabled_fg)):
            image = images[image_name]
            for x, y in check_points:
                self._fill_image(image, color, x, y, x + 2, y + 2)
        for name in ("radio_off", "radio_on", "radio_off_disabled", "radio_on_disabled"):
            image = images[name]
            edge = disabled_fg if "disabled" in name else border
            inside = disabled_bg if "disabled" in name else bg
            for y in range(16):
                for x in range(16):
                    distance = ((x - 7.5) ** 2 + (y - 7.5) ** 2) ** 0.5
                    if 6.0 <= distance <= 7.0:
                        image.put(edge, (x, y))
                    elif distance < 6.0:
                        image.put(inside, (x, y))
            if "on" in name:
                dot = disabled_fg if "disabled" in name else accent
                for y in range(5, 11):
                    for x in range(5, 11):
                        if ((x - 7.5) ** 2 + (y - 7.5) ** 2) ** 0.5 <= 3.0:
                            image.put(dot, (x, y))

    def apply_theme(self, theme: str, persist: bool = True) -> None:
        if theme not in {"light", "dark"}:
            return
        dark = theme == "dark"
        palette = ({
            "bg": "#171a1f", "panel": "#222730", "panel2": "#2b313c", "entry": "#303744",
            "fg": "#f5f7fa", "muted": "#c3cad5", "accent": "#4da3ff", "pressed": "#2f78c4",
            "border": "#667085", "disabled_bg": "#252a33", "disabled_fg": "#8b95a5", "grid": "#6b7280",
        } if dark else {
            "bg": "#eef1f5", "panel": "#ffffff", "panel2": "#e4e9f0", "entry": "#ffffff",
            "fg": "#172033", "muted": "#4b5565", "accent": "#1877d1", "pressed": "#0f5fae",
            "border": "#8c98a8", "disabled_bg": "#e5e7eb", "disabled_fg": "#8a94a3", "grid": "#8d99a8",
        })
        self.theme_palette = palette
        bg, panel, panel2, fg, entry, accent = palette["bg"], palette["panel"], palette["panel2"], palette["fg"], palette["entry"], palette["accent"]
        self.root.configure(bg=bg)
        self.style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 9))
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabel", background=bg, foreground=fg)
        self.style.configure("Info.TLabel", background=bg, foreground=palette["muted"], font=("Segoe UI", 9))
        self.style.configure("MarkerDelta.TLabel", background=bg, foreground=accent, font=("Segoe UI", 8, "bold"))
        self.style.configure("CompactMarker.TLabel", background=bg, foreground=fg, font=("Segoe UI", 8))
        self.style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=palette["border"])
        self.style.configure("TLabelframe.Label", background=bg, foreground=fg, font=("Segoe UI", 9, "bold"))
        self.style.configure("TButton", background=panel2, foreground=fg, bordercolor=palette["border"], padding=(5, 2), relief="flat")
        self.style.map("TButton", background=[("pressed", palette["pressed"]), ("active", accent), ("disabled", palette["disabled_bg"])], foreground=[("pressed", "#ffffff"), ("active", "#ffffff"), ("disabled", palette["disabled_fg"])])
        self._paint_indicator_images(palette)
        for style_name in ("TCheckbutton", "TRadiobutton"):
            self.style.configure(style_name, background=bg, foreground=fg, focuscolor=bg, focusthickness=0, padding=(2, 2))
            self.style.map(
                style_name,
                background=[("disabled", bg), ("pressed", bg), ("active", bg), ("selected", bg)],
                foreground=[("disabled", palette["disabled_fg"]), ("pressed", fg), ("active", fg), ("selected", fg)],
            )
        for style_name in ("TEntry", "TSpinbox"):
            self.style.configure(style_name, fieldbackground=entry, foreground=fg, insertcolor=fg, bordercolor=palette["border"], padding=3)
        self.style.configure("TCombobox", fieldbackground=entry, background=panel2, foreground=fg, arrowcolor=fg, bordercolor=palette["border"], padding=3)
        self.style.configure("TNotebook", background=bg, bordercolor=palette["border"])
        self.style.configure("TNotebook.Tab", background=panel2, foreground=fg, padding=(9, 4))
        self.style.configure(
            "Vertical.TScrollbar",
            background=panel2, troughcolor=bg, arrowcolor=fg, bordercolor=palette["border"],
        )
        self.style.configure(
            "Horizontal.TScrollbar",
            background=panel2, troughcolor=bg, arrowcolor=fg, bordercolor=palette["border"],
        )
        self.style.map("TNotebook.Tab", background=[("selected", accent), ("active", palette["pressed"])], foreground=[("selected", "#ffffff"), ("active", "#ffffff")])
        self.style.configure("Calibration.Bad.TLabel", background=bg, foreground="#ffb020", font=("Segoe UI", 9, "bold"))
        self.style.configure("Calibration.Good.TLabel", background=bg, foreground="#32d74b", font=("Segoe UI", 9, "bold"))




        self.root.option_add("*Text.background", panel)
        self.root.option_add("*Text.foreground", fg)
        self.root.option_add("*Text.insertBackground", fg)
        self.root.option_add("*Text.selectBackground", accent)
        self.root.option_add("*Text.selectForeground", "#ffffff")
        self.root.option_add("*Canvas.background", bg)
        self.root.option_add("*TCombobox*Listbox.background", entry)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        for text_widget in (self.terminal_output,):
            text_widget.configure(bg=panel, fg=fg, insertbackground=fg, selectbackground=accent, selectforeground="#ffffff", relief="flat")
        figures = (self.fig_s, self.fig_s12, self.fig_s22, self.fig_z, self.fig_smith, self.fig_tdr) if hasattr(self, "fig_s") else ()
        for figure in figures:
            figure.patch.set_facecolor(panel)
            for ax in figure.axes:
                ax.set_facecolor(panel)
                ax.tick_params(colors=fg, labelsize=9)
                ax.xaxis.label.set_color(fg); ax.yaxis.label.set_color(fg); ax.title.set_color(fg)
                for spine in ax.spines.values():
                    spine.set_color(palette["border"])
                ax.grid(True, color=palette["grid"], alpha=0.28, linewidth=0.7)
                legend = ax.get_legend()
                if legend:
                    legend.get_frame().set_facecolor(panel2); legend.get_frame().set_edgecolor(palette["border"])
                    for item in legend.get_texts(): item.set_color(fg)
        if hasattr(self, "line_s11_db"):
            colors = {"s11": "#4da3ff", "s21": "#ff9f0a", "s12": "#bf5af2", "s22": "#30d158"}
            for name in ("s11", "s21", "s12", "s22"):
                for suffix in ("db", "phase"):
                    getattr(self, f"line_{name}_{suffix}").set_color(colors[name])
            self.line_smith_s11.set_color(colors["s11"]); self.line_smith_s22.set_color(colors["s22"])
        for canvas in getattr(self, "_all_canvases", lambda: [])():
            canvas.draw_idle()
        if persist:
            self.settings.set("theme", theme)

    def apply_language(self, language: str, persist: bool = True) -> None:
        if getattr(self, "_language_update_active", False):
            return
        self._language_update_active = True
        try:
            if self.language_var.get() != language:
                self.language_var.set(language)
            self.tr.load(language)
            self.root.title(self.tr("app_title", "AutoRS VNA"))
            texts = {
                "connect": "disconnect" if self.connected else "connect", "start": "stop" if self.running else "start",
                "single": "single", "range": "range", "calibration": "calibration", "refresh_label": "refresh_ms",
                "cb_impedance": "impedance", "cb_vswr": "vswr", "cb_tdr": "tdr", "cb_memory": "memory",
                "trace_to_mem": "trace_to_mem", "clear_memory": "clear_memory", "tdr_settings": "tdr_settings",
                "tdr_settings_tab": "tdr_settings", "save_settings": "save_settings", "save_csv": "save_measurement",
                "auto_measurements": "auto_measurements", "overlay": "overlay_files", "clear_overlays": "clear_overlays",
                "report": "report", "clear_markers": "clear_markers", "theme_label": "theme", "language_label": "language",
                "refresh_plugins": "refresh_plugins",
            }
            for widget_key, text_key in texts.items():
                widget = self.widgets.get(widget_key)
                if widget:
                    widget.configure(text=self.tr(text_key))
            self.widgets["host_label"].configure(text=self.tr("port", "IP / host"))
            self.widgets["tcp_port_label"].configure(text="TCP")
            self.widgets["terminal_send"].configure(text=self.tr("send", "Wyślij"))
            self.device_section.configure(text=self.tr("device_section", "Urządzenie i pomiar"))
            self.measurement_section.configure(text=self.tr("measurement_section", "Kanały, pamięć i TDR"))
            self.tools_section.configure(text=self.tr("tools_section", "Pliki i narzędzia"))
            self.plugins_section.configure(text=self.tr("plugins", "Wtyczki"))
            self.refresh_plugins()
            tab_names = {
                "sparams": self.tr("tab_sparams", "S11 / S21"), "s12": "S12", "s22": "S22",
                "impedance": self.tr("tab_impedance", "Impedancja / VSWR"), "smith": self.tr("tab_smith", "Smith"),
                "tdr": self.tr("tab_tdr", "TDR"), "terminal": self.tr("tab_terminal", "Terminal ZVL"),
            }
            for frame_key, title in tab_names.items():
                self.notebook.tab(self.frames[frame_key], text=title)
            self.marker_bar.configure(text=self.tr("marker"))
            self._apply_plot_language()
            for canvas in self._all_canvases():
                canvas.draw_idle()
            self._update_marker_delta()
            if not self.tdr_data:
                self.tdr_info_var.set(self.tr("tdr_info_no_data", "Włącz S11 i TDR, aby obliczyć odpowiedź w dziedzinie czasu."))
            self._set_calibration_status(bool(self.calibration))
            if self.connected and len(self.freq):
                self.status_var.set(self.tr("status_connected").format(
                    host=self.host_var.get().strip(),
                    port=self.tcp_port_var.get(),
                    start=f"{self.freq[0] / 1e6:.6g}",
                    stop=f"{self.freq[-1] / 1e6:.6g}",
                    points=len(self.freq),
                    channel=f"{self.vna.channel_number} ({self.vna.channel_name})",
                ))
            elif not self.connected:
                self.status_var.set(self.tr("not_connected"))
            if persist:
                self.settings.set("language", language)

        finally:
            self._language_update_active = False

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {text}\n")
        except OSError:
            pass

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def refresh_ports(self) -> None:
        if not self.host_var.get().strip():
            self.set_status(self.tr("status_enter_address"))

    def toggle_connection(self) -> None:
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        if self.connecting:
            return
        host = self.host_var.get().strip()
        try:
            port = int(self.tcp_port_var.get())
        except Exception:
            messagebox.showwarning(self.tr("warning"), self.tr("error_invalid_tcp_port"), parent=self.root)
            return
        if not host:
            messagebox.showwarning(self.tr("warning"), self.tr("error_enter_host"), parent=self.root)
            return

        self.connecting = True
        self.widgets["connect"].state(["disabled"])
        self.set_status(self.tr("status_connecting").format(host=host, port=port))

        def work():
            info = self.vna.connect(host, port, timeout=30.0)
            frequency = self.vna.get_frequencies("S11")
            calibrated = self.vna.correction_enabled()
            return info, frequency, calibrated, list(self.vna.connection_warnings)

        def ok(result):
            info, frequency, calibrated, warnings = result
            self.connecting = False
            self.widgets["connect"].state(["!disabled"])
            self.connected = True
            self.freq = np.asarray(frequency, float)
            self.settings.data["last_host"] = host
            self.settings.data["tcp_port"] = port
            self.settings.save()
            self.calibration = {"source": "R&S ZVL13", "enabled": True} if calibrated else None
            self._set_calibration_status(calibrated)
            self.apply_language(self.language_var.get(), persist=False)
            status = self.tr("status_connected").format(
                host=host,
                port=port,
                start=f"{self.freq[0]/1e6:.6g}",
                stop=f"{self.freq[-1]/1e6:.6g}",
                points=len(self.freq),
                channel=f"{self.vna.channel_number} ({self.vna.channel_name})",
            )
            if warnings:
                status += " | " + self.tr("status_scpi_warnings").format(count=len(warnings))
            self.set_status(status + f" | {info.idn}")
            for warning in warnings:
                self.log(f"SCPI configuration warning: {warning}")

            if self.pending_start_after_connect:
                self.pending_start_after_connect = False
                self.start_running()
            elif self.pending_single_after_connect:
                self.pending_single_after_connect = False
                self.single_measurement()
            else:
                self.read_once(schedule_after=False)

        def failed(exc):
            self.connecting = False
            self.widgets["connect"].state(["!disabled"])
            self.connected = False
            self.pending_start_after_connect = False
            self.pending_single_after_connect = False
            messagebox.showerror(self.tr("error"), str(exc), parent=self.root)
            self.set_status(self.tr("status_connection_error").format(error=exc))

        self._submit_device_task(work, ok, failed, self.tr("task_connection"))

    def disconnect(self) -> None:
        self.stop_running()
        self.connected = False
        self.connecting = False
        self.pending_start_after_connect = False
        self.pending_single_after_connect = False
        self.calibration = None
        self._set_calibration_status(False)

        self.vna.interrupt()
        self.apply_language(self.language_var.get(), persist=False)
        self.set_status(self.tr("not_connected"))

    def toggle_running(self) -> None:
        if self.running:
            self.stop_running()
        else:
            self.start_running()

    def start_running(self) -> None:
        if not self.connected:
            self.pending_start_after_connect = True
            self.connect()
            return
        if not any(variable.get() for variable in (self.enable_s11_var, self.enable_s21_var, self.enable_s12_var, self.enable_s22_var)):
            messagebox.showwarning(self.tr("warning"), self.tr("error_enable_sparameter"), parent=self.root)
            return
        self.running = True
        self.apply_language(self.language_var.get(), persist=False)
        self.set_status(self.tr("status_auto_read_on"))
        self.read_once(schedule_after=True)

    def stop_running(self) -> None:
        self.running = False
        if self.after_id:
            try:
                self.root.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None
        self.apply_language(self.language_var.get(), persist=False)
        self.set_status(self.tr("status_measurement_stopped"))

    def single_measurement(self) -> None:
        if not self.connected:
            self.pending_single_after_connect = True
            self.connect()
            return
        self.read_once(schedule_after=False)

    def read_once(self, schedule_after: bool = False, completion_callback=None, failure_callback=None) -> None:
        if self.measurement_pending:
            if schedule_after and self.running and not self.after_id:
                self.after_id = self.root.after(100, lambda: self.read_once(True, completion_callback, failure_callback))
            return
        if not self.connected:
            if failure_callback:
                failure_callback(RuntimeError(self.tr("error_device_not_connected")))
            return

        enabled = (
            self.enable_s11_var.get(),
            self.enable_s21_var.get(),
            self.enable_s12_var.get(),
            self.enable_s22_var.get(),
        )
        self.measurement_pending = True
        self.worker_busy = True
        self.after_id = None
        started = time.monotonic()

        def work():
            return self.vna.read_measurement(*enabled)

        def ok(result):
            self._measurement_ready(*result, started, schedule_after, completion_callback, failure_callback)

        def failed(exc):
            self._measurement_failed(exc, schedule_after, failure_callback)

        self._submit_device_task(work, ok, failed, self.tr("task_measurement_read"))

    def _measurement_ready(self, f, raw11, raw21, raw12, raw22, started: float, schedule_after: bool, completion_callback=None, failure_callback=None) -> None:
        self.worker_busy = False
        self.measurement_pending = False
        self.freq = np.asarray(f, float)
        for name, raw in (("s11", raw11), ("s21", raw21), ("s12", raw12), ("s22", raw22)):
            values = np.asarray(raw, complex) if raw is not None else np.array([], complex)
            setattr(self, f"raw_{name}", values)
            setattr(self, name, values.copy())
        entry = {name: getattr(self, name).copy() for name in ("s11", "s21", "s12", "s22") if len(getattr(self, name)) == len(self.freq)}
        if entry:
            self.history.append(entry)
        self.update_plots()
        elapsed = time.monotonic() - started
        metrics = self.current_metrics()
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window.on_measurement(metrics, elapsed, started)
        cal = self.tr("calibrated") if self.calibration else self.tr("uncalibrated")
        self.status_var.set(self.tr("status_measurement_summary").format(points=len(self.freq), start=f"{self.freq[0]/1e6:.6g}", stop=f"{self.freq[-1]/1e6:.6g}", elapsed=f"{elapsed:.2f}", calibration=cal))
        if completion_callback:
            completion_callback()
        if schedule_after and self.running:
            self.after_id = self.root.after(max(100, int(self.interval_var.get() or 800)), lambda: self.read_once(True))

    def _measurement_failed(self, exc: Exception, schedule_after: bool, failure_callback=None) -> None:
        self.worker_busy = False
        self.measurement_pending = False
        self.set_status(self.tr("status_read_error").format(error=exc))
        if failure_callback:
            failure_callback(exc)
        if not self.vna.is_open:
            self.connected = False
            self.running = False
            self.apply_language(self.language_var.get(), persist=False)
            self.set_status(self.tr("status_connection_closed").format(error=exc))
            return
        if schedule_after and self.running:
            self.after_id = self.root.after(max(300, int(self.interval_var.get() or 800)), lambda: self.read_once(True))

    def update_plots(self) -> None:
        if not hasattr(self, "line_s11_db"):
            return
        f_mhz = self.freq / 1e6 if len(self.freq) else np.array([])
        traces = {name: getattr(self, name) for name in ("s11", "s21", "s12", "s22")}
        for name, values in traces.items():
            has = len(values) == len(self.freq) and len(self.freq) > 0
            getattr(self, f"line_{name}_db").set_data(f_mhz if has else [], complex_to_db(values) if has else [])
            getattr(self, f"line_{name}_phase").set_data(f_mhz if has else [], phase_deg(values) if has else [])
            memory = getattr(self, f"mem_{name}")
            show_mem = self.show_memory_var.get() and memory is not None and len(memory) == len(self.freq)
            getattr(self, f"mem_line_{name}_db").set_data(f_mhz if show_mem else [], complex_to_db(memory) if show_mem else [])
            getattr(self, f"mem_line_{name}_phase").set_data(f_mhz if show_mem else [], phase_deg(memory) if show_mem else [])

        has11 = len(self.s11) == len(self.freq) and len(self.freq) > 0
        has22 = len(self.s22) == len(self.freq) and len(self.freq) > 0
        if has11 and self.show_impedance_var.get():
            z11 = gamma_to_impedance(self.s11, self.z0_var.get())
            self.line_z11_r.set_data(f_mhz, z11.real); self.line_z11_x.set_data(f_mhz, z11.imag)
        else:
            self.line_z11_r.set_data([], []); self.line_z11_x.set_data([], [])
        self.line_vswr.set_data(f_mhz, vswr_from_s11(self.s11, clip=100.0)) if has11 and self.show_vswr_var.get() else self.line_vswr.set_data([], [])
        self.line_smith_s11.set_data(self.s11.real if has11 else [], self.s11.imag if has11 else [])
        self.line_smith_s22.set_data(self.s22.real if has22 else [], self.s22.imag if has22 else [])

        if has11 and self.show_tdr_var.get():
            try:
                self.tdr_data = calculate_tdr(self.freq, self.s11, velocity_factor=float(self.vf_var.get()), window=self.tdr_window_var.get(), z0=float(self.z0_var.get()), zero_offset_m=float(self.tdr_zero_offset_var.get()), tr=self.tr)
                tdr_x = self.tdr_data["time_s"] * 1e9 if self.tdr_axis_mode_var.get() == "time" else self.tdr_data["distance_m"]
                axis_label = self.tr("axis_time_ns", "Time [ns]") if self.tdr_axis_mode_var.get() == "time" else self.tr("axis_distance_m", "Distance [m]")
                self.ax_tdr_rho.set_xlabel(axis_label); self.ax_tdr_z.set_xlabel(axis_label)
                self.line_tdr_rho.set_data(tdr_x, np.abs(self.tdr_data["rho"]))
                ztdr = np.real(self.tdr_data["z_ohm"])
                self.line_tdr_z.set_data(tdr_x, np.where(np.isfinite(ztdr), ztdr, np.nan))
                resolution = f"{self.tdr_data['resolution_s']*1e9:.4g} ns" if self.tdr_axis_mode_var.get() == "time" else f"{self.tdr_data['resolution_m']:.4g} m"
                range_text = f"{self.tdr_data['max_unambiguous_time_s']*1e9:.4g} ns" if self.tdr_axis_mode_var.get() == "time" else f"{self.tdr_data['max_unambiguous_distance_m']:.4g} m"
                self.tdr_info_var.set(self.tr("tdr_runtime_info").format(resolution=resolution, range=range_text, bandwidth=f"{self.tdr_data['bandwidth_hz']/1e6:.4g}"))
            except Exception as exc:
                self.tdr_data = {}; self.line_tdr_rho.set_data([], []); self.line_tdr_z.set_data([], []); self.tdr_info_var.set(f"TDR: {exc}")
        else:
            self.tdr_data = {}; self.line_tdr_rho.set_data([], []); self.line_tdr_z.set_data([], [])
            self.tdr_info_var.set(self.tr("tdr_info_no_data", "Włącz S11 i TDR, aby obliczyć odpowiedź w dziedzinie czasu."))

        self._update_marker_artists()
        axes = (
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_s12_db, self.ax_s12_phase, self.ax_s22_db, self.ax_s22_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr, self.ax_tdr_rho, self.ax_tdr_z,
        )
        for ax in axes:
            ax.relim(); ax.autoscale_view()
        if self.overlays_dirty:
            self.redraw_overlays(); self.overlays_dirty = False
        for canvas in self._all_canvases():
            canvas.draw_idle()

    def redraw_overlays(self) -> None:
        for artist in self.overlay_artists:
            try: artist.remove()
            except Exception: pass
        self.overlay_artists.clear()
        axis_map = {
            "s11": (self.ax_s11_db, self.ax_s11_phase), "s21": (self.ax_s21_db, self.ax_s21_phase),
            "s12": (self.ax_s12_db, self.ax_s12_phase), "s22": (self.ax_s22_db, self.ax_s22_phase),
        }
        for overlay in self.overlays:
            f_mhz = np.asarray(overlay["frequency_hz"]) / 1e6
            label = Path(overlay["path"]).stem
            for name, (ax_db, ax_phase) in axis_map.items():
                values = overlay.get(name)
                if values is not None:
                    self.overlay_artists.append(ax_db.plot(f_mhz, complex_to_db(values), lw=1.0, alpha=0.7, label=label)[0])
                    self.overlay_artists.append(ax_phase.plot(f_mhz, phase_deg(values), lw=1.0, alpha=0.7, label=label)[0])
            if overlay.get("s11") is not None:
                values = overlay["s11"]
                self.overlay_artists.append(self.ax_smith.plot(values.real, values.imag, lw=0.9, alpha=0.6, label=label + " S11")[0])
                impedance = gamma_to_impedance(values, self.z0_var.get())
                self.overlay_artists.append(self.ax_z11_r.plot(f_mhz, impedance.real, lw=0.9, alpha=0.6, label=label)[0])
                self.overlay_artists.append(self.ax_z11_x.plot(f_mhz, impedance.imag, lw=0.9, alpha=0.6, label=label)[0])
            if overlay.get("s22") is not None:
                values = overlay["s22"]
                self.overlay_artists.append(self.ax_smith.plot(values.real, values.imag, lw=0.9, alpha=0.6, label=label + " S22")[0])
        self._update_legends()

    def on_plot_click(self, event) -> None:
        if len(self.freq) == 0 or event.xdata is None:
            return
        freq_axes = {
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_s12_db, self.ax_s12_phase, self.ax_s22_db, self.ax_s22_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
        }
        if event.inaxes in freq_axes:
            idx = nearest_index(self.freq, float(event.xdata) * 1e6)
        elif event.inaxes == self.ax_smith and event.ydata is not None and len(self.s11):
            idx = int(np.argmin((self.s11.real - event.xdata) ** 2 + (self.s11.imag - event.ydata) ** 2))
        else:
            return
        self.markers[self.active_marker_var.get()] = idx
        self.update_plots()

    def _update_marker_artists(self) -> None:
        for marker in ("M1", "M2"):
            idx = self.markers.get(marker)
            if idx is None or len(self.freq) == 0:
                for line in self.marker_lines[marker]:
                    line.set_xdata([np.nan, np.nan])
                self.smith_marker_artists[marker].set_data([], [])
                if hasattr(self, "smith_marker_labels"):
                    self.smith_marker_labels[marker].set_visible(False)
                self.marker_vars[marker].set(f"{marker}: --")
                continue
            idx = max(0, min(int(idx), len(self.freq) - 1))
            self.markers[marker] = idx
            f_mhz = self.freq[idx] / 1e6
            for line in self.marker_lines[marker]:
                line.set_xdata([f_mhz, f_mhz])
            if len(self.s11) == len(self.freq):
                x = float(self.s11[idx].real)
                y = float(self.s11[idx].imag)
                self.smith_marker_artists[marker].set_data([x], [y])
                if hasattr(self, "smith_marker_labels"):
                    label = self.smith_marker_labels[marker]
                    label.xy = (x, y)
                    label.set_visible(True)
            else:
                self.smith_marker_artists[marker].set_data([], [])
                if hasattr(self, "smith_marker_labels"):
                    self.smith_marker_labels[marker].set_visible(False)
            self.marker_vars[marker].set(self._format_marker(marker, idx))
        self._update_marker_delta()

    def _update_marker_delta(self) -> None:
        i1, i2 = self.markers.get("M1"), self.markers.get("M2")
        if i1 is None or i2 is None or not len(self.freq):
            self.marker_delta_var.set(self.tr("no_marker_delta", "Δ(M2−M1): ustaw oba znaczniki")); return
        i1 = max(0, min(int(i1), len(self.freq)-1)); i2 = max(0, min(int(i2), len(self.freq)-1))
        parts = [f"Δ(M2−M1): Δf={(self.freq[i2]-self.freq[i1])/1e6:+.6g} MHz"]
        for name in ("s11", "s21", "s12", "s22"):
            values = getattr(self, name)
            if len(values) == len(self.freq):
                db = complex_to_db(values); parts.append(f"Δ{name.upper()}={db[i2]-db[i1]:+.3f} dB")
        if len(self.s11) == len(self.freq):
            z = gamma_to_impedance(self.s11, self.z0_var.get()); swr = vswr_from_s11(self.s11, clip=None)
            parts.append(f"ΔR={z[i2].real-z[i1].real:+.3g} Ω"); parts.append(f"ΔX={z[i2].imag-z[i1].imag:+.3g} Ω")
            if np.isfinite(swr[i1]) and np.isfinite(swr[i2]): parts.append(f"ΔVSWR={swr[i2]-swr[i1]:+.3f}")
        self.marker_delta_var.set(" | ".join(parts))

    def _format_marker(self, marker: str, idx: int) -> str:
        parts = [f"{marker}: {self.freq[idx]/1e6:.6g} MHz"]
        for name in ("s11", "s21", "s12", "s22"):
            values = getattr(self, name)
            if len(values) == len(self.freq):
                parts.append(f"{name.upper()} {complex_to_db(values)[idx]:.3f} dB/{phase_deg(values)[idx]:.1f}°")
        if len(self.s11) == len(self.freq):
            z = gamma_to_impedance(self.s11[idx], self.z0_var.get())
            swr = vswr_from_s11(np.asarray([self.s11[idx]]), clip=None)[0]
            parts.append(f"Z {z.real:.2f}{z.imag:+.2f}j Ω"); parts.append(f"VSWR {swr:.3f}")
        return " | ".join(parts)

    def _uncertainty_at(self, idx: int) -> dict[str, float]:
        samples = [entry["s11"][idx] for entry in self.history if "s11" in entry and len(entry["s11"]) > idx]
        if len(samples) < 3:
            return {}
        arr = np.asarray(samples, complex)
        db = complex_to_db(arr)
        current = arr[-1]
        phase_error = np.rad2deg(np.angle(arr * np.conj(current)))
        z = gamma_to_impedance(arr, self.z0_var.get())
        return {
            "s11_db": float(np.std(db, ddof=1)),
            "s11_phase": float(np.std(phase_error, ddof=1)),
            "z_real": float(np.std(z.real, ddof=1)),
            "z_imag": float(np.std(z.imag, ddof=1)),
        }

    def clear_markers(self) -> None:
        self.markers = {"M1": None, "M2": None}
        self.update_plots()

    def trace_to_mem(self) -> None:
        if not len(self.freq):
            return
        for name in ("s11", "s21", "s12", "s22"):
            values = getattr(self, name)
            setattr(self, f"mem_{name}", values.copy() if len(values) else None)
        self.show_memory_var.set(True)
        self.update_plots()
        self.set_status(self.tr("status_data_to_mem"))

    def clear_memory(self) -> None:
        for name in ("s11", "s21", "s12", "s22"):
            setattr(self, f"mem_{name}", None)
        self.update_plots()
        self.set_status(self.tr("status_memory_cleared"))

    def open_tdr_settings(self) -> None:
        current_sweep = None
        if len(self.freq) >= 2:
            current_sweep = (float(self.freq[0]), float(self.freq[-1]))
        values = {
            "axis_mode": self.tdr_axis_mode_var.get(),
            "velocity_factor": float(self.vf_var.get()),
            "z0_ohm": float(self.z0_var.get()),
            "window": self.tdr_window_var.get(),
            "max_distance_m": float(self.tdr_max_distance_var.get()),
            "zero_offset_m": float(self.tdr_zero_offset_var.get()),
            "auto_zoom": bool(self.tdr_auto_zoom_var.get()),
        }
        TDRSettingsDialog(self.root, values, self.apply_tdr_settings, current_sweep=current_sweep, tr=self.tr)

    def apply_tdr_settings(self, values: dict[str, Any]) -> None:
        self.tdr_axis_mode_var.set(str(values["axis_mode"]))
        self.vf_var.set(float(values["velocity_factor"]))
        self.z0_var.set(float(values["z0_ohm"]))
        self.tdr_window_var.set(str(values["window"]))
        self.tdr_max_distance_var.set(float(values["max_distance_m"]))
        self.tdr_zero_offset_var.set(float(values["zero_offset_m"]))
        self.tdr_auto_zoom_var.set(bool(values["auto_zoom"]))
        self.settings.data.update({
            "tdr_axis_mode": self.tdr_axis_mode_var.get(),
            "velocity_factor": float(self.vf_var.get()),
            "z0_ohm": float(self.z0_var.get()),
            "tdr_window": self.tdr_window_var.get(),
            "tdr_max_distance_m": float(self.tdr_max_distance_var.get()),
            "tdr_zero_offset_m": float(self.tdr_zero_offset_var.get()),
            "tdr_auto_zoom": bool(self.tdr_auto_zoom_var.get()),
        })
        self.settings.save()
        self.update_plots()

    def _channel_changed(self) -> None:
        if not self.enable_s11_var.get():
            self.show_vswr_var.set(False); self.show_tdr_var.set(False)
        self.update_plots()

    def open_range_dialog(self) -> None:
        if not self.connected or len(self.freq) < 2:
            messagebox.showwarning(self.tr("warning"), self.tr("error_connect_first"), parent=self.root); return
        RangeDialog(self.root, (float(self.freq[0]), float(self.freq[-1]), len(self.freq)), self.set_range, self.tr)

    def set_range(self, start: float, stop: float, points: int, completion_callback=None, failure_callback=None) -> None:
        was_running = self.running
        self.stop_running()
        self.set_status(self.tr("status_range_configuring"))

        def work():
            frequency = self.vna.set_sweep(start, stop, points)
            calibrated = self.vna.correction_enabled()
            return frequency, calibrated

        def done(result):
            frequency, calibrated = result
            self.freq = np.asarray(frequency, float)
            for name in ("s11", "s21", "s12", "s22"):
                setattr(self, name, np.array([], complex))
                setattr(self, f"raw_{name}", np.array([], complex))
                setattr(self, f"mem_{name}", None)
            self.history.clear()
            self.clear_markers()
            self.calibration = {"source": "R&S ZVL13", "enabled": True} if calibrated else None
            self._set_calibration_status(calibrated)
            self.set_status(
                self.tr("status_range_set").format(start=f"{self.freq[0]/1e6:.6g}", stop=f"{self.freq[-1]/1e6:.6g}", points=len(self.freq))
            )
            if was_running:
                self.running = True
                self.apply_language(self.language_var.get(), persist=False)
                self.read_once(True, completion_callback, failure_callback)
            else:
                self.read_once(False, completion_callback, failure_callback)

        def failed(exc):
            if failure_callback:
                failure_callback(exc)
            else:
                messagebox.showerror(self.tr("error"), str(exc), parent=self.root)
            self.set_status(self.tr("status_range_error").format(error=exc))
            if was_running and self.connected:
                self.start_running()

        self._submit_device_task(work, done, failed, self.tr("task_range_change"))

    def _device_id(self) -> str:
        return self.vna.info.serial_number or self.vna.info.host or "unknown"

    def _load_calibration_for_current_range(self, calibrated: Optional[bool] = None) -> None:

        state = bool(calibrated) if calibrated is not None else bool(self.calibration)
        self.calibration = {"source": "R&S ZVL13", "enabled": True} if state else None
        self._set_calibration_status(state)
        self.log("Analyzer correction enabled." if state else "Analyzer correction disabled.")

    def _set_calibration_status(self, calibrated: bool) -> None:
        self.calibration_var.set(self.tr("calibrated") if calibrated else self.tr("uncalibrated"))
        self.calibration_label.configure(style="Calibration.Good.TLabel" if calibrated else "Calibration.Bad.TLabel")

    def start_calibration(self) -> None:
        if not self.connected:
            messagebox.showwarning(self.tr("warning"), self.tr("error_connect_first"), parent=self.root)
            return
        if self.device_busy or self.measurement_pending:
            messagebox.showwarning(
                self.tr("warning"),
                self.tr("error_wait_operation"),
                parent=self.root,
            )
            return

        CalibrationModeDialog(
            self.root,
            self._start_calibration_mode,
            self.tr,
            getattr(self, "theme_palette", None),
        )

    def _start_calibration_mode(self, mode: str) -> None:
        







        configurations = {
            "p1": {
                "label": self.tr("cal_p1_title"),
                "type": "Full One Port P1",
                "begin": lambda: self.vna.begin_full_one_port_calibration(1),
                "steps": [
                    ("OPEN", (1,), self.tr("cal_connect_open_p1")),
                    ("SHORT", (1,), self.tr("cal_connect_short_p1")),
                    ("MATCH", (1,), self.tr("cal_connect_match_p1")),
                ],
            },
            "p2": {
                "label": self.tr("cal_p2_title"),
                "type": "Full One Port P2",
                "begin": lambda: self.vna.begin_full_one_port_calibration(2),
                "steps": [
                    ("OPEN", (2,), self.tr("cal_connect_open_p2")),
                    ("SHORT", (2,), self.tr("cal_connect_short_p2")),
                    ("MATCH", (2,), self.tr("cal_connect_match_p2")),
                ],
            },
            "forward": {
                "label": self.tr("cal_forward_title"),
                "type": "One Path Two Port P1->P2",
                "begin": self.vna.begin_one_path_two_port_calibration,
                "steps": [
                    ("OPEN", (1,), self.tr("cal_connect_open_p1")),
                    ("SHORT", (1,), self.tr("cal_connect_short_p1")),
                    ("MATCH", (1,), self.tr("cal_connect_match_p1")),
                    ("THROUGH", (1, 2), self.tr("cal_connect_thru_forward")),
                ],
            },
            "tosm": {
                "label": self.tr("cal_tosm_title"),
                "type": "TOSM",
                "begin": self.vna.begin_tosm_calibration,
                "steps": [
                    ("OPEN", (1,), self.tr("cal_connect_open_p1")),
                    ("SHORT", (1,), self.tr("cal_connect_short_p1")),
                    ("MATCH", (1,), self.tr("cal_connect_match_p1")),
                    ("OPEN", (2,), self.tr("cal_connect_open_p2")),
                    ("SHORT", (2,), self.tr("cal_connect_short_p2")),
                    ("MATCH", (2,), self.tr("cal_connect_match_p2")),
                    ("THROUGH", (1, 2), self.tr("cal_connect_thru")),
                ],
            },
        }
        config = configurations.get(mode)
        if config is None:
            messagebox.showerror(self.tr("error"), self.tr("unknown_calibration_mode").format(mode=mode), parent=self.root)
            return

        was_running = self.running
        self.stop_running()
        steps = config["steps"]
        calibration_finished = {"value": False}

        def resume_measurement(run_single: bool = True) -> None:
            if was_running and self.connected:
                self.start_running()
            elif run_single and self.connected:
                self.single_measurement()

        def abort_work():
            self.vna.abort_calibration()
            return None

        def abort_done(_result=None, message: str | None = None, run_single: bool = False) -> None:
            text = message or self.tr("cal_aborted")
            self.set_status(text + " " + self.tr("cal_restore_suffix"))
            resume_measurement(run_single)

        def fail_after_abort(original_exc: Exception) -> None:
            if calibration_finished["value"]:
                return
            calibration_finished["value"] = True

            def after_abort(_result=None):
                messagebox.showerror(
                    self.tr("error"),
                    self.tr("cal_error").format(label=config["label"], error=original_exc),
                    parent=self.root,
                )
                self.set_status(self.tr("cal_restored"))
                resume_measurement(True)

            self._submit_device_task(abort_work, after_abort, lambda _exc: after_abort(), self.tr("cal_abort_task"))

        def calibration_done(_result=None):
            if calibration_finished["value"]:
                return
            calibration_finished["value"] = True
            self.calibration = {
                "source": "R&S ZVL13",
                "type": config["label"],
                "enabled": True,
            }
            self._set_calibration_status(True)
            self.set_status(self.tr("cal_status_finished").format(label=config["label"]))
            messagebox.showinfo(
                self.tr("info"),
                self.tr("cal_info_applied").format(label=config["label"]),
                parent=self.root,
            )
            resume_measurement(True)

        def next_step(index: int) -> None:
            if calibration_finished["value"]:
                return
            if index >= len(steps):
                self.set_status(self.tr("cal_computing").format(label=config["label"]))
                self._submit_device_task(
                    self.vna.finish_calibration,
                    calibration_done,
                    fail_after_abort,
                    self.tr("cal_save_task"),
                )
                return

            standard, ports, instruction = steps[index]
            accepted = messagebox.askokcancel(
                self.tr("cal_dialog_title").format(label=config["label"]),
                self.tr("cal_step_prompt").format(current=index + 1, total=len(steps), instruction=instruction),
                parent=self.root,
            )
            if not accepted:
                calibration_finished["value"] = True
                self._submit_device_task(
                    abort_work,
                    lambda result: abort_done(result, self.tr("cal_aborted"), False),
                    lambda _exc: abort_done(None, self.tr("cal_aborted"), False),
                    self.tr("cal_cancel_task"),
                )
                return

            port_text = "-".join(str(port) for port in ports)
            self.set_status(self.tr("cal_step_status").format(label=config["label"], standard=standard, port=port_text, current=index + 1, total=len(steps)))
            self._submit_device_task(
                lambda standard=standard, ports=ports: self.vna.acquire_calibration_standard(standard, *ports),
                lambda _result, next_index=index + 1: next_step(next_index),
                fail_after_abort,
                self.tr("cal_step_task").format(standard=standard),
            )

        def calibration_started(_result=None):
            self.set_status(self.tr("cal_method_started").format(label=config["label"]))
            next_step(0)

        self.set_status(self.tr("cal_method_setting").format(label=config["label"]))
        self._submit_device_task(config["begin"], calibration_started, fail_after_abort, self.tr("cal_begin_task"))

    def open_save_settings(self) -> None:
        SaveSettingsDialog(
            self.root,
            dict(self.settings.data["save_fields"]),
            dict(self.settings.get("export_formats", {"csv": True, "s1p": False, "s2p": False})),
            self._save_settings_changed,
            self.tr,
        )

    def _save_settings_changed(self, fields: dict[str, bool], formats: dict[str, bool]) -> None:
        self.settings.data["save_fields"] = fields
        self.settings.data["export_formats"] = formats
        self.settings.save()
        self.set_status(self.tr("export_settings_saved"))

    def manual_save_csv(self) -> None:
        if not len(self.freq):
            messagebox.showwarning(self.tr("warning"), self.tr("no_data_to_save"), parent=self.root)
            return
        initial = self.settings.get("last_output_dir", str(Path.home()))
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title=self.tr("save_measurement", "Zapisz pomiar"),
            initialdir=initial,
            defaultextension=".csv",
            filetypes=[(self.tr("measurement_base_name"), "*.csv"), (self.tr("file_type_all"), "*.*")],
        )
        if not filename:
            return
        path = Path(filename)
        self.settings.set("last_output_dir", str(path.parent))
        formats = dict(self.settings.get("export_formats", {"csv": True, "s1p": False, "s2p": False}))
        written = self._save_snapshot_to_path(path, dict(self.settings.data["save_fields"]), formats)
        self.set_status(self.tr("files_saved").format(files=", ".join(item.name for item in written)))

    def _save_snapshot_to_path(self, path: Path, fields: dict[str, bool], formats: dict[str, bool] | None = None) -> list[Path]:
        selected_formats = formats or {"csv": True, "s1p": False, "s2p": False}
        kwargs = {}
        for name in ("s11", "s21", "s12", "s22"):
            values = getattr(self, name); memory = getattr(self, f"mem_{name}")
            kwargs[name] = values.copy() if len(values) else None
            kwargs[f"mem_{name}"] = None if memory is None else memory.copy()
        return save_measurement_files(
            base_path=path, formats=selected_formats, frequency_hz=self.freq.copy(), **kwargs,
            markers=dict(self.markers), fields=fields, z0=float(self.z0_var.get()),
            tdr_data={key: (value.copy() if hasattr(value, "copy") else value) for key, value in self.tdr_data.items()} if self.tdr_data else None,
        )

    def _automatic_snapshot(
        self,
        index: int,
        filename_stem: str,
        folder: Path,
        fields: dict[str, bool],
        formats: dict[str, bool],
    ) -> list[Path]:
        if not len(self.freq):
            raise RuntimeError(self.tr("no_current_measurement"))
        folder.mkdir(parents=True, exist_ok=True)
        stem = filename_stem
        suffix = 0
        while True:
            candidate_stem = stem if suffix == 0 else f"{stem}_{suffix:03d}"
            candidates = []
            if formats.get("csv", True):
                candidates.append(folder / f"{candidate_stem}.csv")
            if formats.get("s1p", False):
                candidates.append(folder / f"{candidate_stem}.s1p")
            if formats.get("s2p", False):
                candidates.append(folder / f"{candidate_stem}.s2p")
            if fields.get("tdr", False):
                candidates.append(folder / f"{candidate_stem}_TDR.csv")
            if not any(candidate.exists() for candidate in candidates):
                break
            suffix += 1
        written = self._save_snapshot_to_path(folder / candidate_stem, fields, formats)
        self.set_status(self.tr("auto_index_saved").format(index=index, name=candidate_stem))
        return written

    def open_automation(self) -> None:
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window.lift()
            return
        folder = Path(self.settings.get("last_output_dir", str(Path.home())))
        self.auto_window = AutomationWindow(
            self.root,
            dict(self.settings.data["save_fields"]),
            dict(self.settings.get("export_formats", {"csv": True, "s1p": False, "s2p": False})),
            folder,
            self._automatic_snapshot,
            self._automation_report,
            self.set_status,
            self.python_command,
            self.reference_options,
            self.apply_reference,
            self.automation_data_to_mem,
            self.automation_set_range,
            self.tr,
            measurement_callback=self.automation_request_measurement,
            timed_save_callback=self.automation_measure_and_save,
        )

    def automation_data_to_mem(self) -> str:
        if not len(self.freq): raise RuntimeError(self.tr("no_current_measurement"))
        self.trace_to_mem()
        return self.tr("data_mem_automation")

    def automation_set_range(self, start_hz: float, stop_hz: float, completion_callback, failure_callback) -> None:
        if not self.connected or len(self.freq) < 2: raise RuntimeError(self.tr("error_connect_first"))
        if start_hz <= 0 or stop_hz <= start_hz: raise ValueError(self.tr("stop_must_exceed_start"))
        self.set_range(start_hz, stop_hz, len(self.freq), completion_callback, failure_callback)

    def automation_request_measurement(self, started_callback, completion_callback, failure_callback) -> None:
        deadline = time.monotonic() + 10.0

        def attempt() -> None:
            if not self.connected:
                failure_callback(RuntimeError(self.tr("error_device_not_connected")))
                return
            if self.measurement_pending or self.device_busy or self.worker_busy:
                if time.monotonic() >= deadline:
                    failure_callback(RuntimeError(self.tr("status_device_busy", "Device is busy.")))
                    return
                self.root.after(5, attempt)
                return
            started_mono = time.monotonic()
            started_wall = datetime.now()
            started_callback(started_mono, started_wall)

            def completed() -> None:
                completion_callback(max(0.0, time.monotonic() - started_mono))

            self.read_once(False, completed, failure_callback)

        attempt()

    def automation_measure_and_save(
        self,
        index: int,
        filename_stem: str,
        folder: Path,
        fields: dict[str, bool],
        formats: dict[str, bool],
        planned_mono: float,
        planned_wall: datetime,
        completion_callback,
        failure_callback,
    ) -> None:
        if not self.connected:
            failure_callback(RuntimeError(self.tr("error_device_not_connected")))
            return
        if self.measurement_pending or self.worker_busy or self.device_busy:
            deadline = time.monotonic() + 10.0

            def wait_until_ready() -> None:
                if not self.connected:
                    failure_callback(RuntimeError(self.tr("error_device_not_connected")))
                    return
                if self.measurement_pending or self.worker_busy or self.device_busy:
                    if time.monotonic() >= deadline:
                        failure_callback(RuntimeError(self.tr("status_device_busy", "Device is busy.")))
                        return
                    self.root.after(5, wait_until_ready)
                    return
                self.automation_measure_and_save(
                    index,
                    filename_stem,
                    folder,
                    fields,
                    formats,
                    planned_mono,
                    planned_wall,
                    completion_callback,
                    failure_callback,
                )

            wait_until_ready()
            return
        enabled = (
            self.enable_s11_var.get(),
            self.enable_s21_var.get(),
            self.enable_s12_var.get(),
            self.enable_s22_var.get(),
        )
        if not any(enabled):
            failure_callback(RuntimeError(self.tr("error_enable_sparameter")))
            return
        folder = Path(folder).expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        fields_copy = dict(fields)
        fields_copy["tdr"] = False
        formats_copy = dict(formats)
        formats_copy["csv"] = True
        markers = dict(self.markers)
        z0 = float(self.z0_var.get())
        memories = {}
        for name in ("s11", "s21", "s12", "s22"):
            memory = getattr(self, f"mem_{name}")
            memories[name] = None if memory is None else memory.copy()

        def work():
            measurement = self.vna.read_measurement(*enabled)
            f, raw11, raw21, raw12, raw22 = measurement
            frequency = np.asarray(f, dtype=float)
            traces = {
                "s11": None if raw11 is None else np.asarray(raw11, dtype=complex),
                "s21": None if raw21 is None else np.asarray(raw21, dtype=complex),
                "s12": None if raw12 is None else np.asarray(raw12, dtype=complex),
                "s22": None if raw22 is None else np.asarray(raw22, dtype=complex),
            }
            if len(frequency) < 2:
                raise RuntimeError(self.tr("no_current_measurement"))
            stem = filename_stem
            suffix = 0
            while True:
                candidate_stem = stem if suffix == 0 else f"{stem}_{suffix:03d}"
                candidate_csv = folder / f"{candidate_stem}.csv"
                if not candidate_csv.exists():
                    break
                suffix += 1
            written = save_measurement_files(
                base_path=folder / candidate_stem,
                formats=formats_copy,
                frequency_hz=frequency,
                s11=traces["s11"],
                s21=traces["s21"],
                s12=traces["s12"],
                s22=traces["s22"],
                mem_s11=memories["s11"],
                mem_s21=memories["s21"],
                mem_s12=memories["s12"],
                mem_s22=memories["s22"],
                markers=markers,
                fields=fields_copy,
                z0=z0,
                tdr_data=None,
            )
            csv_written = [path for path in written if Path(path).suffix.lower() == ".csv" and Path(path).exists()]
            if not csv_written:
                raise RuntimeError("Nie zapisano pliku CSV pomiaru.")
            return {
                "index": index,
                "name": candidate_stem,
                "paths": [Path(path) for path in written],
                "measurement": (frequency, traces["s11"], traces["s21"], traces["s12"], traces["s22"]),
            }

        def ok(result) -> None:
            frequency, raw11, raw21, raw12, raw22 = result["measurement"]
            self.freq = np.asarray(frequency, float)
            for name, raw in (("s11", raw11), ("s21", raw21), ("s12", raw12), ("s22", raw22)):
                values = np.asarray(raw, complex) if raw is not None else np.array([], complex)
                setattr(self, f"raw_{name}", values)
                setattr(self, name, values.copy())
            entry = {name: getattr(self, name).copy() for name in ("s11", "s21", "s12", "s22") if len(getattr(self, name)) == len(self.freq)}
            if entry:
                self.history.append(entry)
            self.update_plots()
            completion_callback(result)

        def failed(exception) -> None:
            failure_callback(exception)

        self._submit_device_task(work, ok, failed, self.tr("task_measurement_read"))

    def reference_options(self) -> list[tuple[str, str]]:
        options = [("current", self.tr("current_trace_reference", "Aktualne Data → Mem"))]
        for index, overlay in enumerate(self.overlays):
            path = Path(overlay.get("path", f"overlay_{index}"))
            options.append((f"overlay:{index}", f"{self.tr('overlay_reference', 'Nałożony plik')}: {path.name}"))
        return options

    def _interpolate_reference(self, source_frequency: np.ndarray, values: np.ndarray) -> np.ndarray:
        frequency = np.asarray(source_frequency, dtype=float)
        data = np.asarray(values, dtype=complex)
        count = min(len(frequency), len(data))
        frequency = frequency[:count]
        data = data[:count]
        valid = np.isfinite(frequency) & np.isfinite(data.real) & np.isfinite(data.imag)
        frequency = frequency[valid]
        data = data[valid]
        if len(frequency) < 2:
            raise ValueError(self.tr("reference_too_few_points"))
        order = np.argsort(frequency)
        frequency = frequency[order]
        data = data[order]
        real = np.interp(self.freq, frequency, data.real)
        imag = np.interp(self.freq, frequency, data.imag)
        return real + 1j * imag

    def _set_memory_from_data(self, data: dict[str, Any], label: str) -> str:
        if not len(self.freq): raise RuntimeError(self.tr("no_current_measurement"))
        source_frequency = np.asarray(data.get("frequency_hz", []), dtype=float)
        found = False
        for name in ("s11", "s21", "s12", "s22"):
            memory = self._interpolate_reference(source_frequency, data[name]) if name in data else None
            setattr(self, f"mem_{name}", memory); found = found or memory is not None
        if not found: raise ValueError(self.tr("reference_no_sparams"))
        self.update_plots(); return self.tr("reference_set_from").format(label=label)

    def apply_reference(self, source: str, path: Path | None) -> str:
        if source == "current":
            self.trace_to_mem()
            return self.tr("reference_current_set")
        if source.startswith("overlay:"):
            index = int(source.split(":", 1)[1])
            if index < 0 or index >= len(self.overlays):
                raise IndexError(self.tr("reference_overlay_missing"))
            overlay = self.overlays[index]
            return self._set_memory_from_data(overlay, Path(overlay.get("path", "overlay")).name)
        if source == "file" and path is not None:
            data = load_measurement_file(path, self.tr)
            return self._set_memory_from_data(data, path.name)
        raise ValueError(self.tr("reference_unknown"))

    def add_overlay(self) -> None:
        files = filedialog.askopenfilenames(
            parent=self.root,
            title=self.tr("overlay_file_dialog"),
            filetypes=[(self.tr("measurement_files"), "*.csv *.s1p *.s2p"), (self.tr("file_type_all"), "*.*")],
        )
        if not files:
            return
        added = 0
        for filename in files:
            try:
                self.overlays.append(load_measurement_file(Path(filename), self.tr))
                self.overlays_dirty = True
                added += 1
            except Exception as exception:
                messagebox.showerror(self.tr("error"), f"{filename}\n{exception}", parent=self.root)
        self.update_plots()
        self.set_status(self.tr("overlay_added").format(count=added))
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window._refresh_references()

    def clear_overlays(self) -> None:
        self.overlays.clear()
        self.overlays_dirty = True
        self.update_plots()
        self.set_status(self.tr("overlay_cleared"))
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window._refresh_references()







    def _all_canvases(self) -> tuple[Any, ...]:
        return (self.canvas_s, self.canvas_s12, self.canvas_s22, self.canvas_z, self.canvas_smith, self.canvas_tdr)

    def _append_terminal(self, text: str) -> None:
        self.terminal_output.configure(state="normal")
        self.terminal_output.insert("end", text + "\n")
        self.terminal_output.see("end")
        self.terminal_output.configure(state="disabled")

    def send_terminal_command(self) -> None:
        command = self.terminal_command_var.get().strip()
        if not command:
            return
        if not self.connected:
            messagebox.showwarning(self.tr("warning"), self.tr("error_connect_first"), parent=self.root)
            return
        if command.count("?") > 1:
            messagebox.showwarning(
                self.tr("warning"),
                self.tr("terminal_multi_query"),
                parent=self.root,
            )
            return

        self.terminal_history.append(command)
        self.terminal_history_index = len(self.terminal_history)
        self.terminal_command_var.set("")
        self._append_terminal(f"> {command}")

        def work():
            return self.vna.terminal_command(command)

        def ok(result):
            is_query, response = result
            self._terminal_result(command, is_query, response)

        def failed(exc):
            self._append_terminal(f"! {exc}")
            self.set_status(self.tr("terminal_error").format(error=exc))
            if not self.vna.is_open:
                self.connected = False
                self.running = False
                self.apply_language(self.language_var.get(), persist=False)

        self._submit_device_task(work, ok, failed, "Terminal ZVL")

    def _terminal_result(self, command: str, is_query: bool, response: str) -> None:
        if response:
            prefix = "<" if is_query else "SCPI"
            self._append_terminal(f"{prefix} {response}")
        self.log(f"ZVL terminal: {command}" + (f" -> {response}" if response else ""))

    def _terminal_history_up(self, _event=None):
        if self.terminal_history:
            self.terminal_history_index = max(0, self.terminal_history_index - 1)
            self.terminal_command_var.set(self.terminal_history[self.terminal_history_index])
        return "break"

    def _terminal_history_down(self, _event=None):
        if self.terminal_history:
            self.terminal_history_index = min(len(self.terminal_history), self.terminal_history_index + 1)
            self.terminal_command_var.set("" if self.terminal_history_index == len(self.terminal_history) else self.terminal_history[self.terminal_history_index])
        return "break"




    def _plugin_display_name(self, plugin_path: Path) -> str:
        if plugin_path.name.lower() == "dopasuj_model_linii_v3.py":
            return self.tr("plugin_line_model_name")
        return plugin_path.stem

    def refresh_plugins(self) -> None:
        
        if not hasattr(self, "plugins_button_frame"):
            return
        for child in self.plugins_button_frame.winfo_children():
            child.destroy()

        plugins = sorted(
            path for path in self.plugin_dir.glob("*.py")
            if path.is_file() and not path.name.startswith("_")
        )
        if not plugins:
            ttk.Label(
                self.plugins_button_frame,
                text=self.tr("no_plugins_path").format(path=self.plugin_dir),
            ).grid(row=0, column=0, sticky="w")
            return

        for index, plugin_path in enumerate(plugins):
            title = self._plugin_display_name(plugin_path)
            display = title if len(title) <= 22 else title[:19] + "..."
            ttk.Button(
                self.plugins_button_frame,
                text=display,
                width=20,
                command=lambda path=plugin_path: self.open_plugin(path),
            ).grid(row=index // 3, column=index % 3, sticky="w", padx=2, pady=2)

    def open_plugin(self, plugin_path: Path) -> None:
        if self.plugin_process and self.plugin_process.poll() is None:
            messagebox.showwarning(
                self.tr("warning"),
                self.tr("plugin_other_running"),
                parent=self.root,
            )
            return
        if self.measurement_pending:
            messagebox.showwarning(
                self.tr("warning"),
                self.tr("plugin_read_in_progress"),
                parent=self.root,
            )
            return
        if len(self.freq) < 2:
            messagebox.showwarning(self.tr("warning"), self.tr("plugin_measure_first"), parent=self.root)
            return
        if not plugin_path.exists():
            messagebox.showerror(self.tr("error"), self.tr("plugin_not_found").format(path=plugin_path), parent=self.root)
            self.refresh_plugins()
            return

        if plugin_path.name.lower() == "dopasuj_model_linii_v3.py":
            if len(self.s11) != len(self.freq) or len(self.s21) != len(self.freq):
                messagebox.showwarning(
                    self.tr("warning"),
                    self.tr("status_plugin_needs_s11_s21"),
                    parent=self.root,
                )
                return
            ModelOptionsDialog(
                self.root,
                lambda length, quality, output: self.run_plugin(
                    plugin_path,
                    output,
                    ["--length", length, "--quality", quality, "--no-show"],
                ),
                self.tr,
            )
            return

        PluginOptionsDialog(
            self.root,
            self._plugin_display_name(plugin_path),
            lambda output: self.run_plugin(plugin_path, output),
            self.tr,
        )

    def run_plugin(
        self,
        plugin_path: Path,
        output_base: Path,
        extra_arguments: list[str] | None = None,
    ) -> None:
        




        if not self.python_command:
            messagebox.showerror(
                self.tr("error"),
                self.tr("plugin_python_missing"),
                parent=self.root,
            )
            return
        if self.plugin_process and self.plugin_process.poll() is None:
            messagebox.showwarning(self.tr("warning"), self.tr("status_plugin_running"), parent=self.root)
            return
        if len(self.freq) < 2:
            messagebox.showwarning(self.tr("warning"), self.tr("status_no_measurement"), parent=self.root)
            return

        try:
            output_base = Path(output_base).expanduser().resolve()
            output_dir = output_base / f"{plugin_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            output_dir.mkdir(parents=True, exist_ok=False)
            measurement_path = output_dir / "measurement.csv"
            fields = {
                "s11_db_phase": len(self.s11) == len(self.freq),
                "s21_db_phase": len(self.s21) == len(self.freq),
                "s12_db_phase": False,
                "s22_db_phase": False,
                "s11_complex": False,
                "s21_complex": False,
                "s12_complex": False,
                "s22_complex": False,
                "z_s11": False,
                "vswr": False,
                "memory": False,
                "markers": False,
                "tdr": False,
            }
            self._save_snapshot_to_path(
                measurement_path,
                fields,
                {"csv": True, "s1p": False, "s2p": False},
            )
        except Exception as exc:
            messagebox.showerror(self.tr("error"), self.tr("plugin_prepare_error").format(error=exc), parent=self.root)
            return

        self.plugin_resume_after = self.running
        if self.running:
            self.stop_running()
        self.plugin_output_dir = output_dir
        command = [
            *self.python_command,
            "-u",
            str(plugin_path),
            "--input",
            str(measurement_path),
            "--output",
            str(output_dir),
        ]
        if extra_arguments:
            command.extend(extra_arguments)

        process_options: dict[str, Any] = {}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW

        plugin_env = os.environ.copy()



        plugin_env["PYTHONIOENCODING"] = "utf-8"
        plugin_env["PYTHONUTF8"] = "1"
        plugin_env["AUTORS_VNA_LANGUAGE"] = self.language_var.get()

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(output_dir),
                bufsize=1,
                env=plugin_env,
                **process_options,
            )
            self.plugin_process = process
        except Exception as exc:
            messagebox.showerror(self.tr("error"), str(exc), parent=self.root)
            self.plugin_process = None
            if self.plugin_resume_after:
                self.start_running()
            return

        plugin_name = self._plugin_display_name(plugin_path)
        self.log(f"Plugin started: {plugin_name}; input={measurement_path}; output={output_dir}")
        self.set_status(self.tr("plugin_started").format(name=plugin_name))

        def reader() -> None:
            return_code = process.wait()
            self.post_ui(
                self._plugin_finished,
                process,
                return_code,
                output_dir,
                plugin_name,
            )

        threading.Thread(target=reader, daemon=True, name=f"plugin-{plugin_path.stem}").start()

    def stop_plugin_process(self) -> None:
        process = self.plugin_process
        if process and process.poll() is None:
            try:
                process.terminate()
                self.set_status(self.tr("plugin_abort_requested"))
            except Exception as exc:
                self.set_status(self.tr("plugin_abort_failed").format(error=exc))

    def _plugin_finished(
        self,
        process: subprocess.Popen,
        return_code: int,
        output_dir: Path,
        plugin_name: str,
    ) -> None:
        if self.plugin_process is process:
            self.plugin_process = None
        if return_code == 0:
            self.log(f"Plugin finished successfully: {plugin_name}; output={output_dir}")
            self.set_status(self.tr("plugin_finished").format(name=plugin_name, folder=output_dir))
        else:
            self.log(f"Plugin failed: {plugin_name}; exit code={return_code}")
            self.set_status(self.tr("plugin_exit_code").format(name=plugin_name, code=return_code))
        resume = self.plugin_resume_after
        self.plugin_resume_after = False
        if resume and not self.closing:
            self.start_running()

    def current_metrics(self) -> dict[str, float]:
        metrics = {name: float("nan") for name in AutomationWindow.METRICS}
        i1, i2 = self.markers.get("M1"), self.markers.get("M2")
        if i1 is not None and i2 is not None and len(self.freq) and i1 < len(self.freq) and i2 < len(self.freq):
            metrics["Δf M2-M1 [Hz]"] = float(self.freq[i2] - self.freq[i1])
        for name in ("s11", "s21", "s12", "s22"):
            values = getattr(self, name)
            if not len(values): continue
            db = complex_to_db(values); upper = name.upper()
            key = f"{upper} min [dB]"
            if key in metrics: metrics[key] = float(np.nanmin(db))
            for marker, index in (("M1", i1), ("M2", i2)):
                key = f"{upper} przy {marker} [dB]"
                if key in metrics and index is not None and index < len(values): metrics[key] = float(db[index])
            key = f"Δ{upper} M2-M1 [dB]"
            if key in metrics and i1 is not None and i2 is not None and i1 < len(values) and i2 < len(values): metrics[key] = float(db[i2]-db[i1])
        if len(self.s11):
            vswr = vswr_from_s11(self.s11, clip=None); z = gamma_to_impedance(self.s11, self.z0_var.get())
            metrics["VSWR max"] = float(np.nanmax(vswr[np.isfinite(vswr)])) if np.any(np.isfinite(vswr)) else float("inf")
            for marker, index in (("M1", i1), ("M2", i2)):
                if index is not None and index < len(self.s11):
                    metrics[f"VSWR przy {marker}"] = float(vswr[index]); metrics[f"Re(Z) przy {marker} [ohm]"] = float(np.real(z[index])); metrics[f"|Z| przy {marker} [ohm]"] = float(np.abs(z[index]))
            if i1 is not None and i2 is not None and i1 < len(self.s11) and i2 < len(self.s11):
                metrics["ΔVSWR M2-M1"] = float(vswr[i2]-vswr[i1]); metrics["ΔRe(Z) M2-M1 [ohm]"] = float(np.real(z[i2]-z[i1])); metrics["Δ|Z| M2-M1 [ohm]"] = float(np.abs(z[i2])-np.abs(z[i1]))
        return metrics

    def _report_marker_rows(self) -> list[list[str]]:
        rows: list[list[str]] = []
        for marker in ("M1", "M2"):
            idx = self.markers.get(marker)
            if idx is None or idx >= len(self.freq): continue
            values = []
            for name in ("s11", "s21", "s12", "s22"):
                trace = getattr(self, name)
                values.append(f"{complex_to_db(trace)[idx]:.4f} dB / {phase_deg(trace)[idx]:.2f}°" if len(trace) == len(self.freq) else "--")
            z_text = vswr_text = "--"
            if len(self.s11) == len(self.freq):
                z = gamma_to_impedance(self.s11[idx], self.z0_var.get()); swr = vswr_from_s11(np.asarray([self.s11[idx]]), clip=None)[0]
                z_text = f"{z.real:.3f}{z.imag:+.3f}j Ω"; vswr_text = f"{swr:.4f}"
            rows.append([marker, f"{self.freq[idx]:.6g} Hz", *values, z_text, vswr_text])
        return rows

    def _save_plot_images(self, folder: Path) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, figure in (("s11_s21", self.fig_s), ("s12", self.fig_s12), ("s22", self.fig_s22), ("impedancja_vswr", self.fig_z), ("smith", self.fig_smith), ("tdr", self.fig_tdr)):
            output = folder / f"{name}.png"; figure.savefig(output, dpi=170, bbox_inches="tight"); paths.append(output)
        return paths

    def manual_report(self) -> None:
        if not len(self.freq):
            messagebox.showwarning(self.tr("warning"), self.tr("report_no_data"), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title=self.tr("report_pdf"), defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not filename:
            return
        try:
            path = self._generate_report(Path(filename), {}, [], [])
            self.set_status(self.tr("report_generated").format(path=path))
        except Exception as exc:
            messagebox.showerror(self.tr("error"), str(exc), parent=self.root)

    def _automation_report(self, output_path: Path, description: dict[str, str], images: list[Path], saved_files: list[Path]) -> Path | None:
        if not len(self.freq):
            return None
        path = self._generate_report(output_path, description, images, saved_files)
        self.set_status(self.tr("report_series_generated").format(name=path.name))
        return path

    def _generate_report(self, output_path: Path, description: dict[str, str], images: list[Path], saved_files: list[Path]) -> Path:
        assets = output_path.parent / (output_path.stem + "_assets")
        plots = self._save_plot_images(assets)
        measurement = {
            self.tr("measurement_range"): f"{self.freq[0]:.6g}–{self.freq[-1]:.6g} Hz", self.tr("measurement_points"): len(self.freq), "Z0": f"{self.z0_var.get():g} Ω",
            **{name.upper(): self.tr("enabled") if getattr(self, f"enable_{name}_var").get() else self.tr("disabled") for name in ("s11", "s21", "s12", "s22")},
            "TDR": self.tr("enabled") if self.show_tdr_var.get() else self.tr("disabled"), "VF": self.vf_var.get(), self.tr("tdr_window"): self.tdr_window_var.get(),
        }
        note = self.tr("repeatability_note").format(count=len(self.history))
        raw_device = self.vna.info.to_dict()
        device_info = {
            self.tr("report_device_host"): raw_device.get("host", ""),
            self.tr("report_device_tcp_port"): raw_device.get("tcp_port", ""),
            self.tr("report_device_idn"): raw_device.get("idn", ""),
            self.tr("report_device_manufacturer"): raw_device.get("manufacturer", ""),
            self.tr("report_device_product"): raw_device.get("product", ""),
            self.tr("report_device_serial"): raw_device.get("serial_number", ""),
            self.tr("report_device_firmware"): raw_device.get("firmware_info", ""),
            self.tr("report_device_demo"): self.tr("report_yes") if raw_device.get("is_demo") else self.tr("report_no"),
            self.tr("report_device_port"): raw_device.get("port", ""),
        }
        calibration_info = None
        if self.calibration:
            calibration_info = {
                self.tr("report_cal_source"): self.calibration.get("source", "R&S ZVL13"),
                self.tr("report_cal_type"): self.calibration.get("type", self.tr("calibrated")),
                self.tr("report_cal_enabled"): self.tr("report_yes") if self.calibration.get("enabled") else self.tr("report_no"),
            }
        report_label_keys = (
            "report_date", "report_device", "report_measurement_parameters", "report_field", "report_value",
            "report_unavailable", "report_calibration", "report_correction_off", "report_markers_uncertainty",
            "report_marker", "report_conditions", "report_photos", "report_image_error", "report_series_files",
            "report_page", "reportlab_missing", "report_plot_s11_s21", "report_plot_s12", "report_plot_s22",
            "report_plot_impedancja_vswr", "report_plot_smith", "report_plot_tdr",
        )
        return generate_pdf_report(
            output_path, description.get(self.tr("study_title_internal"), self.tr("report_measurement_title")), device_info, measurement,
            calibration_info, self._report_marker_rows(), note, description=description, plot_paths=plots, image_paths=images, saved_files=saved_files,
            labels={key: self.tr(key) for key in report_label_keys},
        )

    def on_close(self) -> None:
        self.closing = True
        try:
            self.stop_running()
            if self.auto_window and self.auto_window.winfo_exists():
                self.auto_window.stop()
            self.stop_plugin_process()
            self.vna.interrupt()
            try:
                self.device_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self.settings.data.update({
                "last_host": self.host_var.get().strip(), "tcp_port": int(self.tcp_port_var.get()), "refresh_ms": int(self.interval_var.get()),
                "z0_ohm": float(self.z0_var.get()), "velocity_factor": float(self.vf_var.get()), "tdr_window": self.tdr_window_var.get(),
                "tdr_axis_mode": self.tdr_axis_mode_var.get(), "tdr_max_distance_m": float(self.tdr_max_distance_var.get()),
                "tdr_zero_offset_m": float(self.tdr_zero_offset_var.get()), "tdr_auto_zoom": bool(self.tdr_auto_zoom_var.get()),
                "theme": self.theme_var.get(), "language": self.language_var.get(),
            })
            self.settings.save()
        finally:
            self.root.destroy()
