import platform
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

BASE_DIR = Path(__file__).resolve().parent


def launch_program(folder_name):
    folder = BASE_DIR / folder_name
    system = platform.system()

    try:
        if system == "Windows":
            script = folder / "run_windows.bat"

            if not script.is_file():
                raise FileNotFoundError(f"File not found:\n{script}")

            subprocess.Popen(
                ["cmd.exe", "/c", str(script)],
                cwd=str(folder),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )

        elif system == "Linux":
            script = folder / "run_linux"

            if not script.is_file():
                alternative_script = folder / "run_linux.sh"

                if alternative_script.is_file():
                    script = alternative_script
                else:
                    raise FileNotFoundError(f"File not found:\n{script}")

            subprocess.Popen(
                ["/bin/bash", str(script)],
                cwd=str(folder),
                start_new_session=True
            )

        else:
            raise RuntimeError(f"Unsupported operating system: {system}")

        root.destroy()

    except Exception as error:
        messagebox.showerror("Launch Error", str(error))


def set_hover_effect(widgets, frame):
    def on_enter(event):
        frame.configure(
            highlightbackground="#2f80ed",
            highlightcolor="#2f80ed",
            bg="#dcecff"
        )

        for widget in widgets:
            widget.configure(bg="#dcecff")

    def on_leave(event):
        frame.configure(
            highlightbackground="#b8c2cc",
            highlightcolor="#b8c2cc",
            bg="#f4f6f8"
        )

        for widget in widgets:
            widget.configure(bg="#f4f6f8")

    for widget in [frame, *widgets]:
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)


def create_tile(parent, image, label, folder_name):
    frame = tk.Frame(
        parent,
        bg="#f4f6f8",
        highlightthickness=4,
        highlightbackground="#b8c2cc",
        highlightcolor="#b8c2cc",
        cursor="hand2"
    )

    image_label = tk.Label(
        frame,
        image=image,
        bg="#f4f6f8",
        cursor="hand2"
    )
    image_label.pack(padx=10, pady=(10, 4))

    text_label = tk.Label(
        frame,
        text=label,
        font=("Arial", 18, "bold"),
        bg="#f4f6f8",
        fg="#17202a",
        cursor="hand2"
    )
    text_label.pack(pady=(4, 12))

    action = lambda event: launch_program(folder_name)

    for widget in [frame, image_label, text_label]:
        widget.bind("<Button-1>", action)

    set_hover_effect([image_label, text_label], frame)

    return frame


root = tk.Tk()
root.title("AutoVNA")
root.configure(bg="#eef1f5")
root.resizable(False, False)

title_label = tk.Label(
    root,
    text="AutoVNA",
    font=("Arial", 30, "bold"),
    bg="#eef1f5",
    fg="#17202a"
)
title_label.pack(pady=(24, 20))

container = tk.Frame(root, bg="#eef1f5")
container.pack(padx=24, pady=(0, 24))

nanovna_image = tk.PhotoImage(
    file=BASE_DIR / "picture/nanoVNA.png"
)

zvl_image = tk.PhotoImage(
    file=BASE_DIR / "picture/ZVL.png"
)

nanovna_tile = create_tile(
    container,
    nanovna_image,
    "NanoVNA",
    "AUTO_NanoVNA"
)
nanovna_tile.grid(row=0, column=0, padx=(0, 12))

zvl_tile = create_tile(
    container,
    zvl_image,
    "ZVL",
    "AUTO_RS_VNA"
)
zvl_tile.grid(row=0, column=1, padx=(12, 0))

root.update_idletasks()

window_width = root.winfo_width()
window_height = root.winfo_height()

x_position = (root.winfo_screenwidth() - window_width) // 2
y_position = (root.winfo_screenheight() - window_height) // 2

root.geometry(
    f"{window_width}x{window_height}+{x_position}+{y_position}"
)

root.mainloop()