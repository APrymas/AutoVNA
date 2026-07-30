from __future__ import annotations

import math
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional

import numpy as np


class ZVLCommunicationError(RuntimeError):
    pass


@dataclass
class DeviceInfo:
    host: str = ""
    tcp_port: int = 5025
    idn: str = ""
    manufacturer: str = ""
    product: str = ""
    serial_number: str = ""
    firmware_info: str = ""
    is_demo: bool = False

    @property
    def port(self) -> str:
        
        return f"{self.host}:{self.tcp_port}" if self.host else ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["port"] = self.port
        return data


class ZVL13:
    
    DEFAULT_PORT = 5025
    DEMO_HOST = "DEMO"
    MIN_FREQUENCY_HZ = 5_000.0
    MAX_FREQUENCY_HZ = 15_000_000_000.0
    MIN_POINTS = 2
    MAX_POINTS = 4001
    MAX_SCPI_RESPONSE_BYTES = 64 * 1024 * 1024

    TRACE_NAMES = {
        "S11": "AUTORS_S11",
        "S21": "AUTORS_S21",
        "S12": "AUTORS_S12",
        "S22": "AUTORS_S22",
    }

    def __init__(self, translator: Callable[[str, str | None], str] | None = None) -> None:
        self.translator = translator
        self.sock: Optional[socket.socket] = None
        self.lock = threading.RLock()
        self.info = DeviceInfo()
        self.demo = False
        self._rx_buffer = bytearray()
        self._timeout = 30.0

        self.trace_names = dict(self.TRACE_NAMES)
        self._trace_catalog_cache: dict[str, str] = {}
        self._verified_trace_selects: set[str] = set()
        self.connection_warnings: list[str] = []

        self.channel_number = 1
        self.channel_name = "Ch1"
        self.channels: dict[int, str] = {1: "Ch1"}

        self._demo_start = 100_000.0
        self._demo_stop = 10_000_000.0
        self._demo_points = 101
        self._demo_phase = 0.0
        self._demo_drift = 0.0
        self._demo_sweep_index = 0
        self._demo_standard: Optional[str] = None
        self._demo_standard_port = 1
        self._demo_correction_enabled = False
        self._demo_rng = np.random.default_rng(2202)
        self._demo_continuous = True

        self._calibration_active = False
        self._calibration_previous_continuous: Optional[bool] = None
        self._calibration_mode = ""
        self._calibration_port = 1

    def set_translator(self, translator: Callable[[str, str | None], str] | None) -> None:
        self.translator = translator

    def _t(self, key: str, fallback: str, **values) -> str:
        text = self.translator(key, fallback) if self.translator else fallback
        return text.format(**values) if values else text

    @property
    def is_open(self) -> bool:
        return self.demo or self.sock is not None

    @property
    def calibration_active(self) -> bool:
        return self._calibration_active

    def connect(self, host: str, port: int = DEFAULT_PORT, timeout: float = 30.0) -> DeviceInfo:
        host = host.strip()
        if not host:
            raise ValueError(self._t("dev_host_required", "Enter the ZVL13 analyzer IP address or host name."))
        try:
            port_i = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError(self._t("dev_port_integer", "The TCP port must be an integer.")) from exc
        if not 1 <= port_i <= 65535:
            raise ValueError(self._t("dev_port_range", "The TCP port must be in the range 1–65535."))

        self.close()
        self._timeout = max(0.2, float(timeout))
        self.connection_warnings.clear()
        self.trace_names = dict(self.TRACE_NAMES)
        self._trace_catalog_cache.clear()
        self._verified_trace_selects.clear()
        self.channel_number = 1
        self.channel_name = "Ch1"
        self.channels = {1: "Ch1"}

        if host.upper() == self.DEMO_HOST:
            self.demo = True
            self._demo_phase = 0.0
            self._demo_drift = 0.0
            self._demo_sweep_index = 0
            self._demo_standard = None
            self._demo_standard_port = 1
            self._demo_correction_enabled = False
            self._demo_rng = np.random.default_rng(2202)
            self.info = DeviceInfo(
                host=self.DEMO_HOST,
                tcp_port=port_i,
                idn="Rohde&Schwarz,ZVL13,DEMO-RS35-0002,DEMO 2.2",
                manufacturer="Rohde & Schwarz",
                product=self._t("dev_demo_product", "live demo generator"),
                serial_number="DEMO-RS35-0002",
                firmware_info="DEMO 2.2 / passive two-port",
                is_demo=True,
            )
            return self.info

        sock: Optional[socket.socket] = None
        try:
            sock = socket.create_connection((host, port_i), timeout=self._timeout)
            sock.settimeout(self._timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.lock:
                self.sock = sock
                self._rx_buffer.clear()
                idn = self._query_locked("*IDN?", timeout=self._timeout)
                if not idn:
                    raise ZVLCommunicationError(
                        self._t("dev_no_idn", "The ZVL13 accepted the TCP connection but did not answer *IDN?.")
                    )

                fields = [item.strip() for item in idn.split(",")]
                self.info = DeviceInfo(
                    host=host,
                    tcp_port=port_i,
                    idn=idn,
                    manufacturer=fields[0] if fields else "Rohde & Schwarz",
                    product=fields[1] if len(fields) > 1 else "R&S ZVL13",
                    serial_number=fields[2] if len(fields) > 2 else "",
                    firmware_info=fields[3] if len(fields) > 3 else "",
                    is_demo=False,
                )

                channel_catalog = self._query_locked(
                    "CONFigure:CHANnel1:CATalog?", timeout=10.0
                )
                channels = self._parse_channel_catalog(channel_catalog)
                self.channels = dict(channels)
                if channels:
                    self.channel_number = 1 if 1 in channels else min(channels)
                    self.channel_name = channels[self.channel_number]
                else:
                    self._send_line_locked("CONFigure:CHANnel1:STATe ON")
                    self._check_error_locked(self._t("dev_channel_create", "Could not create channel 1"))
                    self.channel_number = 1
                    self.channel_name = "Ch1"
                    self.channels = {1: "Ch1"}

                self._send_line_locked("FORMat:DATA ASCii")
                self._send_line_locked(f"TRIGger{self.channel_number}:SEQuence:SOURce IMMediate")

                self._send_line_locked("SYSTem:DATA:SIZE ALL")
                self._collect_connection_warnings_locked()

            return self.info
        except Exception as exc:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self.lock:
                self.sock = None
                self._rx_buffer.clear()
            if isinstance(exc, (ValueError, ZVLCommunicationError)):
                raise
            raise ZVLCommunicationError(self._t("dev_connect_failed", "Could not connect to {host}:{port}.\n\nDetails: {error}\n\nCheck the IP address, port 5025, ping, LAN settings and system firewall.", host=host, port=port_i, error=exc)) from exc

    def close(self) -> None:
        with self.lock:
            self._close_socket_locked()
            self.demo = False
            self.info = DeviceInfo()
            self._trace_catalog_cache.clear()
            self._verified_trace_selects.clear()
            self.channel_number = 1
            self.channel_name = "Ch1"
            self.channels = {1: "Ch1"}
            self._calibration_active = False
            self._calibration_previous_continuous = None
            self._calibration_mode = ""
            self._demo_standard = None
            self._demo_standard_port = 1
            self._demo_correction_enabled = False

    def interrupt(self) -> None:
        
        self._close_socket_locked()
        self.demo = False
        self._trace_catalog_cache.clear()

    def _close_socket_locked(self) -> None:
        sock = self.sock
        self.sock = None
        self._rx_buffer.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _require_socket(self) -> socket.socket:
        if self.sock is None:
            raise ZVLCommunicationError(self._t("dev_not_connected", "The R&S ZVL13 is not connected."))
        return self.sock

    def _send_line_locked(self, command: str) -> None:
        sock = self._require_socket()
        clean = command.rstrip("\r\n")
        if not clean:
            raise ValueError(self._t("dev_empty_command", "The SCPI command cannot be empty."))
        try:
            payload = (clean + "\n").encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(self._t("dev_ascii_command", "SCPI commands must contain ASCII characters only.")) from exc
        try:
            sock.sendall(payload)
        except (OSError, socket.timeout) as exc:
            self._close_socket_locked()
            raise ZVLCommunicationError(self._t("dev_send_error", "SCPI command transmission error: {error}", error=exc)) from exc

    def _read_line_locked(self, timeout: Optional[float] = None) -> str:
        sock = self._require_socket()
        old_timeout = sock.gettimeout()
        timeout_s = self._timeout if timeout is None else max(0.1, float(timeout))
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                newline_index = self._rx_buffer.find(b"\n")
                if newline_index >= 0:
                    raw = bytes(self._rx_buffer[:newline_index])
                    del self._rx_buffer[: newline_index + 1]
                    return raw.rstrip(b"\r\x00").decode("ascii", errors="replace").strip()

                if len(self._rx_buffer) > self.MAX_SCPI_RESPONSE_BYTES:
                    raise ZVLCommunicationError(
                        self._t("dev_response_too_large", "The SCPI response exceeded 64 MB or was not terminated with LF.")
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        self._t("dev_response_timeout", "Timed out after {timeout:.1f} s while waiting for a complete LF-terminated SCPI response.", timeout=timeout_s)
                    )

                sock.settimeout(min(remaining, 1.0))
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as exc:
                    raise ZVLCommunicationError(self._t("dev_receive_error", "SCPI response receive error: {error}", error=exc)) from exc

                if not chunk:
                    raise ZVLCommunicationError(
                        self._t("dev_remote_closed", "The analyzer closed the TCP connection while the response was being received.")
                    )
                self._rx_buffer.extend(chunk)
        finally:
            try:
                sock.settimeout(old_timeout)
            except OSError:
                pass

    def _query_locked(self, command: str, timeout: Optional[float] = None) -> str:
        self._send_line_locked(command)
        try:
            return self._read_line_locked(timeout)
        except (TimeoutError, ZVLCommunicationError, OSError) as exc:


            self._close_socket_locked()
            if isinstance(exc, TimeoutError):
                raise ZVLCommunicationError(
                    self._t("dev_connection_closed_sync", "{error}\nThe connection was closed to prevent SCPI responses from becoming misaligned.", error=exc)
                ) from exc
            raise

    def write(self, command: str) -> None:
        if self.demo:
            self._demo_command(command, query=False)
            return
        with self.lock:
            self._send_line_locked(command)

    def query(self, command: str, timeout: Optional[float] = None) -> str:
        if self.demo:
            return self._demo_command(command, query=True)
        with self.lock:
            return self._query_locked(command, timeout)

    def terminal_command(self, command: str) -> tuple[bool, str]:
        

        command = command.strip()
        if not command:
            return False, ""
        if self._calibration_active:
            raise RuntimeError(self._t("dev_terminal_calibration", "The ZVL terminal is locked while calibration is active."))
        query_count = command.count("?")
        if query_count > 1:
            raise ValueError(self._t("dev_terminal_multi_query", "The terminal accepts at most one query per line. Send queries separately to prevent TCP responses from becoming misaligned."))
        if self.demo:
            if query_count:
                return True, self._demo_command(command, query=True)
            self._demo_command(command, query=False)
            return False, '0,"No error"'

        with self.lock:
            if query_count == 1:
                return True, self._query_locked(command, timeout=30.0)
            self._send_line_locked(command)
            error = self._query_locked("SYSTem:ERRor?", timeout=5.0)
            return False, error

    def _parse_ascii_numbers(self, text: str) -> np.ndarray:
        if text.lstrip().startswith("#"):
            raise ZVLCommunicationError(
                self._t("dev_binary_response", "The ZVL13 returned a binary block. The program requires FORM:DATA ASCII.")
            )
        tokens = text.replace(";", ",").replace("\r", "").replace("\n", ",").split(",")
        values: list[float] = []
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            try:
                values.append(float(token.replace("D", "E").replace("d", "e")))
            except ValueError as exc:
                raise ZVLCommunicationError(
                    self._t("dev_number_parse", "Could not parse a numeric value from the response: {token}", token=repr(token[:120]))
                ) from exc
        return np.asarray(values, dtype=float)

    def _parse_complex_trace(self, text: str, label: str = "trace") -> np.ndarray:
        values = self._parse_ascii_numbers(text)
        if len(values) < 4:
            raise ZVLCommunicationError(
                self._t("dev_too_little_data", "Too little data for {label}: received {count} values.", label=label, count=len(values))
            )
        if len(values) % 2:
            raise ZVLCommunicationError(
                self._t("dev_odd_sdata", "Odd SDATA value count for {label}: {count}.", label=label, count=len(values))
            )
        return values[0::2] + 1j * values[1::2]

    @staticmethod
    def _parse_catalog(text: str) -> dict[str, str]:
        if not text or text.strip() in {'""', "''"}:
            return {}
        cleaned = text.replace("'", "").replace('"', "")
        items = [item.strip() for item in cleaned.split(",") if item.strip()]
        return {
            items[index].upper(): items[index + 1].upper()
            for index in range(0, len(items) - 1, 2)
        }

    @staticmethod
    def _parse_channel_catalog(text: str) -> dict[int, str]:
        
        if not text or text.strip() in {'""', "''"}:
            return {}
        cleaned = text.replace("'", "").replace('\"', "")
        items = [item.strip() for item in cleaned.split(",") if item.strip()]
        channels: dict[int, str] = {}
        for index in range(0, len(items) - 1, 2):
            try:
                number = int(float(items[index]))
            except ValueError:
                continue
            channels[number] = items[index + 1]
        return channels

    @staticmethod
    def _error_is_clear(text: str) -> bool:
        normalized = text.strip().lstrip("+")
        return normalized == "0" or normalized.startswith("0,") or normalized.startswith('0,"')

    def _collect_connection_warnings_locked(self) -> None:
        
        for _ in range(12):
            try:
                error = self._query_locked("SYSTem:ERRor?", timeout=5.0)
            except Exception:
                raise
            if self._error_is_clear(error):
                return
            self.connection_warnings.append(error)

    def _check_error_locked(self, context: str) -> None:
        error = self._query_locked("SYSTem:ERRor?", timeout=5.0)
        if not self._error_is_clear(error):
            raise ZVLCommunicationError(f"{context}: {error}")

    def _expect_opc(self, response: str, context: str) -> None:
        if response.strip() not in {"1", "+1"}:
            raise ZVLCommunicationError(
                self._t("dev_opc_unexpected", "{context}: unexpected *OPC? response: {response}", context=context, response=repr(response))
            )


    def _read_catalog_locked(self, refresh: bool = False) -> dict[str, str]:
        if refresh or not self._trace_catalog_cache:
            response = self._query_locked(f"CALCulate{self.channel_number}:PARameter:CATalog?", timeout=10.0)
            self._trace_catalog_cache = self._parse_catalog(response)
        return dict(self._trace_catalog_cache)

    def ensure_measurement_setup(self, parameters: tuple[str, ...] | list[str]) -> None:
        if self.demo:
            return
        with self.lock:
            catalog = self._read_catalog_locked(refresh=False)
            created = False
            for parameter_raw in parameters:
                parameter = parameter_raw.upper()
                if parameter not in self.trace_names:
                    raise ValueError(self._t("dev_unsupported_parameter", "Unsupported parameter: {parameter}", parameter=parameter))

                trace_name = self.trace_names[parameter]
                current = catalog.get(trace_name.upper())
                if current == parameter:
                    continue

                if current is not None and current != parameter:
                    base_name = self.TRACE_NAMES[parameter]
                    suffix = 2
                    candidate = base_name
                    while candidate.upper() in catalog:
                        candidate = f"{base_name}_{suffix}"
                        suffix += 1
                    trace_name = candidate
                    self.trace_names[parameter] = trace_name

                self._send_line_locked(
                    f"CALCulate{self.channel_number}:PARameter:SDEFine '{trace_name}','{parameter}'"
                )
                self._check_error_locked(self._t("dev_trace_create", "Could not create the {parameter} trace", parameter=parameter))
                catalog[trace_name.upper()] = parameter
                created = True

            if created:
                self._verified_trace_selects.clear()


                catalog = self._read_catalog_locked(refresh=True)
                for parameter_raw in parameters:
                    parameter = parameter_raw.upper()
                    trace_name = self.trace_names[parameter]
                    if catalog.get(trace_name.upper()) != parameter:
                        raise ZVLCommunicationError(
                            self._t("dev_trace_not_confirmed", "The ZVL13 did not confirm creation of the {parameter} trace ({trace}).", parameter=parameter, trace=trace_name)
                        )

    def _select_trace_locked(self, parameter: str) -> str:
        parameter = parameter.upper()
        trace_name = self.trace_names[parameter]
        self._send_line_locked(
            f"CALCulate{self.channel_number}:PARameter:SELect '{trace_name}'"
        )

        verify_key = f"{self.channel_number}:{trace_name.upper()}"
        if verify_key not in self._verified_trace_selects:
            self._check_error_locked(
                self._t("dev_trace_select", "Could not select trace {trace} in channel {channel}", trace=trace_name, channel=self.channel_number)
            )
            self._verified_trace_selects.add(verify_key)
        return trace_name

    def get_frequencies(self, parameter: str = "S11") -> np.ndarray:
        if self.demo:
            return np.linspace(self._demo_start, self._demo_stop, self._demo_points)
        with self.lock:
            self.ensure_measurement_setup((parameter,))
            self._select_trace_locked(parameter)
            response = self._query_locked(f"CALCulate{self.channel_number}:DATA:STIMulus?", timeout=30.0)
            values = self._parse_ascii_numbers(response)
        if len(values) < 2:
            raise ZVLCommunicationError(self._t("dev_frequency_too_short", "The ZVL13 returned too few frequency points."))
        return values

    def get_continuous(self) -> bool:
        if self.demo:
            return self._demo_continuous
        response = self.query(f"INITiate{self.channel_number}:CONTinuous?", timeout=5.0)
        return response.strip().upper() in {"1", "ON", "TRUE"}

    def set_continuous(self, enabled: bool) -> None:
        if self.demo:
            self._demo_continuous = bool(enabled)
            return
        self.write(f"INITiate{self.channel_number}:CONTinuous {'ON' if enabled else 'OFF'}")

    def _ensure_continuous_measurement_locked(self) -> bool:
        

        response = self._query_locked(f"INITiate{self.channel_number}:CONTinuous?", timeout=5.0)
        continuous = response.strip().upper() in {"1", "ON", "TRUE"}
        if continuous:
            return False

        self._send_line_locked(f"INITiate{self.channel_number}:CONTinuous ON")
        self._check_error_locked(self._t("dev_continuous_failed", "Could not start continuous measurement"))
        return True

    @staticmethod
    def _all_selected_traces_are_zero(data: dict[str, np.ndarray]) -> bool:
        
        if not data:
            return True
        return all(
            values.size > 0 and np.all(values.real == 0.0) and np.all(values.imag == 0.0)
            for values in data.values()
        )

    def _read_trace_locked(self, parameter: str) -> np.ndarray:
        self._select_trace_locked(parameter)
        response = self._query_locked(f"CALCulate{self.channel_number}:DATA? SDATa", timeout=60.0)
        return self._parse_complex_trace(response, parameter)

    def read_measurement(
        self,
        read_s11: bool = True,
        read_s21: bool = True,
        read_s12: bool = False,
        read_s22: bool = False,
    ) -> tuple[
        np.ndarray,
        Optional[np.ndarray],
        Optional[np.ndarray],
        Optional[np.ndarray],
        Optional[np.ndarray],
    ]:
        if not self.is_open:
            raise ZVLCommunicationError(self._t("dev_not_connected", "The R&S ZVL13 is not connected."))
        if self._calibration_active:
            raise RuntimeError(self._t("dev_read_blocked_calibration", "Measurement readout is locked during ZVL13 calibration."))

        selected = [
            name
            for name, enabled in (
                ("S11", read_s11),
                ("S21", read_s21),
                ("S12", read_s12),
                ("S22", read_s22),
            )
            if enabled
        ]
        if not selected:
            raise ValueError(self._t("dev_enable_sparameter", "Enable at least one S-parameter."))

        if self.demo:
            frequency = self.get_frequencies(selected[0])
            self._demo_phase += 0.018
            demo_data = self._demo_measurement(frequency)
            data = {name: demo_data[name] for name in selected}
        else:
            with self.lock:


                self._send_line_locked("FORMat:DATA ASCii")
                self.ensure_measurement_setup(selected)
                just_started = self._ensure_continuous_measurement_locked()

                self._select_trace_locked(selected[0])
                frequency = self._parse_ascii_numbers(
                    self._query_locked(f"CALCulate{self.channel_number}:DATA:STIMulus?", timeout=30.0)
                )
                data = {name: self._read_trace_locked(name) for name in selected}




                if just_started and self._all_selected_traces_are_zero(data):
                    for delay_s in (0.25, 0.5, 1.0, 2.0, 4.0):
                        time.sleep(delay_s)
                        data = {name: self._read_trace_locked(name) for name in selected}
                        if not self._all_selected_traces_are_zero(data):
                            break

                expected_points = int(
                    round(float(self._query_locked(f"SENSe{self.channel_number}:SWEep:POINts?", timeout=5.0)))
                )

            lengths = {"frequency": len(frequency), **{name: len(value) for name, value in data.items()}}
            if any(length != expected_points for length in lengths.values()):
                raise ZVLCommunicationError(self._t("dev_inconsistent_sweep", "The ZVL13 returned an incomplete or inconsistent sweep: {lengths}, expected {expected}.", lengths=", ".join(f"{name}={length}" for name, length in lengths.items()), expected=expected_points))

        lengths = [len(frequency), *(len(values) for values in data.values())]
        count = min(lengths)
        if count < 2:
            raise ZVLCommunicationError(self._t("dev_incomplete_measurement", "The ZVL13 returned an incomplete measurement."))

        return (
            frequency[:count],
            data.get("S11")[:count] if "S11" in data else None,
            data.get("S21")[:count] if "S21" in data else None,
            data.get("S12")[:count] if "S12" in data else None,
            data.get("S22")[:count] if "S22" in data else None,
        )

    def set_sweep(self, start_hz: float, stop_hz: float, points: int) -> np.ndarray:
        if self._calibration_active:
            raise RuntimeError(self._t("dev_range_during_calibration", "The sweep range cannot be changed during ZVL13 calibration."))
        start_hz = float(start_hz)
        stop_hz = float(stop_hz)
        points = int(points)
        if not (self.MIN_FREQUENCY_HZ <= start_hz < stop_hz <= self.MAX_FREQUENCY_HZ):
            raise ValueError(
                self._t("dev_range_limits", "The ZVL13 range must be between 5 kHz and 15 GHz, and STOP must be greater than START.")
            )
        if not self.MIN_POINTS <= points <= self.MAX_POINTS:
            raise ValueError(self._t("dev_points_limits", "The ZVL13 point count must be in the range 2–4001."))

        if self.demo:
            self._demo_start, self._demo_stop, self._demo_points = start_hz, stop_hz, points
            return self.get_frequencies()

        with self.lock:
            current_stop = float(
                self._query_locked(f"SENSe{self.channel_number}:FREQuency:STOP?", timeout=5.0)
            )
            self._send_line_locked(f"SENSe{self.channel_number}:SWEep:TYPE LINear")


            if start_hz >= current_stop:
                self._send_line_locked(f"SENSe{self.channel_number}:FREQuency:STOP {stop_hz:.15g}")
                self._send_line_locked(f"SENSe{self.channel_number}:FREQuency:STARt {start_hz:.15g}")
            else:
                self._send_line_locked(f"SENSe{self.channel_number}:FREQuency:STARt {start_hz:.15g}")
                self._send_line_locked(f"SENSe{self.channel_number}:FREQuency:STOP {stop_hz:.15g}")
            self._send_line_locked(f"SENSe{self.channel_number}:SWEep:POINts {points}")

            opc = self._query_locked("*OPC?", timeout=30.0)
            self._expect_opc(opc, self._t("dev_range_setting", "Sweep-range setting"))
            self._check_error_locked(self._t("dev_range_rejected", "The ZVL13 rejected the sweep-range setting"))



            parameter = next(iter(self.trace_names))
            self.ensure_measurement_setup((parameter,))
            self._select_trace_locked(parameter)
            frequency = self._parse_ascii_numbers(
                self._query_locked(f"CALCulate{self.channel_number}:DATA:STIMulus?", timeout=30.0)
            )

        if len(frequency) != points:
            raise ZVLCommunicationError(
                self._t("dev_range_points_mismatch", "After changing the range, {received} points were received instead of {expected}.", received=len(frequency), expected=points)
            )
        return frequency

    def correction_enabled(self) -> bool:
        if self.demo:
            return self._demo_correction_enabled
        response = self.query(f"SENSe{self.channel_number}:CORRection:STATe?", timeout=5.0)
        return response.strip().upper() in {"1", "ON", "TRUE"}

    def _begin_calibration(self, method_token: str, mode: str, port: int = 1) -> None:
        if self._calibration_active:
            raise RuntimeError(self._t("dev_cal_active", "A calibration procedure is already active."))
        previous = self.get_continuous()
        self._calibration_previous_continuous = previous
        self._calibration_mode = mode
        self._calibration_port = port
        try:
            self.set_continuous(False)
            if not self.demo:
                with self.lock:
                    self._send_line_locked(f"TRIGger{self.channel_number}:SEQuence:SOURce IMMediate")
                    opc = self._query_locked(
                        f"SENSe{self.channel_number}:CORRection:COLLect:METHod {method_token};*OPC?",
                        timeout=30.0,
                    )
                    self._expect_opc(opc, self._t("dev_cal_method_setting", "Calibration method setting"))
                    self._check_error_locked(self._t("dev_cal_method_failed", "Could not set the calibration method"))
            self._calibration_active = True
        except Exception:
            try:
                self.set_continuous(previous)
            except Exception:
                pass
            self._calibration_previous_continuous = None
            self._calibration_mode = ""
            raise

    def begin_tosm_calibration(self) -> None:

        self._begin_calibration("TOSM", "TOSM", 1)

    def begin_one_path_two_port_calibration(self) -> None:
        
        self._begin_calibration("FOPTport", "FOPTPORT", 1)

    def begin_full_one_port_calibration(self, port: int = 1) -> None:
        if port not in (1, 2):
            raise ValueError(self._t("dev_cal_port", "The calibration port must be 1 or 2."))
        self._begin_calibration(f"FOPort{port}", "FOPORT", port)

    def acquire_calibration_standard(self, standard: str, *ports: int) -> None:
        if not self._calibration_active:
            raise RuntimeError(self._t("dev_cal_start_first", "Start a calibration procedure first."))

        standard_u = standard.upper()
        if standard_u == "THROUGH":
            token = "THRough"
        elif standard_u in {"OPEN", "SHORT", "MATCH"}:
            if len(ports) != 1 or ports[0] not in (1, 2):
                raise ValueError(self._t("dev_cal_standard_port", "Standard {standard} requires one port, either 1 or 2.", standard=standard_u))
            token = f"{standard_u}{ports[0]}"
        else:
            raise ValueError(self._t("dev_cal_standard_unsupported", "Unsupported calibration standard: {standard}", standard=standard))

        if self.demo:
            mapping = {
                "OPEN": "open",
                "SHORT": "short",
                "MATCH": "load",
                "THROUGH": "thru",
            }
            self._demo_standard = mapping[standard_u]
            self._demo_standard_port = ports[0] if ports else 1
            self._demo_measurement(self.get_frequencies())
            time.sleep(0.05)
            self._demo_standard = None
            return

        with self.lock:
            opc = self._query_locked(
                f"SENSe{self.channel_number}:CORRection:COLLect {token};*OPC?",
                timeout=240.0,
            )
            self._expect_opc(opc, self._t("dev_cal_standard_measure", "Measurement of standard {token}", token=token))
            self._check_error_locked(self._t("dev_cal_standard_error", "Standard {token} measurement error", token=token))

    def finish_calibration(self) -> None:
        if not self._calibration_active:
            raise RuntimeError(self._t("dev_cal_not_active", "No calibration procedure is active."))
        try:
            if self.demo:
                self._demo_correction_enabled = True
                self._demo_standard = None
            else:
                with self.lock:
                    opc = self._query_locked(
                        f"SENSe{self.channel_number}:CORRection:COLLect:SAVE;*OPC?",
                        timeout=240.0,
                    )
                    self._expect_opc(opc, self._t("dev_cal_calculation", "Calibration calculation"))
                    self._send_line_locked(f"SENSe{self.channel_number}:CORRection:STATe ON")
                    self._check_error_locked(self._t("dev_cal_apply_failed", "Could not apply the calibration"))
        finally:
            self._restore_after_calibration()

    def abort_calibration(self) -> None:
        self._restore_after_calibration()

    def _restore_after_calibration(self) -> None:
        previous = self._calibration_previous_continuous
        self._calibration_active = False
        self._calibration_previous_continuous = None
        self._calibration_mode = ""
        self._demo_standard = None
        if previous is not None:
            try:
                self.set_continuous(previous)
            except Exception:
                pass


    def _demo_command(self, command: str, query: bool) -> str:
        normalized = command.strip().upper()
        if normalized == "*IDN?":
            return "Rohde&Schwarz,ZVL13,DEMO-0001,DEMO"
        if normalized in {"INIT:CONT?", "INIT1:CONT?", "INITIATE1:CONTINUOUS?"}:
            return "1" if self._demo_continuous else "0"
        if "INIT" in normalized and ":CONT" in normalized and "?" not in normalized:
            self._demo_continuous = normalized.rsplit(" ", 1)[-1] in {"1", "ON", "TRUE"}
            return ""
        if normalized in {"SYST:ERR?", "SYSTEM:ERROR?", "SYSTEM:ERROR:NEXT?"}:
            return '0,"No error"'
        if normalized.endswith("*OPC?") or normalized == "*OPC?":
            return "1"
        if "SWE" in normalized and "POIN?" in normalized:
            return str(self._demo_points)
        if "FREQ" in normalized and "STOP?" in normalized:
            return str(self._demo_stop)
        if "FREQ" in normalized and "STAR" in normalized and "?" in normalized:
            return str(self._demo_start)
        if "CORR" in normalized and "STAT" in normalized and "?" in normalized:
            return "0"
        if "PAR" in normalized and "CAT" in normalized and "?" in normalized:
            pairs = []
            for parameter, name in self.trace_names.items():
                pairs.extend([name, parameter])
            return ",".join(pairs)
        if "DATA:STIM" in normalized:
            return ",".join(f"{value:.12g}" for value in self.get_frequencies())
        return "" if not query else "0"

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
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        a, b, c, d = matrix
        denominator = a + b / z0 + c * z0 + d
        determinant = a * d - b * c
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            s11 = (a + b / z0 - c * z0 - d) / denominator
            s21 = 2.0 / denominator
            s12 = 2.0 * determinant / denominator
            s22 = (-a + b / z0 - c * z0 + d) / denominator
        return s11, s21, s12, s22

    def set_demo_standard(self, standard: Optional[str], port: int = 1) -> None:
        if standard is not None:
            standard = standard.strip().lower()
        valid = {None, "open", "short", "load", "thru"}
        if standard not in valid:
            raise ValueError(f"Unknown demo calibration standard: {standard}")
        if port not in (1, 2):
            raise ValueError("The demo calibration port must be 1 or 2.")
        with self.lock:
            self._demo_standard = standard
            self._demo_standard_port = port

    def _demo_true_dut(
        self,
        frequency_hz: np.ndarray,
        normalized_frequency: np.ndarray,
        drift: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        z0 = 50.0
        n = len(frequency_hz)
        one = np.ones(n, dtype=complex)
        zero = np.zeros(n, dtype=complex)
        matrix = (one.copy(), zero.copy(), zero.copy(), one.copy())

        x1 = (normalized_frequency - (0.43 + drift)) / 0.090
        x2 = (normalized_frequency - (0.54 + 0.70 * drift)) / 0.100

        temperature = 1.0 + 0.012 * math.sin(self._demo_phase * 0.37)
        z1 = 2.20 * temperature + 1j * z0 * 0.90 * x1
        y1 = 1.0 / 2500.0 + 1j * (0.50 / z0) * x1
        z2 = 2.60 * temperature + 1j * z0 * 0.72 * x2
        y2 = 1.0 / 2100.0 + 1j * (0.40 / z0) * x2

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

        return self._demo_abcd_to_s(matrix, z0=z0)

    def _demo_error_terms(
        self,
        frequency_hz: np.ndarray,
        normalized_frequency: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        slow = 0.012 * math.sin(self._demo_phase * 0.11)

        directivity_db = 34.0 + 2.0 * np.sin(2.0 * np.pi * normalized_frequency + 0.2)
        directivity = 10.0 ** (-directivity_db / 20.0) * np.exp(
            1j * (0.45 + 2.0 * np.pi * 0.80 * normalized_frequency + slow)
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
        return directivity, source_match, reflection_tracking, transmission_tracking, isolation

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

    def _demo_measurement(self, frequency: np.ndarray) -> dict[str, np.ndarray]:
        frequency_hz = np.asarray(frequency, dtype=float)
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

        span = max(float(self._demo_stop - self._demo_start), 1.0)
        normalized_frequency = (frequency_hz - self._demo_start) / span
        true_s11, true_s21, true_s12, true_s22 = self._demo_true_dut(
            frequency_hz,
            normalized_frequency,
            drift,
        )
        directivity, source_match, reflection_tracking, transmission_tracking, isolation = (
            self._demo_error_terms(frequency_hz, normalized_frequency)
        )

        reverse_directivity = directivity * 1.07 * np.exp(
            1j * (0.31 + 0.10 * np.sin(2.0 * np.pi * normalized_frequency))
        )
        reverse_source_match = source_match * 0.94 * np.exp(
            1j * (-0.23 + 0.08 * np.cos(2.0 * np.pi * normalized_frequency))
        )
        reverse_reflection_tracking = reflection_tracking * 10.0 ** (-0.08 / 20.0) * np.exp(
            -1j * 2.0 * np.pi * frequency_hz * 0.9e-9
        )
        reverse_transmission_tracking = transmission_tracking * 10.0 ** (-0.05 / 20.0) * np.exp(
            1j * (0.025 + 0.015 * np.sin(2.0 * np.pi * normalized_frequency))
        )
        reverse_isolation = isolation * 1.15 * np.exp(1j * 0.41)

        standard = self._demo_standard
        if standard in {"open", "short", "load"}:
            gamma_value = {"open": 1.0, "short": -1.0, "load": 0.0}[standard]
            standard_gamma = np.full(points, gamma_value, dtype=complex)
            if self._demo_standard_port == 2:
                measured_s11 = directivity.copy()
                measured_s22 = self._demo_apply_reflection_errors(
                    standard_gamma,
                    reverse_directivity,
                    reverse_source_match,
                    reverse_reflection_tracking,
                )
            else:
                measured_s11 = self._demo_apply_reflection_errors(
                    standard_gamma,
                    directivity,
                    source_match,
                    reflection_tracking,
                )
                measured_s22 = reverse_directivity.copy()
            measured_s21 = isolation.copy()
            measured_s12 = reverse_isolation.copy()
        elif standard == "thru":
            thru_mismatch_1 = 0.010 * np.exp(
                1j * (0.30 + 2.0 * np.pi * 0.65 * normalized_frequency)
            )
            thru_mismatch_2 = 0.011 * np.exp(
                1j * (-0.20 + 2.0 * np.pi * 0.72 * normalized_frequency)
            )
            measured_s11 = self._demo_apply_reflection_errors(
                thru_mismatch_1,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s22 = self._demo_apply_reflection_errors(
                thru_mismatch_2,
                reverse_directivity,
                reverse_source_match,
                reverse_reflection_tracking,
            )
            measured_s21 = transmission_tracking + isolation
            measured_s12 = reverse_transmission_tracking + reverse_isolation
        elif self._demo_correction_enabled:
            residual_phase = 0.0025 * np.sin(2.0 * np.pi * normalized_frequency)
            reflection_residual = 1.0 + 0.00035 * np.cos(
                2.0 * np.pi * 1.3 * normalized_frequency
            ) + 1j * residual_phase
            transmission_residual = 1.0 - 0.00018 * normalized_frequency + 1j * 0.45 * residual_phase
            measured_s11 = true_s11 * reflection_residual
            measured_s22 = true_s22 * np.conj(reflection_residual)
            measured_s21 = true_s21 * transmission_residual
            measured_s12 = true_s12 * np.conj(transmission_residual)
        else:
            measured_s11 = self._demo_apply_reflection_errors(
                true_s11,
                directivity,
                source_match,
                reflection_tracking,
            )
            measured_s22 = self._demo_apply_reflection_errors(
                true_s22,
                reverse_directivity,
                reverse_source_match,
                reverse_reflection_tracking,
            )
            measured_s21 = true_s21 * transmission_tracking + isolation
            measured_s12 = true_s12 * reverse_transmission_tracking + reverse_isolation

        standard_noise_factor = 0.55 if standard is not None else 1.0
        correction_noise_factor = 0.65 if self._demo_correction_enabled else 1.0
        measured_s11 = measured_s11 + self._demo_noise(
            points,
            2.8e-4 * standard_noise_factor * correction_noise_factor,
        )
        measured_s22 = measured_s22 + self._demo_noise(
            points,
            3.0e-4 * standard_noise_factor * correction_noise_factor,
        )
        measured_s21 = measured_s21 + self._demo_noise(
            points,
            4.0e-5 * standard_noise_factor * correction_noise_factor,
        )
        measured_s12 = measured_s12 + self._demo_noise(
            points,
            4.3e-5 * standard_noise_factor * correction_noise_factor,
        )
        return {
            "S11": measured_s11,
            "S21": measured_s21,
            "S12": measured_s12,
            "S22": measured_s22,
        }

