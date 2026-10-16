"""Kanji Speaker - paste Japanese text, see kanji readings, hear them spoken.

Small always-on-top window. Extracts kanji tokens (context-aware compound
readings via pykakasi), shows 漢字 -> ひらがな, and speaks each reading in
Japanese using edge-tts (online) with a pyttsx3/SAPI Japanese voice as
offline fallback. CPU-only.

The global hotkey uses RegisterHotKey (MOD_NOREPEAT) on a dedicated
message-pump thread; the selection is copied via an injected Ctrl+C whose
success is verified through the clipboard sequence number.
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
from ctypes import wintypes
from tkinter import ttk

import edge_tts
import pygame
import pyttsx3
from pykakasi import kakasi

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

VOICE = "ja-JP-NanamiNeural"
# Pick a combo no common app uses: RegisterHotKey does not suppress the
# keystroke, so it also reaches the focused app (ctrl+h opens History in
# Chrome, Replace in Notepad/Word, etc.).
HOTKEY = "ctrl+shift+x"  # press anywhere to copy selection -> paste -> speak


def config_path():
    base = (
        os.path.dirname(sys.executable)
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    return os.path.join(base, "config.json")


SAFE_BARE_KEYS = {
    "insert", "delete", "home", "end", "page up", "page down",
    "print screen", "scroll lock", "pause", "caps lock",
}


def ok_bare_key(name):
    return bool(re.fullmatch(r"f([1-9]|1\d|2[0-4])", name)) or name in SAFE_BARE_KEYS


# ---- global hotkey (RegisterHotKey on a dedicated thread) ----

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_REREGISTER = 0x8001  # WM_APP + 1, private message to the hotkey thread

MOD_BY_NAME = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "left ctrl": MOD_CONTROL, "right ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT, "left shift": MOD_SHIFT, "right shift": MOD_SHIFT,
    "alt": MOD_ALT, "menu": MOD_ALT, "left alt": MOD_ALT, "right alt": MOD_ALT,
    "win": MOD_WIN, "windows": MOD_WIN,
    "left windows": MOD_WIN, "right windows": MOD_WIN,
}

NAMED_VKS = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "spacebar": 0x20,
    "page up": 0x21, "prior": 0x21, "page down": 0x22, "next": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E,
    "print screen": 0x2C, "print": 0x2C, "num lock": 0x90,
    "scroll lock": 0x91, "caps lock": 0x14, "pause": 0x13, "break": 0x13,
    "apps": 0x5D, "menu key": 0x5D,
}
for _i in range(1, 25):
    NAMED_VKS[f"f{_i}"] = 0x6F + _i
for _i in range(10):
    NAMED_VKS[f"keypad {_i}"] = 0x60 + _i
NAMED_VKS.update({
    "keypad decimal": 0x6E, "keypad add": 0x6B, "keypad subtract": 0x6D,
    "keypad multiply": 0x6A, "keypad divide": 0x6F,
})

_KP_OPS = {
    "decimal": "keypad decimal", "separator": "keypad decimal",
    "add": "keypad add", "subtract": "keypad subtract",
    "multiply": "keypad multiply", "divide": "keypad divide",
}


def parse_hotkey(text):
    """'ctrl+shift+x' -> (mods | MOD_NOREPEAT, vk), or None if invalid."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        return None
    mods = 0
    for p in parts[:-1]:
        m = MOD_BY_NAME.get(p)
        if m is None:
            return None
        mods |= m
    key = parts[-1]
    if key in MOD_BY_NAME:
        return None  # combo must end with a real key, not a modifier
    if key in NAMED_VKS:
        vk = NAMED_VKS[key]
    elif len(key) == 1:
        r = user32.VkKeyScanW(key)
        if r == -1 or (r & 0xFF) == 0xFF:
            return None
        vk = r & 0xFF
        shift_state = (r >> 8) & 0xFF
        if shift_state & 1:
            mods |= MOD_SHIFT
        if shift_state & 2:
            mods |= MOD_CONTROL
        if shift_state & 4:
            mods |= MOD_ALT
    else:
        return None
    return mods | MOD_NOREPEAT, vk


class HotkeyThread(threading.Thread):
    """Owns the RegisterHotKey registration and pumps WM_HOTKEY messages."""

    HOTKEY_ID = 1

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.tid = None
        self.ready = threading.Event()
        self.registered = False

    def run(self):
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # create queue
        self.tid = kernel32.GetCurrentThreadId()
        self._register()
        self.ready.set()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                return
            if msg.message == WM_HOTKEY:
                self.app.on_hotkey()
            elif msg.message == WM_REREGISTER:
                self._register()
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

    def _register(self):
        if self.registered:
            user32.UnregisterHotKey(None, self.HOTKEY_ID)
            self.registered = False
        parsed = parse_hotkey(self.app.hotkey)
        if parsed is None:
            self.app.hk_error = f"Invalid hotkey: '{self.app.hotkey}'"
            self.app.set_status(self.app.hk_error)
            return
        mods, vk = parsed
        if user32.RegisterHotKey(None, self.HOTKEY_ID, mods, vk):
            self.registered = True
            self.app.hk_error = None
            if self.app.kk_ready.is_set():
                self.app.set_status(
                    f"Ready - paste text, or select text anywhere + "
                    f"{self.app.hotkey.upper()}"
                )
        else:
            self.app.hk_error = (
                f"Cannot register '{self.app.hotkey}' "
                "(used by another app, or reserved)"
            )
            self.app.set_status(self.app.hk_error)

    def retarget(self):
        """Ask the hotkey thread to re-register the current hotkey."""
        self.ready.wait()
        if self.tid is not None:
            user32.PostThreadMessageW(self.tid, WM_REREGISTER, 0, 0)


def send_copy_keystroke():
    """Inject Ctrl+C into the focused app.

    The user usually still holds the hotkey's modifiers when this runs, so
    the held modifiers are released first (otherwise the app would see e.g.
    Ctrl+Shift+C) and re-pressed afterwards.
    """
    MOD_VKS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0x5B, 0x5C, 0xA5)
    held = [vk for vk in MOD_VKS if user32.GetAsyncKeyState(vk) & 0x8000]
    for vk in held:
        user32.keybd_event(vk, 0, 2, 0)
    for vk, flags in ((0x11, 0), (0x43, 0), (0x43, 2), (0x11, 2)):
        user32.keybd_event(vk, 0, flags, 0)
    for vk in held:
        user32.keybd_event(vk, 0, 0, 0)

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
        self.hk_error = None

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
        self.hk_entry.bind("<FocusIn>", self._hotkey_focusin)
        self.hk_entry.bind("<KeyPress>", self._entry_keypress)
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

        self.hk_thread = HotkeyThread(self)
        self.hk_thread.start()
        self.apply_hotkey()

    # ---- background helpers ----

    def _load_config(self):
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                hotkey = json.load(f).get("hotkey", HOTKEY)
            return hotkey if parse_hotkey(hotkey) else HOTKEY
        except Exception:
            return HOTKEY

    def _save_config(self):
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump({"hotkey": self.hotkey}, f)
        except Exception:
            pass

    # Modifier-only keysyms: ignored while capturing (we wait for a real key).
    _MOD_KEYSYMS = {
        "shift_l", "shift_r", "control_l", "control_r", "alt_l", "alt_r",
        "meta_l", "meta_r", "super_l", "super_r", "hyper_l", "hyper_r",
        "iso_level3_shift", "mode_switch",
    }
    # Tk keysym -> hotkey name, where they differ.
    _KEYSYM_NAMES = {
        "prior": "page up", "next": "page down", "print": "print screen",
        "scroll_lock": "scroll lock", "caps_lock": "caps lock",
        "num_lock": "num lock", "return": "enter", "backspace": "backspace",
    }
    # Modifier VK codes for capture. event.state is unreliable on Windows Tk
    # (Mod1Mask is set whenever Ctrl is held), so read physical state instead.
    _STATE_MODS = (
        ("ctrl", 0x11), ("alt", 0x12), ("shift", 0x10),
        ("win", 0x5B), ("win", 0x5C),
    )

    def _hotkey_focusin(self, _event):
        # Enter capture mode. The entry sees the keys directly, so no global
        # hook is needed; on_hotkey ignores fires while self.capturing.
        self.capturing = True
        self.hotkey_var.set("press keys...")
        self.set_status("Press a key combo (Esc to cancel)")

    def _entry_keypress(self, event):
        """Entry is display-only; while capturing, a keypress sets the hotkey."""
        if not self.capturing:
            return "break"
        name = (event.keysym or "").lower()
        if name in self._MOD_KEYSYMS:
            return "break"
        if name.startswith("kp_"):
            suffix = name[3:]
            name = f"keypad {suffix}" if suffix.isdigit() else \
                _KP_OPS.get(suffix, self._KEYSYM_NAMES.get(suffix, suffix))
        else:
            name = self._KEYSYM_NAMES.get(name, name)
        mods = []
        for m, vk in self._STATE_MODS:
            if user32.GetAsyncKeyState(vk) & 0x8000 and m not in mods:
                mods.append(m)
        if name == "escape":
            result = None
        elif not mods and not ok_bare_key(name):
            result = "bare"
        else:
            result = "+".join(mods + [name])
        self._cap_done(result)
        return "break"

    def _cap_done(self, result):
        self.capturing = False
        if result == "bare":
            self.set_status(
                "Add a modifier (ctrl/alt/shift), or use F1-F12 / Insert etc."
            )
            result = None
        self.hotkey_var.set(result if result else self.hotkey)
        self.apply_hotkey()
        self.root.focus_set()

    def _hotkey_focusout(self, _event):
        if self.capturing:
            self.capturing = False
            self.hotkey_var.set(self.hotkey)

    def apply_hotkey(self):
        new = self.hotkey_var.get().strip().lower()
        if not new or parse_hotkey(new) is None:
            self.set_status(
                f"Invalid hotkey: '{new}' (e.g. f9, ctrl+j, alt+shift+k)"
            )
            self.hotkey_var.set(self.hotkey)
            return
        if new != self.hotkey:
            self.hotkey = new
            self._save_config()
        self.hk_thread.retarget()

    def _load_kakasi(self):
        self.kk = kakasi()
        self.kk_ready.set()
        if self.hk_error:
            self.set_status(self.hk_error)
        else:
            self.set_status(
                f"Ready - paste text, or select text anywhere + "
                f"{self.hotkey.upper()}"
            )

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
        # Called on the HotkeyThread when the global hotkey fires. Do the
        # copy + clipboard verification off that thread so its message pump
        # stays responsive.
        if self.capturing:
            return
        threading.Thread(target=self._grab_selection, daemon=True).start()

    def _grab_selection(self):
        """Inject Ctrl+C and wait until the clipboard actually changes."""
        seq0 = user32.GetClipboardSequenceNumber()
        copied = False
        for _attempt in range(3):
            send_copy_keystroke()
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if user32.GetClipboardSequenceNumber() != seq0:
                    copied = True
                    break
                time.sleep(0.02)
            if copied:
                break
            time.sleep(0.05)
        if not copied:
            self.set_status(
                "Copy failed - is that window elevated, or is text selected?"
            )
            return
        self.root.after(30, self._paste_clipboard)

    def _paste_clipboard(self):
        # Apps using delayed rendering (e.g. Office) flag the clipboard as
        # changed before the data is actually available; retry briefly.
        for _ in range(5):
            try:
                text = self.root.clipboard_get()
                break
            except tk.TclError:
                text = None
                time.sleep(0.04)
        if text is None:
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
