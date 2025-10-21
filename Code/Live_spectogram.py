#  Imports
from PyQt5 import Qt
from PyQt5.QtCore import pyqtSignal, QTimer, QEvent
from PyQt5.QtGui import QDoubleValidator, QIntValidator
from gnuradio import gr, qtgui, blocks
from gnuradio.fft import fft_vcc, window
import osmosdr, sip, numpy as np, pyqtgraph as pg, re, time, subprocess
from pyqtgraph import PlotWidget, mkPen
from time import monotonic
from LED import OperaCakePanel
from pathlib import Path

# Asset paths
APP_ROOT  = Path(__file__).resolve().parent
ASSETS_DIR = APP_ROOT / "assets"
def asset(*parts) -> Path:
    return ASSETS_DIR.joinpath(*parts)

# Regex helpers
_FREQ_REGEX  = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?)\s*(hz|khz|mhz|ghz)?\s*$", re.I)
_FLOAT_REGEX = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?\s*$")
_INT_REGEX   = re.compile(r"^\s*\d+\s*$")

# Input parsers
def parse_freq_input(text: str) -> float:
    if text is None:
        raise ValueError("Empty frequency")
    m = _FREQ_REGEX.match(text)
    if not m:
        raise ValueError(f"Invalid frequency format: '{text}'")
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    mult = {"":1.0, "hz":1.0, "khz":1e3, "mhz":1e6, "ghz":1e9}[unit]
    return val * mult

def parse_float_strict(text: str, vmin=None, vmax=None, name="value") -> float:
    """Strict float (allows sci notation). No extra chars allowed."""
    if text is None or not _FLOAT_REGEX.match(text):
        raise ValueError(f"Invalid {name}: '{text}'")
    val = float(text)
    if vmin is not None and val < vmin:
        raise ValueError(f"{name} must be ≥ {vmin}")
    if vmax is not None and val > vmax:
        raise ValueError(f"{name} must be ≤ {vmax}")
    return val

def parse_int_strict(text: str, vmin=None, vmax=None, name="value") -> int:
    """Strict int. No extra chars."""
    if text is None or not _INT_REGEX.match(text):
        raise ValueError(f"Invalid {name}: '{text}'")
    val = int(text)
    if vmin is not None and val < vmin:
        raise ValueError(f"{name} must be ≥ {vmin}")
    if vmax is not None and val > vmax:
        raise ValueError(f"{name} must be ≤ {vmax}")
    return val

# Disable mouse interactions when needed
class _MouseBlocker(Qt.QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner

    def eventFilter(self, obj, ev):
        if not getattr(self._owner, "_interactions_enabled", False):
            if ev.type() in (QEvent.Wheel, QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseMove):
                return True
        return False
    
# DSP utilities
def process_fft_data(mags):
    mags = np.copy(mags)
    n = len(mags)
    center = n // 2

    # Suppress DC and surrounding bins
    if center >= 3:
        mags[center-2:center+3] = np.mean([
            mags[center-4], mags[center-3],
            mags[center+3], mags[center+4]
        ])

    # Taper edges to avoid chunk artifacts
    fade = 10
    mags[:fade] *= np.linspace(0.3, 1.0, fade)
    mags[-fade:] *= np.linspace(1.0, 0.3, fade)
    return mags

def spectral_flatness_db(power_lin, eps=1e-12):
    x = np.clip(power_lin, eps, None)
    gmean = np.exp(np.mean(np.log(x)))
    amean = np.mean(x)
    return 10.0 * np.log10((gmean / (amean + eps)) + eps)

def _find_peaks(db, n=5, min_sep_bins=8):
    peaks = []
    used = np.zeros_like(db, dtype=bool)
    for _ in range(n):
        masked = np.where(~used, db, -1e9)
        i = int(np.argmax(masked))
        if masked[i] <= -1e8:
            break
        peaks.append(i)
        used[max(0, i - min_sep_bins): i + min_sep_bins + 1] = True
    return sorted(peaks)

def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v

# Main application window
class LiveSpectrogramWindow(gr.top_block, Qt.QWidget):
    closed = pyqtSignal()
    antenna_changed = pyqtSignal(str)

    def __init__(self, mode):
        gr.top_block.__init__(self, "Live Spectrogram + Frequency Spectrum", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        if mode == "jamming":
            self.setWindowTitle("Live Mode: Event Detection & Switching")
        else:
            self.setWindowTitle(f"Live Mode: {mode.capitalize()} Switching")
        qtgui.util.check_set_qss()

        # Default SDR config
        self.mode = mode
        self.center_freq = 100e6
        self.samp_rate = 20e6
        self.fft_size = 1024
        self.antenna_select = 'A4'
        self.rf_gain = 14
        self.if_gain = 20
        self.bb_gain = 20
        self.last_time_index = 0  # For time switching resume

        # Internal state
        self.sweep_ptr = 0
        self.total_steps = 0
        self.freq_axis = np.array([])
        self.sweep_buffer = np.array([])
        self.sweep_active = False
        self.sweep_busy = False

        self.port_tags = {
            "A4": "FM",
            "A3": "GSM",
            "A2": "3G FDD",
            "A1": "4G B20 FDD",
            "B4": "4G B3 FDD",
            "B3": "5G n38 TDD",
            "B2": "5G n78 TDD",
            "B1": "Free port",
        }

        # Layout and UI
        self.top_layout = Qt.QGridLayout(self)

        # UI BUILD ZONE
        form = Qt.QHBoxLayout()
        self.toolbar = form

        # jamming banner
        if mode == "jamming":
            self.jam_alert_label = Qt.QLabel("⚠️ Event Detected — Switching Antenna")
            self.jam_alert_label.setAlignment(Qt.Qt.AlignCenter)
            self.jam_alert_label.setStyleSheet("""
                QLabel {
                    background-color: #ff0000;
                    color: white;
                    font-weight: bold;
                    font-size: 20px;
                    border-radius: 10px;
                    padding: 10px;
                }
            """)
            self.jam_alert_label.hide()
            self.top_layout.addWidget(self.jam_alert_label, 4, 0)

        if self.mode not in ("wide spectrum", "wide spectrum frequency"):
            self.freq_edit = Qt.QLineEdit("100e6")
            form.addWidget(Qt.QLabel("Central Freq:"))
            form.addWidget(self.freq_edit)
            self.freq_edit.returnPressed.connect(self.update_center_freq)

            self.freq_range_label = Qt.QLabel("")
            self.freq_range_label.setStyleSheet("color: red; font-weight:bold")
            self.freq_range_label.hide()
            form.addWidget(self.freq_range_label)

        # Sample rate
        self.samp_rate_edit = Qt.QLineEdit("20e6")
        form.addWidget(Qt.QLabel("Sample Rate:"))
        form.addWidget(self.samp_rate_edit)
        self.samp_rate_edit.returnPressed.connect(self.update_sample_rate)

        self.samp_rate_range_label = Qt.QLabel("")
        self.samp_rate_range_label.setStyleSheet("color: red; font-weight: bold")
        self.samp_rate_range_label.hide()
        form.addWidget(self.samp_rate_range_label)

        # FFT size
        self.fft_combo = Qt.QComboBox()
        self.fft_combo.addItems([str(v) for v in [32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384]])
        self.fft_combo.setCurrentText(str(self.fft_size))
        self.fft_combo.currentTextChanged.connect(self.update_fft_size)
        form.addWidget(Qt.QLabel("FFT Size:"))
        form.addWidget(self.fft_combo)

        # Mode-specific inputs
        if self.mode == "time":
            self._build_time_mode_ui()
        elif self.mode == "frequency":
            self._build_frequency_mode_ui()
        elif self.mode == "wide spectrum":
            self._build_sweep_mode_ui(form)
        elif self.mode == "wide spectrum frequency":
            self._build_port_sweep_ui()
        # Gains
        self.rf_gain_min, self.rf_gain_max = 0, 14
        self.if_gain_min, self.if_gain_max = 0, 40
        self.bb_gain_min, self.bb_gain_max = 0, 62

        self.rf_gain_edit = Qt.QLineEdit(str(self.rf_gain))
        self.if_gain_edit = Qt.QLineEdit(str(self.if_gain))
        self.bb_gain_edit = Qt.QLineEdit(str(self.bb_gain))

        # Put fields on the toolbar/form
        form.addWidget(Qt.QLabel("RF Gain:")); form.addWidget(self.rf_gain_edit)
        self.rf_gain_error_label = Qt.QLabel("")            
        self.rf_gain_error_label.setStyleSheet("color: red; font-weight: bold")
        self.rf_gain_error_label.hide()
        form.addWidget(self.rf_gain_error_label)

        form.addWidget(Qt.QLabel("IF Gain:")); form.addWidget(self.if_gain_edit)
        self.if_gain_error_label = Qt.QLabel("")              
        self.if_gain_error_label.setStyleSheet("color: red; font-weight: bold")
        self.if_gain_error_label.hide()
        form.addWidget(self.if_gain_error_label)

        form.addWidget(Qt.QLabel("BB Gain:")); form.addWidget(self.bb_gain_edit)
        self.bb_gain_error_label = Qt.QLabel("")             
        self.bb_gain_error_label.setStyleSheet("color: red; font-weight: bold")
        self.bb_gain_error_label.hide()
        form.addWidget(self.bb_gain_error_label)

        # Keep a "dirty" flag so clicking/focusing a field doesn't re-apply.
        self._gains_dirty = False
        self._last_applied_gains = (self.rf_gain, self.if_gain, self.bb_gain)

        def _on_gain_edited(_=None):
            self._gains_dirty = True

        for edit in (self.rf_gain_edit, self.if_gain_edit, self.bb_gain_edit):
            edit.setValidator(QIntValidator(-9999, 9999, self))
            edit.textEdited.connect(_on_gain_edited)
            edit.editingFinished.connect(self.update_gains)
            edit.returnPressed.connect(self.update_gains)  # Enter triggers apply
            
        # Antenna (ONLY for modes that need manual entry)
        if self.mode not in ("time", "frequency", "wide spectrum frequency"):
            self.antenna_edit = Qt.QLineEdit(self.antenna_select)
            self.antenna_edit.returnPressed.connect(self.update_antenna)
            self.antenna_error_label = Qt.QLabel("")
            self.antenna_error_label.setStyleSheet("color: red; font-weight: bold")
            self.antenna_error_label.setAlignment(Qt.Qt.AlignCenter)
            self.antenna_error_label.hide()

            form.addWidget(Qt.QLabel("Antenna:"))
            form.addWidget(self.antenna_edit)
            form.addWidget(self.antenna_error_label)
        else:
            # Keep a placeholder attribute to avoid hasattr checks all over
            self.antenna_edit = None
            self.antenna_error_label = None
        if self.mode not in ("wide spectrum", "wide spectrum frequency", "delay"):
            ctrl_wrap = Qt.QWidget()
            ctrl = Qt.QHBoxLayout(ctrl_wrap)
            ctrl.setContentsMargins(0, 0, 0, 0)
            ctrl.setSpacing(6)

            # Max Hold toggle (mapped to freq_sink.enable_max_hold)
            self.btn_fs_max = Qt.QToolButton()
            self.btn_fs_max.setText("Max Hold")
            self.btn_fs_max.setCheckable(True)
            self.btn_fs_max.setChecked(False)
            self.btn_fs_max.setToolTip("Hold the maximum value per-bin")
            ctrl.addWidget(self.btn_fs_max)

            # Avg control (0..1, mapped to set_fft_average)
            ctrl.addWidget(Qt.QLabel("Avg:"))
            self.spin_fs_avg = Qt.QDoubleSpinBox()
            self.spin_fs_avg.setRange(0.0, 1.0)
            self.spin_fs_avg.setSingleStep(0.05)
            self.spin_fs_avg.setDecimals(2)
            self.spin_fs_avg.setValue(0.20)
            self.spin_fs_avg.setToolTip("Exponential averaging factor (0 = off, 1 = full)")
            self.spin_fs_avg.setFixedWidth(72)
            ctrl.addWidget(self.spin_fs_avg)

            # Insert into the toolbar
            idx = self.toolbar.indexOf(self.toggle_btn) if hasattr(self, "toggle_btn") else -1
            if idx >= 0:
                self.toolbar.insertWidget(idx, ctrl_wrap)
            else:
                self.toolbar.addWidget(ctrl_wrap)
        else:
            self.btn_fs_max = None
            self.spin_fs_avg = None

        # Start/Stop
        self.toggle_btn = Qt.QPushButton("Start")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self.toggle_stream)
        form.addWidget(self.toggle_btn)

        self.top_layout.addLayout(self.toolbar, 0, 0)

        # SDR CONFIG + PIPES
        self.src = osmosdr.source("numchan=1 hackrf")
        self.apply_sdr_config()

        self.sweep_timer = QTimer(self)
        self.jam_timer = QTimer(self)
        self.jam_timer.timeout.connect(self.check_jamming)

        # Base plots
        if self.mode == "delay":
            self.init_waterfall()
        elif self.mode not in ("wide spectrum", "wide spectrum frequency"):
            self.init_waterfall()
            self.init_freq_spectrum()

        # Sweep pipeline
        if self.mode in ("wide spectrum", "wide spectrum frequency"):
            self._build_sweep_pipeline()
            self._build_sweep_plot()
            self.validate_sweep_inputs(quiet=True)   

        # Jamming pipeline
        if self.mode == "jamming":
            self._build_jamming_pipeline(form)

        BOARD_IMG = str(asset("operacake.jpeg"))

        self.oc_panel = OperaCakePanel(BOARD_IMG, self)
        
        self.top_layout.addWidget(self.oc_panel, 5, 0, 1, 5)
        self.oc_panel.setSizePolicy(Qt.QSizePolicy.Expanding, Qt.QSizePolicy.Preferred)
        self.oc_panel.setMinimumHeight(160)
        self.oc_panel.setMaximumHeight(260)
        self.oc_panel.set_active(self.antenna_select, fixed_input="A0", connected=self.is_hackrf_connected())

    # UI BUILD — subparts
    def _build_time_mode_ui(self):
        self.time_config = []
        self.time_inputs = []
        self.time_labels = []
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.perform_time_switch)

        self.timer_start_time = None
        self.elapsed_before_stop = 0
        self.per_port_elapsed = {}

        # Buttons time mode
        self.add_row_btn = Qt.QPushButton("Add Port")
        self.add_row_btn.setToolTip("Add another port-duration pair")
        self.add_row_btn.clicked.connect(self.add_time_row)

        self.remove_row_btn = Qt.QPushButton("Remove Port")
        self.remove_row_btn.setToolTip("Remove the last port row")
        self.remove_row_btn.clicked.connect(self.remove_time_row)

        idx = self.toolbar.indexOf(self.fft_combo)
        if idx != -1:
            self.toolbar.insertWidget(idx + 1, self.add_row_btn)
            self.toolbar.insertWidget(idx + 2, self.remove_row_btn)
        else:
            self.toolbar.addWidget(self.add_row_btn)
            self.toolbar.addWidget(self.remove_row_btn)

        # Error label under toolbar
        self.time_error_label = Qt.QLabel("")
        self.time_error_label.setStyleSheet("color: red; font-weight: bold")
        self.time_error_label.setAlignment(Qt.Qt.AlignCenter)
        self.time_error_label.hide()
        self.top_layout.addWidget(self.time_error_label, 1, 0, 1, 5)

        # Container for rows
        self.time_input_layout = Qt.QGridLayout()
        self.time_input_rows = []
        self.time_input_container = Qt.QWidget()
        self.time_input_container.setLayout(self.time_input_layout)
        self.top_layout.addWidget(self.time_input_container, 2, 0, 1, 5)

        self.add_time_row()

    def _build_frequency_mode_ui(self):
        self.port_inputs = {}

        ports = ['A4', 'A3', 'A2', 'A1', 'B4', 'B3', 'B2', 'B1']
        default_ranges = ['0:100', '101:200', '201:300', '301:400',
                          '401:700', '701:1000', '1001:2000', '2001:4000']

        port_layout = Qt.QGridLayout()
        for i, port in enumerate(ports):
            port_label = Qt.QLabel(f"{port} (MHz):")
            port_input = Qt.QLineEdit(default_ranges[i])
            port_layout.addWidget(port_label, 0, i)
            port_layout.addWidget(port_input, 1, i)
            self.port_inputs[port] = port_input
            port_input.editingFinished.connect(self.refresh_freq_config)

        self.port_error_label = Qt.QLabel("")
        self.port_error_label.setStyleSheet("color: red; font-weight: bold")
        self.port_error_label.setAlignment(Qt.Qt.AlignCenter)
        self.port_error_label.hide()
        port_layout.addWidget(self.port_error_label, 2, 0, 1, len(ports))
        self.top_layout.addLayout(port_layout, 1, 0)

    def _build_sweep_mode_ui(self, form):
        self.sweep_start_edit = Qt.QLineEdit("80e6")
        self.sweep_end_edit = Qt.QLineEdit("100e6")
        form.addWidget(Qt.QLabel("Sweep Start:"))
        form.addWidget(self.sweep_start_edit)
        form.addWidget(Qt.QLabel("Sweep End:"))
        form.addWidget(self.sweep_end_edit)

        self.sweep_error_label = Qt.QLabel("")
        self.sweep_error_label.setStyleSheet("color: red; font-weight: bold")
        self.sweep_error_label.setAlignment(Qt.Qt.AlignCenter)
        self.sweep_error_label.hide()
        self.top_layout.addWidget(self.sweep_error_label, 1, 0, 1, 5)

        self.sweep_start_edit.returnPressed.connect(self.validate_sweep_inputs)
        self.sweep_end_edit.returnPressed.connect(self.validate_sweep_inputs)

        self.last_valid_sweep_start = parse_freq_input(self.sweep_start_edit.text())
        self.last_valid_sweep_end = parse_freq_input(self.sweep_end_edit.text())

        self._rebuilding = False

    def _port_title(self, p: str) -> str:
            tag = self.port_tags.get(p, "")
            return f"{p} ({tag})" if tag else p
    
    def _build_port_sweep_ui(self):
        # Hidden edits so validate_sweep_inputs() keeps working as-is
        self.sweep_start_edit = Qt.QLineEdit("80e6")
        self.sweep_end_edit   = Qt.QLineEdit("100e6")
        self.sweep_error_label = Qt.QLabel("")
        self.sweep_error_label.setStyleSheet("color: red; font-weight: bold")
        self.sweep_error_label.setAlignment(Qt.Qt.AlignCenter)
        self.sweep_error_label.hide()

        # Port grid (like frequency mode, but with radios)
        self.port_inputs  = {}
        self.port_buttons = {}
        self.selected_port = None

        ports = ['A4','A3','A2','A1','B4','B3','B2','B1']
        default_ranges = ['87.5:108', '894.8:939.8', '1977.4:2167.4', '793.5:834.5',
                        '1730:1825', '2570:2620', '3300:3800', '0:1']

        grid = Qt.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        # Radio group for exclusivity
        self._port_group = Qt.QButtonGroup(self)
        self._port_group.setExclusive(True)

        # Cosmetic: green when selected
        rb_css = """
        QRadioButton::indicator {
            width: 14px; height: 14px;
            border: 2px solid #888; border-radius: 8px; background: #222;
        }
        QRadioButton::indicator:checked {
            background: #2ecc71;
            border: 2px solid #2ecc71;
        }
        """

        for i, port in enumerate(ports):
            col = i
            rb = Qt.QRadioButton(self._port_title(port))
            rb.setStyleSheet(rb_css)
            edit = Qt.QLineEdit(default_ranges[i])
            edit.setAlignment(Qt.Qt.AlignCenter)
            edit.setFixedWidth(102)
            edit.setToolTip("MHz range, like 80:120")

            wrap = Qt.QWidget()
            hl = Qt.QHBoxLayout(wrap)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(4)
            hl.addWidget(edit)
            unit = Qt.QLabel("MHz")
            unit.setStyleSheet("color: black;")
            hl.addWidget(unit)

            self.port_inputs[port]  = edit
            self.port_buttons[port] = rb
            self._port_group.addButton(rb)

            # Row 0: radios, Row 1: edits
            grid.addWidget(rb,   0, col, alignment=Qt.Qt.AlignHCenter)
            grid.addWidget(wrap, 1, col, alignment=Qt.Qt.AlignHCenter)

            # When user edits ranges while selected, re-apply sweep limits on the fly
            def _on_edit_finished(p=port):
                if self.selected_port == p and self.toggle_btn.isChecked():
                    self._select_port_and_sweep(p)
            edit.editingFinished.connect(_on_edit_finished)

            def _on_toggled(on, p=port):
                if on:
                    self._select_port_and_sweep(p)
            rb.toggled.connect(_on_toggled)

        # Place grid + (hidden) error label
        self.top_layout.addLayout(grid, 1, 0)
        self.top_layout.addWidget(self.sweep_error_label, 2, 0, 1, 5)

        # Auto-select a sensible default (current antenna if matches, else A4)
        default_port = self.antenna_select if self.antenna_select in ports else 'A4'
        
        # Prevent _on_toggled from firing during initial check mark
        self._port_group.blockSignals(True)
        self.port_buttons[default_port].setChecked(True)
        self._port_group.blockSignals(False)

        # Prime sweep ranges (no hardware touch thanks to guarded update_antenna)
        self._select_port_and_sweep(default_port, start_immediately=False)

    # SDR sub-pipelines
    def _build_sweep_pipeline(self):
        self.stream_to_vector = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)
        self.fft = fft_vcc(self.fft_size, True, window.blackmanharris(self.fft_size), True)
        self.c2mag = blocks.complex_to_mag(self.fft_size)
        self.probe = blocks.probe_signal_vf(self.fft_size)

        self.connect((self.src), (self.stream_to_vector, 0))
        self.connect((self.stream_to_vector, 0), (self.fft, 0))
        self.connect((self.fft, 0), (self.c2mag, 0))
        self.connect((self.c2mag, 0), (self.probe, 0))

    def _build_sweep_plot(self):
        self.pg_plot = PlotWidget()
        self.pg_plot.setTitle("Wideband Frequency Spectrum")
        self.pg_plot.setLabel("bottom", "Frequency", units="MHz")
        self.pg_plot.setLabel("left", "Relative Gain", units="dB")
        self.pg_plot.showGrid(x=True, y=True)
        self.pg_plot.setYRange(-120, 0)
        self.pg_plot.setBackground('w')
        self.pg_curve = self.pg_plot.plot(pen=mkPen(color='b', width=1))

        # Extra traces for peak-hold (hidden by default)
        self.pg_peak = self.pg_plot.plot(pen=mkPen(width=1))
        self.pg_peak.setVisible(False)

        # Lock autorange BEFORE start to avoid jumpy view
        self.pg_plot.enableAutoRange(x=False, y=False)
        self._update_sweep_xrange()

        # Crosshair (start hidden)
        self._vline = pg.InfiniteLine(angle=90, movable=False); self._vline.setVisible(False)
        self._hline = pg.InfiniteLine(angle=0,  movable=False); self._hline.setVisible(False)
        self.pg_plot.addItem(self._vline, ignoreBounds=True)
        self.pg_plot.addItem(self._hline, ignoreBounds=True)

        self._mouse_text = pg.TextItem("", anchor=(1,1), color="k")
        self._mouse_text.setVisible(False)
        self.pg_plot.addItem(self._mouse_text)

        vb = self.pg_plot.getViewBox()

        def _on_move(pos):
            inside = self.pg_plot.sceneBoundingRect().contains(pos)
            if not inside:
                # hide crosshair + label when cursor leaves the plot
                self._vline.setVisible(False)
                self._hline.setVisible(False)
                self._mouse_text.setVisible(False)
                return

            mousePoint = vb.mapSceneToView(pos)
            fx = mousePoint.x(); fy = mousePoint.y()
            self._vline.setPos(fx); self._hline.setPos(fy)
            self._vline.setVisible(True); self._hline.setVisible(True)

            self._mouse_text.setText(f"{fx:.3f} MHz\n{fy:.1f} dB")
            self._mouse_text.setPos(fx, fy)
            self._mouse_text.setVisible(True)

        self.pg_plot.scene().sigMouseMoved.connect(_on_move)

        # Right-click anywhere on the plot to reset peak envelopes
        def _on_click(ev):
            if ev.button() == Qt.Qt.RightButton:
                self.peak_env = None
                if hasattr(self, "_peak_scatter"):
                    self._peak_scatter.clear()
                ev.accept()
        self.pg_plot.scene().sigMouseClicked.connect(_on_click)

        self.top_layout.addWidget(self.pg_plot, 2, 0)

        # Peak toggle buttons on the toolbar
        self.peak_enabled = False

        btn_wrap = Qt.QWidget()
        btns = Qt.QHBoxLayout(btn_wrap)
        btns.setContentsMargins(0, 0, 0, 0)
        btns.setSpacing(6)

        self.btn_peak_hold = Qt.QToolButton()
        self.btn_peak_hold.setText("Max Hold")
        self.btn_peak_hold.setCheckable(True)
        self.btn_peak_hold.setChecked(False)

        btns.addWidget(self.btn_peak_hold)

        btns.addWidget(Qt.QLabel("Avg:"))
        self.sweep_avg_alpha = 0.20
        self.sweep_avg_spin = Qt.QDoubleSpinBox()
        self.sweep_avg_spin.setRange(0.0, 1.0)
        self.sweep_avg_spin.setSingleStep(0.05)
        self.sweep_avg_spin.setDecimals(2)
        self.sweep_avg_spin.setValue(0.20)  # default like the Qt sink
        self.sweep_avg_spin.setToolTip("Exponential averaging factor (0 = off, 1 = full)")
        self.sweep_avg_spin.setFixedWidth(72)
        btns.addWidget(self.sweep_avg_spin)

        # Sweep EMA factor
        def _on_sweep_avg_changed(val):
            try:
                self.sweep_avg_alpha = float(val)
                if self.sweep_avg_alpha <= 0.0:
                    # make "0" truly = no averaging
                    self.sweep_prev_linear = None
            except Exception:
                pass

        self.sweep_avg_spin.valueChanged.connect(_on_sweep_avg_changed)

        # Place buttons just before Start/Stop if present
        idx = self.toolbar.indexOf(self.toggle_btn) if hasattr(self, "toggle_btn") else -1
        if idx >= 0:
            self.toolbar.insertWidget(idx, btn_wrap)
        else:
            self.toolbar.addWidget(btn_wrap)

        def _on_peak_toggle(on: bool):
            self.peak_enabled = on
            self.pg_peak.setVisible(on)
            if not on:
                self.peak_env = None  # clear stored envelope when disabled

        self.sweep_db_cal = 0.0
        self.btn_peak_hold.toggled.connect(_on_peak_toggle)
        self._ensure_mouse_blocker()

    def _build_jamming_pipeline(self, form):
        self.stream_to_vector = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)
        self.fft = fft_vcc(self.fft_size, True, window.blackmanharris(self.fft_size), True)
        self.c2mag = blocks.complex_to_mag(self.fft_size)
        self.probe = blocks.probe_signal_vf(self.fft_size)

        self.connect((self.src), (self.stream_to_vector, 0))
        self.connect((self.stream_to_vector, 0), (self.fft, 0))
        self.connect((self.fft, 0), (self.c2mag, 0))
        self.connect((self.c2mag, 0), (self.probe, 0))

        # Detector params/state
        self.jam_alpha          = 0.05   # baseline EMA
        self.jam_thresh_db      = 6.5    # per-bin ΔdB over baseline
        self.jam_occ_min        = 0.5    # base occupancy requirement
        self.jam_occ_multipeak  = 0.15   # occupancy for multipeak branch
        self.jam_span_min       = 0.008  # contiguous run fraction (~0.8% of masked bins)
        self.jam_sfm_min_db     = -5.0   # allow OFDM (not super flat) to trigger
        self._sfm_offset        = 0.8    # SFM gate = ref + 0.8 dB
        self.jam_median_db      = 2.0    # median-lift fallback (dB)
        self.jam_band_db        = 1.5    # band-power (mean) lift (dB)
        self.jam_hold_s         = 0.4    # hold time to confirm
        self.jam_cooldown_s     = 4.0    # cooldown after switching
        self.jam_ready_at       = time.time() + 1.5
        self.jam_baseline_db    = None
        self.jam_started        = None
        self.jam_cooldown_until = 0.0
        self.sfm_ref            = None
        self.ports = ['A4','A3','A2','A1','B4','B3','B2','B1']
        self.current_port_index = self.ports.index(self.antenna_select) if self.antenna_select in self.ports else 0

        self._rebuild_jam_mask()

        # Debug
        self.jam_debug = True
        self._jam_last_dbg = 0.0

        # Knobs
        knob_bar = Qt.QHBoxLayout()
        knob_bar.setContentsMargins(0, 0, 0, 0)
        knob_bar.setSpacing(6)

        h = self.freq_edit.sizeHint().height() if hasattr(self, "freq_edit") else 24

        def _mk_edit(txt, tip, vmin, vmax, prec, width=56):
            e = Qt.QLineEdit(txt)
            e.setAlignment(Qt.Qt.AlignCenter)
            e.setFixedWidth(width)
            e.setFixedHeight(h)
            dv = QDoubleValidator(vmin, vmax, prec, self)
            dv.setNotation(QDoubleValidator.ScientificNotation)
            e.setValidator(dv)
            e.setToolTip(tip)

            def on_change(_):
                if e.text() == "":
                    e.setStyleSheet("")
                elif e.hasAcceptableInput():
                    e.setStyleSheet("")
                else:
                    e.setStyleSheet("border: 2px solid #d00;")
            e.textChanged.connect(on_change)
            return e

        knob_bar.addWidget(Qt.QLabel("ΔdB:"))
        self.jam_thresh_edit = _mk_edit(
            str(self.jam_thresh_db), "Per-bin threshold above baseline (dB)", -100.0, 100.0, 1
        )
        knob_bar.addWidget(self.jam_thresh_edit)

        knob_bar.addWidget(Qt.QLabel("Occ:"))
        self.jam_occ_edit = _mk_edit(
            str(self.jam_occ_min), "Minimum fraction of bins above ΔdB (0–1)", 0.0, 1.0, 2
        )
        knob_bar.addWidget(self.jam_occ_edit)

        knob_bar.addWidget(Qt.QLabel("SFM≥:"))
        self.jam_sfm_edit = _mk_edit(
            str(self.jam_sfm_min_db), "Minimum spectral flatness (dB)", -60.0, 10.0, 1
        )
        knob_bar.addWidget(self.jam_sfm_edit)

        knob_wrap = Qt.QWidget()
        knob_wrap.setLayout(knob_bar)
        start_idx = form.indexOf(self.toggle_btn)
        if start_idx >= 0:
            form.insertWidget(start_idx, knob_wrap)
        else:
            form.addWidget(knob_wrap)

        # Wire inputs to strict apply
        self.jam_thresh_edit.returnPressed.connect(self._apply_jam_knobs)
        self.jam_occ_edit.returnPressed.connect(self._apply_jam_knobs)
        self.jam_sfm_edit.returnPressed.connect(self._apply_jam_knobs)
        # React on focus loss
        self.jam_thresh_edit.editingFinished.connect(self._apply_jam_knobs)
        self.jam_occ_edit.editingFinished.connect(self._apply_jam_knobs)
        self.jam_sfm_edit.editingFinished.connect(self._apply_jam_knobs)

    def _apply_jam_knobs(self):
        bad = False
        try:
            new_thresh = parse_float_strict(self.jam_thresh_edit.text(), -100.0, 100.0, name="ΔdB threshold")
            self.jam_thresh_edit.setStyleSheet("")
        except ValueError as e:
            bad = True; self.jam_thresh_edit.setStyleSheet("border: 2px solid #d00;"); print(f"[ERROR] {e}")

        try:
            new_occ = parse_float_strict(self.jam_occ_edit.text(), 0.0, 1.0, name="occupancy")
            self.jam_occ_edit.setStyleSheet("")
        except ValueError as e:
            bad = True; self.jam_occ_edit.setStyleSheet("border: 2px solid #d00;"); print(f"[ERROR] {e}")

        try:
            new_sfm = parse_float_strict(self.jam_sfm_edit.text(), -60.0, 10.0, name="SFM minimum")
            self.jam_sfm_edit.setStyleSheet("")
        except ValueError as e:
            bad = True; self.jam_sfm_edit.setStyleSheet("border: 2px solid #d00;"); print(f"[ERROR] {e}")

        if bad:
            self._stop_stream_safely()
            return

        self.jam_thresh_db  = new_thresh
        self.jam_occ_min    = new_occ
        self.jam_sfm_min_db = new_sfm
        self.jam_ignore_until = time.time() + 0.5

        if getattr(self, "jam_debug", False):
            print(f"[JDBG] knobs applied: ΔdB={self.jam_thresh_db:.2f}, "
                f"Occ={self.jam_occ_min:.2f}, SFM≥{self.jam_sfm_min_db:.2f} dB")

    #  Close / Cleanup
    def closeEvent(self, event):
        try:
            self.stop()
            self.wait()
        except Exception as e:
            print(f"[WARN] Exception on stop: {e}")

        if self.mode == "time" and hasattr(self, "timer"):
            self.timer.stop()
            self.timer_start_time = None

        if self.mode in ("wide spectrum", "wide spectrum frequency") and hasattr(self, "sweep_timer"):
            self.sweep_timer.stop()

        if self.mode == "jamming" and hasattr(self, "jam_timer"):
            self.jam_timer.stop()

        if hasattr(self.parent(), "oc_panel") and hasattr(self, "antenna_select"):
            try:
                parent_connected = self.parent().is_hackrf_connected() if hasattr(self.parent(), "is_hackrf_connected") else self.is_hackrf_connected()
                self.parent().oc_panel.set_active(self.antenna_select, fixed_input="A0", connected=parent_connected)
            except Exception:
                pass

        self.closed.emit()
        event.accept()

    #  Plots / Sinks
    def init_waterfall(self):
        if self.mode == "delay":
            self.waterfall = qtgui.waterfall_sink_c(
                self.fft_size, window.WIN_BLACKMAN_hARRIS, self.center_freq, self.samp_rate, "Spectrogram", 1, None
            )
            self.waterfall.set_update_time(0.0005)
        else:
            self.waterfall = qtgui.waterfall_sink_c(
                self.fft_size, window.WIN_BLACKMAN_hARRIS, self.center_freq, self.samp_rate, "Spectrogram", 1, None
            )
            self.waterfall.set_update_time(0.10)

        self.waterfall.enable_grid(False)
        self.waterfall.enable_axis_labels(True)
        self.waterfall.set_intensity_range(-140, 10)

        self._waterfall_win = sip.wrapinstance(self.waterfall.qwidget(), Qt.QWidget)
        self._waterfall_win.setContentsMargins(0, 0, 0, 0)
        self._waterfall_win.setSizePolicy(Qt.QSizePolicy.Expanding, Qt.QSizePolicy.Expanding)

        self.top_layout.addWidget(self._waterfall_win, 3, 0, 1, 5)
        if self.mode != "wide spectrum":
            self.connect((self.src, 0), (self.waterfall, 0))

    def init_freq_spectrum(self):
        self.freq_sink = qtgui.freq_sink_c(
            self.fft_size, window.WIN_BLACKMAN_hARRIS, self.center_freq, self.samp_rate, "Frequency Spectrum", 1
        )
        self.freq_sink.set_update_time(0.10)
        self.freq_sink.set_y_axis(-120, 0)
        self.freq_sink.set_y_label("Relative Gain", "dB")
        self.freq_sink.enable_grid(True)
        self.freq_sink.enable_axis_labels(True)
        self.freq_sink.disable_legend()
        self.freq_sink.set_line_label(0, "Spectrum")

        try:
            self.freq_sink.enable_control_panel(False)
        except Exception:
            pass  
        
        self.freq_sink.set_fft_average(0.20)
        try:
            self.freq_sink.enable_max_hold(False)   # start with Max Hold off
            self.freq_sink.enable_min_hold(False)   # force Min Hold off
        except Exception:
            pass

        self._freq_win = sip.wrapinstance(self.freq_sink.qwidget(), Qt.QWidget)
        self._freq_win.setContentsMargins(0, 0, 0, 0)
        self._freq_win.setSizePolicy(Qt.QSizePolicy.Expanding, Qt.QSizePolicy.Expanding)
        self.top_layout.addWidget(self._freq_win, 4, 0, 1, 5)

        if self.mode != "wide spectrum":
            self.connect((self.src, 0), (self.freq_sink, 0))

        if (getattr(self, "btn_fs_max", None) is not None) and (getattr(self, "spin_fs_avg", None) is not None):
            def _on_max_toggled(on):
                try:
                    # never allow Min Hold
                    self.freq_sink.enable_min_hold(False)
                    self.freq_sink.enable_max_hold(bool(on))
                except Exception:
                    pass

            def _on_avg_changed(val):
                try:
                    self.freq_sink.set_fft_average(float(val))
                except Exception:
                    pass

            # connect once
            try:
                self.btn_fs_max.toggled.disconnect()
            except Exception:
                pass
            try:
                self.spin_fs_avg.valueChanged.disconnect()
            except Exception:
                pass

            self.btn_fs_max.toggled.connect(_on_max_toggled)
            self.spin_fs_avg.valueChanged.connect(_on_avg_changed)

            _on_max_toggled(self.btn_fs_max.isChecked())
            _on_avg_changed(self.spin_fs_avg.value())

    #  Update Handlers (freq/rate/antenna/gains/fft)
    def update_center_freq(self):
        try:
            freq = parse_freq_input(self.freq_edit.text())

            # Clamp
            if freq <= 0:
                freq = 1.0
                self.freq_edit.setText("1")
                self.show_temporary_label("freq_range_label", "0 < fc <= 4e9. Set to to 1 Hz.")
            elif freq > 4e9:
                freq = 4e9
                self.freq_edit.setText("4e9")
                self.show_temporary_label("freq_range_label", "0 < fc <= 4e9. Set to to 4e9 Hz.")
            else:
                # only hide when no clamp
                self._hide_label("freq_range_label")

            if abs(freq - getattr(self, "center_freq", 0)) < 1e-6:
                return
        
            temp_freq = freq

            if self.mode == "frequency":
                self.center_freq = temp_freq
                if not self.validate_port_ranges():
                    print("[ABORT] Central frequency not updated due to invalid port ranges.")
                    self.freq_edit.setText(f"{self.center_freq / 1e6:.2f} MHz")
                    if self.toggle_btn.isChecked():
                        self.toggle_btn.setChecked(False)
                        self.toggle_stream()
                    return

            self.center_freq = temp_freq
            self.src.set_center_freq(temp_freq, 0)

            if hasattr(self, "waterfall"):
                self.waterfall.set_frequency_range(temp_freq, self.samp_rate)
            if hasattr(self, "freq_sink"):
                self.freq_sink.set_frequency_range(temp_freq, self.samp_rate)

            print(f"[INFO] Central frequency set to {temp_freq:.2f} Hz")

            if self.mode == "frequency":
                self.auto_switch_port_if_needed()

            if self.mode == "jamming":
                self.jam_baseline_db = None
                self.jam_ready_at = time.time() + 1.5

        except ValueError as e:
            self._input_error("freq_range_label", str(e), field=self.freq_edit)
            return

    def update_sample_rate(self):
        try:
            sr = parse_freq_input(self.samp_rate_edit.text())

            if sr <= 0:
                sr = 1.0
                self.samp_rate_edit.setText("1")
                self.show_temporary_label("samp_rate_range_label", "0 < sr <= 20e6. Set to 1 S/s.")
            elif sr > 20e6:
                sr = 20e6
                self.samp_rate_edit.setText("20e6")
                self.show_temporary_label("samp_rate_range_label", "0 < sr <= 20e6. Set to 20 MS/s.")
            else:
                self._hide_label("samp_rate_range_label")

            if self.mode in ("wide spectrum", "wide spectrum frequency"):
                if self.sweep_active:
                    print("[INFO] Sweep active — applying sample rate live.")
                    self.lock()
                    try:
                        self.src.set_sample_rate(sr)
                        self.samp_rate = sr
                        self.validate_sweep_inputs()
                    finally:
                        self.unlock()
                    self.sweep_ptr = 0
                    self.sweep_timer.stop()
                    self.sweep_active = True
                    QTimer.singleShot(100, self.begin_sweep_timer)
                else:
                    self.src.set_sample_rate(sr)
                    self.samp_rate = sr
                    self.validate_sweep_inputs()
            else:
                self.src.set_sample_rate(sr)
                self.samp_rate = sr

                if self.mode == "jamming":
                    self.jam_baseline_db = None
                    self.jam_ready_at = time.time() + 1.5

                if hasattr(self, "waterfall"):
                    self.waterfall.set_frequency_range(self.center_freq, sr)
                if hasattr(self, "freq_sink"):
                    self.freq_sink.set_frequency_range(self.center_freq, sr)

            print(f"[INFO] Sample rate set to {sr:.2f} S/s")

        except ValueError as e:
            self._input_error("samp_rate_range_label", str(e), field=self.samp_rate_edit)
            return

    def update_antenna(self, val: str = None):
        if val is None:
            if self.antenna_edit is None:
                val = self.antenna_select
            else:
                val = str(self.antenna_edit.text()).strip().upper()
        else:
            val = str(val).strip().upper()

        valid = ['A1','A2','A3','A4','B1','B2','B3','B4']
        if val in valid:
            self.antenna_select = val

            # SDR build guard
            if hasattr(self, "src") and self.src is not None:
                try:
                    self.src.set_antenna(val, 0)
                except Exception as e:
                    print(f"[WARN] set_antenna({val}) failed: {e}")

            print(f"[INFO] Switched antenna to: {val}")

            # Panel build guard
            if hasattr(self, "oc_panel") and self.oc_panel is not None:
                self.oc_panel.set_active(val, fixed_input="A0", connected=self.is_hackrf_connected())

            self.antenna_changed.emit(val)

            if self.mode == "jamming":
                self.jam_baseline_db = None
                self.jam_ready_at = time.time() + 1.5
                if hasattr(self, "ports") and self.antenna_select in self.ports:
                    self.current_port_index = self.ports.index(self.antenna_select)
                self.jam_last_switch = time.time()
        else:
            print(f"[WARN] Invalid antenna: {val}")
            if self.antenna_error_label is not None:
                self.show_temporary_label(
                    "antenna_error_label",
                    f"Antenna '{val}' doesn't exist.\nAvailable: {', '.join(valid)}"
                )
            if self.antenna_edit is not None:
                self.antenna_edit.setText(self.antenna_select)

    def update_gains(self):
        if not getattr(self, "_gains_dirty", False):
            return

        fields = [
            ("RF Gain", self.rf_gain_edit, "rf_gain_error_label", self.rf_gain_min, self.rf_gain_max),
            ("IF Gain", self.if_gain_edit, "if_gain_error_label", self.if_gain_min, self.if_gain_max),
            ("BB Gain", self.bb_gain_edit, "bb_gain_error_label", self.bb_gain_min, self.bb_gain_max),
        ]

        # Empty/invalid erro message
        for name, edit, lbl_attr, vmin, vmax in fields:
            if edit.text().strip() == "" or not edit.hasAcceptableInput():
                edit.setStyleSheet("border: 2px solid #d00;")
                QTimer.singleShot(1200, lambda e=edit: e.setStyleSheet(""))
                self.show_temporary_label(lbl_attr, f"{name} must be an integer between {vmin} and {vmax}")
                self._stop_stream_safely()
                return

        raw_vals, clamped_vals, clamped_flags = [], [], []
        for name, edit, lbl_attr, vmin, vmax in fields:
            raw = int(edit.text())
            val = clamp(raw, vmin, vmax)
            raw_vals.append(raw)
            clamped_vals.append(val)
            clamped_flags.append(val != raw)

        rf, ifg, bb = clamped_vals

        # Reflect values 
        for (_, edit, _, _, _), val in zip(fields, clamped_vals):
            edit.setText(str(val))

        # Per-field inline error when clamped
        for (name, edit, lbl_attr, vmin, vmax), was_clamped in zip(fields, clamped_flags):
            if was_clamped:
                self.show_temporary_label(lbl_attr, f"{name} must be an integer between {vmin} and {vmax}")
                edit.setStyleSheet("border: 2px solid #d00;")
                QTimer.singleShot(1200, lambda e=edit: e.setStyleSheet(""))

        if (rf, ifg, bb) == getattr(self, "_last_applied_gains", (None, None, None)) and not any(clamped_flags):
            self._gains_dirty = False
            return
    
        # Apply gains
        self.rf_gain, self.if_gain, self.bb_gain = rf, ifg, bb
        try:
            self.src.set_gain(rf, 0)
            self.src.set_if_gain(ifg, 0)
            self.src.set_bb_gain(bb, 0)
            print(f"[INFO] Gains set: RF={rf}, IF={ifg}, BB={bb}")
        except Exception as e:
            print(f"[WARN] Applying gains failed: {e}")

        self._last_applied_gains = (rf, ifg, bb)
        self._gains_dirty = False

        if self.mode == "jamming":
            self.jam_baseline_db = None
            self.jam_ready_at = time.time() + 1.5

    def update_fft_size(self):
        try:
            size = int(self.fft_combo.currentText())
        except Exception:
            print("[ERROR] FFT size update: invalid combo value")
            return
        if size == self.fft_size:
            return

        print(f"[INFO] FFT size changed to {size}, rebuilding sinks.")
        was_running = self.toggle_btn.isChecked()

        # stop timers that could fire mid-rebuild
        if self.mode in ("wide spectrum", "wide spectrum frequency") and hasattr(self, "sweep_timer"):
            try:
                self.sweep_timer.stop()
                self.sweep_timer.timeout.disconnect(self.update_sweep_freq)
            except Exception:
                pass
        if self.mode == "jamming" and hasattr(self, "jam_timer"):
            try:
                self.jam_timer.stop()
            except Exception:
                pass

        self._rebuilding = True

        # Stop FG if it was running
        if was_running:
            try:
                self.stop(); self.wait()
            except Exception:
                pass

        # (optional) hide footer image briefly to avoid flicker
        footer_was_vis = hasattr(self, "oc_panel") and self.oc_panel.isVisible()
        if footer_was_vis:
            try: self.oc_panel.hide()
            except Exception: pass

        # disconnect everything & invalidate old DSP refs
        try:
            self.disconnect_all()
        except Exception:
            pass
        self.stream_to_vector = None
        self.fft = None
        self.c2mag = None
        self.probe = None

        # apply new size
        self.fft_size = size

        try:
            if self.mode in ("wide spectrum", "wide spectrum frequency"):
                # Fresh sweep chain for new size
                self.stream_to_vector = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)
                self.fft = fft_vcc(self.fft_size, True, window.blackmanharris(self.fft_size), True)
                self.c2mag = blocks.complex_to_mag(self.fft_size)
                self.probe = blocks.probe_signal_vf(self.fft_size)

                # reset size-dependent state
                self.sweep_prev_linear = None
                self.peak_env = None
                self.last_fft = None
                self._cap_retries = 0
                self._step_freq_hz = None
                self.sweep_busy = False

                # recompute sweep plan & buffer
                ok = self.validate_sweep_inputs()
                if not ok:
                    print("[BLOCK] Sweep config invalid after FFT resize.")

                # connect pipeline
                self.connect((self.src), (self.stream_to_vector, 0))
                self.connect((self.stream_to_vector, 0), (self.fft, 0))
                self.connect((self.fft, 0), (self.c2mag, 0))
                self.connect((self.c2mag, 0), (self.probe, 0))

            elif self.mode == "jamming":
                # Rebuild QtGUI sinks (waterfall/freq)
                if hasattr(self, "_waterfall_win"):
                    try:
                        self.top_layout.removeWidget(self._waterfall_win)
                        self._waterfall_win.deleteLater()
                    except Exception:
                        pass
                if hasattr(self, "_freq_win"):
                    try:
                        self.top_layout.removeWidget(self._freq_win)
                        self._freq_win.deleteLater()
                    except Exception:
                        pass
                self.init_waterfall()
                self.init_freq_spectrum()

                # Rebuild jamming DSP chain 
                self._reinit_jamming_dsp_chain()

                # Re-apply toolbar state to the new freq_sink
                if hasattr(self, "btn_fs_max") and self.btn_fs_max is not None:
                    try:
                        self.freq_sink.enable_min_hold(False)
                        self.freq_sink.enable_max_hold(self.btn_fs_max.isChecked())
                    except Exception:
                        pass
                if hasattr(self, "spin_fs_avg") and self.spin_fs_avg is not None:
                    try:
                        self.freq_sink.set_fft_average(float(self.spin_fs_avg.value()))
                    except Exception:
                        pass

            else:
                # Rebuild QtGUI for non sweep modes
                if hasattr(self, "_waterfall_win"):
                    try:
                        self.top_layout.removeWidget(self._waterfall_win)
                        self._waterfall_win.deleteLater()
                    except Exception:
                        pass
                if hasattr(self, "_freq_win"):
                    try:
                        self.top_layout.removeWidget(self._freq_win)
                        self._freq_win.deleteLater()
                    except Exception:
                        pass
                self.init_waterfall()
                self.init_freq_spectrum()

        except Exception as e:
            print(f"[ERROR] FFT size rebuild failed: {e}")

        try:
            if was_running:
                self.start()
                if self.mode in ("wide spectrum", "wide spectrum frequency"):
                    self.sweep_ptr = 0
                    self.sweep_active = True
                    try:
                        self.begin_sweep_timer()
                    except Exception as e:
                        print(f"[WARN] begin_sweep_timer failed: {e}")
                elif self.mode == "jamming" and hasattr(self, "jam_timer"):
                    try:
                        self.jam_timer.start(100)
                    except Exception:
                        pass
            else:
                if self.mode in ("wide spectrum", "wide spectrum frequency"):
                    self.sweep_active = False
        finally:
            if footer_was_vis:
                try:
                    self.oc_panel.show()
                    self.oc_panel.repaint()
                except Exception:
                    pass
            self._rebuilding = False

    #  Frequency-mode helpers
    def auto_switch_port_if_needed(self):
        if not self.validate_port_ranges():
            print("[ABORT] Port ranges are invalid — please fix them first.")
            return

        current_mhz = self.center_freq / 1e6
        selected_port = None
        for port, line in self.port_inputs.items():
            text = line.text().strip()
            if not text:
                continue
            try:
                parts = text.replace(" ", "").split(":")
                low = float(parts[0])
                high = float(parts[1])
                print(f"[DEBUG] Checking port {port} for range {low}–{high} MHz")
                if low <= current_mhz <= high:
                    selected_port = port
                    break
            except Exception as e:
                print(f"[WARN] Bad format in {port}: '{text}' — {e}")

        if selected_port:
            if selected_port != self.antenna_select:
                print(f"[AUTO-SWITCH] Switching to {selected_port} for {current_mhz:.2f} MHz")
                self.update_antenna(selected_port)
            else:
                print(f"[SKIP] {selected_port} already selected.")
        else:
            print(f"[INFO] No matching port for {current_mhz:.2f} MHz")

    def validate_port_ranges(self):
        ranges = []
        for port, line in self.port_inputs.items():
            text = line.text().strip()
            try:
                parts = text.replace(" ", "").split(":")
                if len(parts) != 2:
                    raise ValueError("Invalid format")
                low = float(parts[0])
                high = float(parts[1])
                if low >= high:
                    raise ValueError("Lower bound must be < upper bound")
                if high > 4000:
                    raise ValueError("Upper limit exceeds 4000 MHz")
                ranges.append((low, high, port))
            except Exception as e:
                msg = f"Invalid range in {port}: '{text}' — {e}"
                print(msg)
                self.show_temporary_label("port_error_label", msg)
                return False

        ranges.sort()
        for i in range(len(ranges) - 1):
            current_high = ranges[i][1]
            next_low = ranges[i + 1][0]
            if current_high >= next_low:
                msg = (f"Overlapping or touching ranges: {ranges[i][2]} ends at {current_high}, "
                       f"{ranges[i+1][2]} starts at {next_low}")
                print(msg)
                self.show_temporary_label("port_error_label", msg)
                return False
        return True

    #  Time mode
    def add_time_row(self):
        index = len(self.time_inputs)
        row = index // 4
        col = (index % 4) * 2

        port_label = Qt.QLabel(f"Port {index + 1}:")
        port_input = Qt.QComboBox()
        valid_ports = ['A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4']
        port_input.addItems(valid_ports)
        dur_label = Qt.QLabel("Duration (s)")
        dur_input = Qt.QLineEdit()

        dur_input.setPlaceholderText("seconds > 0")

        # Accept positive floats (optionally allow scientific notation)
        v = QDoubleValidator(0.000001, 1e9, 6, self)   # min, max, decimals
        v.setNotation(QDoubleValidator.ScientificNotation)  # allow "1e-3" etc
        dur_input.setValidator(v)

        def _on_dur_edited():
            txt = dur_input.text().strip()
            if not txt:
                dur_input.setStyleSheet("")
                dur_input.setToolTip("")
                return
            # Acceptable? clear; otherwise paint red
            if dur_input.hasAcceptableInput():
                dur_input.setStyleSheet("")
                dur_input.setToolTip("")
            else:
                dur_input.setStyleSheet("border: 2px solid #d00;")
                dur_input.setToolTip("Enter a positive number of seconds (e.g., 1, 0.5, 2e-3)")

        dur_input.textChanged.connect(_on_dur_edited)

        self.time_input_layout.addWidget(port_label, row * 2, col)
        self.time_input_layout.addWidget(port_input, row * 2 + 1, col)
        self.time_input_layout.addWidget(dur_label, row * 2, col + 1)
        self.time_input_layout.addWidget(dur_input, row * 2 + 1, col + 1)

        self.time_inputs.append((port_input, dur_input))
        self.time_labels.append((port_label, dur_label))

        def safe_refresh():
            if hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
                self.refresh_time_config()

        dur_input.editingFinished.connect(safe_refresh)
        port_input.currentIndexChanged.connect(safe_refresh)

    def remove_time_row(self):
        if self.time_inputs:
            port_input, dur_input = self.time_inputs.pop()
            port_label, dur_label = self.time_labels.pop()
            for w in [port_input, dur_input, port_label, dur_label]:
                self.time_input_layout.removeWidget(w)
                w.deleteLater()
            if hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
                valid = self.refresh_time_config()
                if not valid:
                    print("[INFO] All ports removed. Stream halted.")
        else:
            self.show_temporary_label("time_error_label", "No more ports to remove.")

    #  Start/Stop + Timers
    def toggle_stream(self):
        if self.toggle_btn.isChecked():
            # START branch
            if not self._validate_all_inputs():
                print("[BLOCK] Cannot start — fix the highlighted fields.")
                self.toggle_btn.setChecked(False)
                return

            if self.mode == "wide spectrum" and not self.validate_sweep_inputs():
                print("[BLOCK] Cannot start sweep due to invalid input.")
                self.toggle_btn.setChecked(False)
                return

            if self.mode == "frequency":
                if not self.validate_port_ranges():
                    print("[ABORT] Invalid port ranges — blocking Start.")
                    self.toggle_btn.setChecked(False)
                    return
                try:
                    center_freq_mhz = parse_freq_input(self.freq_edit.text()) / 1e6
                except ValueError:
                    print("[ABORT] Central frequency text is invalid.")
                    self.toggle_btn.setChecked(False)
                    return
                matched = False
                for line in self.port_inputs.values():
                    text = line.text().strip()
                    if ":" in text:
                        try:
                            low, high = map(float, text.replace(" ", "").split(":"))
                            if low <= center_freq_mhz <= high:
                                matched = True
                                break
                        except:
                            continue
                if not matched:
                    print(f"[ABORT] Central frequency {center_freq_mhz:.2f} MHz does not match any port range.")
                    self.toggle_btn.setChecked(False)
                    return

            if self.mode == "time":

                if hasattr(self, "timer"):
                    self.timer.stop()
                new_cfg = []
                for port_input, dur_input in self.time_inputs:
                    try:
                        port = port_input.currentText()
                        dur = float(dur_input.text().strip())
                        if port and dur > 0:
                            new_cfg.append((port, dur))
                    except:
                        print("[WARN] Invalid entry skipped")
                if not new_cfg:
                    self.show_temporary_label("time_error_label",
                                            "No valid port/duration entries. Time switching aborted.")
                    self.toggle_btn.setChecked(False)
                    self.toggle_btn.setText("Start")
                    return

                old_ports = [p for p, _ in getattr(self, "time_config", [])]
                new_ports = [p for p, _ in new_cfg]
                if old_ports != new_ports:

                    self.per_port_elapsed.clear()
                    self.last_time_index = 0

                self.time_config = new_cfg

            if self.mode != "wide spectrum" and hasattr(self, "freq_edit"):
                self.update_center_freq()

            try:
                self.stop()
                self.wait()
            except Exception:
                pass

            try:
                self.start()
            except RuntimeError as e:
                print(f"[ERROR] Failed to start flowgraph: {e}")
                self.toggle_btn.setChecked(False)
                self.toggle_btn.setText("Start")
                return

            self.toggle_btn.setText("Stop")

            # Enable interactions everywhere while running
            self._set_interactions(True)

            # Peak buttons are usable while running
            if hasattr(self, "btn_peak_hold"): self.btn_peak_hold.setEnabled(True)

            # Make trace visibility follow current toggle state (don’t reset envelopes)
            if hasattr(self, "pg_peak"): self.pg_peak.setVisible(getattr(self, "btn_peak_hold", None) and self.btn_peak_hold.isChecked())

            if self.mode in ("wide spectrum", "wide spectrum frequency"):
                self.sweep_timer.stop()
                self.sweep_active = True
                self.sweep_ptr = 0
                QTimer.singleShot(100, self.begin_sweep_timer)

            if self.mode == "jamming":
                self.jam_baseline_db = None
                self.jam_started = None
                self.jam_cooldown_until = 0.0
                self.jam_ready_at = time.time() + 1.5
                self.jam_last_switch = time.time()
                self.jam_last_score_at_switch = 0.0 
                self._score_ema = 0.0               
                if hasattr(self, "jam_timer"):
                    self.jam_timer.stop()
                    self.jam_timer.start(100)

            if self.mode == "time":
                num = len(self.time_config)
                # Find current or next port with remaining time
                for _ in range(num):
                    idx = self.last_time_index
                    port, dur = self.time_config[idx]
                    elapsed = self.per_port_elapsed.get(idx, 0.0)
                    remaining = max(0.0, dur - elapsed)
                    if remaining > 0.0:
                        break
                    # finished - reset and advance
                    self.per_port_elapsed[idx] = 0.0
                    self.last_time_index = (self.last_time_index + 1) % num
                else:
                    # all ports finished - start fresh cycle from current index
                    self.per_port_elapsed = {i: 0.0 for i in range(num)}
                    idx = self.last_time_index
                    port, dur = self.time_config[idx]
                    remaining = dur

                self.timer_start_time = time.time()
                print(f"[TIME] Starting at {port} for {remaining:.2f}s (resumed)")
                self.update_antenna(port)
                self.timer.setSingleShot(True)
                self.timer.start(int(remaining * 1000))

        else:
            # STOP branch
            if self.mode == "time" and self.timer_start_time is not None:
                elapsed = time.time() - self.timer_start_time
                self.per_port_elapsed[self.last_time_index] = self.per_port_elapsed.get(self.last_time_index, 0) + elapsed
                print(f"[PAUSE] Elapsed on port {self.last_time_index}: {elapsed:.2f}s")

            try:
                self.stop()
                self.wait()
            except Exception as e:
                print(f"[WARN] Error stopping flowgraph: {e}")

            self.toggle_btn.setText("Start")

            # Disable SDR-driven interactions (QtGUI sinks), BUT allow zoom/pan on the sweep plot
            self._set_interactions(False)
            if hasattr(self, "pg_plot") and self.pg_plot is not None:
                try:
                    vb = self.pg_plot.getViewBox()
                    vb.setMouseEnabled(True, True)  # let user zoom/pan on the frozen plot
                except Exception:
                    pass

            # Peak buttons enabled
            if hasattr(self, "btn_peak_hold"): self.btn_peak_hold.setEnabled(True)

            if self.mode == "time":
                self.timer.stop()
                self.timer_start_time = None

            if self.mode in ("wide spectrum", "wide spectrum frequency"):
                self.sweep_timer.stop()
                self.sweep_active = False

            if self.mode == "jamming" and hasattr(self, "jam_timer"):
                self.jam_timer.stop()

    def perform_time_switch(self):
        if not self.time_config:
            print("[WARN] perform_time_switch(): Empty config, stopping timer and flowgraph.")
            self.timer.stop()
            try:
                self.stop(); self.wait()
            except Exception as e:
                print(f"[WARN] Failed to stop flowgraph: {e}")
            if hasattr(self, "toggle_btn"):
                self.toggle_btn.setChecked(False)
                self.toggle_btn.setText("Start")
            return

        now = time.time()

        if self.timer_start_time is not None:
            elapsed_seg = now - self.timer_start_time
            self.per_port_elapsed[self.last_time_index] = (
                self.per_port_elapsed.get(self.last_time_index, 0.0) + elapsed_seg
            )

            port, dur = self.time_config[self.last_time_index]
            if self.per_port_elapsed.get(self.last_time_index, 0.0) >= (dur - 1e-3):
                self.per_port_elapsed[self.last_time_index] = 0.0

        num = len(self.time_config)
        self.last_time_index = (self.last_time_index + 1) % num

        for _ in range(num):
            idx = self.last_time_index
            port, dur = self.time_config[idx]
            elapsed = self.per_port_elapsed.get(idx, 0.0)
            remaining = max(0.0, dur - elapsed)
            if remaining > 0.0:
                break
            self.per_port_elapsed[idx] = 0.0
            self.last_time_index = (self.last_time_index + 1) % num
        else:
            self.per_port_elapsed = {i: 0.0 for i in range(num)}
            idx = self.last_time_index
            port, dur = self.time_config[idx]
            remaining = dur

        self.timer_start_time = time.time()
        print(f"[TIME] Switching to {port} for {remaining:.3f}s")
        self.update_antenna(port)
        self.timer.setSingleShot(True)
        self.timer.start(int(remaining * 1000))

    #  Sweep logic
    def begin_sweep_timer(self):
        if self.mode not in ("wide spectrum", "wide spectrum frequency"):
            return
        try:
            self.sweep_timer.timeout.disconnect(self.update_sweep_freq)
        except Exception:
            pass
        self._sweep_settle_ms = 85
        self.sweep_timer.timeout.connect(self.update_sweep_freq)
        self.sweep_timer.start(30)

    def _update_sweep_xrange(self):
        if not hasattr(self, "pg_plot") or self.pg_plot is None:
            return

        s = getattr(self, "last_valid_sweep_start", 80e6)
        e = getattr(self, "last_valid_sweep_end", 100e6)

        # ~0.2% of span, min 100 kHz padding on each side
        span = max(1.0, e - s)
        eps  = max(1e5, 0.005 * span) 
        self._sweep_eps = eps

        lo = (s - eps) / 1e6
        hi = (e + eps) / 1e6

        self.pg_plot.setXRange(lo, hi, padding=0)

        vb = self.pg_plot.getViewBox()
        vb.setLimits(
            xMin=lo, xMax=hi,
            minXRange=(e - s) * 0.05 / 1e6,      # don't zoom in past 5% of span
            maxXRange=(e - s + 2*eps) / 1e6      # don't zoom out past padded span
        )

    def update_sweep_freq(self):
        if self.mode not in ("wide spectrum", "wide spectrum frequency") or not self.sweep_active or self.sweep_busy or getattr(self, "_rebuilding", False):
            return
        cf_list = getattr(self, "center_freqs", None)
        if not cf_list:
            return

        self.sweep_busy = True  
        try:
            if self.sweep_ptr >= len(cf_list):
                self.sweep_ptr = 0

            cf_hz = float(cf_list[self.sweep_ptr])
            self._step_freq_hz = cf_hz
            self._cap_retries = 0

            try:
                tuned = float(self.src.get_center_freq(0))
            except Exception:
                tuned = None
            if tuned is None or abs(tuned - cf_hz) > 1.0:
                try:
                    self.src.set_center_freq(cf_hz)
                except Exception as e:
                    print(f"[WARN] set_center_freq({cf_hz/1e6:.3f} MHz) failed: {e}")
                    QTimer.singleShot(int(getattr(self, "_sweep_settle_ms", 85)), self.capture_fft)
                    return

            idx   = self.sweep_ptr + 1
            total = len(cf_list)
            if self.mode == "wide spectrum frequency":
                port = getattr(self, "selected_port", self.antenna_select)
                print(f"[STEP] ({port}) {idx}/{total} @ {cf_hz/1e6:.3f} MHz")
            else:
                print(f"[STEP] {idx}/{total} @ {cf_hz/1e6:.3f} MHz")

            QTimer.singleShot(int(getattr(self, "_sweep_settle_ms", 85)), self.capture_fft)
        finally:
            pass

    def capture_fft(self):
        # Guards
        if self.mode not in ("wide spectrum", "wide spectrum frequency") or not self.sweep_active or getattr(self, "_rebuilding", False):
            self.sweep_busy = False
            return
        if getattr(self, "probe", None) is None or not hasattr(self, "center_freqs") or not self.center_freqs:
            self.sweep_busy = False
            return

        try:
            # Make sure update_sweep_freq() set the step
            step = getattr(self, "_step_freq_hz", None)
            if step is None:
                self._cap_retries = 0
                self.sweep_busy = False
                return

            # Verify tuner already at that step (allow a few retries)
            try:
                tuned = float(self.src.get_center_freq(0))
            except Exception:
                tuned = None
            if (tuned is not None) and (abs(tuned - float(step)) > 1.0):
                if self._cap_retries < 3:
                    self._cap_retries += 1
                    Qt.QTimer.singleShot(8, self.capture_fft)
                    return
                self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
                self._cap_retries = 0
                return

            # Read FFT magnitudes from the probe
            try:
                raw = self.probe.level()
            except Exception:
                raw = None
            mags = np.asarray(raw, dtype=np.float32) if raw is not None else None
            if mags is None or mags.ndim != 1 or mags.size != self.fft_size or not np.isfinite(mags).all():
                if self._cap_retries < 3:
                    self._cap_retries += 1
                    Qt.QTimer.singleShot(8, self.capture_fft)
                    return
                self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
                self._cap_retries = 0
                return

            # Re-check tuner AFTER capture; discard if LO moved late
            try:
                tuned_after = float(self.src.get_center_freq(0))
            except Exception:
                tuned_after = None
            if (tuned_after is None) or (abs(tuned_after - float(step)) > 1.0):
                if self._cap_retries < 3:
                    self._cap_retries += 1
                    Qt.QTimer.singleShot(8, self.capture_fft)
                    return
                self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
                self._cap_retries = 0
                return

            # Stale-vector guard: identical to previous capture → retry
            if hasattr(self, "last_fft") and (self.last_fft is not None):
                if np.allclose(mags, self.last_fft, rtol=0.0, atol=1e-8):
                    if self._cap_retries < 3:
                        self._cap_retries += 1
                        Qt.QTimer.singleShot(8, self.capture_fft)
                        return
                    # If it keeps repeating, skip this step once
                    self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
                    self._cap_retries = 0
                    return
            self.last_fft = mags.copy()

            # Scale like QtGUI sink
            mags *= (1.0 / float(self.fft_size))

            # DC notch (center ±2 bins, filled with neighbor mean)
            c = self.fft_size // 2
            k = 2
            if self.fft_size >= 16:
                loL, loR = max(0, c - (k + 3)), max(0, c - (k + 1))
                hiL, hiR = min(self.fft_size, c + (k + 1)), min(self.fft_size, c + (k + 3))
                neigh = []
                if loL < loR: neigh.extend(mags[loL:loR])
                if hiL < hiR: neigh.extend(mags[hiL:hiR])
                if len(neigh) >= 2:
                    mags[max(0, c - k):min(self.fft_size, c + k + 1)] = float(np.mean(neigh))

            # Ensure sweep buffer
            expected = len(self.center_freqs) * self.fft_size
            if self.sweep_buffer.size != expected:
                self.sweep_buffer = np.zeros(expected, dtype=np.float32)

            # Write this step’s segment
            s = self.sweep_ptr * self.fft_size
            e = s + self.fft_size
            self.sweep_buffer[s:e] = mags

            # Build the real X/Y that lie inside [start, end] (no linspace)
            start = self.last_valid_sweep_start
            end   = self.last_valid_sweep_end
            mask  = (self.freq_axis >= start) & (self.freq_axis <= end)
            idx   = np.flatnonzero(mask)
            if idx.size == 0:
                self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
                self._cap_retries = 0
                self.sweep_busy = False
                return

            # Downsample to at most 1024 points for plotting
            max_bins = 1024
            stride = int(np.ceil(idx.size / max_bins))
            sel = idx[::stride]

            y_lin = self.sweep_buffer[sel]
            x_hz  = self.freq_axis[sel]

            # Averaging 
            a = float(np.clip(getattr(self, "sweep_avg_alpha", 0.20), 0.0, 1.0))
            prev = getattr(self, "sweep_prev_linear", None)
            if (prev is None) or (getattr(prev, "size", 0) != y_lin.size):
                prev = y_lin.copy()
            sm_lin = (1.0 - a) * prev + a * y_lin
            self.sweep_prev_linear = sm_lin

            # To dB
            db = 20.0 * np.log10(np.clip(sm_lin, 1e-12, 1e9)) + float(getattr(self, "sweep_db_cal", 0.0))
            db = np.maximum(db, -140.0)

            # Plot at true frequencies
            self.pg_curve.setData(x_hz / 1e6, db)

            # Peak-hold overlay (same X)
            if getattr(self, "peak_enabled", False):
                if not hasattr(self, "peak_env") or self.peak_env is None or self.peak_env.size != db.size:
                    self.peak_env = db.copy()
                else:
                    self.peak_env = np.maximum(self.peak_env, db)
                self.pg_peak.setData(x_hz / 1e6, self.peak_env)
                self.pg_peak.setVisible(True)
            else:
                self.pg_peak.setVisible(False)

            self._update_sweep_xrange()

            # Advance to next center
            self.sweep_ptr = (self.sweep_ptr + 1) % len(self.center_freqs)
            self._cap_retries = 0

        except Exception as e:
            print(f"[ERROR] capture_fft failed: {e}")
        finally:
            self.sweep_busy = False

    def validate_sweep_inputs(self, *, quiet=False):
        try:
            sweep_start = parse_freq_input(self.sweep_start_edit.text())
            sweep_end   = parse_freq_input(self.sweep_end_edit.text())

            # Clamp hard limits (1 Hz .. 4 GHz) with inline hints
            if sweep_start <= 0:
                sweep_start = 1.0
                self.sweep_start_edit.setText("1")
                self.show_temporary_label("sweep_error_label", "Sweep Start set to 1 Hz")
            elif sweep_start > 4e9:
                sweep_start = 4e9
                self.sweep_start_edit.setText("4e9")
                self.show_temporary_label("sweep_error_label", "Sweep Start set to 4 GHz")

            if sweep_end <= 0:
                sweep_end = 1.0
                self.sweep_end_edit.setText("1")
                self.show_temporary_label("sweep_error_label", "Sweep End set to 1 Hz")
            elif sweep_end > 4e9:
                sweep_end = 4e9
                self.sweep_end_edit.setText("4e9")
                self.show_temporary_label("sweep_error_label", "Sweep End set to 4 GHz")

            if sweep_end <= sweep_start:
                msg = (f"Sweep End ({sweep_end/1e6:.2f} MHz) must be greater than "
                    f"Sweep Start ({sweep_start/1e6:.2f} MHz)")
                self.show_temporary_label("sweep_error_label", msg)

                # Safe fallback so downstream plot code doesn't crash
                self.total_bins = self.fft_size
                self.freq_axis = np.linspace(0, self.samp_rate, self.fft_size)
                self.sweep_buffer = np.zeros(self.total_bins, dtype=np.float32)

                # If we were running, stop the sweep safely
                if hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
                    try:
                        self.stop(); self.wait()
                        if hasattr(self, "sweep_timer"):
                            self.sweep_timer.stop()
                    except Exception as e:
                        print(f"[WARN] Error stopping flowgraph: {e}")
                    self.toggle_btn.setChecked(False)
                    self.toggle_btn.setText("Start")
                return False

            # Save last valid range for the plot's x-range helper
            self.last_valid_sweep_start = sweep_start
            self.last_valid_sweep_end   = sweep_end

            # Plan steps: one FFT window per sample-rate chunk
            step = float(self.samp_rate)
            if step <= 0.0:
                raise ValueError("Sample rate must be > 0 for sweep planning.")
            ratio = (sweep_end - sweep_start) / step
            num_steps = int(np.ceil(ratio))
            if num_steps < 1:
                raise ValueError("Sweep range too narrow for current sample rate.")

            # Round the ends a bit to tame text edits that don't change the plan materially.
            sig = (round(sweep_start, 1), round(sweep_end, 1), int(round(step)), int(self.fft_size), int(num_steps))
            same_as_last = (getattr(self, "_last_sweep_sig", None) == sig)
            self._last_sweep_sig = sig

            # Build the center frequencies for each step
            self.center_freqs = [sweep_start + (i + 0.5) * step for i in range(num_steps)]
            self.total_steps  = len(self.center_freqs)
            self.total_bins   = self.total_steps * self.fft_size

            # Frequency axis for the stitched spectrum (use baseband bins around each CF)
            bin_freqs = np.linspace(-step / 2.0, step / 2.0, self.fft_size, endpoint=False)
            self.freq_axis = np.concatenate([bin_freqs + cf for cf in self.center_freqs])

            # (Re)allocate sweep buffer + reset state tied to size/plan
            if self.sweep_buffer.size != self.total_bins:
                self.sweep_buffer = np.zeros(self.total_bins, dtype=np.float32)
            else:
                self.sweep_buffer.fill(0.0)

            self.sweep_prev_linear = None
            self.sweep_ptr = 0
            self.peak_env = None
            
            if hasattr(self, "_peak_scatter"):
                try: self._peak_scatter.clear()
                except Exception: pass

            # Update X limits of the pyqtgraph plot to the new span
            self._update_sweep_xrange()

            if not quiet and not same_as_last:
                print(f"[SWEEP] Configured: {self.total_steps} steps × {self.fft_size} bins = {self.total_bins} total bins")

            return True

        except Exception as e:
            # Parsing/error fallbacks
            print(f"[ERROR] Failed to parse sweep inputs: {e}")
            self.show_temporary_label("sweep_error_label", "[ERROR] Invalid sweep inputs")

            self.total_bins = self.fft_size
            self.freq_axis = np.linspace(0, self.samp_rate, self.fft_size)
            self.sweep_buffer = np.zeros(self.total_bins, dtype=np.float32)

            if hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
                try:
                    self.stop(); self.wait()
                except Exception as ee:
                    print(f"[WARN] Error stopping flowgraph: {ee}")
                self.toggle_btn.setChecked(False)
                self.toggle_btn.setText("Start")
            return False

    #  Jamming detection
    def check_jamming(self):
        if getattr(self, "_rebuilding", False):
            return
        if self.mode != "jamming" or not self.toggle_btn.isChecked():
            return
        try:
            try:
                raw = self.probe.level()
            except Exception:
                return

            mags = np.asarray(raw, dtype=np.float32) if raw is not None else None
            if mags is None or mags.size != self.fft_size or not np.isfinite(mags).all():
                return

            mags *= (1.0 / float(self.fft_size))
            power_db = 20.0 * np.log10(np.clip(mags, 1e-12, 1e9))

            now = time.time()
            if hasattr(self, "jam_ignore_until") and now < self.jam_ignore_until:
                return

            # baseline update with FREEZE window
            freeze_until = getattr(self, "_baseline_freeze_until", 0.0)
            if self.jam_baseline_db is None:
                self.jam_baseline_db = power_db.copy()
                return
            if now >= freeze_until:  
                self.jam_baseline_db = (1.0 - self.jam_alpha) * self.jam_baseline_db + self.jam_alpha * power_db
            if now < self.jam_ready_at:
                return

            m = self.jam_mask
            if m is None or m.size == 0 or not np.any(m):
                return

            masked_curr = power_db[m]
            masked_base = self.jam_baseline_db[m]
            Nmask = int(masked_curr.size)

            base_med = float(np.median(masked_base))
            curr_med = float(np.median(masked_curr))
            med_lift = curr_med - base_med

            # protect against huge AGC/step changes
            if med_lift > 8.0:
                self.jam_baseline_db = power_db.copy()
                self.jam_ignore_until = now + 0.8
                return

            # spectral flatness
            p_lin = 10.0 ** (masked_curr / 10.0)
            sfm_db = spectral_flatness_db(p_lin)
            if self.sfm_ref is None:
                self.sfm_ref = sfm_db
            else:
                self.sfm_ref = 0.95 * self.sfm_ref + 0.05 * sfm_db
            sfm_gate = max(self.jam_sfm_min_db, self.sfm_ref + getattr(self, "_sfm_offset", 0.8))

            # occupancy vs baseline
            over = (masked_curr - masked_base) >= self.jam_thresh_db
            occ  = float(np.sum(over)) / max(1, Nmask)

            # scale occ requirement + mild SFM relief
            scale   = (256.0 / max(64.0, float(Nmask))) ** 0.5
            occ_req = max(0.10, min(0.45, self.jam_occ_min * scale))
            if   sfm_db >= -3.5: occ_req = max(0.10, 0.85 * occ_req)
            elif sfm_db >= -4.0: occ_req = max(0.12, 0.92 * occ_req)

            # smooth the printed/used occ requirement so it doesn't flicker
            if not hasattr(self, "_occ_req_ema") or self._occ_req_ema is None:
                self._occ_req_ema = occ_req
            else:
                self._occ_req_ema = 0.6 * self._occ_req_ema + 0.4 * occ_req
            occ_req_eff = float(np.clip(self._occ_req_ema, 0.10, 0.45))

            # band/mean power lift
            eps = 1e-12
            band_lift = 10.0 * np.log10(
                (np.mean(10.0**(masked_curr/10.0)) + eps) /
                (np.mean(10.0**(masked_base/10.0)) + eps)
            )

            # longest contiguous run
            over_i = over.astype(np.int8)
            edges  = np.diff(np.pad(over_i, (1, 1)))
            starts = np.where(edges == +1)[0]
            ends   = np.where(edges == -1)[0]
            run_lengths = (ends - starts) if (starts.size and ends.size) else np.array([], dtype=int)
            max_run     = int(run_lengths.max()) if run_lengths.size else 0
            span_frac   = max_run / float(Nmask)

            # ANTI-FLAP: hysteresis + min bin count + baseline freeze
            band_on = getattr(self, "jam_band_db", 1.5)
            med_on  = getattr(self, "jam_median_db", 2.0)
            band_off = max(0.5, band_on - 0.8)
            med_off  = max(0.5, med_on  - 0.8)

            band_state = getattr(self, "_jam_band_state", False)
            med_state  = getattr(self, "_jam_med_state",  False)
            band_ok = (band_lift >= (band_on if not band_state else band_off))
            med_ok  = (med_lift  >= (med_on  if not med_state  else med_off))
            self._jam_band_state = band_ok
            self._jam_med_state  = med_ok

            # minimum absolute number of "over" bins
            min_bins = max(6, int(0.006 * Nmask))
            enough_bins = (int(np.sum(over)) >= min_bins)

            # pre-condition to freeze baseline briefly (prevents EMA chasing)
            pre = (occ >= 0.8 * occ_req_eff) and (band_lift >= 0.8 * band_on or med_lift >= 0.8 * med_on or span_frac >= 0.8 * self.jam_span_min)
            if pre and now >= freeze_until:
                self._baseline_freeze_until = now + 0.6  # ~6 ticks if jam_timer=100 ms

            # decision with anti-flap guards
            cond_wideband  = enough_bins and (occ >= occ_req_eff) and ((sfm_db >= sfm_gate) or band_ok)
            cond_multipeak = enough_bins and (occ >= self.jam_occ_multipeak) and (span_frac >= self.jam_span_min)
            cond_median    = med_ok
            cond = cond_wideband or cond_multipeak or cond_median

            # optional score + stickiness to avoid ping-pong
            score = 0.6 * (occ / max(occ_req_eff, 1e-6)) + 0.25 * max(0.0, band_lift / max(band_on, 1e-6)) + 0.15 * max(0.0, med_lift / max(med_on, 1e-6))
            if not hasattr(self, "_score_ema"):
                self._score_ema = score
            else:
                self._score_ema = 0.8 * self._score_ema + 0.2 * score

            age   = now - getattr(self, "jam_last_switch", 0.0)
            decay = 0.5 ** max(0.0, age / 8.0)
            last  = getattr(self, "_last_score_at_switch", 0.0) * decay

            need  = max(1.10, last + 0.10)  
            improves = (self._score_ema >= need)

            if getattr(self, "jam_debug", False) and (now - getattr(self, "_jam_last_dbg", 0.0)) > 0.5:
                print(f"[JDBG] ready={now >= self.jam_ready_at} occ={occ:.2f}/{occ_req_eff:.2f} "
                    f"sfm={sfm_db:.1f}/{sfm_gate:.1f}dB span={span_frac:.3f} "
                    f"band+{band_lift:.1f}dB med+{med_lift:.1f}dB bins={np.sum(over)}/{Nmask} "
                    f"bandHys={band_ok} medHys={med_ok}")
                self._jam_last_dbg = now

            # hold / cooldown / dwell gating + switch (with 'improves' stickiness)
            if cond and improves:
                if self.jam_started is None:
                    self.jam_started = now

                held        = (now - self.jam_started) >= self.jam_hold_s
                dwell_ok    = (now - getattr(self, "jam_last_switch", 0.0)) >= 0.6
                cooldown_ok = now >= self.jam_cooldown_until

                if held and dwell_ok and cooldown_ok:
                    # visual alert
                    self.jam_alert_label.show()
                    QTimer.singleShot(3000, self.jam_alert_label.hide)

                    # rotate port
                    self.current_port_index = (self.current_port_index + 1) % len(self.ports)
                    new_port = self.ports[self.current_port_index]
                    if self.antenna_edit is not None:
                        self.antenna_edit.setText(new_port)
                    self.update_antenna()

                    # reset / re-arm
                    self.jam_last_switch     = now
                    self.jam_cooldown_until  = now + self.jam_cooldown_s
                    self.jam_started         = None
                    self.jam_ready_at        = now + 0.8
                    self._baseline_freeze_until = now + 0.6  # keep baseline steady right after switch
                    self.jam_baseline_db     = power_db.copy()
                    self.sfm_ref             = sfm_db
                    self._last_score_at_switch = self._score_ema
                    self._jam_last_dbg       = now
            else:
                self.jam_started = None

        except Exception as e:
            print(f"[ERROR] check_jamming: {e}")

    #  UI helpers / misc
    def _hide_label(self, label_attr: str):
        label = getattr(self, label_attr, None)
        if isinstance(label, Qt.QLabel):
            label.hide()
            label.setText("")
            if hasattr(self, "_label_timers") and label in self._label_timers:
                try:
                    self._label_timers[label].stop()
                    del self._label_timers[label]
                except Exception:
                    pass
                
    def show_temporary_label(self, label_attr, message=""):
        if not hasattr(self, "_label_timers"):
            self._label_timers = {}

        label = getattr(self, label_attr, None)
        if not isinstance(label, Qt.QLabel):
            return

        if message:
            label.setText(message)
        label.show()

        if label in self._label_timers:
            self._label_timers[label].stop()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(label.hide)
        timer.start(5000)
        self._label_timers[label] = timer

    def _reinit_jamming_dsp_chain(self):
        # build fresh blocks
        self.stream_to_vector = blocks.stream_to_vector(gr.sizeof_gr_complex, self.fft_size)
        self.fft = fft_vcc(self.fft_size, True, window.blackmanharris(self.fft_size), True)
        self.c2mag = blocks.complex_to_mag(self.fft_size)
        self.probe = blocks.probe_signal_vf(self.fft_size)

        # wire them up
        self.connect((self.src, 0), (self.stream_to_vector, 0))
        self.connect((self.stream_to_vector, 0), (self.fft, 0))
        self.connect((self.fft, 0), (self.c2mag, 0))
        self.connect((self.c2mag, 0), (self.probe, 0))

        # refresh mask + detection state
        self._rebuild_jam_mask()
        self.jam_baseline_db = None
        self.jam_started = None
        self.jam_cooldown_until = 0.0
        self.jam_ready_at = time.time() + 1.5
        self._jam_last_dbg = 0.0

    def refresh_time_config(self):
        self.time_config = []
        for port_input, dur_input in self.time_inputs:
            try:
                port = port_input.currentText()
                dur = float(dur_input.text().strip())
                if port and dur > 0:
                    self.time_config.append((port, dur))
            except:
                print("[WARN] Invalid entry skipped")

        if not self.time_config:
            print("[ERROR] No valid port/duration entries. Time switching halted.")
            self.timer.stop()
            try:
                self.stop()
                self.wait()
            except Exception as e:
                print(f"[WARN] Flowgraph stop failed: {e}")
            if hasattr(self, "toggle_btn"):
                self.toggle_btn.setChecked(False)
                self.toggle_btn.setText("Start")
            return False

        if self.last_time_index >= len(self.time_config):
            self.last_time_index = 0
        return True

    def refresh_freq_config(self):
        valid = True
        error_text = ""
        try:
            ranges = []
            for port, line in self.port_inputs.items():
                text = line.text().strip()
                parts = text.replace(" ", "").split(":")
                if len(parts) != 2:
                    raise ValueError(f"Invalid format in {port}: '{text}'")
                low = float(parts[0])
                high = float(parts[1])
                if low >= high:
                    raise ValueError(f"Invalid range in {port}: '{text}' — Lower must be < Upper")
                if high > 4000:
                    raise ValueError(f"Invalid range in {port}: '{text}' — Upper limit exceeds 4000 MHz")
                ranges.append((low, high, port))

            ranges.sort()
            for i in range(len(ranges) - 1):
                if ranges[i][1] >= ranges[i+1][0]:
                    raise ValueError(f"Overlapping or touching ranges: {ranges[i][2]} ends at {ranges[i][1]}, "
                                     f"{ranges[i+1][2]} starts at {ranges[i+1][0]}")
        except ValueError as e:
            error_text = str(e)
            valid = False

        if not valid:
            print(error_text)
            self.show_temporary_label("port_error_label", error_text)
            if self.toggle_btn.isChecked():
                print("[STOP] Flowgraph halted due to invalid frequency range.")
                try:
                    self.stop()
                    self.wait()
                except Exception as e:
                    print(f"[WARN] Error stopping flowgraph: {e}")
                self.toggle_btn.setChecked(False)
                self.toggle_btn.setText("Start")
        else:
            self.port_error_label.hide()

    def _rebuild_jam_mask(self):
        self.jam_center_k = max(2, self.fft_size // 256)   # scales with N
        c = self.fft_size // 2
        m = np.ones(self.fft_size, dtype=bool)
        m[max(0, c - self.jam_center_k): min(self.fft_size, c + self.jam_center_k + 1)] = False
        m[:2] = False
        m[-2:] = False
        self.jam_mask = m
        if getattr(self, "jam_debug", False):
            print(f"[JDBG] jam mask: center ±{self.jam_center_k} bins, edges 2 bins")

    def _stop_stream_safely(self):
        if hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
            try:
                self.stop()
                self.wait()
            except Exception as e:
                print(f"[WARN] Error stopping flowgraph: {e}")
            # stop any timers tied to modes
            if self.mode == "time"   and hasattr(self, "timer"):        self.timer.stop()
            if self.mode == "wide spectrum"  and hasattr(self, "sweep_timer"):  self.sweep_timer.stop(); self.sweep_active = False
            if self.mode == "jamming" and hasattr(self, "jam_timer"):   self.jam_timer.stop()
            self.toggle_btn.setChecked(False)
            self.toggle_btn.setText("Start")

    def _input_error(self, label_attr: str, message: str, field: Qt.QLineEdit = None):
        print(f"[ERROR] {message}")
        self.show_temporary_label(label_attr, message)
        if field is not None:
            field.setStyleSheet("border: 2px solid #d00;")
            QTimer.singleShot(1800, lambda: field.setStyleSheet(""))
        self._stop_stream_safely()
    
    def _select_port_and_sweep(self, port: str, *, start_immediately: bool = True, quiet: bool = True):
        try:
            sel = str(port).upper()
            if sel not in self.port_inputs:
                raise KeyError(f"Unknown port '{sel}'")

            txt = self.port_inputs[sel].text().strip().replace(" ", "")
            if ":" not in txt:
                raise ValueError("expected format 'low:high' in MHz")
            lo_s, hi_s = txt.split(":", 1)
            lo_mhz = float(lo_s)
            hi_mhz = float(hi_s)
            if not np.isfinite(lo_mhz) or not np.isfinite(hi_mhz):
                raise ValueError("bounds must be finite numbers")
            if hi_mhz <= lo_mhz:
                raise ValueError("upper bound must be > lower bound")

            # Remember selection
            self.selected_port = sel

            # Update UI footer (OperaCake panel), regardless of SDR build state
            if hasattr(self, "oc_panel"):
                try:
                    self.oc_panel.set_active(sel, fixed_input="A0", connected=self.is_hackrf_connected())
                except Exception:
                    pass

            # Switch hardware path ONLY if SDR already exists; stay silent otherwise
            if hasattr(self, "src") and self.src is not None:
                try:
                    self.antenna_select = sel
                    self.src.set_antenna(sel, 0)
                    self.antenna_changed.emit(sel)
                except Exception:
                    pass
            else:
                self.antenna_select = sel

            # Feed the hidden sweep inputs so validate_sweep_inputs() works unchanged
            self.sweep_start_edit.setText(f"{lo_mhz}e6")
            self.sweep_end_edit.setText(f"{hi_mhz}e6")

            # Recompute sweep plan (quiet to avoid duplicate
            ok = self.validate_sweep_inputs(quiet=bool(quiet))
            if not ok:
                return

            # If already running, (re)start the sweep timer after a small settle
            if start_immediately and hasattr(self, "toggle_btn") and self.toggle_btn.isChecked():
                try:
                    self.sweep_timer.stop()
                except Exception:
                    pass
                self.sweep_ptr = 0
                self.sweep_active = True
                # Let RF switch + tuner settle a bit
                QTimer.singleShot(int(getattr(self, "_sweep_settle_ms",85)), self.begin_sweep_timer)

        except Exception as e:
            self.show_temporary_label(
                "sweep_error_label",
                f"Invalid range for {port}: '{self.port_inputs.get(port, Qt.QLineEdit()).text()}' — {e}"
            )
            return

    def _validate_all_inputs(self):
        bad = False

        if hasattr(self, "freq_edit"):
            try:
                _ = parse_freq_input(self.freq_edit.text())
                self.freq_edit.setStyleSheet("")
            except ValueError as e:
                bad = True
                self.freq_edit.setStyleSheet("border: 2px solid #d00;")
                print(f"[ERROR] {e}")

        try:
            _ = parse_freq_input(self.samp_rate_edit.text())
            self.samp_rate_edit.setStyleSheet("")
        except ValueError as e:
            bad = True
            self.samp_rate_edit.setStyleSheet("border: 2px solid #d00;")
            print(f"[ERROR] {e}")

        for edit in [self.rf_gain_edit, self.if_gain_edit, self.bb_gain_edit]:
            if edit.text() == "" or not edit.hasAcceptableInput():
                bad = True
                edit.setStyleSheet("border: 2px solid #d00;")

        if self.mode == "frequency" and not self.validate_port_ranges():
            bad = True

        if self.mode in ("wide spectrum","wide spectrum frequency") and not self.validate_sweep_inputs():
            bad = True

        return not bad
    
    def is_hackrf_connected(self) -> bool:
        try:
            r = subprocess.run(["hackrf_info"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return b"Found HackRF" in r.stdout
        except Exception:
            return False

    def _ensure_mouse_blocker(self):
        # Create once
        if not hasattr(self, "_mouse_blocker"):
            self._interactions_enabled = False
            self._mouse_blocker = _MouseBlocker(self)
        # (Re)install on currently built widgets
        for wname in ("_freq_win", "_waterfall_win"):
            w = getattr(self, wname, None)
            if isinstance(w, Qt.QWidget):
                w.installEventFilter(self._mouse_blocker)
        if hasattr(self, "pg_plot") and self.pg_plot is not None:
            self.pg_plot.installEventFilter(self._mouse_blocker)
            # For PyQtGraph also hard-disable zoom/pan until enabled
            vb = self.pg_plot.getViewBox()
            vb.setMouseEnabled(False, False)
            vb.setMenuEnabled(False)

    def _set_interactions(self, enabled: bool):
        self._interactions_enabled = bool(enabled)
        # Re-toggle pyqtgraph ViewBox mouse
        if hasattr(self, "pg_plot") and self.pg_plot is not None:
            vb = self.pg_plot.getViewBox()
            vb.setMouseEnabled(enabled, enabled)

    def apply_sdr_config(self):
        self.src.set_sample_rate(self.samp_rate)
        self.src.set_center_freq(self.center_freq)
        self.src.set_gain(self.rf_gain, 0)
        self.src.set_if_gain(self.if_gain, 0)
        self.src.set_bb_gain(self.bb_gain, 0)
        self.src.set_antenna(self.antenna_select, 0)  
