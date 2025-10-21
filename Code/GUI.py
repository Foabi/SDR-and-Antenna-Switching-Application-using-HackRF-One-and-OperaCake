from PyQt5 import QtWidgets, QtGui, QtCore
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtCore import QUrl, QThread, pyqtSignal
import sys, subprocess, os, time, sip
from functools import partial
from pathlib import Path

# Set up the application's asset directory
APP_ROOT  = Path(__file__).resolve().parent
ASSETS_DIR = APP_ROOT / "assets"
def asset(*parts) -> Path:
    return ASSETS_DIR.joinpath(*parts)

# Import custom widgets for the application
from live_spectogram import LiveSpectrogramWindow
from LED import OperaCakePanel

# Check if the WebEngine module is available for PDF viewing
try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings, QWebEnginePage
    _PDF_WEBENGINE = True
except Exception:
    _PDF_WEBENGINE = False

class PortSwitchWorker(QThread):
    # Worker thread for performing port-switching and latency tests in the background.
    log = pyqtSignal(str)
    finished = pyqtSignal(float, float, float)  # max, min, avg (ms)
    error = pyqtSignal(str)

    def __init__(self, num_cycles=50, dwell_s=1.5, ports=("A4", "B4"), parent=None):
        super().__init__(parent)
        self.num_cycles = int(num_cycles)
        self.dwell_s = float(dwell_s)
        self.ports = tuple(ports)
        self._stop = False

    def stop(self):
        self._stop = True

    def _switch_once(self, port: str) -> float:
        # Executes the external hackrf_operacake command to switch ports.
        start = time.time()
        self.log.emit(f"[{time.strftime('%H:%M:%S')}] >>> Switching to {port}")
        try:
            p = subprocess.run(["hackrf_operacake", "-o", "0", "-a", port],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if p.returncode != 0:
                self.error.emit(p.stderr.decode(errors="ignore").strip() or "hackrf_operacake failed")
        except Exception as e:
            self.error.emit(str(e))
        end = time.time()
        self.log.emit(f"[{time.strftime('%H:%M:%S')}] <<< Switch to {port} complete")
        return (end - start) * 1000.0

    def run(self):
        # The main loop for the latency test.
        lat = []
        try:
            for _ in range(self.num_cycles):
                if self._stop: break
                ms1 = self._switch_once(self.ports[0]); lat.append(ms1)
                if self._stop: break
                time.sleep(self.dwell_s)
                ms2 = self._switch_once(self.ports[1]); lat.append(ms2)
                if self._stop: break
                time.sleep(self.dwell_s)
        except Exception as e:
            self.error.emit(str(e))
        if lat:
            self.finished.emit(max(lat), min(lat), sum(lat)/len(lat))
        else:
            self.finished.emit(float("nan"), float("nan"), float("nan"))


class DelayToolsWindow(QtWidgets.QDialog):
    # Dialog for running port-switching latency tests.
    def __init__(self, parent_main: "MainControlWindow"):
        super().__init__(parent_main)
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowModality(QtCore.Qt.NonModal)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("Delay Tools")
        self.setMinimumSize(560, 420)
        self.parent_main = parent_main
        self.worker = None

        self.setStyleSheet("""
            QWidget { background-color: #0e0e0e; color: #c8facc; font-family: 'Consolas','Segoe UI',monospace; }
            QPushButton {
                background-color: #1a1f1a; border: 2px solid #2eff7d; padding: 10px; border-radius: 10px;
                color: #2eff7d; font-weight: bold;
            }
            QPushButton:hover { background-color: #2c2f2c; border: 2px solid #3fff90; color: #3fff90; }
            QPushButton:pressed { background-color: #144d39; border: 2px solid #32ff99; color: #ffffff; }
            QLineEdit { background:#121212; border:1px solid #2eff7d; border-radius:8px; padding:6px; color:#c8facc; }
            QLabel#titleLabel { font-size:18px; font-weight:bold; color:#2eff7d; padding:4px 0; }
            QTextEdit { background:#0a0a0a; border:1px solid #2eff7d; border-radius:8px; color:#c8facc; }
        """)

        layout = QtWidgets.QVBoxLayout(self)

        ctrl = QtWidgets.QHBoxLayout()
        self.cycles_edit = QtWidgets.QLineEdit("50"); self.cycles_edit.setFixedWidth(80)
        self.dwell_edit  = QtWidgets.QLineEdit("1.5"); self.dwell_edit.setFixedWidth(80)
        ctrl.addWidget(QtWidgets.QLabel("Cycles:"))
        ctrl.addWidget(self.cycles_edit)
        ctrl.addSpacing(12)
        ctrl.addWidget(QtWidgets.QLabel("Dwell (s):"))
        ctrl.addWidget(self.dwell_edit)
        ctrl.addStretch(1)
        layout.addLayout(ctrl)

        btns = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("Run Port-Switch Latency Test (A4 ↔ B4)")
        self.btn_stop = QtWidgets.QPushButton("Stop Test")
        self.btn_obs  = QtWidgets.QPushButton("Open Delay Observe")
        self.btn_stop.setEnabled(False)
        btns.addWidget(self.btn_run)
        btns.addWidget(self.btn_stop)
        btns.addStretch(1)
        btns.addWidget(self.btn_obs)
        layout.addLayout(btns)

        self.log_box = QtWidgets.QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(220)
        layout.addWidget(self.log_box)

        self.btn_run.clicked.connect(self.on_run_clicked)
        self.btn_stop.clicked.connect(self.on_stop_clicked)
        self.btn_obs.clicked.connect(self.on_open_observe)

    def append_log(self, text: str):
        # Appends text to the log box and scrolls to the end.
        self.log_box.append(text)
        self.log_box.moveCursor(QtGui.QTextCursor.End)

    def _has_operacake_cli(self) -> bool:
        # Checks if the hackrf_operacake command-line tool is available.
        try:
            subprocess.run(["hackrf_operacake", "-h"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except Exception:
            return False

    def on_run_clicked(self):
        # Starts the port-switching latency test.
        if getattr(self, "worker", None) and self.worker.isRunning():
            return
        try:
            cycles = int(self.cycles_edit.text())
            dwell  = float(self.dwell_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Invalid inputs", "Cycles must be int, dwell must be float.")
            return

        if not self._has_operacake_cli():
            QtWidgets.QMessageBox.critical(
                self, "hackrf_operacake not found",
                "The 'hackrf_operacake' tool was not found in PATH.\n\n"
                "Install HackRF tools or add them to PATH, then try again."
            )
            return

        if not self.parent_main.is_hackrf_connected():
            QtWidgets.QMessageBox.information(
                self, "HackRF not connected",
                "No HackRF board detected. Connect a board to run the latency test."
            )
            return

        self.log_box.clear()
        self.append_log(f"Starting latency test: {cycles} cycles, dwell {dwell:.2f}s, ports A4<->B4")

        self.worker = PortSwitchWorker(num_cycles=cycles, dwell_s=dwell, ports=("A4", "B4"))
        self.worker.log.connect(self.append_log)
        self.worker.error.connect(lambda e: self.append_log(f"[ERROR] {e}"))

        def on_done(mx, mn, avg):
            if any((v != v) for v in (mx, mn, avg)):  # NaN guard
                self.append_log("\n--- No samples captured. ---")
            else:
                self.append_log("\n--- Latency statistics ---")
                self.append_log(f"Max: {mx:.2f} ms")
                self.append_log(f"Min: {mn:.2f} ms")
                self.append_log(f"Avg: {avg:.2f} ms")
            self.btn_run.setEnabled(True)
            self.btn_stop.setEnabled(False)

        self.worker.finished.connect(on_done)
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.worker.start()

    def on_stop_clicked(self):
        # Stops the running latency test.
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.append_log("Stopping… (waiting for the current step to finish)")
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def on_open_observe(self):
        # Launches the "Delay Observe" mode in the main application.
        if self.parent_main.active_window is None:
            self.parent_main.launch_mode("delay")
            self.close()
        else:
            QtWidgets.QMessageBox.information(
                self, "Already running",
                "A mode window is already open. Close it first to open Delay Observe."
            )

    def closeEvent(self, e):
        # Ensures the worker thread is stopped when the dialog is closed.
        try:
            if getattr(self, "worker", None) and self.worker.isRunning():
                self.worker.stop()
                self.worker.wait(1500)
        except Exception:
            pass
        e.accept()

class PdfViewerWindow(QtWidgets.QMainWindow):
    # A window for viewing PDF files using Qt WebEngine.
    def __init__(self, pdf_path: Path, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        self.setWindowTitle(f"Documentation – {pdf_path.name}")
        self.resize(1000, 750)
        self._pdf_path = pdf_path
        self._last_query = ""

        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)

        s = self.view.settings()
        def _en(attr):
            key = getattr(QWebEngineSettings, attr, None)
            if key is not None:
                s.setAttribute(key, True)
        for attr in ("PluginsEnabled", "PdfViewerEnabled", "JavascriptEnabled",
                     "LocalContentCanAccessFileUrls", "LocalStorageEnabled"):
            _en(attr)

        # Minimal FIND bar (hidden by default)
        self.find_bar = QtWidgets.QFrame(self)
        self.find_bar.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.find_bar.setStyleSheet("""
            QFrame { background: #0e0e0e; border: 1px solid #2eff7d; border-radius: 8px; }
            QLineEdit { background:#121212; border:1px solid #2eff7d; border-radius:6px; padding:4px; color:#c8facc; }
            QPushButton { background:#1a1f1a; border:1px solid #2eff7d; border-radius:6px; padding:4px 8px; color:#2eff7d; }
            QPushButton:hover { border-color:#3fff90; color:#3fff90; }
            QCheckBox { color:#c8facc; }
        """)
        hb = QtWidgets.QHBoxLayout(self.find_bar); hb.setContentsMargins(8,6,8,6); hb.setSpacing(6)
        self.find_edit = QtWidgets.QLineEdit(self.find_bar); self.find_edit.setPlaceholderText("Find…")
        self.case_cb   = QtWidgets.QCheckBox("Aa", self.find_bar)  # case sensitive toggle (disabled for PDF)
        btn_prev = QtWidgets.QPushButton("◀", self.find_bar)
        btn_next = QtWidgets.QPushButton("▶", self.find_bar)
        btn_close = QtWidgets.QPushButton("✕", self.find_bar); btn_close.setFixedWidth(28)
        hb.addWidget(self.find_edit, 1); hb.addWidget(self.case_cb); hb.addWidget(btn_prev); hb.addWidget(btn_next); hb.addWidget(btn_close)

        self._place_find_bar()
        self.find_bar.hide()
        self._orig_resize_event = self.resizeEvent
        self.resizeEvent = self._wrap_resize(self._orig_resize_event)

        QtWidgets.QShortcut(QtGui.QKeySequence.Find, self, activated=self._toggle_find)
        QtWidgets.QShortcut(QtGui.QKeySequence.FindNext, self, activated=lambda: self._do_find(True))
        QtWidgets.QShortcut(QtGui.QKeySequence.FindPrevious, self, activated=lambda: self._do_find(False))
        QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self, activated=self._hide_find)

        btn_next.clicked.connect(lambda: self._do_find(True))
        btn_prev.clicked.connect(lambda: self._do_find(False))
        btn_close.clicked.connect(self._hide_find)
        self.find_edit.returnPressed.connect(lambda: self._do_find(True))

        self.view.loadFinished.connect(self._on_loaded)
        self.view.setUrl(QtCore.QUrl.fromLocalFile(str(pdf_path)))

    def _on_loaded(self, ok: bool):
        # Sets properties and provides a warning if the PDF fails to load.
        self.case_cb.setEnabled(False)
        self.case_cb.setToolTip("PDF search in this viewer is case-insensitive (Qt WebEngine limitation).")
        if not ok:
            QtWidgets.QMessageBox.warning(self, "PDF Viewer", "Failed to render PDF in WebEngine.")

    def _place_find_bar(self):
        # Positions the find bar at the top-right of the window.
        m = 12; w = 420; h = 40
        self.find_bar.setGeometry(self.width() - w - m, m, w, h)
        self.find_bar.raise_()

    def _wrap_resize(self, old_resize):
        # Wraps the resize event to reposition the find bar.
        def handler(ev):
            old_resize(ev)
            self._place_find_bar()
        return handler

    def _toggle_find(self):
        # Shows or hides the find bar.
        if self.find_bar.isVisible():
            self._hide_find()
        else:
            self.find_bar.show()
            self.find_edit.setFocus()
            self.find_edit.selectAll()

    def _hide_find(self):
        # Hides the find bar and returns focus to the main view
        self.find_bar.hide()
        self.view.setFocus()

    def _do_find(self, forward: bool):
        # Performs the text search within the loaded PDF.
        query = self.find_edit.text().strip()
        if not query:
            return
        flags = QWebEnginePage.FindFlags()  
        if (not forward) and hasattr(QWebEnginePage, "FindBackward"):
            flags |= QWebEnginePage.FindBackward
        if hasattr(QWebEnginePage, "FindWrapsAroundDocument"):
            flags |= QWebEnginePage.FindWrapsAroundDocument
        self.view.findText(query, flags)

class MainControlWindow(QtWidgets.QWidget):
    # The main application window for controlling the HackRF OperaCake.
    def __init__(self):
        super().__init__()

        self.setWindowTitle("HackRF OperaCake Unified Controller")
        self.setGeometry(300, 200, 800, 600)

        self.current_port = "NONE"
        self.setStyleSheet("""
            QWidget {
                background-color: #0e0e0e;
                color: #c8facc;
                font-family: 'Consolas', 'Segoe UI', monospace;
                font-size: 14px;
            }
            QPushButton {
                background-color: #1a1f1a;
                border: 2px solid #2eff7d;
                padding: 12px;
                border-radius: 10px;
                color: #2eff7d;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2c2f2c;
                border: 2px solid #3fff90;
                color: #3fff90;
            }
            QPushButton:pressed {
                background-color: #144d39;
                border: 2px solid #32ff99;
                color: #ffffff;
            }
            QLabel#titleLabel {
                font-size: 22px;
                font-weight: bold;
                color: #2eff7d;
                padding: 20px;
            }
            QLabel#statusIdle {
                font-size: 13px;
                color: #2eff7d;
                font-weight: bold;
                padding-top: 10px;
            }
            QLabel#statusRunning {
                font-size: 13px;
                color: #ff4d4d;
                font-weight: bold;
                padding-top: 10px;
            }
        """)

        self.layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("⚡ HackRF OperaCake Controller")
        title.setObjectName("titleLabel")
        title.setAlignment(QtCore.Qt.AlignCenter)
        self.layout.addWidget(title)

        self.live_spec_btn = QtWidgets.QPushButton("Manual Switching Mode")
        self.live_spec_btn.clicked.connect(lambda: self.launch_mode("manual"))
        self.layout.addWidget(self.live_spec_btn)

        self.freq_mode_btn = QtWidgets.QPushButton("Frequency Switching Mode")
        self.freq_mode_btn.clicked.connect(lambda: self.launch_mode("frequency"))
        self.layout.addWidget(self.freq_mode_btn)

        self.time_mode_btn = QtWidgets.QPushButton("Time Switching Mode")
        self.time_mode_btn.clicked.connect(lambda: self.launch_mode("time"))
        self.layout.addWidget(self.time_mode_btn)

        self.delay_mode_btn = QtWidgets.QPushButton("Delay Mode")
        self.delay_mode_btn.clicked.connect(self.open_delay_tools)
        self.layout.addWidget(self.delay_mode_btn)

        self.wide_sweep_btn = QtWidgets.QPushButton("Wide Spectrum Mode")
        self.wide_sweep_btn.clicked.connect(lambda: self.launch_mode("wide spectrum"))
        self.layout.addWidget(self.wide_sweep_btn)

        self.btn_port_sweep = QtWidgets.QPushButton("Wide Spectrum Frequency Mode")
        self.btn_port_sweep.clicked.connect(lambda: self.launch_mode("wide spectrum frequency"))
        self.layout.addWidget(self.btn_port_sweep)

        self.jamming_mode_btn = QtWidgets.QPushButton("Event Detection and Switching Mode")
        self.jamming_mode_btn.clicked.connect(lambda: self.launch_mode("jamming"))
        self.layout.addWidget(self.jamming_mode_btn)

        self.docs_btn = QtWidgets.QPushButton("Documentation")
        self.docs_btn.clicked.connect(self.open_documentation_pdfs)
        self.layout.addWidget(self.docs_btn)

        self.status_label = QtWidgets.QLabel("Status: Idle")
        self.status_label.setObjectName("statusIdle")
        self.status_label.setAlignment(QtCore.Qt.AlignCenter)
        self.layout.addWidget(self.status_label)

        img_path = str(asset("operacake.jpeg"))
        self.oc_panel = OperaCakePanel(img_path, self, label_color="white")
        self.oc_panel.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.oc_panel.setMinimumHeight(220)
        self.layout.addWidget(self.oc_panel)
        self.oc_panel.set_active(self.current_port, fixed_input="A0", connected=self.is_hackrf_connected())

        self.layout.addItem(QtWidgets.QSpacerItem(0, 0, QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Expanding))

        self.hackrf_status_label = QtWidgets.QLabel()
        self.hackrf_status_label.setAlignment(QtCore.Qt.AlignLeft)
        self.layout.addWidget(self.hackrf_status_label)

        self.active_window = None
        self.delay_tools_win = None
        self._open_docs_refs = []  

        self.refresh_hackrf_status()

        self.hackrf_timer = QtCore.QTimer(self)
        self.hackrf_timer.timeout.connect(self.refresh_hackrf_status)
        self.hackrf_timer.start(3000)

    def open_delay_tools(self):
        # Checks for a HackRF connection and opens the DelayToolsWindow.
        if not self.is_hackrf_connected():
            QtWidgets.QMessageBox.warning(
                self,
                "HackRF Not Connected",
                "⚠️ No HackRF board detected.\n\nPlease connect the HackRF board before starting any mode."
            )
            return

        w = getattr(self, "delay_tools_win", None)

        def _needs_new(win):
            if win is None:
                return True
            try:
                return sip.isdeleted(win) or not isinstance(win, DelayToolsWindow)
            except Exception:
                return True

        if _needs_new(w) or not w.isVisible():
            self.delay_tools_win = DelayToolsWindow(self)
            self.delay_tools_win.destroyed.connect(lambda *_: setattr(self, "delay_tools_win", None))
            self.delay_tools_win.show()
            self.delay_tools_win.raise_()
            self.delay_tools_win.activateWindow()
        else:
            w.raise_()
            w.activateWindow()

    def _open_with_system(self, path: Path) -> bool:
        # Tries to open a file using the system's default application.
        url = QUrl.fromLocalFile(str(path))
        if QDesktopServices.openUrl(url):
            return True
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
            return True
        except Exception:
            return False

    def open_documentation_pdfs(self):
        # Opens the documentation PDF in either the embedded viewer or the system viewer.
        hackrf_pdf = asset("hackrf.pdf")
        if not hackrf_pdf.exists():
            QtWidgets.QMessageBox.warning(
                self, "Documentation Missing",
                "The following PDF was not found in the 'assets' folder:\n\n"
                f"• {hackrf_pdf.name}\n\nPlace it here:\n{ASSETS_DIR}"
            )
            return

        if _PDF_WEBENGINE:
            win = PdfViewerWindow(hackrf_pdf, self)
            win.destroyed.connect(partial(self._discard_doc_ref, win))
            self._open_docs_refs.append(win)
            win.show()
            win.raise_()
            win.activateWindow()
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Embedded PDF Viewer Unavailable",
                "PyQt WebEngine is not installed, so the PDF will open with the system viewer.\n\n"
                "To enable the in-app viewer on Ubuntu:\n"
                "  sudo apt install python3-pyqt5.qtwebengine"
            )
            if not self._open_with_system(hackrf_pdf):
                QtWidgets.QMessageBox.information(
                    self, "Open PDF",
                    "Tried to open the PDF via the default viewer.\n"
                    f"If nothing appeared, open it manually from:\n{ASSETS_DIR}"
                )

    def _discard_doc_ref(self, win, *args):
        # Removes the reference to a closed PDF viewer window.
        try:
            self._open_docs_refs.remove(win)
        except ValueError:
            pass

    def refresh_hackrf_status(self):
        # Updates the HackRF connection status label and the OperaCake panel.
        connected = self.is_hackrf_connected()

        if connected:
            self.hackrf_status_label.setText("HackRF Board Status: ✅ Connected")
            self.hackrf_status_label.setStyleSheet("color: green; font-weight: bold")
        else:
            self.hackrf_status_label.setText("HackRF Board Status: ❌ Disconnected")
            self.hackrf_status_label.setStyleSheet("color: red; font-weight: bold")

        for btn in (
            self.live_spec_btn,
            self.freq_mode_btn,
            self.time_mode_btn,
            self.delay_mode_btn,
            self.wide_sweep_btn,
            self.jamming_mode_btn,
        ):
            btn.setToolTip("" if connected else "HackRF not connected — this action is blocked.")
            btn.style().unpolish(btn); btn.style().polish(btn)

        if hasattr(self, "oc_panel"):
            shown_port = self.current_port if connected else "NONE"
            self.oc_panel.set_active(shown_port, fixed_input="A0", connected=connected)

    def launch_mode(self, mode):
        # Launches the selected operating mode in a new window.
        if not self.is_hackrf_connected():
            QtWidgets.QMessageBox.warning(
                self,
                "HackRF Not Connected",
                "⚠️ No HackRF board detected.\n\nPlease connect the HackRF board before starting any mode."
            )
            return

        if self.active_window is not None:
            return

        if mode == "jamming":
            display_name = "Event Detection & Switching"
        else:
            display_name = mode.capitalize()

        self.status_label.setText(f"Status: Running {display_name} Mode")
        self.set_status_color("running")

        self.active_window = LiveSpectrogramWindow(mode=mode)

        try:
            self.active_window.closed.connect(self.on_window_closed)
        except Exception:
            self.active_window.destroyed.connect(lambda *_: self.on_window_closed())

        try:
            def on_ant_change(p: str):
                self.current_port = p
                self.oc_panel.set_active(p, fixed_input="A0", connected=self.is_hackrf_connected())
            self.active_window.antenna_changed.connect(on_ant_change)
            self.current_port = getattr(self.active_window, "antenna_select", self.current_port)
            self.oc_panel.set_active(self.current_port, fixed_input="A0", connected=self.is_hackrf_connected())
        except Exception:
            pass

        self.active_window.show()

    def closeEvent(self, event):
        # Ensures all child windows are closed when the main window is closed.
        try:
            if getattr(self, 'active_window', None) is not None:
                self.active_window.close()
        except Exception as e:
            print(f"[ERROR] Failed to close subwindow: {e}")
        event.accept()

    def on_window_closed(self):
        # Resets the status of the main window when a child window is closed.
        self.status_label.setText("Status: Idle")
        self.set_status_color("idle")
        self.active_window = None
        self.oc_panel.set_active(self.current_port, fixed_input="A0", connected=self.is_hackrf_connected())
        self.refresh_hackrf_status()

    def set_status_color(self, state):
        # Updates the color of the status label.
        self.status_label.setObjectName("statusIdle" if state == "idle" else "statusRunning")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def is_hackrf_connected(self):
        # Checks for the presence of a HackRF board by running the hackrf_info command.
        try:
            result = subprocess.run(["hackrf_info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return b"Found HackRF" in result.stdout
        except Exception:
            return False

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    main_win = MainControlWindow()
    main_win.show()
    sys.exit(app.exec_())
