import time
import threading
from typing import Callable, Optional

import adafruit_fingerprint


class FingerprintSensor:
    def __init__(self) -> None:
        self._uart = None
        self._finger: Optional[adafruit_fingerprint.Adafruit_Fingerprint] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, port: str, baud: int = 57600) -> None:
        import serial

        uart = serial.Serial(port, baudrate=baud, timeout=1)
        finger = adafruit_fingerprint.Adafruit_Fingerprint(uart)
        try:
            result = finger.verify_password()
            verified = (result is True) or (result == adafruit_fingerprint.OK)
        except Exception as exc:
            uart.close()
            raise ConnectionError(f"Sensor did not respond: {exc}") from exc
        if not verified:
            uart.close()
            raise ConnectionError("Sensor password verification failed.")
        self._uart = uart
        self._finger = finger
        self._connected = True

    def disconnect(self) -> None:
        if self._uart and self._uart.is_open:
            self._uart.close()
        self._connected = False

    # ------------------------------------------------------------------
    # Stored templates
    # ------------------------------------------------------------------

    def get_templates(self) -> list:
        self._finger.read_templates()
        return list(self._finger.templates)

    def delete_model(self, location: int) -> None:
        result = self._finger.delete_model(location)
        if result != adafruit_fingerprint.OK:
            raise RuntimeError(f"Delete failed (code {result:#04x})")

    # ------------------------------------------------------------------
    # Enroll  (2-scan workflow)
    # Returns the raw image bytes captured during the second scan, or None.
    # ------------------------------------------------------------------

    def enroll_finger(
        self,
        location: int,
        callback: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[bytes]:
        af = adafruit_fingerprint
        finger = self._finger

        def notify(msg: str) -> None:
            if callback:
                callback(msg)

        def wait_for_finger() -> None:
            while True:
                if stop_event and stop_event.is_set():
                    raise RuntimeError("Cancelled")
                result = finger.get_image()
                if result == af.OK:
                    return
                if result != af.NOFINGER:
                    raise RuntimeError(f"Image capture error (code {result:#04x})")
                time.sleep(0.01)

        def wait_for_removal() -> None:
            while True:
                if stop_event and stop_event.is_set():
                    raise RuntimeError("Cancelled")
                if finger.get_image() == af.NOFINGER:
                    return
                time.sleep(0.01)

        # First scan
        notify("Place your finger on the sensor...")
        wait_for_finger()
        notify("Processing first scan...")
        r = finger.image_2_tz(1)
        if r != af.OK:
            raise RuntimeError(f"Failed to process first scan (code {r:#04x})")

        notify("Remove your finger...")
        wait_for_removal()

        # Second scan — capture raw image immediately after get_image(),
        # before image_2_tz clears/overwrites the image buffer.
        notify("Place the same finger again...")
        wait_for_finger()
        notify("Capturing fingerprint image...")
        image_data = self._upload_image(notify)

        notify("Processing second scan...")
        r = finger.image_2_tz(2)
        if r != af.OK:
            raise RuntimeError(f"Failed to process second scan (code {r:#04x})")

        notify("Creating template...")
        r = finger.create_model()
        if r == af.ENROLLMISMATCH:
            raise RuntimeError("Fingerprints did not match — please try again.")
        if r != af.OK:
            raise RuntimeError(f"Template creation failed (code {r:#04x})")

        notify("Saving to sensor...")
        r = finger.store_model(location)
        if r != af.OK:
            raise RuntimeError(f"Failed to store template (code {r:#04x})")

        return image_data

    # ------------------------------------------------------------------
    # Identify  (1-scan search)
    # Returns (finger_id, confidence, image_bytes).  finger_id is None if
    # no match.  image_bytes may be None if the sensor doesn't support upload.
    # ------------------------------------------------------------------

    def identify_finger(
        self,
        callback: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> tuple:
        af = adafruit_fingerprint
        finger = self._finger

        def notify(msg: str) -> None:
            if callback:
                callback(msg)

        notify("Place your finger on the sensor...")
        while True:
            if stop_event and stop_event.is_set():
                raise RuntimeError("Cancelled")
            result = finger.get_image()
            if result == af.OK:
                break
            if result != af.NOFINGER:
                raise RuntimeError(f"Image capture error (code {result:#04x})")
            time.sleep(0.01)

        # Capture raw image immediately after get_image(), while the
        # image buffer is guaranteed fresh and untouched.
        notify("Capturing fingerprint image...")
        image_data = self._upload_image(notify)

        notify("Processing fingerprint...")
        r = finger.image_2_tz(1)
        if r != af.OK:
            raise RuntimeError(f"Failed to process image (code {r:#04x})")

        notify("Searching database...")
        r = finger.finger_fast_search()
        if r == af.OK:
            return finger.finger_id, finger.confidence, image_data
        if r == af.NOTFOUND:
            return None, 0, image_data
        raise RuntimeError(f"Search failed (code {r:#04x})")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upload_image(self, notify: Callable[[str], None]) -> Optional[bytes]:
        """Upload the raw image from the sensor's image buffer.
        Temporarily increases the UART timeout to handle the full transfer
        (~36 kB at 57600 baud can take several seconds)."""
        old_timeout = self._uart.timeout
        try:
            self._uart.timeout = 10
            data = self._finger.get_fpdata(sensorbuffer="image")
            return bytes(data) if data else None
        except Exception as exc:
            notify(f"Image upload failed: {exc}")
            return None
        finally:
            self._uart.timeout = old_timeout
