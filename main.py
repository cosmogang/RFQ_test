import sys
from pathlib import Path
from datetime import datetime

import serial
from serial.tools import list_ports

from PySide6.QtCore import QFile, QObject
from PySide6.QtWidgets import QApplication
from PySide6.QtUiTools import QUiLoader


BAUDRATE = 460_800


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


def find_child(window: QObject, name: str, type_hint=None):
    obj = window.findChild(QObject, name)
    if obj is None:
        raise RuntimeError(f"No encontré el widget '{name}' en el .ui (revisá objectName).")
    if type_hint is not None and not isinstance(obj, type_hint):
        # No forzamos import de clases QtWidgets acá; solo avisamos
        pass
    return obj


class AppController:
    def __init__(self, window):
        self.window = window
        self.serial_port: serial.Serial | None = None

        # Widgets por objectName
        self.port_combo = find_child(window, "portCombo")
        self.refresh_btn = find_child(window, "refreshPortsButton")
        self.connect_btn = find_child(window, "connectButton")
        self.disconnect_btn = find_child(window, "disconnectButton")
        self.log_check = find_child(window, "logCheck")
        self.console = find_child(window, "consoleText")

        # Estado inicial
        self.disconnect_btn.setEnabled(False)

        # Conexiones de señales
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.connect_btn.clicked.connect(self.connect_serial)
        self.disconnect_btn.clicked.connect(self.disconnect_serial)

        # Primera carga de puertos
        self.refresh_ports()

    def log(self, msg: str):
        # QPlainTextEdit
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
            # p.device: "/dev/ttyUSB0" o "COM3"
            # p.description: texto descriptivo
            # p.hwid: id hw
            label = f"{p.device} — {p.description}"
            self.port_combo.addItem(label, p.device)

        # Intentar restaurar selección previa
        if current_data:
            idx = self.port_combo.findData(current_data)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

        self.log(f"Puertos detectados: {len(ports)}")

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
                timeout=0,           # no bloqueante (por ahora)
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

    def disconnect_serial(self):
        if self.serial_port is None:
            self.log("No estoy conectado.")
            return

        port = self.serial_port.port
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
        # Para cerrar limpio al salir
        if self.serial_port is not None:
            self.disconnect_serial()


def main():
    app = QApplication(sys.argv)

    ui_path = Path(__file__).resolve().parent / "main_window.ui"
    window = load_ui(ui_path)
    window.setWindowTitle("RFQ_test")

    controller = AppController(window)

    # Cierre limpio
    def on_about_to_quit():
        controller.shutdown()

    app.aboutToQuit.connect(on_about_to_quit)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
