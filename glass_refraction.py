"""Experimental independent rounded compositor underlay for GlassCalendar."""
import ctypes
import math
from ctypes import wintypes as wt
from shiboken6 import isValid
from PySide6.QtCore import QObject, QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
dwm = ctypes.WinDLL('dwmapi')

class RECT(ctypes.Structure):
    _fields_ = [('left', wt.LONG), ('top', wt.LONG), ('right', wt.LONG), ('bottom', wt.LONG)]
class SIZE(ctypes.Structure):
    _fields_ = [('cx', wt.LONG), ('cy', wt.LONG)]
class POINT(ctypes.Structure):
    _fields_ = [('x', wt.LONG), ('y', wt.LONG)]
class THUMBNAIL(ctypes.Structure):
    _fields_ = [('flags', wt.DWORD), ('destination', RECT), ('source', RECT), ('opacity', wt.BYTE), ('visible', wt.BOOL), ('client_only', wt.BOOL)]
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]; user32.FindWindowW.restype = wt.HWND
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.IsWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowRgn.argtypes = [wt.HWND, wt.HRGN, wt.BOOL]
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6; gdi32.CreateRoundRectRgn.restype = wt.HRGN
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
dwm.DwmRegisterThumbnail.argtypes = [wt.HWND, wt.HWND, ctypes.POINTER(wt.HANDLE)]; dwm.DwmRegisterThumbnail.restype = ctypes.c_long
dwm.DwmUnregisterThumbnail.argtypes = [wt.HANDLE]
dwm.DwmQueryThumbnailSourceSize.argtypes = [wt.HANDLE, ctypes.POINTER(SIZE)]
dwm.DwmUpdateThumbnailProperties.argtypes = [wt.HANDLE, ctypes.POINTER(THUMBNAIL)]

def glass_radius(logical_width):
    return min(52.0, max(38.0, logical_width * .055))

def glass_bands(width, height, radius, inset=1):
    """Rectangles inside a rounded silhouette; DWM thumbnails ignore window regions."""
    radius = min(round(radius), (width - 2 * inset) // 2, (height - 2 * inset) // 2)
    if radius <= 0:
        return []
    bands = []
    for y in range(0, radius, 2):
        end = min(y + 2, radius)
        left = inset + math.ceil(radius - math.sqrt(radius * radius - (radius - y) ** 2))
        bands.append((left, inset + y, width - left, inset + end))
    if inset + radius < height - inset - radius:
        bands.append((inset, inset + radius, width - inset, height - inset - radius))
    bands.extend((left, height - bottom, right, height - top)
                 for left, top, right, bottom in reversed(bands[:(radius + 1) // 2]))
    return bands

class _Underlay(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    def paintEvent(self, _event):
        # A nonzero alpha gives DWM a composited surface without tinting the desktop.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))

class DesktopRim(QObject):
    def __init__(self, widget, source_provider=None):
        super().__init__(widget)
        self.widget = widget
        self.source_provider = source_provider or (lambda: user32.FindWindowW('Progman', None))
        self.underlay = _Underlay()
        self.handles = []
        self.source = 0
        self.target = 0
        self.shape = None
        self.last_mapping = []
        self.last_signature = None
        self.timer = QTimer(self); self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer); self.timer.timeout.connect(self.sync)
        self.poll = QTimer(self); self.poll.timeout.connect(self.sync); self.poll.start(1500)
        widget.installEventFilter(self)
        widget.destroyed.connect(self.clear)
        QApplication.instance().aboutToQuit.connect(self.clear)

    def clear(self, *_):
        if isValid(self.underlay):
            self.underlay.hide()
        for handle in self.handles:
            dwm.DwmUnregisterThumbnail(handle)
        self.handles.clear()
        self.source = self.target = 0; self.last_signature = None; self.last_mapping = []

    def eventFilter(self, _obj, event):
        if event.type() == QEvent.Type.Move:
            self.sync()
        elif event.type() in (QEvent.Type.Show, QEvent.Type.Resize,
                            QEvent.Type.WinIdChange, QEvent.Type.WindowStateChange,
                            QEvent.Type.ZOrderChange):
            if not self.timer.isActive(): self.timer.start(8)
        elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
            self.clear()
        return False

    def sync(self):
        if not self.widget.isVisible() or self.widget.isMinimized():
            self.clear(); return
        hwnd = int(self.widget.winId())
        source = self.source_provider()
        if not source or not user32.IsWindow(source):
            self.clear(); return
        target = int(self.underlay.winId())
        if self.source != source or self.target != target or not self.handles:
            self.clear()
            h = wt.HANDLE()
            if dwm.DwmRegisterThumbnail(target, source, ctypes.byref(h)) != 0:
                return
            self.handles.append(h)
            self.source, self.target = source, target
        client, root, origin, size = RECT(), RECT(), POINT(0, 0), SIZE()
        if not (user32.GetClientRect(hwnd, ctypes.byref(client)) and
                user32.ClientToScreen(hwnd, ctypes.byref(origin)) and
                user32.GetWindowRect(source, ctypes.byref(root)) and
                dwm.DwmQueryThumbnailSourceSize(self.handles[0], ctypes.byref(size)) == 0):
            self.clear(); return
        width, height = client.right-client.left, client.bottom-client.top
        rootw, rooth = root.right-root.left, root.bottom-root.top
        if min(width, height, rootw, rooth, size.cx, size.cy) <= 0:
            self.clear(); return
        scale = width / self.widget.width() if self.widget.width() else 1.0
        # Enlarge the live background around the center, consistently across the glass.
        zoom = 0.98
        sx, sy = size.cx/rootw, size.cy/rooth
        x0 = origin.x + width * (1-zoom) / 2 - root.left
        y0 = origin.y + height * (1-zoom) / 2 - root.top
        l, t = max(0, int(-x0/zoom + .9999)), max(0, int(-y0/zoom + .9999))
        r, b = min(width, int((rootw-x0)/zoom)), min(height, int((rooth-y0)/zoom))
        if l >= r or t >= b:
            self.clear(); return
        signature = (target, source, width, height, origin.x, origin.y,
                     root.left, root.top, root.right, root.bottom, size.cx, size.cy)
        if signature != self.last_signature:
            radius = round(glass_radius(self.widget.width()) * scale)
            bands = []
            for left, top, right, bottom in glass_bands(width, height, radius, max(1, round(scale))):
                dest = (max(l, left), max(t, top), min(r, right), min(b, bottom))
                left, top, right, bottom = dest
                if left >= right or top >= bottom:
                    continue
                src = (round((x0+left*zoom)*sx), round((y0+top*zoom)*sy),
                       round((x0+right*zoom)*sx), round((y0+bottom*zoom)*sy))
                sx1, sy1 = min(size.cx - 1, max(0, src[0])), min(size.cy - 1, max(0, src[1]))
                src = (sx1, sy1, max(sx1 + 1, min(size.cx, src[2])),
                       max(sy1 + 1, min(size.cy, src[3])))
                bands.append((dest, src, zoom, True))
            if not bands:
                self.clear(); return
            while len(self.handles) < len(bands):
                h = wt.HANDLE()
                if dwm.DwmRegisterThumbnail(target, source, ctypes.byref(h)) != 0:
                    self.clear(); return
                self.handles.append(h)
            while len(self.handles) > len(bands):
                dwm.DwmUnregisterThumbnail(self.handles.pop())
            for handle, (dest, src, _, _) in zip(self.handles, bands):
                props = THUMBNAIL(1|2|4|8, RECT(*dest), RECT(*src), 255, True, False)
                if dwm.DwmUpdateThumbnailProperties(handle, ctypes.byref(props)) != 0:
                    self.clear(); return
            self.last_signature = signature
            self.last_mapping = bands
        # Match the main window's rounded outline using physical-pixel dimensions.
        shape = (target, width, height, round(glass_radius(self.widget.width()) * scale))
        if shape != self.shape:
            radius = shape[3]
            rgn = gdi32.CreateRoundRectRgn(0, 0, width+1, height+1, 2*radius, 2*radius)
            if not rgn or not user32.SetWindowRgn(target, rgn, True):
                if rgn: gdi32.DeleteObject(rgn)
                self.clear(); return
            self.shape = shape
        if not self.underlay.isVisible(): self.underlay.show()
        # Position directly below the main HWND, with no focus or activation.
        if not user32.SetWindowPos(target, hwnd, origin.x, origin.y, width, height,
                                  0x0010 | 0x0040):
            self.clear()
