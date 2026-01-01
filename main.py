import sys
from pathlib import Path
from datetime import datetime
import time as _time

import serial
from serial.tools import list_ports

from PySide6.QtCore import QFile, QObject, QThread, Signal
from PySide6.QtWidgets import QApplication
from PySide6.QtUiTools import QUiLoader


BAUDRATE = 460_800
FRAME_DELIM = b"\r"


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


class SerialReader(QThread):
    frame_received = Signal(bytes)
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
                    data = self.ser.read(4096)  # timeout=0 => no bloquea
                except Exception as e:
                    self.error.emit(f"ERROR leyendo serial: {e}")
                    break

                if data:
                    self._buf.extend(data)
                    while True:
                        idx = self._buf.find(FRAME_DELIM)
                        if idx < 0:
                            break
                        frame = bytes(self._buf[:idx])  # sin el \r
                        del self._buf[:idx + 1]
                        self.frame_received.emit(frame)
                else:
                    # dormir un poquito para no quemar CPU
                    self.msleep(2)
        finally:
            self.info.emit("RX thread detenido.")


class AppController:
    def __init__(self, window):
        self.window = window
        self.serial_port: serial.Serial | None = None
        self.reader: SerialReader | None = None

        # Widgets
        self.port_combo = find_child(window, "portCombo")
        self.refresh_btn = find_child(window, "refreshPortsButton")
        self.connect_btn = find_child(window, "connectButton")
        self.disconnect_btn = find_child(window, "disconnectButton")
        self.log_check = find_child(window, "logCheck")
        self.console = find_child(window, "consoleText")

        # Estado inicial
        self.disconnect_btn.setEnabled(False)

        # Signals
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_serial)
        self.disconnect_btn.clicked.connect(self.disconnect_serial)

        self.refresh_ports()

    def log(self, msg: str):
        self.console.appendPlainText(f"[{ts()}] {msg}")

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

    def start_reader(self):
        if self.serial_port is None:
            return
        if self.reader is not None:
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

    def on_frame_received(self, frame: bytes):
        # Por ahora asumimos ASCII/texto para estos comandos.
        # (Más adelante, para binario, esto se trata distinto.)
        try:
            text = frame.decode("ascii", errors="replace")
        except Exception:
            text = str(frame)
        self.log(f"RX: {text}")

    def send_line(self, line: str):
        if self.serial_port is None:
            self.log("TX falló: no conectado.")
            return

        payload = (line + "\r").encode("ascii", errors="strict")
        try:
            self.serial_port.write(payload)
        except Exception as e:
            self.log(f"ERROR TX: {e}")
            return

        self.log(f"TX: {line}")

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

        # Arrancar RX
        self.start_reader()

        # Handshake: set time y pedir time
        now = datetime.now()  # hora local del PC
        hhmmss = now.strftime("%H:%M:%S")
        self.send_line(f"time={hhmmss}")
        # pequeñísimo delay para no pegar comandos en el mismo burst si el AVR es sensible
        _time.sleep(0.02)
        self.send_line("time")

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

    def shutdown(self):
        if self.serial_port is not None:
            self.disconnect_serial()


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
