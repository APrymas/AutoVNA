                      
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path


def resource_base() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def installation_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    from gui.main_window import RSLab
    root = tk.Tk()
    RSLab(root, resource_base(), installation_base())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
