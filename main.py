import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import serial
from serial.tools import list_ports

from PySide6.QtCore import QFile, QObject, QThread, Signal, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtUiTools import QUiLoader


# -----------------------------
# Protocol constants (V2)
# -----------------------------
SOF1 = 0x55
SOF2 = 0xAA

CMD_MIN = 0x00
CMD_MAX = 0x3F

LEN_MIN = 1
LEN_MAX = 80

# CMDs implemented (per your doc examples)
CMD_VI = 0x00     # DC/DC enable control (was "vi")  :contentReference[oaicite:12]{index=12}
CMD_TIME = 0x01   # Time                            :contentReference[oaicite:13]{index=13}
CMD_DAC = 0x02    # DAC output control              :contentReference[oaicite:14]{index=14}

# OP baseline (request side)
OP_GET = 0x00
OP_SET = 0x01
OP_RESET = 0x02

# Response pairing (recommended): OP_RSP = OP_REQ + 0x20  :contentReference[oaicite:15]{index=15}
OP_RSP_OFFSET = 0x20

BAUDRATE = 460_800

# DAC UI mapping (your current firmware: 10-bit counts 0..1023)
DAC_MAX_COUNTS = 1023
DAC_VREF = 5.0


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def load_ui(ui_path: Path):
    loader = QUiLoader()
    ui_file = QFile(str(ui_path))
    if not ui_file.open(QFile.ReadOnly):
        raise RuntimeError(f"No pude abrir el .ui: {ui_path}")
    try:
        window = loader.load(ui_file, None)
    finally:
        ui_file.close()

    if window is None:
        raise RuntimeError("QUiLoader no pudo cargar la UI (window=None).")
    return window


def find_child(window: QObject, name: str):
    obj = window.findChild(QObject, name)
    if obj is None:
        raise RuntimeError(f"No encontré el widget '{name}' en el .ui (revisá objectName).")
    return obj


def xor8_of_frame(cmd: int, length: int, payload: bytes) -> int:
    """
    XOR = CMD ^ LEN ^ OP ^ PARAM[...] (SOF not included, XOR not self-included) :contentReference[oaicite:16]{index=16}
    """
    x = cmd ^ length
    for b in payload:
        x ^= b
    return x & 0xFF


def build_frame(cmd: int, op: int, params: bytes = b"") -> bytes:
    """
    Frame = 55 AA CMD LEN OP [PARAM...] XOR
    LEN = 1 + len(params)  (OP always present) :contentReference[oaicite:17]{index=17}
    """
    if not (CMD_MIN <= cmd <= CMD_MAX):
        raise ValueError(f"CMD fuera de rango: {cmd:#02x}")
    length = 1 + len(params)
    if not (LEN_MIN <= length <= LEN_MAX):
        raise ValueError(f"LEN fuera de rango: {length} (params={len(params)})")
    payload = bytes([op]) + params
    x = xor8_of_frame(cmd, length, payload)
    return bytes([SOF1, SOF2, cmd, length]) + payload + bytes([x])


@dataclass
class ParsedFrame:
    cmd: int
    length: int
    op: int
    params: bytes
    xor_ok: bool
    raw: bytes


class SerialReader(QThread):
    frame_received = Signal(object)  # ParsedFrame
    info = Signal(str)
    error = Signal(str)

    def __init__(self, ser: serial.Serial):
        super().__init__()
        self.ser = ser
        self._stop = False
        self._buf = bytearray()

    def stop(self):
        self._stop = True

    def run(self):
        self.info.emit("RX thread iniciado.")
        try:
            while not self._stop:
                try:
                    data = self.ser.read(4096)  # non-blocking (timeout=0)
                except Exception as e:
                    self.error.emit(f"ERROR leyendo serial: {e}")
                    break

                if data:
                    self._buf.extend(data)
                    self._parse_buffer()
                else:
                    self.msleep(2)
        finally:
            self.info.emit("RX thread detenido.")

    def _parse_buffer(self):
        # Stateful parsing by scanning SOF and using LEN, then XOR verify :contentReference[oaicite:18]{index=18}
        while True:
            if len(self._buf) < 2:
                return

            # resync to SOF1
            sof1_idx = self._buf.find(bytes([SOF1]))
            if sof1_idx < 0:
                self._buf.clear()
                return
            if sof1_idx > 0:
                del self._buf[:sof1_idx]

            if len(self._buf) < 2:
                return
            if self._buf[0] != SOF1:
                # should not happen due to find, but keep safe
                del self._buf[0]
                continue

            # require SOF2 immediately
            if self._buf[1] != SOF2:
                # discard SOF1 and resync
                del self._buf[0]
                continue

            # need at least header up to LEN
            if len(self._buf) < 4:
                return

            cmd = self._buf[2]
            length = self._buf[3]

            # validate early
            if not (CMD_MIN <= cmd <= CMD_MAX):
                # bad CMD -> discard SOF1 and resync
                del self._buf[0]
                continue
            if not (LEN_MIN <= length <= LEN_MAX):
                del self._buf[0]
                continue

            total_size = 5 + length  # 2 SOF + CMD + LEN + payload(LEN) + XOR  :contentReference[oaicite:19]{index=19}
            if len(self._buf) < total_size:
                return

            raw = bytes(self._buf[:total_size])
            del self._buf[:total_size]

            payload = raw[4:4 + length]  # OP + PARAM
            rx_xor = raw[-1]
            calc_xor = xor8_of_frame(cmd, length, payload)
            xor_ok = (rx_xor == calc_xor)

            op = payload[0]
            params = payload[1:] if length > 1 else b""

            pf = ParsedFrame(
                cmd=cmd,
                length=length,
                op=op,
                params=params,
                xor_ok=xor_ok,
                raw=raw,
            )
            self.frame_received.emit(pf)


class AppController:
    def __init__(self, window):
        self.window = window
        self.serial_port: Optional[serial.Serial] = None
        self.reader: Optional[SerialReader] = None

        # Widgets (must match your .ui objectName)
        self.port_combo = find_child(window, "portCombo")
        self.refresh_btn = find_child(window, "refreshPortsButton")
        self.connect_btn = find_child(window, "connectButton")
        self.disconnect_btn = find_child(window, "disconnectButton")
        self.log_check = find_child(window, "logCheck")
        self.console = find_child(window, "consoleText")

        self.vi_check = find_child(window, "viEnableCheck")

        self.dac_group = find_child(window, "dacGroup")
        self.dac_volts = find_child(window, "dacVoltsSpin")
        self.dac_counts_label = find_child(window, "dacCountsLabel")
        self.send_dac_btn = find_child(window, "sendDacButton")

        # Internal guards (avoid sending when UI updated from RX)
        self._updating_vi_from_rx = False
        self._updating_dac_from_rx = False

        # Initial UI state
        self.disconnect_btn.setEnabled(False)
        self.vi_check.setEnabled(False)
        self.dac_group.setEnabled(False)

        # Signals
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_serial)
        self.disconnect_btn.clicked.connect(self.disconnect_serial)

        self.vi_check.toggled.connect(self.on_vi_toggled)

        self.dac_volts.valueChanged.connect(self.update_dac_preview)
        self.send_dac_btn.clicked.connect(self.send_dac)

        # Init
        self.refresh_ports()
        self.update_dac_preview()

    def log(self, msg: str):
        self.console.appendPlainText(f"[{ts()}] {msg}")

    # -----------------------------
    # Port handling
    # -----------------------------
    def refresh_ports(self):
        current_data = self.port_combo.currentData()

        self.port_combo.clear()
        ports = list(list_ports.comports())

        if not ports:
            self.port_combo.addItem("(no hay puertos)", None)
            self.port_combo.setEnabled(False)
            self.connect_btn.setEnabled(False)
            self.log("No se detectaron puertos serie.")
            return

        self.port_combo.setEnabled(True)
        self.connect_btn.setEnabled(self.serial_port is None)

        for p in ports:
            label = f"{p.device} — {p.description}"
            self.port_combo.addItem(label, p.device)

        if current_data:
            idx = self.port_combo.findData(current_data)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

        self.log(f"Puertos detectados: {len(ports)}")

    # -----------------------------
    # Serial reader thread
    # -----------------------------
    def start_reader(self):
        if self.serial_port is None or self.reader is not None:
            return
        self.reader = SerialReader(self.serial_port)
        self.reader.info.connect(self.log)
        self.reader.error.connect(self.log)
        self.reader.frame_received.connect(self.on_frame_received)
        self.reader.start()

    def stop_reader(self):
        if self.reader is None:
            return
        self.reader.stop()
        self.reader.wait(1000)
        self.reader = None

    # -----------------------------
    # TX helpers (binary frames)
    # -----------------------------
    def send_frame(self, cmd: int, op: int, params: bytes = b""):
        if self.serial_port is None:
            self.log("TX falló: no conectado.")
            return
        try:
            frame = build_frame(cmd, op, params)
        except Exception as e:
            self.log(f"TX build ERROR: {e}")
            return
        try:
            self.serial_port.write(frame)
        except Exception as e:
            self.log(f"TX ERROR: {e}")
            return

        self.log(f"TX: {frame.hex(' ').upper()} (CMD={cmd:02X} OP={op:02X} LEN={1+len(params)})")

    # -----------------------------
    # Connection lifecycle
    # -----------------------------
    def connect_serial(self):
        if self.serial_port is not None:
            self.log("Ya estoy conectado.")
            return

        port = self.port_combo.currentData()
        if not port:
            self.log("No hay puerto seleccionado.")
            return

        try:
            self.serial_port = serial.Serial(
                port=port,
                baudrate=BAUDRATE,
                timeout=0,
                write_timeout=0,
            )
        except Exception as e:
            self.serial_port = None
            self.log(f"ERROR al conectar con {port}: {e}")
            return

        self.log(f"Conectado a {port} @ {BAUDRATE} bps")

        # UI state
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)

        self.vi_check.setEnabled(True)
        self.dac_group.setEnabled(True)

        # Start RX
        self.start_reader()

        # Handshake sequence (non-blocking):
        # 1) TIME SET hh:mm:ss  (CMD=01, LEN=4, OP=01, hh,mm,ss) :contentReference[oaicite:20]{index=20}
        now = datetime.now()
        hh = now.hour & 0xFF
        mm = now.minute & 0xFF
        ss = now.second & 0xFF
        self.send_frame(CMD_TIME, OP_SET, bytes([hh, mm, ss]))

        # 2) TIME GET
        QTimer.singleShot(50, lambda: self.send_frame(CMD_TIME, OP_GET))

        # 3) VI GET
        QTimer.singleShot(100, lambda: self.send_frame(CMD_VI, OP_GET))

        # 4) DAC GET
        QTimer.singleShot(150, lambda: self.send_frame(CMD_DAC, OP_GET))

    def disconnect_serial(self):
        if self.serial_port is None:
            self.log("No estoy conectado.")
            return

        port = self.serial_port.port

        self.stop_reader()

        try:
            self.serial_port.close()
        except Exception as e:
            self.log(f"ERROR al cerrar puerto: {e}")
        finally:
            self.serial_port = None

        self.log(f"Desconectado de {port}")

        # UI state
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.port_combo.setEnabled(True)
        self.refresh_btn.setEnabled(True)

        self.vi_check.setEnabled(False)
        self.dac_group.setEnabled(False)

    def shutdown(self):
        if self.serial_port is not None:
            self.disconnect_serial()

    # -----------------------------
    # RX handling / decoding
    # -----------------------------
    def on_frame_received(self, pf: ParsedFrame):
        # Always show raw
        ok = "OK" if pf.xor_ok else "BAD_XOR"
        self.log(f"RX: {pf.raw.hex(' ').upper()} ({ok}) CMD={pf.cmd:02X} LEN={pf.length:02X} OP={pf.op:02X}")

        if not pf.xor_ok:
            return

        # Decode known CMDs (only the ones currently implemented)
        if pf.cmd == CMD_VI:
            # Expect response OP = GET_RSP (0x20) and 1 byte param: 0/1 :contentReference[oaicite:21]{index=21}
            if pf.op == (OP_GET + OP_RSP_OFFSET) and len(pf.params) >= 1:
                val = 1 if pf.params[0] else 0
                self.apply_vi_state_from_rx(val)

        elif pf.cmd == CMD_DAC:
            # Expect response OP = GET_RSP and uint16 value (big-endian) in params :contentReference[oaicite:22]{index=22}
            if pf.op == (OP_GET + OP_RSP_OFFSET) and len(pf.params) >= 2:
                counts = (pf.params[0] << 8) | pf.params[1]
                self.apply_dac_from_rx(counts)

        elif pf.cmd == CMD_TIME:
            # Expect response OP = GET_RSP and 3 bytes hh,mm,ss (uint8) :contentReference[oaicite:23]{index=23}
            if pf.op == (OP_GET + OP_RSP_OFFSET) and len(pf.params) >= 3:
                hh, mm, ss = pf.params[0], pf.params[1], pf.params[2]
                self.log(f"TIME(RSP): {hh:02d}:{mm:02d}:{ss:02d}")

    # -----------------------------
    # VI (DC/DC enable)
    # -----------------------------
    def on_vi_toggled(self, checked: bool):
        if self._updating_vi_from_rx:
            return
        if checked:
            # Enable: SET with param=1 (LEN=2)
            self.send_frame(CMD_VI, OP_SET, bytes([0x01]))
        else:
            # Disable: RESET with no params (LEN=1) per example 1a :contentReference[oaicite:24]{index=24}
            self.send_frame(CMD_VI, OP_RESET)

    def apply_vi_state_from_rx(self, value: int):
        self._updating_vi_from_rx = True
        try:
            self.vi_check.setChecked(bool(value))
        finally:
            self._updating_vi_from_rx = False

    # -----------------------------
    # DAC helpers (UI)
    # -----------------------------
    def volts_to_counts(self, volts: float) -> int:
        if volts < 0.0:
            volts = 0.0
        if volts > DAC_VREF:
            volts = DAC_VREF
        counts = int(round((volts / DAC_VREF) * DAC_MAX_COUNTS))
        if counts < 0:
            counts = 0
        if counts > DAC_MAX_COUNTS:
            counts = DAC_MAX_COUNTS
        return counts

    def counts_to_volts(self, counts: int) -> float:
        if counts < 0:
            counts = 0
        if counts > DAC_MAX_COUNTS:
            counts = DAC_MAX_COUNTS
        return (counts / DAC_MAX_COUNTS) * DAC_VREF

    def update_dac_preview(self):
        counts = self.volts_to_counts(float(self.dac_volts.value()))
        self.dac_counts_label.setText(f"Counts: {counts}")

    def send_dac(self):
        counts = self.volts_to_counts(float(self.dac_volts.value()))
        # uint16 big-endian: MSB first, then LSB :contentReference[oaicite:25]{index=25}
        msb = (counts >> 8) & 0xFF
        lsb = counts & 0xFF
        self.send_frame(CMD_DAC, OP_SET, bytes([msb, lsb]))

    def apply_dac_from_rx(self, counts: int):
        # Update UI without re-sending
        self._updating_dac_from_rx = True
        try:
            volts = self.counts_to_volts(counts)
            self.dac_volts.setValue(volts)
            self.dac_counts_label.setText(f"Counts: {counts}")
        finally:
            self._updating_dac_from_rx = False


def main():
    app = QApplication(sys.argv)

    ui_path = Path(__file__).resolve().parent / "main_window.ui"
    window = load_ui(ui_path)
    window.setWindowTitle("RFQ_test")

    controller = AppController(window)
    app.aboutToQuit.connect(controller.shutdown)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
