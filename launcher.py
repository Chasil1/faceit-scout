"""
Faceit Scout — GUI launcher.
• Single-instance (Windows Mutex)
• Normal window → appears in Alt+Tab & taskbar
• Dark title bar via DWM
• System tray (pystray) — minimize to tray, double-click to restore
• Room input (URL or raw ID) → auto-open browser with room pre-loaded
"""
import os
import sys
import ctypes
import re
import threading
import webbrowser
import tkinter as tk

# ── Single-instance guard (must be first) ─────────────────────────────────
_MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "FaceitScout_v1_Mutex")
if ctypes.windll.kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
    ctypes.windll.user32.MessageBoxW(
        0,
        "Faceit Scout вже запущений!\nЗнайдіть його в треї або на панелі завдань.",
        "Faceit Scout",
        0x40 | 0x1000,   # MB_ICONINFORMATION | MB_SETFOREGROUND
    )
    sys.exit(0)

# ── Path resolution ────────────────────────────────────────────────────────
def _base():
    return sys._MEIPASS if hasattr(sys, "_MEIPASS") else os.path.dirname(os.path.abspath(__file__))

os.environ["APP_BASE_PATH"] = _base()

# ── Palette (mirrors index.html CSS variables) ────────────────────────────
BG      = "#111111"
BG2     = "#181818"
BG3     = "#202020"
BORDER  = "#2a2a2a"
ORANGE  = "#FF5500"
ORANGE2 = "#cc4400"
TEXT    = "#f0f0f0"
DIM     = "#888888"
GREEN   = "#3db870"
RED     = "#d23c3c"

W, H    = 390, 330      # window dimensions
PLACEHOLDER = "Room ID або повний URL кімнати"

_server     = None
_tray_icon  = None


# ── DWM dark title bar ─────────────────────────────────────────────────────
def _dark_titlebar(hwnd):
    try:
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), 4)
    except Exception:
        pass


# ── Room-ID extraction ─────────────────────────────────────────────────────
def _extract_room(text: str):
    text = text.strip()
    m = re.search(r"/room/(1-[0-9a-f-]+)", text, re.I)
    if m:
        return m.group(1)
    if re.match(r"^1-[0-9a-f-]+$", text, re.I):
        return text
    return None


# ── uvicorn server helpers ─────────────────────────────────────────────────
def _run_server(on_ready, on_error):
    global _server
    try:
        import uvicorn
        from server import app
        cfg = uvicorn.Config(app, host="127.0.0.1", port=8000,
                             log_level="warning", log_config=None)
        _server = uvicorn.Server(cfg)
        on_ready()
        _server.run()
    except Exception as exc:
        on_error(str(exc))


def _stop_server():
    global _server
    if _server:
        _server.force_exit = True
        _server.should_exit = True
        _server = None


# ── Tray icon ──────────────────────────────────────────────────────────────
def _make_tray_image():
    """Create a 64×64 orange circle with white 'F'."""
    try:
        from PIL import Image, ImageDraw
        sz = 64
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.ellipse([2, 2, sz - 3, sz - 3], fill=(255, 85, 0))
        # simple 'F' shape
        d.rectangle([20, 14, 28, 50], fill=(255, 255, 255))
        d.rectangle([20, 14, 44, 23], fill=(255, 255, 255))
        d.rectangle([20, 29, 40, 38], fill=(255, 255, 255))
        return img
    except ImportError:
        return None


def _setup_tray(root, on_show, on_exit):
    try:
        import pystray
        img = _make_tray_image()
        if img is None:
            return None

        def _show(icon, _item):
            root.after(0, on_show)

        def _quit(icon, _item):
            icon.stop()
            root.after(0, on_exit)

        menu = pystray.Menu(
            pystray.MenuItem("Відкрити", _show, default=True),
            pystray.MenuItem("Вийти",    _quit),
        )
        icon = pystray.Icon("FaceitScout", img, "Faceit Scout", menu)
        threading.Thread(target=icon.run, daemon=True).start()
        return icon
    except ImportError:
        return None


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    global _tray_icon

    root = tk.Tk()
    root.title("Faceit Scout")
    root.configure(bg=BG)
    root.resizable(False, False)

    # Centre on screen
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

    # Dark title bar (apply after window is realised)
    root.update()
    _dark_titlebar(root.winfo_id())

    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))

    # ── App state ────────────────────────────────────────────────────────────
    srv_running = [False]

    # ── Exit / tray helpers ──────────────────────────────────────────────────
    def exit_app():
        _stop_server()
        if _tray_icon:
            try:
                _tray_icon.stop()
            except Exception:
                pass
        root.destroy()
        sys.exit(0)

    def show_window():
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))

    def hide_to_tray():
        if _tray_icon:
            root.withdraw()
        else:
            root.iconify()

    root.protocol("WM_DELETE_WINDOW", exit_app)

    # ── Layout ───────────────────────────────────────────────────────────────
    # Orange accent line
    tk.Frame(root, bg=ORANGE, height=3).pack(fill="x")

    # Logo
    logo_f = tk.Frame(root, bg=BG, pady=16)
    logo_f.pack(fill="x")
    tk.Label(logo_f, text="FACEIT SCOUT",
             bg=BG, fg=TEXT, font=("Segoe UI", 20, "bold")).pack()
    tk.Label(logo_f, text="DOTA 2 DRAFT HELPER",
             bg=BG, fg=ORANGE, font=("Segoe UI", 8, "bold")).pack(pady=(2, 0))
    credits_lbl = tk.Label(logo_f, text="Created by Chasil",
                           bg=BG, fg=DIM,
                           font=("Segoe UI", 8), cursor="hand2")
    credits_lbl.pack(pady=(4, 0))
    credits_lbl.bind("<Button-1>",
                     lambda _e: webbrowser.open("https://steamcommunity.com/id/Chasil/"))
    credits_lbl.bind("<Enter>", lambda _e: credits_lbl.config(fg=ORANGE))
    credits_lbl.bind("<Leave>", lambda _e: credits_lbl.config(fg=DIM))

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    # Room input
    inp_f = tk.Frame(root, bg=BG2, pady=12)
    inp_f.pack(fill="x")
    tk.Label(inp_f, text="МАТЧ", bg=BG2, fg=DIM,
             font=("Segoe UI", 7, "bold")).pack(padx=18, anchor="w")

    entry = tk.Entry(inp_f,
                     bg=BG3, fg=DIM, insertbackground=TEXT,
                     relief="flat", bd=0, font=("Segoe UI", 9))
    entry.pack(fill="x", padx=18, pady=(5, 2), ipady=8)
    entry.insert(0, PLACEHOLDER)

    # Thin border under entry
    tk.Frame(inp_f, bg=BORDER, height=1).pack(fill="x", padx=18)

    def _clear_placeholder():
        if entry.get() == PLACEHOLDER:
            entry.delete(0, "end")
            entry.config(fg=TEXT)

    def _focus_in(_e):
        _clear_placeholder()

    def _focus_out(_e):
        if not entry.get().strip():
            entry.delete(0, "end")
            entry.insert(0, PLACEHOLDER)
            entry.config(fg=DIM)

    def _on_paste(_e):
        _clear_placeholder()
        # Let tkinter handle the actual paste after we clear placeholder
        entry.after(1, lambda: entry.config(fg=TEXT))
        return None  # don't block the event

    # Right-click context menu
    ctx = tk.Menu(root, tearoff=0, bg=BG3, fg=TEXT,
                  activebackground=ORANGE, activeforeground=TEXT,
                  relief="flat", bd=1)
    ctx.add_command(label="Вставити",  command=lambda: [_clear_placeholder(), entry.event_generate("<<Paste>>")])
    ctx.add_command(label="Копіювати", command=lambda: entry.event_generate("<<Copy>>"))
    ctx.add_command(label="Вирізати",  command=lambda: entry.event_generate("<<Cut>>"))
    ctx.add_separator()
    ctx.add_command(label="Виділити все", command=lambda: entry.select_range(0, "end"))

    entry.bind("<FocusIn>",   _focus_in)
    entry.bind("<FocusOut>",  _focus_out)
    entry.bind("<<Paste>>",   _on_paste)
    entry.bind("<Button-3>",  lambda e: ctx.tk_popup(e.x_root, e.y_root))

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    # Status row
    st_f = tk.Frame(root, bg=BG2, pady=9)
    st_f.pack(fill="x")
    dot  = tk.Label(st_f, text="●", bg=BG2, fg=DIM, font=("Segoe UI", 9))
    dot.pack(side="left", padx=(18, 4))
    msg  = tk.Label(st_f, text="Готовий до запуску", bg=BG2, fg=DIM, font=("Segoe UI", 9))
    msg.pack(side="left")
    url  = tk.Label(st_f, text="", bg=BG2, fg="#444", font=("Segoe UI", 8))
    url.pack(side="right", padx=18)

    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    def set_status(color, text_, url_=""):
        dot.config(fg=color)
        msg.config(fg=color, text=text_)
        url.config(text=url_)

    # Buttons
    btn_f = tk.Frame(root, bg=BG, pady=18)
    btn_f.pack(fill="x", padx=18)

    start_lbl = tk.StringVar(value="  ЗАПУСТИТИ  ")
    start_btn = tk.Button(btn_f, textvariable=start_lbl,
                          bg=ORANGE, fg=TEXT,
                          font=("Segoe UI", 10, "bold"),
                          relief="flat", bd=0, padx=10, pady=9, cursor="hand2",
                          activebackground=ORANGE2, activeforeground=TEXT)
    start_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

    tray_btn = tk.Button(btn_f, text="  В ТРЕЙ  ",
                         bg=BG3, fg=DIM,
                         font=("Segoe UI", 10, "bold"),
                         relief="flat", bd=0, padx=10, pady=9, cursor="hand2",
                         activebackground="#252525", activeforeground=TEXT,
                         command=hide_to_tray)
    tray_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))

    exit_btn = tk.Button(btn_f, text="  ВИЙТИ  ",
                         bg=BG3, fg=DIM,
                         font=("Segoe UI", 10, "bold"),
                         relief="flat", bd=0, padx=10, pady=9, cursor="hand2",
                         activebackground="#252525", activeforeground=TEXT,
                         command=exit_app)
    exit_btn.pack(side="left", expand=True, fill="x")

    for btn, hover, norm in [
        (start_btn, ORANGE2, ORANGE),
        (tray_btn,  "#252525", BG3),
        (exit_btn,  "#252525", BG3),
    ]:
        btn.bind("<Enter>", lambda e, b=btn, c=hover: b.config(bg=c))
        btn.bind("<Leave>", lambda e, b=btn, c=norm:  b.config(bg=c))

    # ── Server callbacks ─────────────────────────────────────────────────────
    def _on_ready(room_id):
        srv_running[0] = True

        def _apply():
            set_status(GREEN, "Сервер запущено", "http://127.0.0.1:8000")
            start_lbl.set("  ВІДКРИТИ  ")
            start_btn.config(state=tk.NORMAL)
            url_str = f"http://127.0.0.1:8000/?room={room_id}" if room_id else "http://127.0.0.1:8000"
            webbrowser.open(url_str)

        root.after(0, _apply)

    def _on_error(err):
        def _apply():
            set_status(RED, f"Помилка: {err[:42]}")
            start_lbl.set("  ЗАПУСТИТИ  ")
            start_btn.config(state=tk.NORMAL)

        root.after(0, _apply)

    # ── Start / open action ──────────────────────────────────────────────────
    def do_start():
        raw     = entry.get().strip()
        raw     = "" if raw == PLACEHOLDER else raw
        room_id = _extract_room(raw)

        if srv_running[0]:
            url_str = f"http://127.0.0.1:8000/?room={room_id}" if room_id else "http://127.0.0.1:8000"
            webbrowser.open(url_str)
            return

        start_btn.config(state=tk.DISABLED)
        set_status(ORANGE, "Запускаємо…")
        threading.Thread(
            target=_run_server,
            args=(lambda: _on_ready(room_id), _on_error),
            daemon=True,
        ).start()

    start_btn.config(command=do_start)
    entry.bind("<Return>", lambda _e: do_start())

    # ── Tray setup ───────────────────────────────────────────────────────────
    _tray_icon = _setup_tray(root, show_window, exit_app)
    if _tray_icon is None:
        # pystray unavailable → fall back to normal minimize
        tray_btn.config(text="  ЗГОРНУТИ  ", command=root.iconify)

    root.mainloop()


if __name__ == "__main__":
    main()
