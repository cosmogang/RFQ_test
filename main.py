import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QFile
from PySide6.QtUiTools import QUiLoader


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


def main():
    app = QApplication(sys.argv)

    ui_path = Path(__file__).resolve().parent / "main_window.ui"
    window = load_ui(ui_path)
    window.setWindowTitle("RFQ_test")
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
