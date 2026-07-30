from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
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

from app.config import Settings
from app.i18n import Translator
from app.runtime import python_command
from device.nanovna import NanoVNA
from export.pdf_report import generate_pdf_report
from gui.automation import AutomationWindow
from gui.dialogs import RangeDialog, SaveSettingsDialog, ModelOptionsDialog, PluginOptionsDialog, ProcessLogWindow, TDRSettingsDialog
from measurement.calibration import CalibrationStore, CalibrationProfile, create_profile
from measurement.calculations import (
    complex_to_db, phase_deg, gamma_to_impedance,
    vswr_from_s11, nearest_index,
)
from measurement.csv_io import save_measurement_files, load_measurement_file
from measurement.tdr import calculate_tdr


class NanoVNALab:
    def __init__(self, root: tk.Tk, base_dir: Path, installation_dir: Path | None = None) -> None:
        self.root = root
        self.base_dir = base_dir
        self.installation_dir = installation_dir or base_dir
        self.plugin_dir = self.installation_dir / "plugins"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        bundled_plugins = self.base_dir / "plugins"
        if bundled_plugins.exists() and bundled_plugins.resolve() != self.plugin_dir.resolve():
            for bundled_plugin in bundled_plugins.glob("*.py"):
                target = self.plugin_dir / bundled_plugin.name
                if not target.exists():
                    shutil.copy2(bundled_plugin, target)
        self.settings = Settings()
        self.tr = Translator(base_dir, self.settings.get("language", "pl"))
        self.vna = NanoVNA()
        self.python_command = python_command(base_dir)
        self.calibration_store = CalibrationStore()
        self.calibration: Optional[CalibrationProfile] = None
        self.connected = False
        self.running = False
        self.worker_busy = False
        self.after_id: Optional[str] = None
        self.plugin_process: Optional[subprocess.Popen] = None
        self.plugin_resume_after = False

        self.freq = np.array([], float)
        self.raw_s11 = np.array([], complex)
        self.raw_s21 = np.array([], complex)
        self.s11 = np.array([], complex)
        self.s21 = np.array([], complex)
        self.mem_s11: Optional[np.ndarray] = None
        self.mem_s21: Optional[np.ndarray] = None
        self.tdr_data: dict[str, np.ndarray] = {}
        self.markers: dict[str, Optional[int]] = {"M1": None, "M2": None}
        self.history: deque[dict[str, np.ndarray]] = deque(maxlen=max(2, int(self.settings.get("uncertainty_samples", 10))))
        self.overlays: list[dict[str, Any]] = []
        self.overlay_artists: list[Any] = []
        self.overlays_dirty = True
        self.auto_window: Optional[AutomationWindow] = None
        self.port_map: dict[str, str] = {}
        self.ui_queue: queue.Queue = queue.Queue()
        self.closing = False

        self.root.title("AutoNanoVNA")
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
        self.port_var = tk.StringVar(value=self.settings.get("last_port", ""))
        self.interval_var = tk.IntVar(value=int(self.settings.get("refresh_ms", 800)))
        self.enable_s11_var = tk.BooleanVar(value=True)
        self.enable_s21_var = tk.BooleanVar(value=True)
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
            path = self.base_dir / "assets" / filename
            if path.exists():
                self.flag_images[language] = tk.PhotoImage(file=str(path))
        self.calibration_label = ttk.Label(top_bar, textvariable=self.calibration_var, style="Calibration.Bad.TLabel")
        self.calibration_label.pack(side="left", padx=(2, 8))
        language_box = ttk.Frame(top_bar)
        language_box.pack(side="right")
        self.widgets["language_label"] = ttk.Label(language_box, text=self.tr("language", "Język"))
        self.widgets["language_label"].pack(side="left", padx=(0, 3))
        self.language_buttons = {}
        for language in ("pl", "en"):
            button = ttk.Button(language_box, command=lambda value=language: self.language_var.set(value), width=3)
            if language in self.flag_images:
                button.configure(image=self.flag_images[language])
            else:
                button.configure(text=language.upper())
            button.pack(side="left", padx=1)
            self.language_buttons[language] = button

        self.device_section = ttk.LabelFrame(header, text=self.tr("device_section", "Urządzenie i pomiar"), padding=(5, 3))
        self.device_section.grid(row=1, column=0, sticky="nsew", padx=(0, 2), pady=2)
        self.widgets["port_label"] = ttk.Label(self.device_section, text=self.tr("port"))
        self.widgets["port_label"].grid(row=0, column=0, sticky="w")
        self.port_box = ttk.Combobox(self.device_section, textvariable=self.port_var, state="readonly", width=20)
        self.port_box.grid(row=0, column=1, columnspan=3, sticky="ew", padx=3)
        self.widgets["refresh_ports"] = ttk.Button(self.device_section, command=self.refresh_ports)
        self.widgets["refresh_ports"].grid(row=0, column=4, padx=1)
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
        self.widgets["cb_s11"] = ttk.Checkbutton(channel_frame, variable=self.enable_s11_var, command=self._channel_changed)
        self.widgets["cb_s11"].pack(side="left", padx=2)
        self.widgets["cb_s21"] = ttk.Checkbutton(channel_frame, variable=self.enable_s21_var, command=self._channel_changed)
        self.widgets["cb_s21"].pack(side="left", padx=2)
        self.widgets["cb_impedance"] = ttk.Checkbutton(channel_frame, variable=self.show_impedance_var, command=self.update_plots)
        self.widgets["cb_impedance"].pack(side="left", padx=2)
        self.widgets["cb_vswr"] = ttk.Checkbutton(channel_frame, variable=self.show_vswr_var, command=self.update_plots)
        self.widgets["cb_vswr"].pack(side="left", padx=2)
        self.widgets["cb_tdr"] = ttk.Checkbutton(channel_frame, variable=self.show_tdr_var, command=self.update_plots)
        self.widgets["cb_tdr"].pack(side="left", padx=2)
        self.widgets["cb_memory"] = ttk.Checkbutton(channel_frame, variable=self.show_memory_var, command=self.update_plots)
        self.widgets["cb_memory"].pack(side="left", padx=2)
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
        self.frames = {name: ttk.Frame(self.notebook) for name in ("sparams", "impedance", "smith", "tdr", "log")}
        for name in ("sparams", "impedance", "smith", "tdr"):
            self.notebook.add(self.frames[name], text="")
        self.log_text = tk.Text(self.frames["log"], wrap="word", font=("Consolas", 9), state="disabled")
        log_scroll = ttk.Scrollbar(self.frames["log"], command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
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

        self.fig_z = Figure(figsize=(12, 7.2), dpi=100)
        impedance_grid = self.fig_z.add_gridspec(2, 2, height_ratios=(1, 1), hspace=0.58, wspace=0.20)
        self.ax_z11_r = self.fig_z.add_subplot(impedance_grid[0, 0])
        self.ax_z11_x = self.fig_z.add_subplot(impedance_grid[0, 1])
        self.ax_vswr = self.fig_z.add_subplot(impedance_grid[1, :])
        self.fig_z.subplots_adjust(left=0.075, right=0.985, top=0.95, bottom=0.10, hspace=0.58, wspace=0.20)
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

        for canvas in (self.canvas_s, self.canvas_z, self.canvas_smith):
            canvas.mpl_connect("button_press_event", self.on_plot_click)
        self._initialize_plot_artists()

    def _initialize_plot_artists(self) -> None:
        for ax in (self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
                   self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
                   self.ax_tdr_rho, self.ax_tdr_z):
            ax.clear()
            ax.grid(True, alpha=0.35)
        self._apply_plot_language()

        (self.line_s11_db,) = self.ax_s11_db.plot([], [], label="S11", lw=1.6)
        (self.line_s21_db,) = self.ax_s21_db.plot([], [], label="S21", lw=1.6)
        (self.line_s11_phase,) = self.ax_s11_phase.plot([], [], label="S11", lw=1.6)
        (self.line_s21_phase,) = self.ax_s21_phase.plot([], [], label="S21", lw=1.6)
        (self.mem_line_s11_db,) = self.ax_s11_db.plot([], [], "--", label="Mem S11", alpha=0.65)
        (self.mem_line_s21_db,) = self.ax_s21_db.plot([], [], "--", label="Mem S21", alpha=0.65)
        (self.mem_line_s11_phase,) = self.ax_s11_phase.plot([], [], "--", label="Mem S11", alpha=0.65)
        (self.mem_line_s21_phase,) = self.ax_s21_phase.plot([], [], "--", label="Mem S21", alpha=0.65)
        (self.line_z11_r,) = self.ax_z11_r.plot([], [], label="R(S11)", lw=1.5)
        (self.line_z11_x,) = self.ax_z11_x.plot([], [], label="X(S11)", lw=1.5)
        (self.line_vswr,) = self.ax_vswr.plot([], [], label="VSWR", lw=1.5)
        (self.line_tdr_rho,) = self.ax_tdr_rho.plot([], [], lw=1.5)
        (self.line_tdr_z,) = self.ax_tdr_z.plot([], [], lw=1.5)

        self._draw_smith_grid()
        (self.line_smith_s11,) = self.ax_smith.plot([], [], label="S11", lw=1.5)
        self.smith_marker_artists = {
            "M1": self.ax_smith.plot([], [], marker="o", ms=14, mew=2.2, mec="white", mfc="#ff3b30", ls="None", label="M1", zorder=8)[0],
            "M2": self.ax_smith.plot([], [], marker="D", ms=13, mew=2.0, mec="#111111", mfc="#ffd60a", ls="None", label="M2", zorder=8)[0],
        }
        self.smith_marker_labels = {
            "M1": self.ax_smith.annotate("M1", (0, 0), xytext=(9, 9), textcoords="offset points", fontsize=10, fontweight="bold", color="#ff3b30", visible=False, zorder=9),
            "M2": self.ax_smith.annotate("M2", (0, 0), xytext=(9, -15), textcoords="offset points", fontsize=10, fontweight="bold", color="#ffd60a", visible=False, zorder=9),
        }
        self.marker_lines: dict[str, list[Any]] = {"M1": [], "M2": []}
        for marker, ls in (("M1", ":"), ("M2", "--")):
            for ax in (self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
                       self.ax_z11_r, self.ax_z11_x, self.ax_vswr):
                self.marker_lines[marker].append(ax.axvline(np.nan, ls=ls, lw=1.1, alpha=0.8))
        self._update_legends()
        self.fig_s.tight_layout()
        self.fig_z.subplots_adjust(left=0.075, right=0.985, top=0.95, bottom=0.10, hspace=0.58, wspace=0.20)
        self.fig_tdr.tight_layout()
        self.fig_smith.tight_layout()

    def _draw_smith_grid(self) -> None:
        self.ax_smith.clear()
        self.ax_smith.set_title("Wykres Smitha")
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
        palette = getattr(self, "theme_palette", None)
        for ax in (self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
                   self.ax_z11_r, self.ax_z11_x, self.ax_vswr, self.ax_smith):
            try:
                legend = ax.legend(loc="best", fontsize=8)
                if palette:
                    frame = legend.get_frame()
                    frame.set_facecolor(palette["panel2"])
                    frame.set_edgecolor(palette["border"])
                    frame.set_alpha(0.85)
                    for label in legend.get_texts():
                        label.set_color(palette["fg"])
            except Exception:
                pass

    def apply_theme(self, theme: str, persist: bool = True) -> None:
        if theme not in {"light", "dark"}:
            return
        dark = theme == "dark"
        if dark:
            palette = {
                "bg": "#171a1f", "panel": "#222730", "panel2": "#2b313c",
                "entry": "#303744", "fg": "#f5f7fa", "muted": "#c3cad5",
                "accent": "#4da3ff", "pressed": "#2f78c4", "border": "#667085",
                "disabled_bg": "#252a33", "disabled_fg": "#8b95a5", "grid": "#6b7280",
            }
        else:
            palette = {
                "bg": "#eef1f5", "panel": "#ffffff", "panel2": "#e4e9f0",
                "entry": "#ffffff", "fg": "#172033", "muted": "#4b5565",
                "accent": "#1877d1", "pressed": "#0f5fae", "border": "#8c98a8",
                "disabled_bg": "#e5e7eb", "disabled_fg": "#8a94a3", "grid": "#8d99a8",
            }
        self.theme_palette = palette
        bg, panel, panel2 = palette["bg"], palette["panel"], palette["panel2"]
        fg, entry, accent = palette["fg"], palette["entry"], palette["accent"]
        self.root.configure(bg=bg)

                                                                             
                                                                       
        self.style.configure(".", background=bg, foreground=fg, font=("Segoe UI", 9))
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabel", background=bg, foreground=fg)
        self.style.configure("Info.TLabel", background=bg, foreground=palette["muted"], font=("Segoe UI", 9))
        self.style.configure("MarkerDelta.TLabel", background=bg, foreground=accent, font=("Segoe UI", 8, "bold"))
        self.style.configure("CompactMarker.TLabel", background=bg, foreground=fg, font=("Segoe UI", 8))
        self.style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=palette["border"])
        self.style.configure("TLabelframe.Label", background=bg, foreground=fg, font=("Segoe UI", 9, "bold"))
        self.style.configure("TButton", background=panel2, foreground=fg, bordercolor=palette["border"],
                             lightcolor=panel2, darkcolor=panel2, padding=(5, 2), relief="flat")
        self.style.map("TButton",
                       background=[("pressed", palette["pressed"]), ("active", accent), ("disabled", palette["disabled_bg"])],
                       foreground=[("pressed", "#ffffff"), ("active", "#ffffff"), ("disabled", palette["disabled_fg"])],
                       bordercolor=[("focus", accent), ("active", accent)])
        self.style.configure("TCheckbutton", background=bg, foreground=fg, focuscolor=accent, padding=(2, 2))
        self.style.map("TCheckbutton", foreground=[("disabled", palette["disabled_fg"]), ("active", fg)],
                       background=[("active", bg)])
        self.style.configure("TRadiobutton", background=bg, foreground=fg, focuscolor=accent, padding=(2, 2))
        self.style.map("TRadiobutton", foreground=[("disabled", palette["disabled_fg"]), ("active", fg)],
                       background=[("active", bg)])
        for style_name in ("TEntry", "TSpinbox"):
            self.style.configure(style_name, fieldbackground=entry, foreground=fg, insertcolor=fg,
                                 bordercolor=palette["border"], lightcolor=palette["border"],
                                 darkcolor=palette["border"], padding=3)
            self.style.map(style_name,
                           fieldbackground=[("disabled", palette["disabled_bg"]), ("readonly", entry)],
                           foreground=[("disabled", palette["disabled_fg"]), ("readonly", fg)],
                           bordercolor=[("focus", accent)])
        self.style.configure("TCombobox", fieldbackground=entry, background=panel2, foreground=fg,
                             arrowcolor=fg, bordercolor=palette["border"], padding=3)
        self.style.map("TCombobox",
                       fieldbackground=[("readonly", entry), ("disabled", palette["disabled_bg"])],
                       foreground=[("readonly", fg), ("disabled", palette["disabled_fg"])],
                       selectbackground=[("readonly", accent)], selectforeground=[("readonly", "#ffffff")],
                       arrowcolor=[("disabled", palette["disabled_fg"]), ("readonly", fg)],
                       bordercolor=[("focus", accent)])
        self.style.configure("TNotebook", background=bg, bordercolor=palette["border"])
        self.style.configure("TNotebook.Tab", background=panel2, foreground=fg, padding=(9, 4))
        self.style.map("TNotebook.Tab",
                       background=[("selected", accent), ("active", palette["pressed"])],
                       foreground=[("selected", "#ffffff"), ("active", "#ffffff")])
        self.style.configure("Vertical.TScrollbar", background=panel2, troughcolor=bg, arrowcolor=fg)
        self.style.configure("Calibration.Bad.TLabel", background=bg, foreground="#ffb020", font=("Segoe UI", 9, "bold"))
        self.style.configure("Calibration.Good.TLabel", background=bg, foreground="#32d74b", font=("Segoe UI", 9, "bold"))

                                                                                       
        self.root.option_add("*TCombobox*Listbox.background", entry)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*Text.background", panel)
        self.root.option_add("*Text.foreground", fg)
        self.root.option_add("*Text.insertBackground", fg)
        self.root.option_add("*Text.selectBackground", accent)
        self.root.option_add("*Text.selectForeground", "#ffffff")
        self.root.option_add("*Canvas.background", bg)

        self.log_text.configure(bg=panel, fg=fg, insertbackground=fg, selectbackground=accent,
                                selectforeground="#ffffff", relief="flat")
        figures = (self.fig_s, self.fig_z, self.fig_smith, self.fig_tdr) if hasattr(self, "fig_s") else ()
        for fig in figures:
            fig.patch.set_facecolor(panel)
            for ax in fig.axes:
                ax.set_facecolor(panel)
                ax.tick_params(colors=fg, labelsize=9)
                ax.xaxis.label.set_color(fg)
                ax.yaxis.label.set_color(fg)
                ax.title.set_color(fg)
                for spine in ax.spines.values():
                    spine.set_color(palette["border"])
                ax.grid(True, color=palette["grid"], alpha=0.28, linewidth=0.7)
                legend = ax.get_legend()
                if legend:
                    legend.get_frame().set_facecolor(panel2)
                    legend.get_frame().set_edgecolor(palette["border"])
                    for text in legend.get_texts():
                        text.set_color(fg)
        if hasattr(self, "line_s11_db"):
            for line in (self.line_s11_db, self.line_s11_phase, self.line_z11_r, self.line_z11_x, self.line_vswr, self.line_tdr_rho):
                line.set_color("#4da3ff" if dark else "#1167b1")
            for line in (self.line_s21_db, self.line_s21_phase, self.line_tdr_z):
                line.set_color("#ff9f0a" if dark else "#c85d00")
            self.line_smith_s11.set_color("#4da3ff" if dark else "#1167b1")
        if figures:
            for canvas in (self.canvas_s, self.canvas_z, self.canvas_smith, self.canvas_tdr):
                canvas.draw_idle()
        if persist:
            self.settings.set("theme", theme)

    def _apply_plot_language(self) -> None:
        frequency = self.tr("plot_frequency_mhz", "Częstotliwość [MHz]")
        phase_s11 = self.tr("plot_phase_s11", "Faza S11")
        phase_s21 = self.tr("plot_phase_s21", "Faza S21")
        impedance = self.tr("plot_impedance_ohm", "Impedancja [Ω]")
        distance = self.tr("plot_distance_m", "Odległość [m]")
        time_ns = self.tr("plot_time_ns", "Czas [ns]")

        self.ax_s11_db.set(title="S11 [dB]", xlabel=frequency, ylabel="dB")
        self.ax_s21_db.set(title="S21 [dB]", xlabel=frequency, ylabel="dB")
        self.ax_s11_phase.set(title=phase_s11, xlabel=frequency, ylabel="°")
        self.ax_s21_phase.set(title=phase_s21, xlabel=frequency, ylabel="°")
        self.ax_z11_r.set(
            title=self.tr("plot_real_z_s11", "Re{Z(S11)}"),
            xlabel=frequency,
            ylabel=impedance,
        )
        self.ax_z11_x.set(
            title=self.tr("plot_imag_z_s11", "Im{Z(S11)}"),
            xlabel=frequency,
            ylabel=impedance,
        )
        self.ax_vswr.set(
            title=self.tr("plot_vswr_s11", "VSWR z S11"),
            xlabel=frequency,
            ylabel="VSWR",
        )
        tdr_xlabel = time_ns if self.tdr_axis_mode_var.get() == "time" else distance
        self.ax_tdr_rho.set(
            title=self.tr("plot_tdr_reflection", "TDR — |ρ(t)|"),
            xlabel=tdr_xlabel,
            ylabel="|ρ|",
        )
        self.ax_tdr_z.set(
            title=self.tr("plot_tdr_impedance", "TDR — Re{Z}"),
            xlabel=tdr_xlabel,
            ylabel=impedance,
        )

        for canvas_name in ("canvas_s", "canvas_z", "canvas_smith", "canvas_tdr"):
            canvas = getattr(self, canvas_name, None)
            if canvas is not None:
                canvas.draw_idle()

    def apply_language(self, language: str, persist: bool = True) -> None:
        self.tr.load(language)
        self.root.title("AutoNanoVNA")
        texts = {
            "port_label": "port",
            "refresh_ports": "refresh_ports",
            "connect": "disconnect" if self.connected else "connect",
            "start": "stop" if self.running else "start",
            "single": "single",
            "range": "range",
            "calibration": "calibration",
            "refresh_label": "refresh_ms",
            "cb_s11": "s11",
            "cb_s21": "s21",
            "cb_impedance": "impedance",
            "cb_vswr": "vswr",
            "cb_tdr": "tdr",
            "cb_memory": "memory",
            "trace_to_mem": "trace_to_mem",
            "clear_memory": "clear_memory",
            "tdr_settings": "tdr_settings",
            "tdr_settings_tab": "tdr_settings",
            "save_settings": "save_settings",
            "save_csv": "save_measurement",
            "auto_measurements": "auto_measurements",
            "overlay": "overlay_files",
            "clear_overlays": "clear_overlays",
            "report": "report",
            "clear_markers": "clear_markers",
            "theme_label": "theme",
            "language_label": "language",
            "refresh_plugins": "refresh_plugins",
        }
        for widget_key, text_key in texts.items():
            widget = self.widgets.get(widget_key)
            if widget:
                widget.configure(text=self.tr(text_key))
        self.device_section.configure(text=self.tr("device_section", "Urządzenie i pomiar"))
        self.measurement_section.configure(text=self.tr("measurement_section", "Kanały, pamięć i TDR"))
        self.tools_section.configure(text=self.tr("tools_section", "Pliki i narzędzia"))
        self.plugins_section.configure(text=self.tr("plugins", "Wtyczki"))
        for frame_key, text_key in (("sparams", "tab_sparams"), ("impedance", "tab_impedance"), ("smith", "tab_smith"), ("tdr", "tab_tdr")):
            self.notebook.tab(self.frames[frame_key], text=self.tr(text_key))
        self.marker_bar.configure(text=self.tr("marker"))
        self._apply_plot_language()
        self._update_marker_delta()
        if not self.tdr_data:
            self.tdr_info_var.set(self.tr("tdr_info_no_data", "Włącz S11 i TDR, aby obliczyć odpowiedź w dziedzinie czasu."))
        self._set_calibration_status(self.calibration is not None)
        if hasattr(self, "line_tdr_rho"):
            self.update_plots()
        if persist:
            self.settings.set("language", language)

    def log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.log(text)

    def refresh_ports(self) -> None:
        ports = self.vna.list_ports()
        self.port_map = {f"{device} | {label}": device for device, label in ports}
        values = list(self.port_map)
        self.port_box["values"] = values
        last = self.settings.get("last_port", "")
        selected = next((display for display, dev in self.port_map.items() if dev == last), None)
        if selected:
            self.port_var.set(selected)
        elif values:
            self.port_var.set(values[0])
        self.set_status(f"Ports found: {len(values) - 1} devices + LIVE DEMO.")

    def toggle_connection(self) -> None:
        if self.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        display = self.port_var.get()
        port = self.port_map.get(display, display.split(" | ")[0] if display else "")
        if not port:
            messagebox.showwarning(self.tr("warning"), self.tr("choose_nanovna_port", "Wybierz port NanoVNA."), parent=self.root)
            return
        try:
            info = self.vna.connect(port)
            self.connected = True
            self.settings.set("last_port", port)
            self.freq = self.vna.get_frequencies()
            self._load_calibration_for_current_range()
            self.apply_language(self.language_var.get(), persist=False)
            self.set_status(
                f"Connected: {port} | {self.freq[0]/1e6:.6g}–{self.freq[-1]/1e6:.6g} MHz | "
                f"{len(self.freq)} pkt | {info.protocol or 'protokół nieznany'} | "
                f"{info.firmware_info or 'firmware nieznany'}"
            )
            self.single_measurement()
        except Exception as exc:
            self.connected = False
            messagebox.showerror(self.tr("error"), str(exc), parent=self.root)
            self.set_status(f"Connection error: {exc}")

    def disconnect(self) -> None:
        self.stop_running()
        self.vna.close()
        self.connected = False
        self.calibration = None
        self._set_calibration_status(False)
        self.apply_language(self.language_var.get(), persist=False)
        self.set_status(self.tr("not_connected"))

    def toggle_running(self) -> None:
        if self.running:
            self.stop_running()
        else:
            self.start_running()

    def start_running(self) -> None:
        if not self.connected:
            self.connect()
        if not self.connected:
            return
        if not self.enable_s11_var.get() and not self.enable_s21_var.get():
            messagebox.showwarning(self.tr("warning"), "Włącz co najmniej S11 albo S21.", parent=self.root)
            return
        self.running = True
        self.apply_language(self.language_var.get(), persist=False)
        self.set_status("Continuous measurement started.")
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
        self.set_status("Measurement stopped.")

    def single_measurement(self) -> None:
        if not self.connected:
            self.connect()
            if not self.connected:
                return
        self.read_once(schedule_after=False)

    def read_once(
        self,
        schedule_after: bool = False,
        completion_callback=None,
        failure_callback=None,
    ) -> None:
        if self.worker_busy:
            self.root.after(50, lambda: self.read_once(schedule_after, completion_callback, failure_callback))
            return
        if not self.connected:
            if failure_callback:
                failure_callback(RuntimeError("Urządzenie nie jest połączone."))
            return
        read_s11 = self.enable_s11_var.get()
        read_s21 = self.enable_s21_var.get()
        self.worker_busy = True
        started = time.monotonic()

        def worker():
            try:
                f, s11, s21 = self.vna.read_measurement(read_s11, read_s21)
                self.post_ui(self._measurement_ready, f, s11, s21, started, schedule_after, completion_callback, failure_callback)
            except Exception as exc:
                self.post_ui(self._measurement_failed, exc, schedule_after, failure_callback)

        threading.Thread(target=worker, daemon=True).start()

    def _measurement_ready(self, f, raw11, raw21, started: float, schedule_after: bool, completion_callback=None, failure_callback=None) -> None:
        self.worker_busy = False
        self.freq = np.asarray(f, float)
        self.raw_s11 = np.asarray(raw11, complex) if raw11 is not None else np.array([], complex)
        self.raw_s21 = np.asarray(raw21, complex) if raw21 is not None else np.array([], complex)
        corrected11: Optional[np.ndarray] = self.raw_s11 if len(self.raw_s11) else None
        corrected21: Optional[np.ndarray] = self.raw_s21 if len(self.raw_s21) else None
        if self.calibration is not None:
            corrected11, corrected21 = self.calibration.apply(corrected11, corrected21)
        self.s11 = np.asarray(corrected11, complex) if corrected11 is not None else np.array([], complex)
        self.s21 = np.asarray(corrected21, complex) if corrected21 is not None else np.array([], complex)
        if len(self.s11):
            entry = {"s11": self.s11.copy()}
            if len(self.s21) == len(self.s11):
                entry["s21"] = self.s21.copy()
            self.history.append(entry)
        self.update_plots()
        metrics = self.current_metrics()
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window.on_measurement(metrics)
        elapsed = time.monotonic() - started
        cal = self.tr("calibrated") if self.calibration else self.tr("uncalibrated")
        self.status_var.set(
            f"{len(self.freq)} pkt | {self.freq[0]/1e6:.6g}–{self.freq[-1]/1e6:.6g} MHz | "
            f"odczyt {elapsed:.2f} s | {cal}"
        )
        if completion_callback:
            completion_callback()
        if schedule_after and self.running:
            self.after_id = self.root.after(max(100, int(self.interval_var.get() or 800)), lambda: self.read_once(True))

    def _measurement_failed(self, exc: Exception, schedule_after: bool, failure_callback=None) -> None:
        self.worker_busy = False
        self.set_status(f"Read error: {exc}")
        if failure_callback:
            failure_callback(exc)
        if schedule_after and self.running:
            self.after_id = self.root.after(max(300, int(self.interval_var.get() or 800)), lambda: self.read_once(True))

    def update_plots(self) -> None:
        f_mhz = self.freq / 1e6 if len(self.freq) else np.array([])
        has11 = len(self.s11) == len(self.freq) and len(self.freq) > 0
        has21 = len(self.s21) == len(self.freq) and len(self.freq) > 0

        self.line_s11_db.set_data(f_mhz if has11 else [], complex_to_db(self.s11) if has11 else [])
        self.line_s11_phase.set_data(f_mhz if has11 else [], phase_deg(self.s11) if has11 else [])
        self.line_s21_db.set_data(f_mhz if has21 else [], complex_to_db(self.s21) if has21 else [])
        self.line_s21_phase.set_data(f_mhz if has21 else [], phase_deg(self.s21) if has21 else [])
        show_mem = self.show_memory_var.get()
        if show_mem and self.mem_s11 is not None and len(self.mem_s11) == len(self.freq):
            self.mem_line_s11_db.set_data(f_mhz, complex_to_db(self.mem_s11))
            self.mem_line_s11_phase.set_data(f_mhz, phase_deg(self.mem_s11))
        else:
            self.mem_line_s11_db.set_data([], [])
            self.mem_line_s11_phase.set_data([], [])
        if show_mem and self.mem_s21 is not None and len(self.mem_s21) == len(self.freq):
            self.mem_line_s21_db.set_data(f_mhz, complex_to_db(self.mem_s21))
            self.mem_line_s21_phase.set_data(f_mhz, phase_deg(self.mem_s21))
        else:
            self.mem_line_s21_db.set_data([], [])
            self.mem_line_s21_phase.set_data([], [])

        if has11 and self.show_impedance_var.get():
            z11 = gamma_to_impedance(self.s11, self.z0_var.get())
            self.line_z11_r.set_data(f_mhz, z11.real)
            self.line_z11_x.set_data(f_mhz, z11.imag)
        else:
            self.line_z11_r.set_data([], [])
            self.line_z11_x.set_data([], [])
        if has11 and self.show_vswr_var.get():
            self.line_vswr.set_data(f_mhz, vswr_from_s11(self.s11, clip=100.0))
        else:
            self.line_vswr.set_data([], [])

        self.line_smith_s11.set_data(self.s11.real if has11 else [], self.s11.imag if has11 else [])

        if has11 and self.show_tdr_var.get():
            try:
                self.tdr_data = calculate_tdr(
                    self.freq, self.s11, velocity_factor=float(self.vf_var.get()),
                    window=self.tdr_window_var.get(), z0=float(self.z0_var.get()),
                    zero_offset_m=float(self.tdr_zero_offset_var.get()),
                )
            except Exception as exc:
                self.tdr_data = {}
                self.tdr_info_var.set(f"TDR: {exc}")
            if self.tdr_data and len(self.tdr_data.get("rho", [])):
                if self.tdr_axis_mode_var.get() == "time":
                    tdr_x = self.tdr_data["time_s"] * 1e9
                    self.ax_tdr_rho.set_xlabel(self.tr("plot_time_ns", "Czas [ns]"))
                    self.ax_tdr_z.set_xlabel(self.tr("plot_time_ns", "Czas [ns]"))
                else:
                    tdr_x = self.tdr_data["distance_m"]
                    self.ax_tdr_rho.set_xlabel(self.tr("plot_distance_m", "Odległość [m]"))
                    self.ax_tdr_z.set_xlabel(self.tr("plot_distance_m", "Odległość [m]"))
                self.line_tdr_rho.set_data(tdr_x, np.abs(self.tdr_data["rho"]))
                ztdr = np.real(self.tdr_data["z_ohm"])
                finite = np.where(np.isfinite(ztdr), ztdr, np.nan)
                self.line_tdr_z.set_data(tdr_x, finite)
                resolution_m = float(self.tdr_data["resolution_m"])
                max_range_m = float(self.tdr_data["max_unambiguous_distance_m"])
                bw = float(self.tdr_data["bandwidth_hz"])
                if self.tdr_axis_mode_var.get() == "time":
                    resolution_text = f"{self.tdr_data['resolution_s']*1e9:.4g} ns"
                    range_text = f"{self.tdr_data['max_unambiguous_time_s']*1e9:.4g} ns"
                else:
                    resolution_text = f"{resolution_m:.4g} m"
                    range_text = f"{max_range_m:.4g} m"
                info = self.tr(
                    "tdr_info",
                    "Rozdzielczość ≈ {resolution}; zakres bezaliasowy ≈ {range}; BW={bandwidth}. Obiekty krótsze od rozdzielczości nie będą rozdzielone.",
                ).format(resolution=resolution_text, range=range_text, bandwidth=f"{bw/1e6:.4g} MHz")
                warning = str(self.tdr_data.get("warning", ""))
                if warning:
                    info += " " + warning
                self.tdr_info_var.set(info)
            else:
                self.line_tdr_rho.set_data([], [])
                self.line_tdr_z.set_data([], [])
        else:
            self.tdr_data = {}
            self.line_tdr_rho.set_data([], [])
            self.line_tdr_z.set_data([], [])
            self.tdr_info_var.set(self.tr("tdr_info_no_data", "Włącz S11 i TDR, aby obliczyć odpowiedź w dziedzinie czasu."))

        self._update_marker_artists()
        for ax in (self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
                   self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
                   self.ax_tdr_rho, self.ax_tdr_z):
            ax.relim()
            ax.autoscale_view()
                                                                              
                                                                                   
        if self.tdr_data and len(self.tdr_data.get("rho", [])):
            if self.tdr_axis_mode_var.get() == "time":
                if self.tdr_auto_zoom_var.get():
                    xmax = min(
                        float(self.tdr_data["max_unambiguous_time_s"]) * 1e9,
                        max(5.0 * float(self.tdr_data["resolution_s"]) * 1e9, 10.0),
                    )
                    self.ax_tdr_rho.set_xlim(0.0, xmax)
                    self.ax_tdr_z.set_xlim(0.0, xmax)
            else:
                configured = float(self.tdr_max_distance_var.get())
                if configured > 0:
                    xmin = max(0.0, -float(self.tdr_zero_offset_var.get()))
                    xmax = xmin + configured
                    self.ax_tdr_rho.set_xlim(xmin, xmax)
                    self.ax_tdr_z.set_xlim(xmin, xmax)
                elif self.tdr_auto_zoom_var.get():
                    xmax = min(
                        float(self.tdr_data["max_unambiguous_distance_m"]),
                        max(5.0 * float(self.tdr_data["resolution_m"]), 1.0),
                    )
                    xmin = max(0.0, -float(self.tdr_zero_offset_var.get()))
                    self.ax_tdr_rho.set_xlim(xmin, max(xmin + 1e-9, xmax))
                    self.ax_tdr_z.set_xlim(xmin, max(xmin + 1e-9, xmax))
        if self.overlays_dirty:
            self.redraw_overlays()
            self.overlays_dirty = False
        for canvas in (self.canvas_s, self.canvas_z, self.canvas_smith, self.canvas_tdr):
            canvas.draw_idle()

    def redraw_overlays(self) -> None:
        for artist in self.overlay_artists:
            try:
                artist.remove()
            except Exception:
                pass
        self.overlay_artists.clear()
        for overlay in self.overlays:
            f_mhz = overlay["frequency_hz"] / 1e6
            label = Path(overlay["path"]).stem
            s11 = overlay.get("s11")
            s21 = overlay.get("s21")
            if s11 is not None:
                self.overlay_artists += [
                    self.ax_s11_db.plot(f_mhz, complex_to_db(s11), lw=1.0, alpha=0.7, label=label)[0],
                    self.ax_s11_phase.plot(f_mhz, phase_deg(s11), lw=1.0, alpha=0.7, label=label)[0],
                    self.ax_smith.plot(s11.real, s11.imag, lw=0.9, alpha=0.6, label=label + " S11")[0],
                ]
                z = gamma_to_impedance(s11, self.z0_var.get())
                self.overlay_artists += [
                    self.ax_z11_r.plot(f_mhz, z.real, lw=0.9, alpha=0.6, label=label)[0],
                    self.ax_z11_x.plot(f_mhz, z.imag, lw=0.9, alpha=0.6, label=label)[0],
                ]
            if s21 is not None:
                self.overlay_artists += [
                    self.ax_s21_db.plot(f_mhz, complex_to_db(s21), lw=1.0, alpha=0.7, label=label)[0],
                    self.ax_s21_phase.plot(f_mhz, phase_deg(s21), lw=1.0, alpha=0.7, label=label)[0],
                ]
        for ax in (self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase, self.ax_z11_r, self.ax_z11_x, self.ax_smith):
            ax.relim()
            ax.autoscale_view()
        self._update_legends()

    def on_plot_click(self, event) -> None:
        if len(self.freq) == 0 or event.xdata is None:
            return
        freq_axes = {
            self.ax_s11_db, self.ax_s21_db, self.ax_s11_phase, self.ax_s21_phase,
            self.ax_z11_r, self.ax_z11_x, self.ax_vswr,
        }
        if event.inaxes in freq_axes:
            idx = nearest_index(self.freq, float(event.xdata) * 1e6)
        elif event.inaxes == self.ax_smith and event.ydata is not None and len(self.s11):
            dist = (self.s11.real - event.xdata) ** 2 + (self.s11.imag - event.ydata) ** 2
            idx = int(np.argmin(dist))
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
        i1 = self.markers.get("M1")
        i2 = self.markers.get("M2")
        if i1 is None or i2 is None or not len(self.freq):
            self.marker_delta_var.set(self.tr("no_marker_delta", "Δ(M2−M1): ustaw oba znaczniki"))
            return
        i1 = max(0, min(int(i1), len(self.freq) - 1))
        i2 = max(0, min(int(i2), len(self.freq) - 1))
        parts = [f"Δ(M2−M1): Δf={(self.freq[i2]-self.freq[i1])/1e6:+.6g} MHz"]
        if len(self.s11) == len(self.freq):
            db = complex_to_db(self.s11)
            z = gamma_to_impedance(self.s11, self.z0_var.get())
            swr = vswr_from_s11(self.s11, clip=None)
            parts.append(f"ΔS11={db[i2]-db[i1]:+.3f} dB")
            parts.append(f"ΔR={z[i2].real-z[i1].real:+.3g} Ω")
            parts.append(f"ΔX={z[i2].imag-z[i1].imag:+.3g} Ω")
            if np.isfinite(swr[i1]) and np.isfinite(swr[i2]):
                parts.append(f"ΔVSWR={swr[i2]-swr[i1]:+.3f}")
        if len(self.s21) == len(self.freq):
            db21 = complex_to_db(self.s21)
            parts.append(f"ΔS21={db21[i2]-db21[i1]:+.3f} dB")
        self.marker_delta_var.set(" | ".join(parts))

    def _format_marker(self, marker: str, idx: int) -> str:
        parts = [f"{marker}: {self.freq[idx]/1e6:.6g} MHz"]
        if len(self.s11) == len(self.freq):
            db = complex_to_db(self.s11)[idx]
            ph = phase_deg(self.s11)[idx]
            z = gamma_to_impedance(self.s11[idx], self.z0_var.get())
            swr = vswr_from_s11(np.asarray([self.s11[idx]]), clip=None)[0]
            unc = self._uncertainty_at(idx)
            if unc:
                parts.append(f"S11 {db:.3f}±{unc['s11_db']:.3f} dB")
                parts.append(f"Z {z.real:.2f}±{unc['z_real']:.2f} {z.imag:+.2f}j Ω")
            else:
                parts.append(f"S11 {db:.3f} dB/{ph:.1f}°")
                parts.append(f"Z {z.real:.2f}{z.imag:+.2f}j Ω")
            parts.append(f"VSWR {swr:.3f}")
        if len(self.s21) == len(self.freq):
            parts.append(f"S21 {complex_to_db(self.s21)[idx]:.3f} dB")
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
            messagebox.showwarning(self.tr("warning"), "Najpierw wykonaj pomiar.", parent=self.root)
            return
        self.mem_s11 = self.s11.copy() if len(self.s11) else None
        self.mem_s21 = self.s21.copy() if len(self.s21) else None
        self.update_plots()
        self.set_status("Aktualne ślady zapisano do pamięci.")

    def clear_memory(self) -> None:
        self.mem_s11 = None
        self.mem_s21 = None
        self.update_plots()
        self.set_status(self.tr("memory_cleared", "Wyczyszczono ślady pamięci."))

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
            self.show_vswr_var.set(False)
            self.show_tdr_var.set(False)
        self.update_plots()

    def open_range_dialog(self) -> None:
        if not self.connected or len(self.freq) < 2:
            messagebox.showwarning(self.tr("warning"), self.tr("connect_nanovna_first", "Najpierw połącz NanoVNA."), parent=self.root)
            return
        RangeDialog(self.root, (float(self.freq[0]), float(self.freq[-1]), len(self.freq)), self.set_range, self.tr)

    def set_range(self, start: float, stop: float, points: int, completion_callback=None, failure_callback=None) -> None:
        was_running = self.running
        self.stop_running()
        self.set_status("Ustawianie zakresu…")

        def worker():
            try:
                f = self.vna.set_sweep(start, stop, points)
                self.post_ui(done, f)
            except Exception as exc:
                self.post_ui(failed, exc)

        def done(f):
            self.freq = np.asarray(f, float)
            self.s11 = self.s21 = self.raw_s11 = self.raw_s21 = np.array([], complex)
            self.mem_s11 = self.mem_s21 = None
            self.history.clear()
            self.clear_markers()
            self._load_calibration_for_current_range()
            self.set_status(f"Ustawiono zakres {self.freq[0]/1e6:.6g}–{self.freq[-1]/1e6:.6g} MHz, {len(self.freq)} pkt.")
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
            self.set_status(f"Sweep-range error: {exc}")
            if was_running:
                self.start_running()

        threading.Thread(target=worker, daemon=True).start()

    def _device_id(self) -> str:
        return self.vna.info.serial_number or self.vna.info.port or "unknown"

    def _load_calibration_for_current_range(self) -> None:
        self.calibration = self.calibration_store.load_exact(self.freq, self._device_id())
        self._set_calibration_status(self.calibration is not None)
        if self.calibration:
            self.log(self.tr("calibration_loaded_log", "Wczytano kalibrację: {date}").format(date=self.calibration.metadata.get("created_at", "")))
        else:
            self.log(self.tr("calibration_not_found_log", "Brak kalibracji dla dokładnie tego zakresu i liczby punktów."))

    def _set_calibration_status(self, calibrated: bool) -> None:
        self.calibration_var.set(self.tr("calibrated") if calibrated else self.tr("uncalibrated"))
        self.calibration_label.configure(style="Calibration.Good.TLabel" if calibrated else "Calibration.Bad.TLabel")

    def start_calibration(self) -> None:
        if not self.connected:
            messagebox.showwarning(self.tr("warning"), self.tr("connect_nanovna_first", "Najpierw połącz NanoVNA."), parent=self.root)
            return
        was_running = self.running
        self.stop_running()
        steps = [
            ("open", self.tr("cal_open_prompt", "Podłącz standard OPEN (rozwarcie) do portu 1.")),
            ("short", self.tr("cal_short_prompt", "Podłącz standard SHORT (zwarcie) do portu 1.")),
            ("load", self.tr("cal_load_prompt", "Podłącz standard LOAD 50 Ω do portu 1.")),
            ("thru", self.tr("cal_thru_prompt", "Połącz port 1 z portem 2 standardem THRU.")),
        ]
        captured: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

        def clear_demo_standard() -> None:
            if self.vna.demo:
                self.vna.set_demo_standard(None)

        def next_step(i: int):
            if i >= len(steps):
                clear_demo_standard()
                try:
                    profile = create_profile(
                        captured["open"][0], captured["open"][1], captured["short"][1],
                        captured["load"][1], captured["thru"][2], self.vna.info.to_dict(),
                    )
                    folder = self.calibration_store.save(profile, self._device_id())
                    self.calibration = profile
                    self._set_calibration_status(True)
                    self.set_status(self.tr("calibration_saved_status", "Kalibracja zakończona i zapisana: {folder}").format(folder=folder))
                    messagebox.showinfo(self.tr("info"), self.tr("calibration_saved_current_range", "Kalibracja OSLT została zapisana dla aktualnego zakresu."), parent=self.root)
                except Exception as exc:
                    messagebox.showerror(self.tr("error"), self.tr("calibration_create_failed", "Nie udało się utworzyć kalibracji:\n{error}").format(error=exc), parent=self.root)
                if was_running:
                    self.start_running()
                else:
                    self.single_measurement()
                return
            key, text = steps[i]
            if not messagebox.askokcancel(self.tr("calibration_oslt_title", "Kalibracja OSLT"), text + "\n\n" + self.tr("click_ok_to_calibrate", "Kliknij OK, aby skalibrować."), parent=self.root):
                clear_demo_standard()
                self.set_status(self.tr("calibration_cancelled", "Kalibracja anulowana."))
                if was_running:
                    self.start_running()
                return
            self.set_status(self.tr("calibration_in_progress", "Kalibracja…"))

            def worker():
                try:
                    if self.vna.demo:
                        self.vna.set_demo_standard(key)
                    rows = [self.vna.read_measurement(True, True) for _ in range(3)]
                    n = min(min(len(r[0]), len(r[1]) if r[1] is not None else 0, len(r[2]) if r[2] is not None else 0) for r in rows)
                    f = rows[0][0][:n]
                    s11 = np.mean(np.vstack([r[1][:n] for r in rows]), axis=0)
                    s21 = np.mean(np.vstack([r[2][:n] for r in rows]), axis=0)
                    self.post_ui(captured_done, key, f, s11, s21, i)
                except Exception as exc:
                    self.post_ui(calibration_failed, exc)

            threading.Thread(target=worker, daemon=True).start()

        def captured_done(key, f, s11, s21, i):
            captured[key] = (np.asarray(f), np.asarray(s11), np.asarray(s21))
            self.set_status(self.tr("calibration_in_progress", "Kalibracja…"))
            next_step(i + 1)

        def calibration_failed(exc):
            clear_demo_standard()
            messagebox.showerror(self.tr("error"), self.tr("calibration_measurement_error", "Błąd pomiaru kalibracyjnego:\n{error}").format(error=exc), parent=self.root)
            self.set_status(self.tr("calibration_interrupted", "Kalibracja przerwana."))
            if was_running:
                self.start_running()

        next_step(0)

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
        self.set_status("Export settings saved.")

    def manual_save_csv(self) -> None:
        if not len(self.freq):
            messagebox.showwarning(self.tr("warning"), self.tr("no_data_to_save", "Nie ma danych do zapisania."), parent=self.root)
            return
        initial = self.settings.get("last_output_dir", str(Path.home()))
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title=self.tr("save_measurement", "Zapisz pomiar"),
            initialdir=initial,
            defaultextension=".csv",
            filetypes=[("Measurement base name", "*.csv"), ("All files", "*.*")],
        )
        if not filename:
            return
        path = Path(filename)
        self.settings.set("last_output_dir", str(path.parent))
        formats = dict(self.settings.get("export_formats", {"csv": True, "s1p": False, "s2p": False}))
        written = self._save_snapshot_to_path(path, dict(self.settings.data["save_fields"]), formats)
        self.set_status("Saved: " + ", ".join(item.name for item in written))

    def _save_snapshot_to_path(
        self,
        path: Path,
        fields: dict[str, bool],
        formats: dict[str, bool] | None = None,
    ) -> list[Path]:
        selected_formats = formats or {"csv": True, "s1p": False, "s2p": False}
        return save_measurement_files(
            base_path=path,
            formats=selected_formats,
            frequency_hz=self.freq.copy(),
            s11=self.s11.copy() if len(self.s11) else None,
            s21=self.s21.copy() if len(self.s21) else None,
            mem_s11=None if self.mem_s11 is None else self.mem_s11.copy(),
            mem_s21=None if self.mem_s21 is None else self.mem_s21.copy(),
            markers=dict(self.markers),
            fields=fields,
            z0=float(self.z0_var.get()),
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
            raise RuntimeError("No current measurement.")
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
        self.set_status(f"AUTO: zapisano indeks {index}: {candidate_stem}")
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
        )

    def automation_data_to_mem(self) -> str:
        if not len(self.freq):
            raise RuntimeError("No current measurement.")
        self.mem_s11 = self.s11.copy() if len(self.s11) else None
        self.mem_s21 = self.s21.copy() if len(self.s21) else None
        self.show_memory_var.set(True)
        self.update_plots()
        return "nanoDataMem: ustawiono aktualne dane jako pamięć."

    def automation_set_range(self, start_hz: float, stop_hz: float, completion_callback, failure_callback) -> None:
        if not self.connected or len(self.freq) < 2:
            raise RuntimeError("Najpierw połącz NanoVNA.")
        if start_hz <= 0 or stop_hz <= start_hz:
            raise ValueError("Stop musi być większy od startu.")
        self.set_range(start_hz, stop_hz, len(self.freq), completion_callback, failure_callback)

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
            raise ValueError("Plik referencyjny ma za mało poprawnych punktów.")
        order = np.argsort(frequency)
        frequency = frequency[order]
        data = data[order]
        real = np.interp(self.freq, frequency, data.real)
        imag = np.interp(self.freq, frequency, data.imag)
        return real + 1j * imag

    def _set_memory_from_data(self, data: dict[str, Any], label: str) -> str:
        if not len(self.freq):
            raise RuntimeError("No current measurement.")
        source_frequency = np.asarray(data.get("frequency_hz", []), dtype=float)
        self.mem_s11 = self._interpolate_reference(source_frequency, data["s11"]) if "s11" in data else None
        self.mem_s21 = self._interpolate_reference(source_frequency, data["s21"]) if "s21" in data else None
        if self.mem_s11 is None and self.mem_s21 is None:
            raise ValueError("Plik nie zawiera S11 ani S21.")
        self.update_plots()
        return f"Ustawiono pamięć z: {label}"

    def apply_reference(self, source: str, path: Path | None) -> str:
        if source == "current":
            self.trace_to_mem()
            return "Ustawiono aktualne dane jako pamięć."
        if source.startswith("overlay:"):
            index = int(source.split(":", 1)[1])
            if index < 0 or index >= len(self.overlays):
                raise IndexError("Wybrany nałożony plik nie istnieje.")
            overlay = self.overlays[index]
            return self._set_memory_from_data(overlay, Path(overlay.get("path", "overlay")).name)
        if source == "file" and path is not None:
            data = load_measurement_file(path)
            return self._set_memory_from_data(data, path.name)
        raise ValueError("Nieznane źródło pamięci.")

    def add_overlay(self) -> None:
        files = filedialog.askopenfilenames(
            parent=self.root,
            title=self.tr("overlay_files", "Dodaj pliki do wykresów"),
            filetypes=[("Measurement files", "*.csv *.s1p *.s2p"), ("All files", "*.*")],
        )
        if not files:
            return
        added = 0
        for filename in files:
            try:
                self.overlays.append(load_measurement_file(Path(filename)))
                self.overlays_dirty = True
                added += 1
            except Exception as exception:
                messagebox.showerror(self.tr("error"), f"{filename}\n{exception}", parent=self.root)
        self.update_plots()
        self.set_status(f"Dodano {added} plików do wykresów.")
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window._refresh_references()

    def clear_overlays(self) -> None:
        self.overlays.clear()
        self.overlays_dirty = True
        self.update_plots()
        self.set_status("Usunięto nałożone pomiary.")
        if self.auto_window and self.auto_window.winfo_exists():
            self.auto_window._refresh_references()

    def refresh_plugins(self) -> None:
        if not hasattr(self, "plugins_button_frame"):
            return
        for child in self.plugins_button_frame.winfo_children():
            child.destroy()
        plugins = sorted(path for path in self.plugin_dir.glob("*.py") if not path.name.startswith("_"))
        if not plugins:
            ttk.Label(self.plugins_button_frame, text=self.tr("no_plugins", "Brak plików .py w folderze plugins")).grid(row=0, column=0, sticky="w")
            return
        for index, plugin_path in enumerate(plugins):
            title = plugin_path.stem
            display = title if len(title) <= 20 else title[:17] + "..."
            button = ttk.Button(self.plugins_button_frame, text=display, width=18, command=lambda path=plugin_path: self.open_plugin(path))
            button.grid(row=index // 4, column=index % 4, sticky="w", padx=2, pady=2)

    def open_plugin(self, plugin_path: Path) -> None:
        if not len(self.freq):
            messagebox.showwarning(self.tr("warning"), "Najpierw wykonaj pomiar.", parent=self.root)
            return
        if plugin_path.name == "dopasuj_model_linii_v3.py":
            if not (len(self.s11) and len(self.s21)):
                messagebox.showwarning(self.tr("warning"), "Generator modelu linii wymaga S11 i S21.", parent=self.root)
                return
            ModelOptionsDialog(
                self.root,
                lambda length, quality, output: self.run_plugin(plugin_path, output, ["--length", length, "--quality", quality, "--no-show"]),
                self.tr,
            )
            return
        PluginOptionsDialog(self.root, plugin_path.stem, lambda output: self.run_plugin(plugin_path, output), self.tr)

    def run_plugin(self, plugin_path: Path, output_base: Path, extra_arguments: list[str] | None = None) -> None:
        if not self.python_command:
            messagebox.showerror(self.tr("error"), self.tr("python_interpreter_not_found", "Nie znaleziono interpretera Python."), parent=self.root)
            return
        output_dir = output_base / f"{plugin_path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        output_dir.mkdir(parents=True, exist_ok=True)
        measurement_path = output_dir / "measurement.csv"
        fields = {
            "s11_db_phase": bool(len(self.s11)),
            "s21_db_phase": bool(len(self.s21)),
            "s11_complex": False,
            "s21_complex": False,
            "z_s11": bool(len(self.s11)),
            "vswr": False,
            "memory": False,
            "markers": False,
            "tdr": False,
        }
        self._save_snapshot_to_path(measurement_path, fields, {"csv": True, "s1p": False, "s2p": False})
        self.plugin_resume_after = self.running
        self.stop_running()
        log_window = ProcessLogWindow(self.root, plugin_path.stem, self.stop_plugin_process, self.tr)
        command = [*self.python_command, "-u", str(plugin_path), "--input", str(measurement_path), "--output", str(output_dir)]
        if extra_arguments:
            command.extend(extra_arguments)
        process_options: dict[str, Any] = {}
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.plugin_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(output_dir),
                bufsize=1,
                **process_options,
            )
        except Exception as exception:
            messagebox.showerror(self.tr("error"), str(exception), parent=self.root)
            if self.plugin_resume_after:
                self.start_running()
            return
        self.set_status(f"Plugin started: {plugin_path.name}")

        def reader() -> None:
            assert self.plugin_process and self.plugin_process.stdout
            for line in self.plugin_process.stdout:
                self.post_ui(log_window.append, line.rstrip())
            return_code = self.plugin_process.wait()
            self.post_ui(self._plugin_finished, return_code, output_dir, log_window, plugin_path.name)

        threading.Thread(target=reader, daemon=True).start()

    def stop_plugin_process(self) -> None:
        if self.plugin_process and self.plugin_process.poll() is None:
            try:
                self.plugin_process.terminate()
                self.set_status("Plugin execution stopped.")
            except Exception as exception:
                self.set_status(f"Could not stop plugin: {exception}")

    def _plugin_finished(self, return_code: int, output_dir: Path, log_window: ProcessLogWindow, plugin_name: str) -> None:
        if return_code == 0:
            log_window.append(f"\nFINISHED. Results: {output_dir}")
            self.set_status(f"Plugin {plugin_name} finished: {output_dir}")
        else:
            log_window.append(f"\nProcess finished with code {return_code}.")
            self.set_status(f"Plugin {plugin_name} finished with code {return_code}.")
        self.plugin_process = None
        if self.plugin_resume_after:
            self.start_running()

    def current_metrics(self) -> dict[str, float]:
        metrics = {name: float("nan") for name in AutomationWindow.METRICS}
        i1 = self.markers.get("M1")
        i2 = self.markers.get("M2")
        if i1 is not None and i2 is not None and len(self.freq):
            if i1 < len(self.freq) and i2 < len(self.freq):
                metrics["Δf M2-M1 [Hz]"] = float(self.freq[i2] - self.freq[i1])
        if len(self.s11):
            db11 = complex_to_db(self.s11)
            vswr = vswr_from_s11(self.s11, clip=None)
            z = gamma_to_impedance(self.s11, self.z0_var.get())
            metrics["S11 min [dB]"] = float(np.nanmin(db11))
            metrics["VSWR max"] = float(np.nanmax(vswr[np.isfinite(vswr)])) if np.any(np.isfinite(vswr)) else float("inf")
            if i1 is not None and i1 < len(self.s11):
                metrics["S11 przy M1 [dB]"] = float(db11[i1])
                metrics["VSWR przy M1"] = float(vswr[i1])
                metrics["Re(Z) przy M1 [ohm]"] = float(np.real(z[i1]))
                metrics["|Z| przy M1 [ohm]"] = float(np.abs(z[i1]))
            if i2 is not None and i2 < len(self.s11):
                metrics["S11 przy M2 [dB]"] = float(db11[i2])
                metrics["VSWR przy M2"] = float(vswr[i2])
                metrics["Re(Z) przy M2 [ohm]"] = float(np.real(z[i2]))
                metrics["|Z| przy M2 [ohm]"] = float(np.abs(z[i2]))
            if i1 is not None and i2 is not None and i1 < len(self.s11) and i2 < len(self.s11):
                metrics["ΔS11 M2-M1 [dB]"] = float(db11[i2] - db11[i1])
                metrics["ΔVSWR M2-M1"] = float(vswr[i2] - vswr[i1])
                metrics["ΔRe(Z) M2-M1 [ohm]"] = float(np.real(z[i2] - z[i1]))
                metrics["Δ|Z| M2-M1 [ohm]"] = float(np.abs(z[i2]) - np.abs(z[i1]))
        if len(self.s21):
            db21 = complex_to_db(self.s21)
            metrics["S21 min [dB]"] = float(np.nanmin(db21))
            if i1 is not None and i1 < len(self.s21):
                metrics["S21 przy M1 [dB]"] = float(db21[i1])
            if i2 is not None and i2 < len(self.s21):
                metrics["S21 przy M2 [dB]"] = float(db21[i2])
            if i1 is not None and i2 is not None and i1 < len(self.s21) and i2 < len(self.s21):
                metrics["ΔS21 M2-M1 [dB]"] = float(db21[i2] - db21[i1])
        return metrics

    def _report_marker_rows(self) -> list[list[str]]:
        rows: list[list[str]] = []
        for marker in ("M1", "M2"):
            idx = self.markers.get(marker)
            if idx is None or idx >= len(self.freq):
                continue
            s11_text = s21_text = z_text = vswr_text = "--"
            if len(self.s11) == len(self.freq):
                db = complex_to_db(self.s11)[idx]
                ph = phase_deg(self.s11)[idx]
                z = gamma_to_impedance(self.s11[idx], self.z0_var.get())
                swr = vswr_from_s11(np.asarray([self.s11[idx]]), clip=None)[0]
                unc = self._uncertainty_at(idx)
                s11_text = f"{db:.4f} dB / {ph:.2f}°"
                z_text = f"{z.real:.3f}{z.imag:+.3f}j Ω"
                if unc:
                    s11_text += f"; σ={unc['s11_db']:.4f} dB"
                    z_text += f"; σR={unc['z_real']:.3g}, σX={unc['z_imag']:.3g}"
                vswr_text = f"{swr:.4f}"
            if len(self.s21) == len(self.freq):
                s21_text = f"{complex_to_db(self.s21)[idx]:.4f} dB / {phase_deg(self.s21)[idx]:.2f}°"
            rows.append([marker, f"{self.freq[idx]:.6g} Hz", s11_text, s21_text, z_text, vswr_text])
        i1 = self.markers.get("M1")
        i2 = self.markers.get("M2")
        if i1 is not None and i2 is not None and i1 < len(self.freq) and i2 < len(self.freq):
            ds11 = ds21 = dz = dvswr = "--"
            if len(self.s11) == len(self.freq):
                db11 = complex_to_db(self.s11)
                z = gamma_to_impedance(self.s11, self.z0_var.get())
                swr = vswr_from_s11(self.s11, clip=None)
                ds11 = f"{db11[i2]-db11[i1]:+.4f} dB"
                dz = f"ΔR={z[i2].real-z[i1].real:+.4g} Ω; ΔX={z[i2].imag-z[i1].imag:+.4g} Ω"
                if np.isfinite(swr[i1]) and np.isfinite(swr[i2]):
                    dvswr = f"{swr[i2]-swr[i1]:+.4f}"
            if len(self.s21) == len(self.freq):
                db21 = complex_to_db(self.s21)
                ds21 = f"{db21[i2]-db21[i1]:+.4f} dB"
            rows.append(["Δ M2−M1", f"{self.freq[i2]-self.freq[i1]:+.6g} Hz", ds11, ds21, dz, dvswr])
        return rows

    def _save_plot_images(self, folder: Path) -> list[Path]:
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, fig in (("s_parametry", self.fig_s), ("impedancja_vswr", self.fig_z), ("smith", self.fig_smith), ("tdr", self.fig_tdr)):
            path = folder / f"{name}.png"
            fig.savefig(path, dpi=170, bbox_inches="tight")
            paths.append(path)
        return paths

    def manual_report(self) -> None:
        if not len(self.freq):
            messagebox.showwarning(self.tr("warning"), self.tr("no_report_data", "Brak danych do raportu."), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(parent=self.root, title=self.tr("pdf_report", "Raport PDF"), defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if not filename:
            return
        try:
            path = self._generate_report(Path(filename), {}, [], [])
            self.set_status(f"PDF report generated: {path}")
        except Exception as exc:
            messagebox.showerror(self.tr("error"), str(exc), parent=self.root)

    def _automation_report(self, output_path: Path, description: dict[str, str], images: list[Path], saved_files: list[Path]) -> Path | None:
        if not len(self.freq):
            return None
        path = self._generate_report(output_path, description, images, saved_files)
        self.set_status(f"Wygenerowano raport serii: {path.name}")
        return path

    def _generate_report(self, output_path: Path, description: dict[str, str], images: list[Path], saved_files: list[Path]) -> Path:
        assets = output_path.parent / (output_path.stem + "_assets")
        plots = self._save_plot_images(assets)
        device = self.vna.info.to_dict()
        language = self.language_var.get()
        english = language == "en"
        measurement = {
            ("Frequency range" if english else "Zakres częstotliwości"): f"{self.freq[0]:.6g}–{self.freq[-1]:.6g} Hz",
            ("Number of points" if english else "Liczba punktów"): len(self.freq),
            "Z0": f"{self.z0_var.get():g} Ω",
            "S11": ("enabled" if self.enable_s11_var.get() else "disabled") if english else ("włączone" if self.enable_s11_var.get() else "wyłączone"),
            "S21": ("enabled" if self.enable_s21_var.get() else "disabled") if english else ("włączone" if self.enable_s21_var.get() else "wyłączone"),
            "TDR": ("enabled" if self.show_tdr_var.get() else "disabled") if english else ("włączone" if self.show_tdr_var.get() else "wyłączone"),
            "VF": self.vf_var.get(),
            ("TDR axis" if english else "Oś TDR"): self.tdr_axis_mode_var.get(),
            ("TDR window" if english else "Okno TDR"): self.tdr_window_var.get(),
            ("Maximum TDR distance" if english else "Maks. odległość TDR"): self.tdr_max_distance_var.get(),
            ("TDR zero offset" if english else "Przesunięcie zera TDR"): self.tdr_zero_offset_var.get(),
            ("TDR resolution" if english else "Rozdzielczość TDR"): (
                f"{float(self.tdr_data.get('resolution_m', float('nan'))):.6g} m"
                if self.tdr_data else "--"
            ),
        }
        cal_info = self.calibration.metadata if self.calibration else None
        if english:
            note = (
                "The displayed ± and σ values are Type A repeatability estimates calculated from the latest "
                f"{len(self.history)} readings. They do not include calibration-standard, cable, connector or temperature-drift errors."
            )
            default_title = "AutoNanoVNA measurement report"
        else:
            note = (
                "Podane wartości ± i σ są szacowaną powtarzalnością typu A obliczoną z ostatnich "
                f"{len(self.history)} odczytów. Nie obejmują błędów wzorców kalibracyjnych, przewodów, złączy ani dryftu temperaturowego."
            )
            default_title = "Raport pomiaru AutoNanoVNA"
        title = description.get("Tytuł badania") or description.get("Study title") or default_title
        return generate_pdf_report(
            output_path, title, device, measurement, cal_info, self._report_marker_rows(), note,
            description=description, plot_paths=plots, image_paths=images, saved_files=saved_files,
            language=language,
        )

    def on_close(self) -> None:
        self.closing = True
        try:
            self.stop_running()
            if self.auto_window and self.auto_window.winfo_exists():
                self.auto_window.stop()
            self.stop_plugin_process()
            self.vna.close()
            self.settings.data.update({
                "refresh_ms": int(self.interval_var.get()),
                "z0_ohm": float(self.z0_var.get()),
                "velocity_factor": float(self.vf_var.get()),
                "tdr_window": self.tdr_window_var.get(),
                "tdr_axis_mode": self.tdr_axis_mode_var.get(),
                "tdr_max_distance_m": float(self.tdr_max_distance_var.get()),
                "tdr_zero_offset_m": float(self.tdr_zero_offset_var.get()),
                "tdr_auto_zoom": bool(self.tdr_auto_zoom_var.get()),
                "theme": self.theme_var.get(),
                "language": self.language_var.get(),
            })
            self.settings.save()
        finally:
            self.root.destroy()
