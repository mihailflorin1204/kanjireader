"""Kanji Speaker - paste Japanese text, see kanji readings, hear them spoken.

Small always-on-top window. Extracts kanji tokens (context-aware compound
readings via pykakasi), shows 漢字 -> ひらがな, and speaks each reading in
Japanese using edge-tts (online) with a pyttsx3/SAPI Japanese voice as
offline fallback. CPU-only.
"""

import asyncio
import ctypes
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk

import edge_tts
import keyboard
import pygame
import pyttsx3
from pykakasi import kakasi

VOICE = "ja-JP-NanamiNeural"
HOTKEY = "ctrl+h"  # press anywhere to copy selection -> paste -> speak


def config_path():
    base = (
        os.path.dirname(sys.executable)
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "config.json")


CAPTURE_MODS = ("ctrl", "alt", "shift")
SAFE_BARE_KEYS = {
    "insert", "delete", "home", "end", "page up", "page down",
    "print screen", "scroll lock", "pause", "caps lock",
}


def ok_bare_key(name):
    return bool(re.fullmatch(r"f([1-9]|1\d|2[0-4])", name)) or name in SAFE_BARE_KEYS

BG = "#23272e"
PANEL = "#2c313a"
FG = "#e8eaf0"
SUB = "#9aa3b2"
ACCENT = "#4da3ff"
LIST_HL = "#35507d"
KANJI_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿々]")
GAP_BETWEEN_WORDS = 0.3


def extract_kanji(kk, text):
    """Return [(orig_token, hira_reading)] for tokens containing kanji."""
    return [
        (item["orig"].strip(), item["hira"])
        for item in kk.convert(text)
        if item["orig"].strip() and KANJI_RE.search(item["orig"])
    ]


async def synth_edge(text, rate="+0%"):
    """Synthesize text to mp3 bytes via edge-tts. rate like '-25%'/'+50%'."""
    buf = io.BytesIO()
    async for chunk in edge_tts.Communicate(text, VOICE, rate=rate).stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    if not buf.getbuffer().nbytes:
        raise RuntimeError("edge-tts returned no audio")
    return buf.getvalue()


def play_mp3(data, cancel):
    """Play mp3 bytes through pygame.mixer; abort early if cancel is set."""
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if cancel.is_set():
                pygame.mixer.music.stop()
                return
            time.sleep(0.05)
    finally:
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
        try:
            os.remove(path)
        except OSError:
            pass


def init_sapi():
    """Return a pyttsx3 engine bound to a Japanese voice, or None."""
    try:
        engine = pyttsx3.init()
    except Exception:
        return None
    for v in engine.getProperty("voices") or []:
        langs = " ".join(str(l) for l in (getattr(v, "languages", None) or []))
        blob = f"{v.id} {v.name} {langs}".lower()
        if "ja-jp" in blob or "japanese" in blob or "_ja" in blob or "-ja" in blob:
            engine.setProperty("voice", v.id)
            return engine
    return None


class App:
    def __init__(self, root):
        self.root = root
        self.kk = None
        self.kk_ready = threading.Event()
        self.cancel = threading.Event()
        self.worker = None
        self.tokens = []
        self.full_text = ""
        self.hotkey = self._load_config()
        self._hook = None

        root.title("Kanji Speaker")
        root.geometry("340x360")
        root.minsize(280, 280)
        root.attributes("-topmost", True)
        root.configure(bg=BG)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"), foreground=ACCENT)
        style.configure("Status.TLabel", foreground=SUB, padding=(2, 2))
        style.configure(
            "TButton", font=("Segoe UI", 9, "bold"), padding=(14, 5),
            background=PANEL, foreground=FG, borderwidth=0,
        )
        style.map(
            "TButton",
            background=[("active", "#3a4150"), ("pressed", ACCENT)],
            foreground=[("pressed", "white")],
        )
        style.configure("Accent.TButton", background=ACCENT, foreground="white")
        style.map(
            "Accent.TButton",
            background=[("active", "#3f8de0"), ("pressed", "#2e6cb0")],
        )
        style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)
        style.configure(
            "TScrollbar", background=PANEL, troughcolor=BG,
            borderwidth=0, arrowcolor=FG,
        )

        header = ttk.Frame(root)
        header.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(header, text="Kanji Speaker", style="Title.TLabel").pack(side="left")
        hk_box = ttk.Frame(header)
        hk_box.pack(side="right")
        ttk.Label(hk_box, text="Hotkey", style="Status.TLabel").pack(side="left", padx=(0, 4))
        self.hotkey_var = tk.StringVar(value=self.hotkey)
        self.hk_entry = ttk.Entry(hk_box, width=10, textvariable=self.hotkey_var, justify="center")
        self.hk_entry.pack(side="right")
        self.capturing = False
        self._cap = None
        self.hk_entry.bind("<FocusIn>", self._hotkey_focusin)
        self.hk_entry.bind("<KeyPress>", lambda e: "break")
        self.hk_entry.bind("<FocusOut>", self._hotkey_focusout)

        self.input = tk.Text(
            root, height=3, font=("Meiryo", 11), wrap="word",
            bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
            highlightthickness=1, highlightbackground="#3a4150",
            highlightcolor=ACCENT, padx=8, pady=6,
        )
        self.input.pack(fill="x", padx=10, pady=2)
        self.input.bind("<<Paste>>", lambda e: root.after(100, self.speak))

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=10, pady=4)
        ttk.Button(btns, text="Paste", style="Accent.TButton", command=self.paste).pack(side="left")
        ttk.Button(btns, text="Stop", command=self.stop).pack(side="left", padx=6)

        speed_row = ttk.Frame(root)
        speed_row.pack(fill="x", padx=10, pady=(0, 2))
        ttk.Label(speed_row, text="Speed").pack(side="left")
        self.speed = tk.DoubleVar(value=1.0)
        self.speed_val = ttk.Label(speed_row, text="1.0x", width=4)
        ttk.Scale(
            speed_row, from_=0.5, to=2.0, orient="horizontal",
            variable=self.speed,
            command=lambda v: self.speed_val.config(text=f"{float(v):.1f}x"),
        ).pack(side="left", fill="x", expand=True, padx=6)
        self.speed_val.pack(side="left")

        list_frame = ttk.Frame(root)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(2, 4))
        self.listbox = tk.Listbox(
            list_frame, font=("Meiryo", 12, "bold"), activestyle="none",
            bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="white",
            relief="flat", highlightthickness=1,
            highlightbackground="#3a4150", highlightcolor=ACCENT,
        )
        sb = ttk.Scrollbar(list_frame, command=self.listbox.yview)
        self.listbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.on_select)

        self.status = ttk.Label(
            root, text="Loading dictionary...", style="Status.TLabel", anchor="w"
        )
        self.status.pack(fill="x", padx=10, pady=(0, 6))

        try:
            pygame.mixer.init()
        except Exception:
            pass

        threading.Thread(target=self._load_kakasi, daemon=True).start()

        self.apply_hotkey()

    # ---- background helpers ----

    def _load_config(self):
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                return json.load(f).get("hotkey", HOTKEY)
        except Exception:
            return HOTKEY

    def _save_config(self):
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump({"hotkey": self.hotkey}, f)
        except Exception:
            pass

    def _hotkey_focusin(self, _event):
        # Enter capture mode: unregister the global hook so pressing a combo
        # here can't trigger copy/paste, then watch physical keys via the
        # same 'keyboard' lib that will register the hotkey.
        self.capturing = True
        if self._hook is not None:
            try:
                keyboard.remove_hotkey(self._hook)
            except Exception:
                pass
            self._hook = None
        self.hotkey_var.set("press keys...")
        self.set_status("Press a key combo (Esc to cancel)")
        self._cap = keyboard.on_press(self._cap_press, suppress=False)

    def _cap_press(self, event):
        name = (event.name or "").lower()
        if name in CAPTURE_MODS or "shift" in name or "ctrl" in name \
                or "alt" in name or "windows" in name or "menu" in name:
            return
        parts = [m for m in CAPTURE_MODS if keyboard.is_pressed(m)]
        if name == "esc":
            result = None
        elif not parts and not ok_bare_key(name):
            result = "bare"
        else:
            parts.append(name)
            result = "+".join(parts)
        self.root.after(0, lambda: self._cap_done(result))

    def _end_capture(self):
        self.capturing = False
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                keyboard.unhook(cap)
            except Exception:
                pass

    def _cap_done(self, result):
        self._end_capture()
        if result == "bare":
            self.set_status(
                "Add a modifier (ctrl/alt/shift), or use F1-F12 / Insert etc."
            )
            result = None
        self.hotkey_var.set(result if result else self.hotkey)
        self.apply_hotkey()
        self.root.focus_set()

    def _hotkey_focusout(self, _event):
        was_capturing = self.capturing
        self._end_capture()
        if was_capturing:
            self.hotkey_var.set(self.hotkey)
        self.apply_hotkey()

    def apply_hotkey(self):
        new = self.hotkey_var.get().strip().lower()
        if not new or (new == getattr(self, "_applied", None) and self._hook is not None):
            return
        try:
            hook = keyboard.add_hotkey(new, self.on_hotkey, suppress=True)
        except Exception:
            self.set_status(f"Invalid hotkey: '{new}' (e.g. f9, ctrl+j, alt+shift+k)")
            self.hotkey_var.set(self._applied or HOTKEY)
            return
        if self._hook is not None:
            try:
                keyboard.remove_hotkey(self._hook)
            except Exception:
                pass
        self._hook = hook
        self._applied = new
        self.hotkey = new
        self._save_config()
        if self.kk_ready.is_set():
            self.set_status(f"Ready - paste text, or select text anywhere + {new.upper()}")

    def _load_kakasi(self):
        self.kk = kakasi()
        self.kk_ready.set()
        self.set_status(f"Ready - paste text, or select text anywhere + {self.hotkey.upper()}")

    def set_status(self, msg):
        self.root.after(0, lambda: self.status.config(text=msg))

    def highlight(self, idx):
        def _h():
            for i in range(self.listbox.size()):
                self.listbox.itemconfig(i, background=PANEL)
            if idx is not None and idx < self.listbox.size():
                self.listbox.itemconfig(idx, background=LIST_HL)
                self.listbox.see(idx)
        self.root.after(0, _h)

    # ---- UI actions ----

    def on_hotkey(self):
        # Called from keyboard's hook thread: copy the current selection in
        # the focused app, then grab the clipboard on the UI thread.
        if self.capturing:
            return
        try:
            keyboard.send("ctrl+c")
        except Exception:
            return
        self.root.after(350, self._paste_clipboard)

    def _paste_clipboard(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.speak()

    def paste(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.set_status("Clipboard is empty")
            return
        self.input.delete("1.0", "end")
        self.input.insert("1.0", text)
        self.speak()

    def speak(self):
        text = self.input.get("1.0", "end").strip()
        if not text:
            self.set_status("Paste some Japanese text first")
            return
        self.stop()
        self.cancel = threading.Event()
        self.worker = threading.Thread(
            target=self._speak_worker,
            args=(text, self.cancel),
            kwargs={"factor": self.speed.get()},
            daemon=True,
        )
        self.worker.start()

    def stop(self):
        self.cancel.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

    def on_select(self, _event):
        sel = self.listbox.curselection()
        if not sel:
            return
        i = sel[0]
        if i == 0:
            single = ("Speaking sentence...", self.full_text)
        elif i <= len(self.tokens):
            orig, hira = self.tokens[i - 1]
            single = (f"Speaking: {orig} -> {hira}", hira)
        else:
            return
        self.stop()
        self.cancel = threading.Event()
        threading.Thread(
            target=self._speak_worker,
            args=(None, self.cancel),
            kwargs={"single": single, "index": i, "factor": self.speed.get()},
            daemon=True,
        ).start()

    # ---- worker ----

    def _speak_worker(self, text, cancel, single=None, index=None, factor=1.0):
        self.kk_ready.wait()
        edge_rate = f"{int(round((factor - 1) * 100)):+d}%"
        # Each utterance: (label_for_status, text_to_speak, list_index_or_None)
        if single is not None:
            label, say_text = single
            utterances = [(label, say_text, index)]
        else:
            try:
                tokens = extract_kanji(self.kk, text)
            except Exception as e:
                self.set_status(f"Conversion error: {e}")
                return
            self.tokens = tokens
            self.full_text = text
            def fill():
                self.listbox.delete(0, "end")
                self.listbox.insert("end", "[ full sentence ]")
                for orig, hira in tokens:
                    self.listbox.insert("end", f"{orig}  →  {hira}")
            self.root.after(0, fill)
            utterances = [("Speaking sentence...", text, None)]
            utterances += [
                (f"Speaking: {o} -> {h}", h, i) for i, (o, h) in enumerate(tokens)
            ]
            if not tokens:
                self.set_status("No kanji found - speaking text only")

        engine = "edge"
        sapi = None
        for label, say_text, idx in utterances:
            if cancel.is_set():
                break
            self.set_status(label)
            self.highlight(idx)
            if engine == "edge":
                try:
                    data = asyncio.run(synth_edge(say_text, edge_rate))
                    if cancel.is_set():
                        break
                    play_mp3(data, cancel)
                except Exception:
                    engine = "sapi"
            if engine == "sapi" and not cancel.is_set():
                if sapi is None:
                    sapi = init_sapi()
                    if sapi is None:
                        self.set_status(
                            "edge-tts failed and no Japanese Windows voice "
                            "found. Connect to the internet, or install the "
                            "Japanese voice in Windows Settings > Speech."
                        )
                        break
                try:
                    sapi.setProperty("rate", int(200 * factor))
                    sapi.say(say_text)
                    sapi.runAndWait()
                except Exception as e:
                    self.set_status(f"Speech error: {e}")
                    break
            time.sleep(GAP_BETWEEN_WORDS)

        self.highlight(None)
        self.set_status("Stopped" if cancel.is_set() else "Done")


def main():
    # Single instance: if another copy is running, don't start a second one
    # (each would hook the hotkey and speak the same text in duplicate).
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW(None, True, "KanjiSpeakerSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
