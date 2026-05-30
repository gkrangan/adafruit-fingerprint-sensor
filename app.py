import math
import queue
import threading
import tkinter as tk
from datetime import datetime
from typing import Optional

import customtkinter as ctk
import serial.tools.list_ports
from PIL import Image, ImageEnhance, ImageOps, ImageTk

import storage
from sensor import FingerprintSensor

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

_CANVAS_W = 192
_CANVAS_H = 216


def _unpack_nibbles(data: bytes) -> list:
    """Expand 4-bit packed sensor bytes into a list of 0-255 pixel values."""
    out = []
    for b in data:
        out.append((b >> 4) * 17)
        out.append((b & 0x0F) * 17)
    return out


class _EnrollDialog(ctk.CTkToplevel):
    """Modal dialog that asks for a fingerprint name and returns it."""

    def __init__(self, parent: ctk.CTk, next_slot: int) -> None:
        super().__init__(parent)
        self.title("Enroll New Fingerprint")
        self.geometry("340x160")
        self.resizable(False, False)
        self.grab_set()

        self._result: Optional[str] = None

        ctk.CTkLabel(
            self, text=f"Name for slot #{next_slot}:", font=ctk.CTkFont(size=13)
        ).pack(pady=(18, 6), padx=20, anchor="w")

        self._entry = ctk.CTkEntry(self, placeholder_text="e.g. John Doe", width=300)
        self._entry.pack(padx=20)
        self._entry.bind("<Return>", lambda _: self._ok())
        self._entry.focus()

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(pady=14)
        ctk.CTkButton(btn_row, text="Enroll", width=130, command=self._ok).pack(
            side="left", padx=6
        )
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=130,
            fg_color="transparent",
            border_width=1,
            command=self.destroy,
        ).pack(side="left", padx=6)

    def _ok(self) -> None:
        name = self._entry.get().strip()
        if name:
            self._result = name
            self.destroy()

    def get_result(self) -> Optional[str]:
        self.wait_window()
        return self._result


class FingerprintApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Fingerprint Manager")
        self.geometry("960x660")
        self.resizable(False, False)

        self._sensor = FingerprintSensor()
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._op_running = False
        self._tk_img: Optional[ImageTk.PhotoImage] = None
        self._enrolled_widgets: list = []
        self._fid_to_widget: dict = {}  # finger_id → row widget for O(1) delete

        self._build_ui()
        self._refresh_ports()
        self._poll_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._left = ctk.CTkFrame(self, width=290, corner_radius=0)
        self._left.pack(side="left", fill="y")
        self._left.pack_propagate(False)

        self._right = ctk.CTkFrame(self, fg_color="transparent")
        self._right.pack(side="right", fill="both", expand=True, padx=12, pady=12)

        self._build_left()
        self._build_right()

    def _build_left(self) -> None:
        p = self._left

        ctk.CTkLabel(
            p, text="Fingerprint Manager", font=ctk.CTkFont(size=15, weight="bold")
        ).pack(pady=(18, 12))

        # --- Port selection ---
        ctk.CTkLabel(p, text="Serial Port", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=16
        )
        port_row = ctk.CTkFrame(p, fg_color="transparent")
        port_row.pack(fill="x", padx=16, pady=(2, 6))

        self._port_var = ctk.StringVar()
        self._port_combo = ctk.CTkComboBox(port_row, variable=self._port_var, width=200)
        self._port_combo.pack(side="left")
        ctk.CTkButton(
            port_row, text="↺", width=34, command=self._refresh_ports
        ).pack(side="left", padx=(6, 0))

        self._connect_btn = ctk.CTkButton(p, text="Connect", command=self._toggle_connect)
        self._connect_btn.pack(fill="x", padx=16, pady=4)

        self._conn_label = ctk.CTkLabel(
            p, text="● Disconnected", text_color="gray", font=ctk.CTkFont(size=11)
        )
        self._conn_label.pack(pady=(0, 10))

        # --- Divider ---
        ctk.CTkFrame(p, height=1, fg_color=("gray80", "gray30")).pack(
            fill="x", padx=14, pady=6
        )

        # --- Enrolled list ---
        ctk.CTkLabel(
            p,
            text="Enrolled Fingerprints",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(4, 3))

        self._enrolled_scroll = ctk.CTkScrollableFrame(p, height=260)
        self._enrolled_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        ctk.CTkButton(
            p,
            text="Refresh List",
            fg_color="transparent",
            border_width=1,
            command=self._refresh_enrolled,
        ).pack(fill="x", padx=16, pady=(4, 14))

    def _build_right(self) -> None:
        p = self._right

        # --- Fingerprint image ---
        img_card = ctk.CTkFrame(p)
        img_card.pack(fill="x")

        ctk.CTkLabel(
            img_card,
            text="Fingerprint Image",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(pady=(8, 4))

        self._canvas = tk.Canvas(
            img_card,
            width=_CANVAS_W,
            height=_CANVAS_H,
            bg="#111122",
            highlightthickness=1,
            highlightbackground="#334",
        )
        self._canvas.pack(pady=(0, 8))
        self._draw_placeholder()

        # --- Action buttons ---
        btn_row = ctk.CTkFrame(p, fg_color="transparent")
        btn_row.pack(fill="x", pady=10)

        self._enroll_btn = ctk.CTkButton(
            btn_row,
            text="Enroll Finger",
            height=40,
            state="disabled",
            command=self._start_enroll,
        )
        self._enroll_btn.pack(side="left", expand=True, fill="x", padx=(0, 5))

        self._identify_btn = ctk.CTkButton(
            btn_row,
            text="Identify Finger",
            height=40,
            state="disabled",
            fg_color="#2e7d32",
            hover_color="#1b5e20",
            command=self._start_identify,
        )
        self._identify_btn.pack(side="left", expand=True, fill="x", padx=(5, 0))

        self._cancel_btn = ctk.CTkButton(
            p,
            text="Cancel Operation",
            height=34,
            fg_color="#b71c1c",
            hover_color="#7f0000",
            command=self._cancel_op,
        )
        # cancel_btn is only packed when an op is running

        # --- Status log ---
        ctk.CTkLabel(
            p, text="Status Log", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", pady=(4, 2))

        self._log_box = ctk.CTkTextbox(
            p, state="disabled", font=ctk.CTkFont(family="Courier", size=12)
        )
        self._log_box.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # Port / connection
    # ------------------------------------------------------------------

    def _refresh_ports(self) -> None:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        usb = [
            p
            for p in ports
            if any(k in p.lower() for k in ("usbserial", "usbmodem", "slab", "ftdi"))
        ]
        choices = usb if usb else ports
        self._port_combo.configure(values=choices)
        if choices:
            self._port_var.set(choices[0])

    def _toggle_connect(self) -> None:
        if self._sensor.connected:
            self._sensor.disconnect()
            self._set_connected(False)
        else:
            port = self._port_var.get().strip()
            if not port:
                self._log("No port selected.")
                return
            try:
                self._sensor.connect(port)
                self._set_connected(True)
                self._refresh_enrolled()
            except Exception as exc:
                self._log(f"Connection failed: {exc}")

    def _set_connected(self, connected: bool) -> None:
        if connected:
            self._connect_btn.configure(text="Disconnect", fg_color="#c62828", hover_color="#8b0000")
            self._conn_label.configure(text="● Connected", text_color="#4caf50")
            self._enroll_btn.configure(state="normal")
            self._identify_btn.configure(state="normal")
            self._log(f"Connected to {self._port_var.get()}")
        else:
            self._connect_btn.configure(
                text="Connect",
                fg_color=ctk.ThemeManager.theme["CTkButton"]["fg_color"],
                hover_color=ctk.ThemeManager.theme["CTkButton"]["hover_color"],
            )
            self._conn_label.configure(text="● Disconnected", text_color="gray")
            self._enroll_btn.configure(state="disabled")
            self._identify_btn.configure(state="disabled")
            self._log("Disconnected.")

    # ------------------------------------------------------------------
    # Enrolled list
    # ------------------------------------------------------------------

    def _refresh_enrolled(self) -> None:
        if not self._sensor.connected:
            return
        try:
            templates = self._sensor.get_templates()
        except Exception as exc:
            self._log(f"Could not read templates: {exc}")
            return

        for w in self._enrolled_widgets:
            w.destroy()
        self._enrolled_widgets.clear()
        self._fid_to_widget.clear()

        names = storage.list_all()

        if not templates:
            lbl = ctk.CTkLabel(
                self._enrolled_scroll,
                text="No fingerprints enrolled",
                text_color="gray",
                font=ctk.CTkFont(size=11),
            )
            lbl.pack(pady=24)
            self._enrolled_widgets.append(lbl)
            return

        for fid in sorted(templates):
            name = names.get(fid, f"ID #{fid}")
            self._add_enrolled_row(fid, name)

    def _add_enrolled_row(self, fid: int, name: str) -> None:
        row = ctk.CTkFrame(self._enrolled_scroll, fg_color=("gray88", "gray22"))
        row.pack(fill="x", pady=2, padx=2)
        self._enrolled_widgets.append(row)
        self._fid_to_widget[fid] = row

        ctk.CTkLabel(
            row,
            text=f"#{fid}",
            width=36,
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).pack(side="left", padx=(8, 2))

        ctk.CTkLabel(
            row, text=name, font=ctk.CTkFont(size=11), anchor="w"
        ).pack(side="left", padx=4, fill="x", expand=True)

        ctk.CTkButton(
            row,
            text="✕",
            width=28,
            height=24,
            fg_color="#c62828",
            hover_color="#8b0000",
            command=lambda f=fid, n=name: self._delete_finger(f, n),
        ).pack(side="right", padx=6, pady=4)

    def _append_enrolled_row(self, fid: int, name: str) -> None:
        """Add one row to the list without re-querying the sensor.
        Clears the 'No fingerprints enrolled' placeholder if present."""
        if fid in self._fid_to_widget:
            return  # already shown
        # Remove the placeholder label if it's the only thing in the list
        if len(self._enrolled_widgets) == 1 and not self._fid_to_widget:
            self._enrolled_widgets[0].destroy()
            self._enrolled_widgets.clear()
        self._add_enrolled_row(fid, name)

    def _delete_finger(self, finger_id: int, name: str) -> None:
        if self._op_running:
            return
        try:
            self._sensor.delete_model(finger_id)
            storage.delete_name(finger_id)
        except Exception as exc:
            self._log(f"Delete failed: {exc}")
            return

        # Remove the row directly — don't re-query the sensor, whose EEPROM
        # commit may lag behind the acknowledged delete command.
        row = self._fid_to_widget.pop(finger_id, None)
        if row:
            self._enrolled_widgets.remove(row)
            row.destroy()

        if not self._fid_to_widget:
            lbl = ctk.CTkLabel(
                self._enrolled_scroll,
                text="No fingerprints enrolled",
                text_color="gray",
                font=ctk.CTkFont(size=11),
            )
            lbl.pack(pady=24)
            self._enrolled_widgets.append(lbl)

        self._log(f"Deleted: {name} (slot #{finger_id})")

    # ------------------------------------------------------------------
    # Operations (run in background thread)
    # ------------------------------------------------------------------

    def _start_enroll(self) -> None:
        templates = self._sensor.get_templates()
        try:
            slot = storage.next_available_id(templates)
        except RuntimeError as exc:
            self._log(str(exc))
            return

        dialog = _EnrollDialog(self, slot)
        name = dialog.get_result()
        if not name:
            return

        self._set_busy(True)
        self._log(f"Starting enrollment for '{name}' → slot #{slot}...")
        self._stop_event.clear()

        def run() -> None:
            img_data = None
            try:
                img_data = self._sensor.enroll_finger(
                    slot,
                    callback=lambda m: self._queue.put(("log", m)),
                    stop_event=self._stop_event,
                )
                storage.save_name(slot, name)
                self._queue.put(("log", f"'{name}' enrolled successfully (slot #{slot})"))
                self._queue.put(("image", img_data))
            except Exception as exc:
                self._queue.put(("log", f"Enrollment failed: {exc}"))
            finally:
                self._queue.put(("add_row", slot, name))
                self._queue.put(("busy", False))

        threading.Thread(target=run, daemon=True).start()

    def _start_identify(self) -> None:
        self._set_busy(True)
        self._log("Starting identification — place your finger on the sensor...")
        self._stop_event.clear()

        def run() -> None:
            try:
                finger_id, confidence, img_data = self._sensor.identify_finger(
                    callback=lambda m: self._queue.put(("log", m)),
                    stop_event=self._stop_event,
                )
                if finger_id is not None:
                    name = storage.get_name(finger_id)
                    self._queue.put(
                        ("log", f"Match: {name}  (slot #{finger_id}, confidence {confidence})")
                    )
                else:
                    self._queue.put(("log", "No match found in database."))
                self._queue.put(("image", img_data))
            except Exception as exc:
                self._queue.put(("log", f"Identification failed: {exc}"))
            finally:
                self._queue.put(("busy", False))

        threading.Thread(target=run, daemon=True).start()

    def _cancel_op(self) -> None:
        self._stop_event.set()
        self._log("Cancelling...")

    def _set_busy(self, busy: bool) -> None:
        self._op_running = busy
        state = "disabled" if busy else "normal"
        self._enroll_btn.configure(state=state)
        self._identify_btn.configure(state=state)
        if busy:
            self._cancel_btn.pack(fill="x", pady=(0, 8))
        else:
            self._cancel_btn.pack_forget()

    # ------------------------------------------------------------------
    # Queue polling (thread → GUI bridge)
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                msg = self._queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "image":
                    if msg[1]:
                        self._display_fp_image(msg[1])
                    else:
                        self._log("Note: fingerprint image not available from sensor")
                elif kind == "busy":
                    self._set_busy(msg[1])
                elif kind == "refresh":
                    self._refresh_enrolled()
                elif kind == "add_row":
                    self._append_enrolled_row(msg[1], msg[2])
        except queue.Empty:
            pass
        self.after(40, self._poll_queue)

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        self._log_box.insert("end", f"[{ts}]  {message}\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    # ------------------------------------------------------------------
    # Fingerprint image display
    # ------------------------------------------------------------------

    def _draw_placeholder(self) -> None:
        self._canvas.delete("all")
        cx, cy = _CANVAS_W // 2, _CANVAS_H // 2
        self._canvas.create_text(cx, cy - 20, text="◈", font=("Arial", 44), fill="#334466")
        self._canvas.create_text(cx, cy + 28, text="No image", font=("Arial", 11), fill="#334466")

    def _display_fp_image(self, raw: bytes) -> None:
        """Decode sensor image data and render it on the canvas.

        The adafruit library may return either:
          - 4-bit packed  (2 px/byte): 36 864 bytes for 256×288
          - 8-bit unpacked (1 px/byte): 73 728 bytes for 256×288
        We detect which by checking the byte count.
        """
        try:
            n = len(raw)
            self._log(f"Image data received: {n} bytes")

            # --- Determine pixel format and dimensions ---
            # 4-bit packed variants
            if n == 256 * 288 // 2:      # 36 864
                w, h = 256, 288
                pixels = _unpack_nibbles(raw)
            elif n == 256 * 256 // 2:    # 32 768
                w, h = 256, 256
                pixels = _unpack_nibbles(raw)
            # 8-bit unpacked variants
            elif n == 256 * 288:         # 73 728
                w, h = 256, 288
                pixels = list(raw)
            elif n == 256 * 256:         # 65 536
                w, h = 256, 256
                pixels = list(raw)
            else:
                # Unknown length — log it and fall back to 4-bit square guess
                self._log(f"Unrecognised image size ({n} B); attempting 4-bit decode")
                side = int(math.isqrt(n * 2))
                w = h = side
                pixels = _unpack_nibbles(raw)

            pixels = pixels[: w * h]
            img = Image.new("L", (w, h))
            img.putdata(pixels)

            # Histogram equalisation makes ridge detail visible regardless of
            # how bright/dark the raw capture is, then a mild sharpness pass.
            img = ImageOps.equalize(img)
            img = ImageEnhance.Sharpness(img).enhance(2.0)
            img = img.resize((_CANVAS_W, _CANVAS_H), Image.LANCZOS)

            self._tk_img = ImageTk.PhotoImage(img)
            self._canvas.delete("all")
            self._canvas.create_image(0, 0, anchor="nw", image=self._tk_img)
        except Exception as exc:
            self._log(f"Could not display image: {exc}")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def on_closing(self) -> None:
        self._stop_event.set()
        self._sensor.disconnect()
        self.destroy()
