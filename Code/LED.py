from PyQt5.QtWidgets import QWidget, QLabel, QSizePolicy, QToolTip
from PyQt5.QtGui import QPixmap, QPainter, QBrush, QPen, QColor, QCursor
from PyQt5.QtCore import Qt, QRectF
from enum import Enum

class HoverLabel(QLabel):
    # Custom QLabel that shows a tooltip on hover
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WA_Hover, True)

    def enterEvent(self, e):
        tip = self.toolTip()
        if tip:
            QToolTip.showText(QCursor.pos(), tip, self)
        super().enterEvent(e)

class PortState(Enum):
    # Defines the possible states for a port's LED indicator
    ACTIVE = 1
    INACTIVE = 2
    UNAVAILABLE = 3
    DISCONNECTED = 4

class _LedDot(QWidget):
    # A custom QWidget that draws a colored circle (LED)
    def __init__(self, diameter=16, on=False, color=Qt.gray, parent=None):
        super().__init__(parent)
        self._on = on
        self._diam = diameter
        self._color = QColor(color)
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WA_Hover, True)

    def set_on(self, on: bool, color=None):
        # Sets the LED's state (on/off) and color
        if color is not None:
            self._color = QColor(color)
        if self._on != on or color is not None:
            self._on = bool(on)
            self.update()

    def enterEvent(self, e):
        tip = self.toolTip()
        if tip:
            QToolTip.showText(QCursor.pos(), tip, self)
        super().enterEvent(e)

    def paintEvent(self, _):
        # Draws the colored ellipse for the LED
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = self._color if self._on else Qt.gray
        p.setPen(QPen(Qt.black, 1))
        p.setBrush(QBrush(col))
        r = QRectF(1, 1, self._diam - 2, self._diam - 2)
        p.drawEllipse(r)
        p.end()

class OperaCakePanel(QWidget):
    # The main widget that displays the OperaCake panel with LEDs and port labels
    def __init__(self, image_path: str, parent=None, label_color: str = "black"):
        super().__init__(parent)
        self._pix = QPixmap(image_path)
        if self._pix.isNull():
            # Fallback background if the image is not found
            self._pix = QPixmap(900, 523)
            self._pix.fill(Qt.darkGreen)

        self._labels = {}
        self._leds   = {}
        self._states = {}
        self._img_size = self._pix.size()
        self._img_pos = (0, 0)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(160)
        self._label_css = f"color: {label_color}; font-weight: bold;"
        self._active_port = None
        self._build_overlay()
        self._init_tooltips()

    def _build_overlay(self):
        # Creates the LED and label widgets for each port
        y_positions = [0.20, 0.34, 0.48, 0.62, 0.75]
        left_x, right_x = 0.04, 0.96

        # A side (left)
        for i, y in enumerate(y_positions):
            port = f"A{4 - i}"
            self._add_indicator(port, left_x, y, align=Qt.AlignLeft)

        # B side (right)
        for i, y in enumerate(y_positions):
            port = f"B{4 - i}"
            self._add_indicator(port, right_x, y, align=Qt.AlignRight)

    def _init_tooltips(self):
        # Sets the initial state and tooltips for all ports
        for port in self._leds:
            self._set_port_state(port, PortState.INACTIVE, fixed_input="A0", connected=False)

    def sizeHint(self):
        return self._pix.size()

    def minimumSizeHint(self):
        return self._pix.size() * 0.5

    def _add_indicator(self, port, x_rel, y_rel, align):
        # Helper function to create and add an LED and label to the panel
        led = _LedDot(diameter=14, on=False, parent=self)
        lbl = HoverLabel(port, self)
        lbl.setStyleSheet(self._label_css)
        lbl.adjustSize()
        self._leds[port] = led
        self._labels[port] = lbl
        led._rel = (x_rel, y_rel, align)
        lbl._rel = (x_rel, y_rel, align)
        led.raise_(); lbl.raise_()

    def resizeEvent(self, ev):
        # Relayouts the panel elements on window resize
        self._relayout()

    def _relayout(self):
        # Recalculates and repositions LEDs and labels based on the new window size
        w, h = self.width(), self.height()
        img = self._pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._img_size = img.size()
        self._img_pos  = ((w - img.width()) // 2, (h - img.height()) // 2)

        for port, led in self._leds.items():
            x_rel, y_rel, align = led._rel
            x = self._img_pos[0] + int(x_rel * self._img_size.width())
            y = self._img_pos[1] + int(y_rel * self._img_size.height())

            if align == Qt.AlignLeft:
                x -= led.width() + 6
            else:
                x += 6
            led.move(x, y - led.height() // 2)

            lbl = self._labels[port]
            lbl.adjustSize()
            if align == Qt.AlignLeft:
                lbl.move(x - lbl.width() - 4, y - lbl.height() // 2)
            else:
                lbl.move(x + led.width() + 4, y - lbl.height() // 2)

        self.update()

    def paintEvent(self, ev):
        # Renders the background image for the panel
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        img = self._pix.scaled(self.width(), self.height(),
                               Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x, y = (self.width() - img.width()) // 2, (self.height() - img.height()) // 2
        p.drawPixmap(x, y, img)
        p.end()

    def show_disconnected(self, red_input: str = "A0"):
        # Changes the visual style of labels to indicate a disconnected state
        GREY_BG, GREY_FG, GREY_BORD = "#5a5a5a", "#dddddd", "#767676"
        RED_BG,  RED_FG,  RED_BORD  = "#c21d1d", "#ffffff", "#8a1111"

        for name, lbl in self._labels.items():
            if name.upper() == red_input.upper():
                lbl.setStyleSheet(
                    f"QLabel {{ background-color:{RED_BG}; color:{RED_FG}; "
                    f"border:2px solid {RED_BORD}; border-radius:10px; padding:2px 6px; }}"
                )
            else:
                lbl.setStyleSheet(
                    f"QLabel {{ background-color:{GREY_BG}; color:{GREY_FG}; "
                    f"border:2px solid {GREY_BORD}; border-radius:10px; padding:2px 6px; }}"
                )

    def set_active(self, port: str, connected: bool, fixed_input: str = "A0"):
        # Sets the active state for the LEDs and labels based on the selected port
        port = (port or "").upper()
        fixed_input = (fixed_input or "A0").upper()
        self._active_port = port if connected else None
        
        for p in self._leds.keys():
            self._set_port_state(p, PortState.INACTIVE, fixed_input, connected)

        if fixed_input in self._leds:
            self._set_port_state(fixed_input,
                                 PortState.ACTIVE if connected else PortState.DISCONNECTED,
                                 fixed_input,
                                 connected)

        if not connected:
            return

        if "B0" in self._leds:
            self._set_port_state("B0", PortState.UNAVAILABLE, fixed_input, connected)

        if len(port) == 2 and port[0] in ("A", "B") and port[1].isdigit():
            idx = int(port[1])
            if 0 <= idx <= 4 and port in self._leds:
                self._set_port_state(port, PortState.ACTIVE, fixed_input, connected)

                other_side = "B" if port[0] == "A" else "A"
                paired = f"{other_side}{idx}"
                if paired in self._leds:
                    self._set_port_state(paired, PortState.UNAVAILABLE, fixed_input, connected)

    def _set_port_state(self, port: str, state: PortState, fixed_input: str, connected: bool):
        # Applies the color, on/off state, and tooltip for a single port
        self._states[port] = state

        GREEN  = QColor(0, 200, 0)
        RED    = QColor(220, 20, 60)
        ORANGE = QColor(255, 165, 0)
        GREY   = QColor(110, 110, 110)

        if state == PortState.ACTIVE:
            col, on = GREEN, True
        elif state == PortState.UNAVAILABLE:
            col, on = ORANGE, True
        elif state == PortState.DISCONNECTED:
            col, on = RED, True
        else:
            col, on = GREY, False

        led = self._leds[port]
        led.set_on(on, col)

        tip = self._tooltip_for(port, state, fixed_input, connected)
        led.setToolTip(tip)

    def _tooltip_for(self, port: str, state: PortState, fixed_input: str, connected: bool) -> str:
        # Generates the appropriate tooltip text for a given port state
        port = (port or "").upper()
        fixed_input = (fixed_input or "A0").upper()

        other_primary_bank = "B" if fixed_input.startswith("A") else "A"
        other_primary0 = f"{other_primary_bank}0"

        if port == fixed_input and state == PortState.DISCONNECTED:
            return "HackRF not connected"

        if state == PortState.INACTIVE and port == other_primary0:
            return f"Primary port {other_primary0} — Inactive"

        if state == PortState.INACTIVE:
            return f"Port {port}: Inactive"

        if port == fixed_input and state == PortState.ACTIVE:
            return f"Primary port {fixed_input}"

        if state == PortState.ACTIVE:
            suffix = "" if port == fixed_input else f" (routed via {fixed_input})"
            return f"Port {port}: Active{suffix}"

        if state == PortState.UNAVAILABLE:
            if port == other_primary0:
                return f"Primary port {other_primary0}"

            sel = getattr(self, "_active_port", None)
            if sel and len(sel) == 2 and sel[1].isdigit() and len(port) == 2 and port[1].isdigit():
                if port[0] != sel[0] and port[1] == sel[1]:
                    return f"Port {port}: Reserved by {other_primary0}"

            return f"Port {port}: Unavailable"

        return f"Port {port}: {state.name.title()}"