from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


class RangeDialog(tk.Toplevel):
    def __init__(self, parent, current: tuple[float, float, int], callback, tr=None):
        super().__init__(parent)
        self.tr = tr
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.title(translate("range_title", "Zakres częstotliwości"))
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        start, stop, points = current
        self.start_var = tk.StringVar(value=f"{start:g}")
        self.stop_var = tk.StringVar(value=f"{stop:g}")
        self.points_var = tk.IntVar(value=points)
        self.unit_var = tk.StringVar(value="Hz")
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        for row, (label, variable) in enumerate(((translate("start_freq", "Start"), self.start_var), (translate("stop_freq", "Stop"), self.stop_var))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=variable, width=18).grid(row=row, column=1, padx=5)
        ttk.Label(frame, text=translate("unit", "Jednostka")).grid(row=0, column=2, padx=(8, 2))
        ttk.Combobox(frame, textvariable=self.unit_var, values=["Hz", "kHz", "MHz"], state="readonly", width=7).grid(row=0, column=3)
        ttk.Label(frame, text=translate("points", "Punkty")).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=2, to=10001, textvariable=self.points_var, width=16).grid(row=2, column=1, padx=5)
        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, columnspan=4, pady=(12, 0), sticky="e")
        ttk.Button(buttons, text=translate("cancel", "Anuluj"), command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text=translate("set", "Ustaw"), command=self.accept).pack(side="right", padx=4)

    def accept(self):
        try:
            multiplier = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}[self.unit_var.get()]
            start = float(self.start_var.get().replace(",", ".")) * multiplier
            stop = float(self.stop_var.get().replace(",", ".")) * multiplier
            points = int(self.points_var.get())
            if start <= 0 or stop <= start or points < 2:
                raise ValueError
        except Exception:
            messagebox.showerror(translate("error", "Błąd"), translate("invalid_range_points", "Podaj poprawny zakres i liczbę punktów."), parent=self)
            return
        self.destroy()
        self.callback(start, stop, points)


class SaveSettingsDialog(tk.Toplevel):
    FIELD_LABELS = {
        "s11_db_phase": "S11: dB i faza",
        "s21_db_phase": "S21: dB i faza",
        "s11_complex": "S11: Re i Im",
        "s21_complex": "S21: Re i Im",
        "z_s11": "Impedancja z S11",
        "vswr": "VSWR",
        "memory": "Ślady pamięci",
        "markers": "Znaczniki M1/M2",
        "tdr": "Osobny plik TDR CSV",
    }
    FORMAT_LABELS = {
        "csv": "CSV NanoVNA",
        "s1p": "Touchstone .s1p",
        "s2p": "Touchstone .s2p",
    }

    def __init__(self, parent, fields: dict[str, bool], formats: dict[str, bool], callback, tr=None):
        super().__init__(parent)
        self.tr = tr
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.title(translate("save_settings_title", "Ustawienia zapisu"))
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.field_vars = {key: tk.BooleanVar(value=fields.get(key, False)) for key in self.FIELD_LABELS}
        self.format_vars = {key: tk.BooleanVar(value=formats.get(key, False)) for key in self.FORMAT_LABELS}
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        formats_frame = ttk.LabelFrame(frame, text=translate("save_formats", "Formaty plików"), padding=8)
        formats_frame.pack(fill="x", pady=(0, 8))
        for column, (key, label) in enumerate(self.FORMAT_LABELS.items()):
            ttk.Checkbutton(formats_frame, text=translate(f"save_format_{key}", label), variable=self.format_vars[key]).grid(row=0, column=column, sticky="w", padx=8, pady=2)
        ttk.Label(formats_frame, text=translate("s2p_assumption", ".s2p zapisuje S12=S21 i S22=S11, ponieważ NanoVNA udostępnia S11 i S21."), wraplength=510).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(5, 0))
        fields_frame = ttk.LabelFrame(frame, text=translate("choose_saved_fields", "Kolumny CSV"), padding=8)
        fields_frame.pack(fill="x")
        for row, (key, label) in enumerate(self.FIELD_LABELS.items()):
            ttk.Checkbutton(fields_frame, text=translate(f"save_field_{key}", label), variable=self.field_vars[key]).grid(row=row // 2, column=row % 2, sticky="w", padx=8, pady=2)
        ttk.Label(frame, text=translate("save_format_note", "Wybrane formaty są zapisywane równocześnie z jedną nazwą bazową."), wraplength=510).pack(anchor="w", pady=(9, 5))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text=translate("cancel", "Anuluj"), command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text=translate("save_settings_button", "Zapisz ustawienia"), command=self.accept).pack(side="right", padx=4)

    def accept(self):
        fields = {key: variable.get() for key, variable in self.field_vars.items()}
        formats = {key: variable.get() for key, variable in self.format_vars.items()}
        if not any(formats.values()):
            messagebox.showerror(translate("error", "Błąd"), translate("choose_one_file_format", "Wybierz co najmniej jeden format pliku."), parent=self)
            return
        self.callback(fields, formats)
        self.destroy()


class ModelOptionsDialog(tk.Toplevel):
    def __init__(self, parent, callback, tr=None):
        super().__init__(parent)
        self.tr = tr
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.title(translate("model_title", "Generowanie modelu linii"))
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.length_var = tk.StringVar(value="unknown")
        self.quality_var = tk.StringVar(value="balanced")
        self.output_var = tk.StringVar(value=str(Path.home()))
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=translate("line_length", "Długość linii [m] lub unknown")).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.length_var, width=30).grid(row=0, column=1, padx=6)
        ttk.Label(frame, text=translate("quality", "Dokładność")).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.quality_var, values=["quick", "balanced", "thorough"], state="readonly", width=27).grid(row=1, column=1, padx=6)
        ttk.Label(frame, text=translate("output_folder", "Folder wyników")).grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.output_var, width=30).grid(row=2, column=1, padx=6)
        ttk.Button(frame, text=translate("choose", "Wybierz…"), command=self.choose).grid(row=2, column=2)
        ttk.Label(frame, text=translate("model_time_warning", "Pomiar zostanie zatrzymany na czas działania dodatku."), wraplength=470).grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 4))
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text=translate("cancel", "Anuluj"), command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text=translate("run", "Uruchom"), command=self.accept).pack(side="right", padx=4)

    def choose(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def accept(self):
        length = self.length_var.get().strip() or "unknown"
        if length.lower() != "unknown":
            try:
                if float(length.replace(",", ".")) <= 0:
                    raise ValueError
                length = length.replace(",", ".")
            except Exception:
                messagebox.showerror(translate("error", "Błąd"), translate("invalid_length_unknown", "Długość musi być dodatnia albo 'unknown'."), parent=self)
                return
        output = Path(self.output_var.get()).expanduser()
        self.destroy()
        self.callback(length, self.quality_var.get(), output)


class PluginOptionsDialog(tk.Toplevel):
    def __init__(self, parent, plugin_name: str, callback, tr=None):
        super().__init__(parent)
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.title(plugin_name)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.output_var = tk.StringVar(value=str(Path.home()))
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=translate("output_folder", "Folder wyników")).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.output_var, width=38).grid(row=0, column=1, padx=6)
        ttk.Button(frame, text=translate("choose", "Wybierz…"), command=self.choose).grid(row=0, column=2)
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text=translate("cancel", "Anuluj"), command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text=translate("run", "Uruchom"), command=self.accept).pack(side="right", padx=4)

    def choose(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def accept(self):
        output = Path(self.output_var.get()).expanduser()
        self.destroy()
        self.callback(output)


class TDRSettingsDialog(tk.Toplevel):
    def __init__(self, parent, values: dict, callback, current_sweep: tuple[float, float] | None = None, tr=None):
        super().__init__(parent)
        self.tr = tr
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.title(translate("tdr_settings_title", "Ustawienia TDR"))
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.callback = callback
        self.axis_var = tk.StringVar(value=str(values.get("axis_mode", "distance")))
        self.vf_var = tk.StringVar(value=f"{float(values.get('velocity_factor', 0.66)):g}")
        self.z0_var = tk.StringVar(value=f"{float(values.get('z0_ohm', 50.0)):g}")
        self.window_var = tk.StringVar(value=str(values.get("window", "hann")))
        self.max_distance_var = tk.StringVar(value=f"{float(values.get('max_distance_m', 0.0)):g}")
        self.zero_offset_var = tk.StringVar(value=f"{float(values.get('zero_offset_m', 0.0)):g}")
        self.auto_zoom_var = tk.BooleanVar(value=bool(values.get("auto_zoom", True)))
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        row = 0
        ttk.Label(frame, text=translate("tdr_axis", "Oś TDR")).grid(row=row, column=0, sticky="w", pady=4)
        axis_frame = ttk.Frame(frame)
        axis_frame.grid(row=row, column=1, sticky="w", padx=6)
        ttk.Radiobutton(axis_frame, text=translate("tdr_mode_distance", "Odległość"), value="distance", variable=self.axis_var).pack(side="left")
        ttk.Radiobutton(axis_frame, text=translate("tdr_mode_time", "Czas"), value="time", variable=self.axis_var).pack(side="left", padx=(8, 0))
        row += 1
        ttk.Label(frame, text=translate("tdr_vf", "VF — współczynnik prędkości")).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.vf_var, width=20).grid(row=row, column=1, sticky="w", padx=6)
        row += 1
        ttk.Label(frame, text=translate("tdr_z0", "Z0 — impedancja odniesienia [Ω]")).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.z0_var, width=20).grid(row=row, column=1, sticky="w", padx=6)
        row += 1
        ttk.Label(frame, text=translate("tdr_window_label", "Okno")).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(frame, textvariable=self.window_var, values=["rectangular", "hann", "kaiser"], state="readonly", width=18).grid(row=row, column=1, sticky="w", padx=6)
        row += 1
        ttk.Label(frame, text=translate("tdr_max_distance", "Maks. odległość wykresu [m], 0 = auto")).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.max_distance_var, width=20).grid(row=row, column=1, sticky="w", padx=6)
        row += 1
        ttk.Label(frame, text=translate("tdr_zero_offset", "Przesunięcie zera odległości [m]")).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.zero_offset_var, width=20).grid(row=row, column=1, sticky="w", padx=6)
        row += 1
        ttk.Checkbutton(frame, text=translate("tdr_auto_zoom", "Automatycznie przybliż użyteczny początek wykresu"), variable=self.auto_zoom_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=(5, 2))
        row += 1
        explanation = translate("tdr_settings_explanation", "Z0 jest impedancją odniesienia VNA. VF służy do przeliczenia opóźnienia na metry. Rozdzielczość zależy przede wszystkim od szerokości pasma.")
        ttk.Label(frame, text=explanation, wraplength=570, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 4))
        row += 1
        if current_sweep and current_sweep[1] > current_sweep[0]:
            start, stop = current_sweep
            bandwidth = stop - start
            try:
                velocity_factor = float(self.vf_var.get().replace(",", "."))
                resolution = 299_792_458.0 * velocity_factor / (2.0 * bandwidth)
                text = translate("tdr_current_resolution", "Dla aktualnego pasma szacowana rozdzielczość: {value} m").format(value=f"{resolution:.4g}")
                ttk.Label(frame, text=text, style="Info.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(2, 6))
                row += 1
            except Exception:
                pass
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text=translate("cancel", "Anuluj"), command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text=translate("set", "Ustaw"), command=self.accept).pack(side="right", padx=4)

    def accept(self):
        translate = lambda key, fallback: self.tr(key, fallback) if self.tr else fallback
        try:
            velocity_factor = float(self.vf_var.get().replace(",", "."))
            reference_impedance = float(self.z0_var.get().replace(",", "."))
            max_distance = float(self.max_distance_var.get().replace(",", "."))
            zero_offset = float(self.zero_offset_var.get().replace(",", "."))
            if not 0 < velocity_factor <= 1 or reference_impedance <= 0 or max_distance < 0:
                raise ValueError
        except Exception:
            messagebox.showerror(translate("error", "Błąd"), translate("tdr_settings_invalid", "Sprawdź VF, Z0 oraz zakres odległości."), parent=self)
            return
        values = {
            "axis_mode": self.axis_var.get(),
            "velocity_factor": velocity_factor,
            "z0_ohm": reference_impedance,
            "window": self.window_var.get(),
            "max_distance_m": max_distance,
            "zero_offset_m": zero_offset,
            "auto_zoom": self.auto_zoom_var.get(),
        }
        self.destroy()
        self.callback(values)


class ProcessLogWindow(tk.Toplevel):
    def __init__(self, parent, title: str, stop_callback=None, tr=None):
        super().__init__(parent)
        self.title(title)
        translate = lambda key, fallback: tr(key, fallback) if tr else fallback
        self.geometry("850x500")
        self.stop_callback = stop_callback
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="word", font=("Consolas", 9))
        scrollbar = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        buttons = ttk.Frame(self, padding=(8, 0, 8, 8))
        buttons.pack(fill="x")
        if stop_callback:
            ttk.Button(buttons, text=translate("abort", "Przerwij"), command=stop_callback).pack(side="left")
        ttk.Button(buttons, text=translate("close", "Zamknij"), command=self.destroy).pack(side="right")

    def append(self, line: str):
        self.text.insert("end", line.rstrip() + "\n")
        self.text.see("end")
