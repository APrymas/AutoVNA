from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import serial
import serial.tools.list_ports


@dataclass
class DeviceInfo:
    port: str = ""
    serial_number: str = ""
    manufacturer: str = ""
    product: str = ""
    vid: str = ""
    pid: str = ""
    firmware_info: str = ""
    protocol: str = ""
    device_family: str = ""
    is_demo: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class NanoVNA:
    DEMO_PORT = "LIVE DEMO"

    PROTOCOL_DEMO = "demo"
    PROTOCOL_TEXT = "text"
    PROTOCOL_V2 = "binary-v2"


    _CMD_READ = 0x10
    _CMD_READFIFO = 0x18
    _CMD_WRITE = 0x20
    _CMD_WRITE2 = 0x21
    _CMD_WRITE8 = 0x23


    _ADDR_SWEEP_START = 0x00
    _ADDR_SWEEP_STEP = 0x10
    _ADDR_SWEEP_POINTS = 0x20
    _ADDR_VALUES_PER_FREQUENCY = 0x22
    _ADDR_VALUES_FIFO = 0x30
    _ADDR_DEVICE_VARIANT = 0xF0
    _ADDR_PROTOCOL_VERSION = 0xF1
    _ADDR_HARDWARE_REVISION = 0xF2
    _ADDR_FW_MAJOR = 0xF3
    _ADDR_FW_MINOR = 0xF4

    _V2_POINT_SIZE = 32
    _V2_MAX_FIFO_READ = 255
    _V2_MIN_FREQUENCY_HZ = 50_000

    def __init__(self) -> None:
        self.ser: Optional[serial.Serial] = None
        self.lock = threading.RLock()
        self.info = DeviceInfo()
        self.demo = False
        self.protocol = ""

        self.demo_start = 100_000.0
        self.demo_stop = 10_000_000.0
        self.demo_points = 101
        self._demo_phase = 0.0
        self._demo_drift = 0.0
        self._demo_sweep_index = 0
        self._demo_standard: Optional[str] = None
        self._demo_rng = np.random.default_rng(2202)



        self._binary_start = 100_000
        self._binary_stop = 10_000_000
        self._binary_points = 101
        self._binary_step = int(round((self._binary_stop - self._binary_start) / (self._binary_points - 1)))
        self._binary_hw_variant = 0
        self._binary_hw_revision = 0
        self._binary_protocol_version = 0
        self._binary_fw_major = 0
        self._binary_fw_minor = 0
        self._binary_s21_hack = False

    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = [(NanoVNA.DEMO_PORT, "live demonstration")]
        for p in serial.tools.list_ports.comports():
            label = p.description or p.device
            if p.serial_number:
                label += f" | SN: {p.serial_number}"
            if p.vid is not None and p.pid is not None:
                label += f" | USB {p.vid:04X}:{p.pid:04X}"
            out.append((p.device, label))
        return out

    @property
    def is_open(self) -> bool:
        return self.demo or bool(self.ser and self.ser.is_open)

    def connect(self, port: str, baudrate: int = 115200) -> DeviceInfo:
        self.close()
        if port == self.DEMO_PORT:
            self.demo = True
            self.protocol = self.PROTOCOL_DEMO
            self._demo_phase = 0.0
            self._demo_drift = 0.0
            self._demo_sweep_index = 0
            self._demo_standard = None
            self._demo_rng = np.random.default_rng(2202)
            self.info = DeviceInfo(
                port=port,
                serial_number="DEMO-RS35-0002",
                manufacturer="AutoNanoVNA",
                product="live demo generator",
                firmware_info="DEMO 2.2 / passive two-port",
                protocol="DEMO",
                device_family="Passive two-port with simulated VNA error terms",
                is_demo=True,
            )
            return self.info

        with self.lock:
            try:
                self.ser = serial.Serial(
                    port,
                    baudrate=baudrate,
                    timeout=0.20,
                    write_timeout=2.0,
                )
                time.sleep(0.45)
                self._reset_serial_buffers()

                text_error = ""
                try:
                    self._detect_text_protocol()
                except Exception as exc:
                    text_error = str(exc)
                    self.protocol = ""

                binary_error = ""
                if not self.protocol:
                    try:
                        self._detect_binary_v2_protocol()
                    except Exception as exc:
                        binary_error = str(exc)
                        self.protocol = ""

                if not self.protocol:
                    raise RuntimeError(
                        "Nie rozpoznano protokołu NanoVNA. "
                        f"Tryb tekstowy: {text_error or 'brak odpowiedzi'} | "
                        f"tryb binarny V2: {binary_error or 'brak odpowiedzi'}"
                    )

                port_meta = next((p for p in serial.tools.list_ports.comports() if p.device == port), None)
                firmware = self._read_firmware_description()
                family = (
                    "NanoVNA V1 / H / H4 / F / zgodna konsola tekstowa"
                    if self.protocol == self.PROTOCOL_TEXT
                    else "NanoVNA V2 / S-A-A-2 / LiteVNA / zgodny protokół V2"
                )
                protocol_label = (
                    "tekstowy NanoVNA"
                    if self.protocol == self.PROTOCOL_TEXT
                    else "binarny NanoVNA V2"
                )
                self.info = DeviceInfo(
                    port=port,
                    serial_number=getattr(port_meta, "serial_number", "") or "",
                    manufacturer=getattr(port_meta, "manufacturer", "") or "",
                    product=getattr(port_meta, "product", "")
                    or getattr(port_meta, "description", "")
                    or "",
                    vid=f"{getattr(port_meta, 'vid', 0):04X}"
                    if getattr(port_meta, "vid", None) is not None
                    else "",
                    pid=f"{getattr(port_meta, 'pid', 0):04X}"
                    if getattr(port_meta, "pid", None) is not None
                    else "",
                    firmware_info=firmware,
                    protocol=protocol_label,
                    device_family=family,
                    is_demo=False,
                )
                return self.info
            except Exception:
                self.close()
                raise

    def close(self) -> None:
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.demo = False
            self.protocol = ""
            self._demo_standard = None





    def _detect_text_protocol(self) -> None:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self._reset_serial_buffers()

        self.ser.write(b"\r\r")
        self.ser.flush()
        time.sleep(0.12)
        self._drain_bytes()

        txt = self._text_command("frequencies", wait=0.18, timeout=1.8)
        freq = self._parse_frequencies(txt)
        if len(freq) < 2:
            raise RuntimeError("Brak poprawnej odpowiedzi na komendę frequencies.")
        self.protocol = self.PROTOCOL_TEXT

    def _detect_binary_v2_protocol(self) -> None:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self._reset_serial_buffers()


        self.ser.write(struct.pack("<Q", 0))
        self.ser.flush()
        time.sleep(0.08)
        self._reset_serial_buffers()

        variant = self._binary_read_u8(self._ADDR_DEVICE_VARIANT)
        protocol = self._binary_read_u8(self._ADDR_PROTOCOL_VERSION)
        if variant != 0x02 or protocol != 0x01:
            raise RuntimeError(
                f"Nieoczekiwane rejestry identyfikacyjne: variant={variant}, protocol={protocol}."
            )

        self._binary_hw_variant = variant
        self._binary_protocol_version = protocol
        self._binary_hw_revision = self._binary_read_u8(self._ADDR_HARDWARE_REVISION)
        self._binary_fw_major = self._binary_read_u8(self._ADDR_FW_MAJOR)
        self._binary_fw_minor = self._binary_read_u8(self._ADDR_FW_MINOR)
        if self._binary_fw_major == 0xFF:
            raise RuntimeError("Urządzenie jest w trybie DFU, a nie w trybie pomiarowym.")


        self._binary_s21_hack = (
            self._binary_fw_major == 1 and self._binary_fw_minor <= 1
        )
        self.protocol = self.PROTOCOL_V2
        self._binary_apply_sweep(
            self._binary_start,
            self._binary_stop,
            self._binary_points,
        )

    def _read_firmware_description(self) -> str:
        if self.protocol == self.PROTOCOL_TEXT:
            for cmd in ("info", "version"):
                try:
                    text = self._text_command(cmd, wait=0.20, timeout=1.5).strip()
                    if text:
                        return text
                except Exception:
                    pass
            return "Tekstowa konsola NanoVNA"
        if self.protocol == self.PROTOCOL_V2:
            return (
                f"V2 binary protocol {self._binary_protocol_version} | "
                f"HW {self._binary_hw_variant}.{self._binary_hw_revision} | "
                f"FW {self._binary_fw_major}.{self._binary_fw_minor}"
            )
        return ""





    def _reset_serial_buffers(self) -> None:
        if not self.ser:
            return
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            self._drain_bytes()

    def _drain_bytes(self) -> bytes:
        if not self.ser:
            return b""
        out = bytearray()
        while self.ser.in_waiting:
            out.extend(self.ser.read(self.ser.in_waiting))
            time.sleep(0.005)
        return bytes(out)

    def _read_exact(self, count: int, timeout: float) -> bytes:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        old_timeout = self.ser.timeout
        deadline = time.monotonic() + timeout
        out = bytearray()
        try:
            self.ser.timeout = min(0.25, max(0.02, timeout))
            while len(out) < count and time.monotonic() < deadline:
                chunk = self.ser.read(count - len(out))
                if chunk:
                    out.extend(chunk)
                else:
                    time.sleep(0.005)
        finally:
            self.ser.timeout = old_timeout
        if len(out) != count:
            raise TimeoutError(f"Oczekiwano {count} bajtów, odebrano {len(out)}.")
        return bytes(out)





    def command(self, cmd: str, wait: float = 0.25, timeout: float = 3.0) -> str:

        if self.demo:
            return self._demo_command(cmd)
        if self.protocol == self.PROTOCOL_V2:
            raise RuntimeError("Urządzenie używa protokołu binarnego V2, bez konsoli tekstowej.")
        return self._text_command(cmd, wait=wait, timeout=timeout)

    def _text_command(self, cmd: str, wait: float = 0.25, timeout: float = 3.0) -> str:
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("NanoVNA nie jest podłączony.")
        with self.lock:
            self._drain_bytes()
            self.ser.write((cmd.strip() + "\r").encode("ascii", errors="ignore"))
            self.ser.flush()
            time.sleep(wait)
            out = bytearray()
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                waiting = self.ser.in_waiting
                chunk = self.ser.read(waiting or 1)
                if chunk:
                    out.extend(chunk)
                    text = out.decode(errors="ignore")
                    stripped = text.rstrip()
                    if "ch>" in text or stripped.endswith(">"):
                        break
                else:
                    time.sleep(0.01)

            text = out.decode(errors="ignore")
            lines: list[str] = []
            command_echo = cmd.strip()
            for line in text.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line or line == command_echo:
                    continue
                if line == "ch>" or line.endswith("ch>") or line == ">":
                    continue
                if line.startswith("ch> "):
                    line = line[4:].strip()
                    if not line:
                        continue
                lines.append(line)
            return "\n".join(lines)

    @staticmethod
    def _parse_frequencies(txt: str) -> np.ndarray:
        freqs: list[float] = []
        for token in txt.replace(",", " ").replace(";", " ").split():
            try:
                value = float(token)
                if value > 0:
                    freqs.append(value)
            except ValueError:
                pass
        return np.asarray(freqs, dtype=float)

    @staticmethod
    def _parse_complex_lines(txt: str) -> np.ndarray:
        values: list[complex] = []
        for line in txt.splitlines():
            parts = line.replace(",", " ").replace(";", " ").split()
            if len(parts) < 2:
                continue
            try:
                values.append(complex(float(parts[0]), float(parts[1])))
            except ValueError:
                continue
        return np.asarray(values, dtype=complex)





    def _binary_read_u8(self, address: int) -> int:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self.ser.write(struct.pack("<BB", self._CMD_READ, address & 0xFF))
        self.ser.flush()
        return self._read_exact(1, timeout=0.8)[0]

    def _binary_write_u8(self, address: int, value: int) -> None:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self.ser.write(struct.pack("<BBB", self._CMD_WRITE, address & 0xFF, value & 0xFF))

    def _binary_write_u16(self, address: int, value: int) -> None:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self.ser.write(struct.pack("<BBH", self._CMD_WRITE2, address & 0xFF, value & 0xFFFF))

    def _binary_write_u64(self, address: int, value: int) -> None:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        self.ser.write(struct.pack("<BBQ", self._CMD_WRITE8, address & 0xFF, int(value)))

    def _binary_apply_sweep(self, start_hz: float, stop_hz: float, points: int) -> np.ndarray:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")
        if points > 65535:
            raise ValueError("Protokół V2 obsługuje maksymalnie 65535 punktów w jednym sweepie.")

        start = max(self._V2_MIN_FREQUENCY_HZ, int(round(start_hz)))
        step = max(1, int(round((float(stop_hz) - float(start_hz)) / (points - 1))))
        actual_stop = start + step * (points - 1)

        hack = self._binary_s21_hack and start - step >= self._V2_MIN_FREQUENCY_HZ
        programmed_start = start - step if hack else start
        programmed_points = points + 1 if hack else points

        packet = bytearray()
        packet.extend(struct.pack("<BBQ", self._CMD_WRITE8, self._ADDR_SWEEP_START, programmed_start))
        packet.extend(struct.pack("<BBQ", self._CMD_WRITE8, self._ADDR_SWEEP_STEP, step))
        packet.extend(struct.pack("<BBH", self._CMD_WRITE2, self._ADDR_SWEEP_POINTS, programmed_points))
        packet.extend(struct.pack("<BBH", self._CMD_WRITE2, self._ADDR_VALUES_PER_FREQUENCY, 1))
        self.ser.write(packet)
        self.ser.flush()
        time.sleep(0.08)

        self._binary_start = start
        self._binary_stop = actual_stop
        self._binary_step = step
        self._binary_points = points
        return np.arange(points, dtype=float) * step + start

    def _binary_read_measurement(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if not self.ser:
            raise RuntimeError("Port nie jest otwarty.")

        hack = self._binary_s21_hack and self._binary_start - self._binary_step >= self._V2_MIN_FREQUENCY_HZ
        expected_points = self._binary_points + (1 if hack else 0)


        self.ser.write(struct.pack("<Q", 0))
        self.ser.flush()
        time.sleep(0.04)
        self._drain_bytes()
        self._binary_write_u8(self._ADDR_VALUES_FIFO, 0)
        self.ser.flush()
        time.sleep(0.05)

        s11 = np.full(expected_points, np.nan + 1j * np.nan, dtype=complex)
        s21 = np.full(expected_points, np.nan + 1j * np.nan, dtype=complex)
        received = np.zeros(expected_points, dtype=bool)

        remaining = expected_points

        total_deadline = time.monotonic() + max(4.0, expected_points * 0.08 + 1.5)
        while remaining > 0 and time.monotonic() < total_deadline:
            count = min(self._V2_MAX_FIFO_READ, remaining)
            self.ser.write(struct.pack("<BBB", self._CMD_READFIFO, self._ADDR_VALUES_FIFO, count))
            self.ser.flush()
            raw = self._read_exact(
                count * self._V2_POINT_SIZE,
                timeout=max(1.0, count * 0.08 + 0.5),
            )
            for offset in range(0, len(raw), self._V2_POINT_SIZE):
                (
                    fwd_re,
                    fwd_im,
                    rev0_re,
                    rev0_im,
                    rev1_re,
                    rev1_im,
                    freq_index,
                ) = struct.unpack_from("<iiiiiiHxxxxxx", raw, offset)
                if not 0 <= freq_index < expected_points:
                    continue
                fwd = complex(fwd_re, fwd_im)
                if abs(fwd) == 0:
                    continue
                s11[freq_index] = complex(rev0_re, rev0_im) / fwd
                s21[freq_index] = complex(rev1_re, rev1_im) / fwd
                received[freq_index] = True
            remaining = int(np.count_nonzero(~received))

        if not np.all(received):
            missing = np.flatnonzero(~received)
            preview = ", ".join(str(int(x)) for x in missing[:12])
            raise RuntimeError(
                f"Nie odebrano pełnego sweepu binarnego. Brakujące indeksy: {preview}"
                + ("…" if len(missing) > 12 else "")
            )

        if hack:
            s11 = s11[1:]
            s21 = s21[1:]
        freq = np.arange(self._binary_points, dtype=float) * self._binary_step + self._binary_start
        return freq, s11, s21





    def _demo_command(self, cmd: str) -> str:
        parts = cmd.strip().split()
        if not parts:
            return ""
        if parts[0] == "frequencies":
            return "\n".join(f"{x:.0f}" for x in self.get_frequencies())
        if parts[0] == "sweep" and len(parts) >= 4:
            self.demo_start = float(parts[1])
            self.demo_stop = float(parts[2])
            self.demo_points = int(parts[3])
            return ""
        if parts[0] in {"info", "version"}:
            return "NanoVNA Lab DEMO firmware 2.2"
        return ""

    def get_frequencies(self) -> np.ndarray:
        if self.demo:
            return np.linspace(self.demo_start, self.demo_stop, self.demo_points)
        if self.protocol == self.PROTOCOL_TEXT:
            txt = self._text_command("frequencies", wait=0.30, timeout=2.5)
            freq = self._parse_frequencies(txt)
            if len(freq) < 2:
                raise RuntimeError("Nie udało się pobrać częstotliwości.")
            return freq
        if self.protocol == self.PROTOCOL_V2:
            return np.arange(self._binary_points, dtype=float) * self._binary_step + self._binary_start
        raise RuntimeError("NanoVNA nie jest podłączony.")

    def _get_data_channel(self, channel: int) -> np.ndarray:
        if self.demo:
            return self._demo_data(channel)
        if self.protocol != self.PROTOCOL_TEXT:
            raise RuntimeError("Osobny odczyt kanału jest dostępny tylko w trybie tekstowym.")
        txt = self._text_command(f"data {channel}", wait=0.40, timeout=3.5)
        values = self._parse_complex_lines(txt)
        if len(values) < 2:
            raise RuntimeError(f"Nie udało się pobrać danych kanału {channel}.")
        return values

    @staticmethod
    def _demo_cascade(
        first: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        second: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        a, b, c, d = first
        e, f, g, h = second
        return (
            a * e + b * g,
            a * f + b * h,
            c * e + d * g,
            c * f + d * h,
        )

    @staticmethod
    def _demo_series_abcd(
        impedance: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        one = np.ones_like(impedance, dtype=complex)
        zero = np.zeros_like(impedance, dtype=complex)
        return one, impedance, zero, one

    @staticmethod
    def _demo_shunt_abcd(
        admittance: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        one = np.ones_like(admittance, dtype=complex)
        zero = np.zeros_like(admittance, dtype=complex)
        return one, zero, admittance, one

    @staticmethod
    def _demo_line_abcd(
        frequency_hz: np.ndarray,
        normalized_frequency: np.ndarray,
        characteristic_impedance: float,
        delay_s: float,
        base_loss_neper: float,
        slope_loss_neper: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        propagation = (
            base_loss_neper
            + slope_loss_neper * normalized_frequency
            + 1j * 2.0 * np.pi * frequency_hz * delay_s
        )
        cosh = np.cosh(propagation)
        sinh = np.sinh(propagation)
        return (
            cosh,
            characteristic_impedance * sinh,
            sinh / characteristic_impedance,
            cosh,
        )

    @staticmethod
    def _demo_abcd_to_s(
        matrix: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
        z0: float = 50.0,
    ) -> tuple[np.ndarray, np.ndarray]:
        a, b, c, d = matrix
        denominator = a + b / z0 + c * z0 + d
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            s11 = (a + b / z0 - c * z0 - d) / denominator
            s21 = 2.0 / denominator
        return s11, s21

    def set_demo_standard(self, standard: Optional[str]) -> None:
        if standard is not None:
            standard = standard.strip().lower()
        valid = {None, "open", "short", "load", "thru"}
        if standard not in valid:
            raise ValueError(f"Unknown demo calibration standard: {standard}")
        with self.lock:
            self._demo_standard = standard

    def _demo_true_dut(
        self,
        frequency_hz: np.ndarray,
        normalized_frequency: np.ndarray,
        drift: float,
    ) -> tuple[np.ndarray, np.ndarray]:
        z0 = 50.0
        n = len(frequency_hz)
        one = np.ones(n, dtype=complex)
        zero = np.zeros(n, dtype=complex)
        matrix = (one.copy(), zero.copy(), zero.copy(), one.copy())

        # Two coupled resonant sections form a realistic band-pass response.
        x1 = (normalized_frequency - (0.43 + drift)) / 0.090
        x2 = (normalized_frequency - (0.54 + 0.70 * drift)) / 0.100

        temperature = 1.0 + 0.012 * math.sin(self._demo_phase * 0.37)
        z1 = 2.20 * temperature + 1j * z0 * 0.90 * x1
        y1 = 1.0 / 2500.0 + 1j * (0.50 / z0) * x1
        z2 = 2.60 * temperature + 1j * z0 * 0.72 * x2
        y2 = 1.0 / 2100.0 + 1j * (0.40 / z0) * x2

        # A weak shunt resonance creates a natural transmission notch.
        notch_x = (normalized_frequency - (0.72 - 0.25 * drift)) / 0.025
        notch_y = (1.0 / 85.0) / (1.0 + 1j * notch_x)

        sections = [
            self._demo_line_abcd(
                frequency_hz,
                normalized_frequency,
                characteristic_impedance=51.8,
                delay_s=8e-9,
                base_loss_neper=0.008,
                slope_loss_neper=0.008,
            ),
            self._demo_series_abcd(z1 / 2.0),
            self._demo_shunt_abcd(y1),
            self._demo_series_abcd(z1 / 2.0),
            self._demo_series_abcd(z2 / 2.0),
            self._demo_shunt_abcd(y2),
            self._demo_series_abcd(z2 / 2.0),
            self._demo_shunt_abcd(notch_y),
            self._demo_line_abcd(
                frequency_hz,
                normalized_frequency,
                characteristic_impedance=48.9,
                delay_s=12e-9,
                base_loss_neper=0.012,
                slope_loss_neper=0.012,
            ),
        ]
        for section in sections:
            matrix = self._demo_cascade(matrix, section)

        s11, s21 = self._demo_abcd_to_s(matrix, z0=z0)
        return s11, s21

    def _demo_error_terms(
        self,
        frequency_hz: np.ndarray,
        normalized_frequency: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        slow = 0.012 * math.sin(self._demo_phase * 0.11)

        directivity_db = 34.0 + 2.0 * np.sin(2.0 * np.pi * normalized_frequency + 0.2)
        directivity = 10.0 ** (-directivity_db / 20.0) * np.exp(
            1j
            * (
                0.45
                + 2.0 * np.pi * 0.80 * normalized_frequency
                + slow
            )
        )

        source_match_db = 20.0 + 1.5 * np.cos(2.0 * np.pi * normalized_frequency)
        source_match = 10.0 ** (-source_match_db / 20.0) * np.exp(
            1j * (-0.60 + 2.0 * np.pi * 1.10 * normalized_frequency - 0.5 * slow)
        )

        reflection_tracking_db = (
            0.65
            + 0.25 * normalized_frequency
            + 0.08 * np.sin(2.0 * np.pi * 2.20 * normalized_frequency)
        )
        reflection_tracking = 10.0 ** (-reflection_tracking_db / 20.0) * np.exp(
            1j
            * (
                -2.0 * np.pi * frequency_hz * 6e-9
                + 0.05 * np.sin(2.0 * np.pi * normalized_frequency)
                + 0.3 * slow
            )
        )

        transmission_tracking_db = (
            0.85
            + 0.35 * normalized_frequency
            + 0.12 * np.sin(2.0 * np.pi * 1.70 * normalized_frequency)
        )
        transmission_tracking = 10.0 ** (-transmission_tracking_db / 20.0) * np.exp(
            1j
            * (
                -2.0 * np.pi * frequency_hz * 24e-9
                + 0.08 * np.sin(2.0 * np.pi * normalized_frequency)
                - 0.2 * slow
            )
        )

        isolation = 10.0 ** (-78.0 / 20.0) * np.exp(
            1j * (1.20 + 2.0 * np.pi * 3.10 * normalized_frequency)
        )
        return (
            directivity,
            source_match,
            reflection_tracking,
            transmission_tracking,
            isolation,
        )

    @staticmethod
    def _demo_apply_reflection_errors(
        true_gamma: np.ndarray,
        directivity: np.ndarray,
        source_match: np.ndarray,
        reflection_tracking: np.ndarray,
    ) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return directivity + reflection_tracking * true_gamma / (
                1.0 - source_match * true_gamma
            )

    def _demo_noise(self, points: int, scale: float) -> np.ndarray:
        real = self._demo_rng.standard_normal(points)
        imag = self._demo_rng.standard_normal(points)
        if points >= 5:
            kernel = np.asarray([0.12, 0.23, 0.30, 0.23, 0.12], dtype=float)
            real = np.convolve(real, kernel, mode="same")
            imag = np.convolve(imag, kernel, mode="same")
        return scale * (real + 1j * imag)

    def _generate_demo_measurement(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        frequency_hz = self.get_frequencies()
        points = len(frequency_hz)
        self._demo_phase += 0.030
        self._demo_sweep_index += 1

        self._demo_drift = float(
            np.clip(
                0.965 * self._demo_drift + self._demo_rng.normal(0.0, 0.000045),
                -0.0015,
                0.0015,
            )
        )
        drift = self._demo_drift + 0.00055 * math.sin(self._demo_phase * 0.23)

        span = max(float(self.demo_stop - self.demo_start), 1.0)
        normalized_frequency = (frequency_hz - self.demo_start) / span
        true_s11, true_s21 = self._demo_true_dut(
            frequency_hz,
            normalized_frequency,
            drift,
        )
        directivity, source_match, reflection_tracking, transmission_tracking, isolation = (
            self._demo_error_terms(frequency_hz, normalized_frequency)
        )

        standard = self._demo_standard
        if standard == "open":
            standard_gamma = np.ones(points, dtype=complex)
            measured_s11 = self._demo_apply_reflection_errors(
                standard_gamma,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s21 = isolation.copy()
        elif standard == "short":
            standard_gamma = -np.ones(points, dtype=complex)
            measured_s11 = self._demo_apply_reflection_errors(
                standard_gamma,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s21 = isolation.copy()
        elif standard == "load":
            standard_gamma = np.zeros(points, dtype=complex)
            measured_s11 = self._demo_apply_reflection_errors(
                standard_gamma,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s21 = isolation.copy()
        elif standard == "thru":
            thru_mismatch = 0.010 * np.exp(
                1j * (0.30 + 2.0 * np.pi * 0.65 * normalized_frequency)
            )
            measured_s11 = self._demo_apply_reflection_errors(
                thru_mismatch,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s21 = transmission_tracking + isolation
        else:
            measured_s11 = self._demo_apply_reflection_errors(
                true_s11,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s21 = true_s21 * transmission_tracking + isolation

        # Standards are averaged during calibration, so their noise is slightly lower.
        standard_noise_factor = 0.55 if standard is not None else 1.0
        measured_s11 = measured_s11 + self._demo_noise(
            points,
            2.8e-4 * standard_noise_factor,
        )
        measured_s21 = measured_s21 + self._demo_noise(
            points,
            4.0e-5 * standard_noise_factor,
        )
        return frequency_hz, measured_s11, measured_s21

    def _demo_data(self, channel: int) -> np.ndarray:
        _, s11, s21 = self._generate_demo_measurement()
        return s11 if channel == 0 else s21

    def read_measurement(
        self,
        read_s11: bool = True,
        read_s21: bool = True,
    ) -> tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray]]:
        if not self.is_open:
            raise RuntimeError("NanoVNA is not connected.")
        with self.lock:
            if self.demo:
                freq, all_s11, all_s21 = self._generate_demo_measurement()
                return (
                    freq,
                    all_s11 if read_s11 else None,
                    all_s21 if read_s21 else None,
                )
            if self.protocol == self.PROTOCOL_V2:
                freq, all_s11, all_s21 = self._binary_read_measurement()
                return (
                    freq,
                    all_s11 if read_s11 else None,
                    all_s21 if read_s21 else None,
                )

            freq = self.get_frequencies()
            s11 = self._get_data_channel(0) if read_s11 else None
            s21 = self._get_data_channel(1) if read_s21 else None

        lengths = [len(freq)]
        if s11 is not None:
            lengths.append(len(s11))
        if s21 is not None:
            lengths.append(len(s21))
        n = min(lengths)
        return freq[:n], None if s11 is None else s11[:n], None if s21 is None else s21[:n]

    def set_sweep(self, start_hz: float, stop_hz: float, points: int) -> np.ndarray:
        if start_hz <= 0 or stop_hz <= start_hz:
            raise ValueError("Niepoprawny zakres częstotliwości.")
        if points < 2:
            raise ValueError("Liczba punktów musi być większa od 1.")
        if self.demo:
            self.demo_start, self.demo_stop, self.demo_points = float(start_hz), float(stop_hz), int(points)
            return self.get_frequencies()
        if self.protocol == self.PROTOCOL_V2:
            with self.lock:
                return self._binary_apply_sweep(start_hz, stop_hz, int(points))
        if self.protocol != self.PROTOCOL_TEXT:
            raise RuntimeError("NanoVNA nie jest podłączony.")

        errors: list[str] = []

        def matches(freq: np.ndarray) -> bool:
            if len(freq) != int(points):
                return False
            tol_start = max(2.0, abs(start_hz) * 1e-5)
            tol_stop = max(2.0, abs(stop_hz) * 1e-5)
            return (
                abs(float(freq[0]) - start_hz) <= tol_start
                and abs(float(freq[-1]) - stop_hz) <= tol_stop
            )

        attempts = [
            (
                "sweep START STOP POINTS",
                lambda: self._text_command(
                    f"sweep {int(start_hz)} {int(stop_hz)} {int(points)}",
                    wait=0.45,
                ),
            ),
            (
                "sweep start/stop/points",
                lambda: (
                    self._text_command(f"sweep start {int(start_hz)}"),
                    self._text_command(f"sweep stop {int(stop_hz)}"),
                    self._text_command(f"sweep points {int(points)}"),
                ),
            ),
            (
                "scan START STOP POINTS",
                lambda: self._text_command(
                    f"scan {int(start_hz)} {int(stop_hz)} {int(points)}",
                    wait=0.55,
                    timeout=4.0,
                ),
            ),
        ]

        for name, action in attempts:
            try:
                action()
                time.sleep(0.20)
                freq = self.get_frequencies()
                if matches(freq):
                    return freq
                errors.append(f"{name}: urządzenie nie potwierdziło zakresu.")
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        raise RuntimeError("Nie udało się ustawić zakresu. " + " | ".join(errors))
