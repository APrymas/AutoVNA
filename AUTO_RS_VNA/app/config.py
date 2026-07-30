from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

APP_NAME = "Auto RS VNA"

def user_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "dark",
    "language": "en",
    "refresh_ms": 800,
    "last_host": "",
    "tcp_port": 5025,
    "z0_ohm": 50.0,
    "velocity_factor": 0.66,
    "tdr_window": "hann",
    "tdr_axis_mode": "distance",
    "tdr_max_distance_m": 0.0,
    "tdr_zero_offset_m": 0.0,
    "tdr_auto_zoom": True,
    "uncertainty_samples": 10,
    "last_output_dir": str(Path.home()),
    "export_formats": {"csv": True, "s1p": False, "s2p": False},
    "save_fields": {
        "s11_db_phase": True,
        "s21_db_phase": True,
        "s12_db_phase": True,
        "s22_db_phase": True,
        "s11_complex": False,
        "s21_complex": False,
        "s12_complex": False,
        "s22_complex": False,
        "z_s11": True,
        "vswr": True,
        "memory": True,
        "markers": True,
        "tdr": False,
    },
}


class Settings:
    def __init__(self) -> None:
        self.path = user_data_dir() / "settings.json"
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        data = json.loads(json.dumps(DEFAULT_SETTINGS))
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._deep_update(data, loaded)
        except Exception:
            pass
        return data

    @staticmethod
    def _deep_update(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for key, value in src.items():
            if isinstance(value, dict) and isinstance(dst.get(key), dict):
                Settings._deep_update(dst[key], value)
            else:
                dst[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()
