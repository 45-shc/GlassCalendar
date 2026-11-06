import calendar
import ctypes
from ctypes import wintypes
import json
import os
import random
import sys
import shutil
import winreg
import uuid
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QTimer, QDateTime, QLockFile
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QFont, QFontDatabase, QLinearGradient, QBrush, QIcon, QPixmap, QAction
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QGridLayout, QInputDialog, QCheckBox, QScrollArea, QSizePolicy,
    QSystemTrayIcon, QMenu, QDialog, QDialogButtonBox, QLineEdit, QDateTimeEdit, QFormLayout, QSizeGrip, QMessageBox, QPlainTextEdit, QButtonGroup, QStyle, QStyleOptionButton)
from glass_refraction import DesktopRim, glass_radius

LEGACY_DATA = Path(os.getenv('APPDATA', str(Path.home()))) / 'GlassCalendar' / 'data.json'
DATA = Path.home() / 'GlassCalendar' / 'data.json'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
STARTUP_DIR = Path(os.getenv('APPDATA', str(Path.home()))) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
STARTUP_RETRY = STARTUP_DIR / 'GlassCalendar-retry.vbs'
MARK_COLORS = [('琥珀', '#f6c975'), ('蜜桃', '#f2a9a4'), ('薄荷', '#a6d9d0'), ('天空', '#a9c8f2')]
HAND_FONT = 'ZCOOL KuaiLe'
BROOKLYN_FONT = 'ZCOOL KuaiLe'
BROOKLYN_TEXT_FONT = 'Ma Shan Zheng'
BRUSH_CACHE = {}


def hide_taskbar_button(hwnd):
    """Remove the taskbar button without changing the window's focus behavior."""
    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_ubyte * 16)]
    clsid = GUID((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('56FDF344-FD6D-11D0-958A-006097C9A090').bytes_le))
    iid = GUID((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('56FDF342-FD6D-11D0-958A-006097C9A090').bytes_le))
    ole = ctypes.windll.ole32
    initialized = ole.CoInitialize(None)
    interface = ctypes.c_void_p()
    try:
        result = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(interface))
        if result < 0: return False
        table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        initialize = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(table[3])
        remove = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wintypes.HWND)(table[5])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])
        try:
            return initialize(interface) >= 0 and remove(interface, wintypes.HWND(hwnd)) >= 0
        finally:
            release(interface)
    finally:
        if initialized >= 0: ole.CoUninitialize()


def read_data(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('数据文件格式无效')
    for key, kind in [('events', dict), ('marks', dict), ('todos', list), ('goals', list), ('settings', dict)]:
        if key in data and not isinstance(data[key], kind):
            raise ValueError(f'数据字段 {key} 格式无效')
    return data


def migrate_data(source, target):
    if target.exists() or target.with_suffix('.bak').exists(): return
    if not source.exists() and not source.with_suffix('.bak').exists(): return
    target.parent.mkdir(parents=True, exist_ok=True)
    for original, destination in ((source, target), (source.with_suffix('.bak'), target.with_suffix('.bak'))):
        if original.exists():
            temporary = destination.with_suffix(destination.suffix + '.migration.tmp')
            with original.open('rb') as src, temporary.open('wb') as dst:
                shutil.copyfileobj(src, dst)
                dst.flush(); os.fsync(dst.fileno())
            os.replace(temporary, destination)


def load_hand_font():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    font_path = root / 'assets' / 'ZCOOLKuaiLe-Regular.ttf'
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id)
    return families[0] if families else 'Microsoft YaHei UI'


def doodle_accent():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    pixmap = QPixmap(str(root / 'assets' / 'doodle-sparkles.png'))
    label = QLabel()
    label.setFixedSize(56, 36)
    label.setPixmap(pixmap.scaled(label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label


class BrooklynHeader(QWidget):
    def __init__(self, art):
        super().__init__()
        self.art = art
        self.setFixedSize(300, 92)
        self.setAccessibleName('月度日历')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(QRectF(self.rect()), self.art, QRectF(self.art.rect()))


class SectionTitle(QLabel):
    def __init__(self, text, key, art):
        super().__init__(text)
        self.key, self.art, self.brooklyn = key, art, False

    def paintEvent(self, event):
        if self.brooklyn:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            source = QRectF(75 if self.key == 'todos' else 1265, 325, 850, 85)
            p.drawPixmap(QRectF(0, self.height() - 10, min(165, self.width()), 10), self.art, source)
            p.end()
        super().paintEvent(event)


class HandwrittenCheckBox(QCheckBox):
    def __init__(self, text, brooklyn):
        super().__init__(text)
        self.brooklyn = brooklyn

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.brooklyn and self.isChecked():
            option = QStyleOptionButton(); option.initFrom(self)
            indicator = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
            p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor('#e85a4c'), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath(QPoint(indicator.left() + 3, indicator.center().y()))
            path.lineTo(indicator.left() + 8, indicator.bottom() - 4)
            path.lineTo(indicator.right() - 2, indicator.top() + 3)
            p.drawPath(path)


def marker_brush(variant, color):
    # The generated alpha retains the felt-tip texture; only the ink hue changes.
    key = (variant % 2, color.name())
    if key not in BRUSH_CACHE:
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        source = QPixmap(str(root / 'assets' / f'marker-dry-{key[0] + 1}.png'))
        if source.isNull():
            return source
        source = source.scaled(480, 320, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        smooth = QPixmap(str(root / 'assets' / f'marker-swatch-{key[0] + 1}.png'))
        if not smooth.isNull():
            smooth = smooth.scaled(source.size(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            base = QPixmap(source.size()); base.fill(Qt.GlobalColor.transparent)
            p = QPainter(base); p.setOpacity(.55); p.drawPixmap(0, 0, smooth)
            p.setOpacity(1); p.drawPixmap(0, 0, source); p.end(); source = base
        original = source.copy()
        p = QPainter(source)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        ink = QColor(color); ink.setAlpha(185)
        p.fillRect(source.rect(), ink)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        p.setOpacity(.15); p.drawPixmap(0, 0, original); p.end()
        BRUSH_CACHE[key] = source
    return BRUSH_CACHE[key]


class DayDialog(QDialog):
    def __init__(self, parent, day, text='', mark=None):
        super().__init__(parent)
        mark = mark or {}
        self.setWindowTitle('编辑这一天')
        self.setMinimumWidth(470)
        self.setStyleSheet('''
            QDialog { background: #f7f6f2; }
            QWidget { color: #253447; font-family: "ZCOOL KuaiLe"; font-size: 16px; font-weight: 600; }
            QPlainTextEdit { background: #ffffff; color: #253447; border: 1px solid #dce1e3; border-radius: 10px; padding: 12px; selection-background-color: #a9c8f2; }
            QCheckBox { color: #253447; font-size: 16px; spacing: 8px; }
            QPushButton { color: #344452; background: #e8ebeb; border: 1px solid transparent; border-radius: 9px; padding: 8px 16px; }
            QPushButton:hover { background: #dce5e5; }
            QPushButton#save { color: white; background: #426e76; font-weight: 600; }
            QPushButton#save:hover { background: #345d65; }
        ''')
        layout = QVBoxLayout(self); layout.setContentsMargins(24, 22, 24, 22); layout.setSpacing(12)
        weekday = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][day.weekday()]
        title = QLabel(f'{day.month} 月 {day.day} 日  ·  {weekday}')
        title.setStyleSheet('font-size: 28px; font-weight: 700; color: #243649;')
        layout.addWidget(title)
        sub = QLabel(f'{day.year}  /  写下这一天要做的事'); sub.setStyleSheet('color: #63767b; font-size: 15px;')
        layout.addWidget(sub)
        self.notes = QPlainTextEdit(); self.notes.setPlaceholderText('例如：\n下午 3 点提交报告\n给自己留一点休息时间')
        self.notes.setPlainText(text); self.notes.setMinimumHeight(135); layout.addWidget(self.notes)
        label = QLabel('日期标记'); label.setStyleSheet('font-size: 16px; font-weight: 700; margin-top: 4px;')
        layout.addWidget(label)
        checks = QHBoxLayout()
        self.circle = QCheckBox('圈起日期'); self.circle.setChecked(bool(mark.get('circle')))
        self.fill = QCheckBox('给当天涂色'); self.fill.setChecked(bool(mark.get('fill')))
        checks.addWidget(self.circle); checks.addWidget(self.fill); checks.addStretch(); layout.addLayout(checks)
        palette = QHBoxLayout(); palette.setSpacing(8)
        self.colors = QButtonGroup(self); self.colors.setExclusive(True)
        selected = mark.get('color', MARK_COLORS[0][1])
        for name, color in MARK_COLORS:
            button = QPushButton(name); button.setCheckable(True); button.setProperty('markColor', color)
            button.setStyleSheet(f'QPushButton {{ background: {color}; color: #30414a; border: 2px solid transparent; padding: 7px 13px; }} QPushButton:checked {{ border: 2px solid #385662; }}')
            self.colors.addButton(button); button.setChecked(color == selected); palette.addWidget(button)
        if not self.colors.checkedButton(): self.colors.buttons()[0].setChecked(True)
        palette.addStretch(); layout.addLayout(palette)
        footer = QHBoxLayout(); footer.addStretch()
        cancel = QPushButton('取消'); cancel.clicked.connect(self.reject)
        save = QPushButton('保存这一天'); save.setObjectName('save'); save.setDefault(True); save.clicked.connect(self.accept)
        footer.addWidget(cancel); footer.addWidget(save); layout.addLayout(footer)
        self.notes.setFocus()

    def value(self):
        return self.notes.toPlainText().strip(), {'circle': self.circle.isChecked(), 'fill': self.fill.isChecked(), 'color': self.colors.checkedButton().property('markColor')}


class DayButton(QPushButton):
    def __init__(self, day, current_month, text, mark, parent=None, theme='glass'):
        super().__init__(parent)
        self.day, self.current_month, self.note, self.mark = day, current_month, text, mark or {}
        self.theme = theme
        self.setAccessibleName(day.isoformat() + (' ' + text if text else ''))
        self.setToolTip(text or '点击添加日程或标记')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        color = QColor(self.mark.get('color', MARK_COLORS[0][1]))
        if not color.isValid(): color = QColor(MARK_COLORS[0][1])
        variant = self.mark.get('variant', self.day.toordinal() % 8)
        if self.mark.get('fill'):
            brush = marker_brush(variant, color)
            if not brush.isNull():
                p.save(); p.setClipRect(area.adjusted(2, 2, -2, -2))
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawPixmap(area, brush, QRectF(brush.rect()))
                p.restore()
        if self.underMouse(): p.fillRect(area, QColor(255, 255, 255, 30))
        if self.theme == 'brooklyn':
            p.fillRect(area, QColor(12, 13, 20, 48))
            p.setPen(QPen(QColor(244, 241, 227, 125), 1)); p.drawRect(area)
        else:
            p.setPen(QPen(QColor(222, 238, 251, 95), 1)); p.drawRect(area)
        if self.day == date.today():
            p.fillRect(area.adjusted(2, 2, -2, -2), QColor(255, 235, 175, 28))
            p.setPen(QPen(QColor(255, 229, 154, 245), 2.5)); p.drawRect(area.adjusted(2, 2, -2, -2))
        compact = self.theme == 'brooklyn' and self.height() < 58
        number_size = (17 if self.height() >= 48 else 15) if compact else 26 if self.theme == 'brooklyn' and self.height() >= 75 else 24 if self.theme == 'brooklyn' and self.height() >= 64 else 21 if self.theme == 'brooklyn' else 17
        p.setFont(QFont(BROOKLYN_FONT if self.theme == 'brooklyn' else HAND_FONT, number_size, QFont.Weight.Bold))
        number = QRectF(7, 1, 40, 27) if compact else QRectF(8, 5, 42, 35)
        if self.mark.get('circle'):
            p.save()
            if compact: p.translate(4, 0); p.scale(.78, .68)
            p.translate(29, 22); p.rotate((variant % 5 - 2) * 3); p.translate(-29, -22)
            ink = QColor(color); ink.setAlpha(230)
            p.setPen(QPen(ink, 2.2 + (variant % 3) * .35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            wobble = (variant % 4) - 1.5
            path = QPainterPath(QPoint(11, 23))
            path.cubicTo(8, 10 + wobble, 20, 3, 34, 6 + wobble)
            path.cubicTo(49, 8, 52, 18 + wobble, 46, 29)
            path.cubicTo(40, 39, 25, 41 + wobble, 15, 34)
            path.cubicTo(11, 31, 9, 27, 11, 20); p.drawPath(path)
            if variant % 3 == 1:
                echo = QColor(color); echo.setAlpha(125)
                p.setPen(QPen(echo, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawArc(QRectF(10, 7, 39, 30), 15 * 16, 190 * 16)
            elif variant % 3 == 2:
                p.setPen(QPen(ink, 1.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.drawLine(QPoint(48, 6), QPoint(51, 3))
                p.drawLine(QPoint(51, 12), QPoint(55, 11))
            p.restore()
        p.setPen(QColor(23, 36, 52, 135 if self.current_month else 70))
        p.drawText(number.translated(1, 1), Qt.AlignmentFlag.AlignCenter, str(self.day.day))
        p.setPen(QColor(250, 251, 253, 255 if self.current_month else 125))
        p.drawText(number, Qt.AlignmentFlag.AlignCenter, str(self.day.day))
        if self.note:
            p.setFont(QFont(BROOKLYN_TEXT_FONT if self.theme == 'brooklyn' else HAND_FONT,
                            9 if compact else 12 if self.theme == 'brooklyn' and self.height() >= 75 else 11, QFont.Weight.Bold))
            note_top = (31 if self.height() >= 48 else 28) if compact else 42
            note_area = QRectF(10, note_top, max(0, self.width() - 20), max(0, self.height() - note_top - 2))
            p.setPen(QColor(23, 36, 52, 150 if self.current_month else 75))
            p.drawText(note_area.translated(1, 1), Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop, self.note)
            p.setPen(QColor(255, 251, 231, 240 if self.current_month else 150))
            p.drawText(note_area, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignTop, self.note)


class ItemDialog(QDialog):
    def __init__(self, parent, title, item=None):
        super().__init__(parent)
        item = item or {}
        self.setWindowTitle(title)
        self.setMinimumWidth(390)
        self.setStyleSheet('QDialog { background: #293546; } QLineEdit, QDateTimeEdit { background: #3b495b; border: 1px solid #657385; border-radius: 5px; padding: 7px; } QPushButton { background: #53657a; padding: 7px 15px; border-radius: 5px; }')
        form = QFormLayout(self)
        self.name = QLineEdit(item.get('text', '')); self.name.setPlaceholderText('写下具体内容')
        self.has_due = QCheckBox('设置截止时间和提醒'); self.has_due.setChecked(bool(item.get('due')))
        self.due = QDateTimeEdit(); self.due.setCalendarPopup(True); self.due.setDisplayFormat('yyyy-MM-dd HH:mm')
        parsed = QDateTime.fromString(item.get('due', ''), Qt.DateFormat.ISODate)
        self.due.setDateTime(parsed if parsed.isValid() else QDateTime.currentDateTime().addDays(1))
        self.due.setEnabled(self.has_due.isChecked()); self.has_due.toggled.connect(self.due.setEnabled)
        form.addRow('内容', self.name); form.addRow('', self.has_due); form.addRow('截止时间', self.due)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); form.addRow(buttons)

    def value(self):
        return {'text': self.name.text().strip(), 'due': self.due.dateTime().toString(Qt.DateFormat.ISODate) if self.has_due.isChecked() else ''}


class GlassCalendar(QWidget):
    def __init__(self):
        super().__init__()
        global HAND_FONT, BROOKLYN_FONT, BROOKLYN_TEXT_FONT
        HAND_FONT = load_hand_font()
        available_fonts = QFontDatabase.families()
        BROOKLYN_FONT = next((name for name in ('STXingkai', '华文行楷', 'KaiTi', '楷体') if name in available_fonts), HAND_FONT)
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
        brush_font_id = QFontDatabase.addApplicationFont(str(root / 'assets' / 'MaShanZheng-Regular.ttf'))
        BROOKLYN_TEXT_FONT = QFontDatabase.applicationFontFamilies(brush_font_id)[0]
        self.app_icon = QIcon(str(root / 'assets' / 'glass-calendar-icon.ico'))
        self.brooklyn_header_art = QPixmap(str(root / 'assets' / 'brooklyn-title-exact.png'))
        self.brooklyn_underlines = QPixmap(str(root / 'assets' / 'brooklyn-underlines.png'))
        self.brooklyn_detail = QPixmap(str(root / 'assets' / 'brooklyn-detail-exact.png'))
        self.setWindowIcon(self.app_icon)
        self.setWindowTitle('玻璃日历')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.taskbar_created_message = ctypes.windll.user32.RegisterWindowMessageW('TaskbarButtonCreated')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(900, 780)
        self.setMinimumSize(700, 620)
        self.current = date.today().replace(day=1)
        self.drag = None
        try:
            self.data = read_data(DATA)
        except (OSError, ValueError) as exc:
            backup = DATA.with_suffix('.bak')
            if isinstance(exc, FileNotFoundError) and not backup.exists():
                self.data = {'events': {}, 'todos': [], 'goals': []}
            else:
                try:
                    self.data = read_data(backup)
                    QMessageBox.warning(None, '恢复数据', '数据文件缺失或无法读取，已从上次备份恢复。')
                except (OSError, ValueError):
                    QMessageBox.critical(None, '无法读取数据', f'请检查或备份数据文件后重试：\n{DATA}\n\n{exc}')
                    raise SystemExit(1)
        for key, default in [('events', {}), ('marks', {}), ('todos', []), ('goals', [])]:
            self.data.setdefault(key, default)
        self.theme = self.data.get('settings', {}).get('theme', 'glass')
        if self.theme not in ('glass', 'brooklyn'): self.theme = 'glass'
        self.pending_geometry = None
        self.fallback_geometry = None
        geometry = self.data.get('settings', {}).get('geometry')
        if geometry and len(geometry) == 4:
            self.setGeometry(*geometry)
            if not any(screen.availableGeometry().intersects(QRect(*geometry)) for screen in QApplication.screens()):
                self.pending_geometry = QRect(*geometry)
                self.move(QApplication.primaryScreen().availableGeometry().topLeft() + QPoint(30, 30))
                self.fallback_geometry = self.geometry()
                QApplication.instance().screenAdded.connect(self.restore_pending_geometry)
                for delay in (3000, 10000, 45000):
                    QTimer.singleShot(delay, self.restore_pending_geometry)
        self.build()
        self.setup_tray()
        self.desktop_rim = DesktopRim(self)
        QTimer.singleShot(0, self.desktop_rim.sync)
        self.timer = QTimer(self); self.timer.timeout.connect(self.check_reminders); self.timer.start(30000)
        QTimer.singleShot(2500, self.check_reminders)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.hide_taskbar)
        QTimer.singleShot(500, self.hide_taskbar)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'brooklyn_header') and self.theme == 'brooklyn':
            self.brooklyn_header.setFixedSize(max(210, min(int(self.width() * .36), self.width() - 330)), max(80, min(120, int(self.height() * .13))))
            self.root_layout.setContentsMargins(int(self.width() * .052), 17, int(self.width() * .03), 19)
            self.bottom_gap.changeSize(0, int(self.height() * .025))

    def restore_pending_geometry(self, *_):
        if self.pending_geometry is None: return
        if self.geometry() != self.fallback_geometry:
            self.pending_geometry = None
        elif any(screen.availableGeometry().intersects(self.pending_geometry) for screen in QApplication.screens()):
            geometry = self.pending_geometry
            self.pending_geometry = None
            self.setGeometry(geometry)

    def hide_taskbar(self):
        if self.isVisible(): hide_taskbar_button(int(self.winId()))

    def nativeEvent(self, event_type, message):
        if event_type in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == getattr(self, 'taskbar_created_message', 0):
                QTimer.singleShot(0, self.hide_taskbar)
        return super().nativeEvent(event_type, message)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        # Keep every glow inside the glass silhouette; otherwise the radial
        # highlights paint translucent square corners outside the rounded rim.
        silhouette = QPainterPath()
        radius = glass_radius(w)
        silhouette.addRoundedRect(QRectF(1, 1, w - 2, h - 2), radius, radius)
        p.setClipPath(silhouette)
        glass = QLinearGradient(0, 0, w, h)
        if self.theme == 'brooklyn':
            glass.setColorAt(0, QColor(18, 20, 25, 145))
            glass.setColorAt(.42, QColor(14, 15, 22, 115))
            glass.setColorAt(1, QColor(20, 18, 24, 145))
        else:
            glass.setColorAt(0, QColor(38, 61, 89, 67))
            glass.setColorAt(.42, QColor(25, 43, 64, 39))
            glass.setColorAt(1, QColor(19, 36, 57, 73))
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(glass)
        p.drawRoundedRect(QRectF(1, 1, w - 2, h - 2), radius, radius)

        if self.theme == 'brooklyn':
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.save()
            safe_area = QPainterPath(); safe_area.addRect(QRectF(0, 0, w, h))
            for button in self.findChildren(QPushButton):
                if button.objectName() == 'tool' and button.isVisibleTo(self):
                    pos = button.mapTo(self, QPoint(0, 0))
                    safe_area.addRect(QRectF(pos.x() - 3, pos.y() - 3, button.width() + 6, button.height() + 6))
            p.setClipPath(safe_area, Qt.ClipOperation.IntersectClip)
            # The turquoise corner follows the calendar grid, not the window.
            # Exclude its original location before painting it at the grid corner.
            p.save()
            detail_area = QPainterPath(); detail_area.addRect(QRectF(0, 0, w, h))
            corner_area = QPainterPath(); corner_area.addRect(QRectF(0, h * 450 / 1024, w * 230 / 1135, h * 350 / 1024))
            p.setClipPath(detail_area.subtracted(corner_area), Qt.ClipOperation.IntersectClip)
            p.drawPixmap(QRectF(0, 0, w, h), self.brooklyn_detail, QRectF(200, 0, 1135, 1024))
            p.restore()
            grid = self.grid.geometry()
            scale = min(w / 1050, h / 910)
            corner = QRectF(grid.left() - 20 * scale - 225 * scale,
                            grid.bottom() + 1 - 200 * scale, 430 * scale, 350 * scale)
            p.drawPixmap(corner, self.brooklyn_detail, QRectF(0, 450, 430, 350))
            p.restore()

        # Let the edge light fade into the same glass surface without an inner cutout.
        for start, end, rect in (
            (QPoint(0, 0), QPoint(0, 19), QRectF(0, 0, w, 19)),
            (QPoint(0, h), QPoint(0, h - 19), QRectF(0, h - 19, w, 19)),
            (QPoint(0, 0), QPoint(19, 0), QRectF(0, 0, 19, h)),
            (QPoint(w, 0), QPoint(w - 19, 0), QRectF(w - 19, 0, 19, h)),
        ):
            edge = QLinearGradient(start, end)
            edge.setColorAt(0, QColor(239, 251, 255, 100))
            edge.setColorAt(.13, QColor(221, 242, 255, 65))
            edge.setColorAt(.32, QColor(214, 237, 255, 40))
            edge.setColorAt(.58, QColor(192, 224, 247, 16))
            edge.setColorAt(1, QColor(212, 239, 255, 0))
            p.fillRect(rect, QBrush(edge))

        # Layered edge highlights suggest curved glass without obscuring the wallpaper.
        rim = QLinearGradient(0, 0, w, h)
        rim.setColorAt(0, QColor(236, 250, 255, 205))
        rim.setColorAt(.20, QColor(198, 224, 246, 85))
        rim.setColorAt(.52, QColor(219, 240, 255, 105))
        rim.setColorAt(.80, QColor(177, 215, 241, 60))
        rim.setColorAt(1, QColor(242, 250, 255, 155))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(rim), 2)); p.drawRoundedRect(QRectF(2, 2, w - 4, h - 4), radius - 1, radius - 1)

    def save(self):
        try:
            DATA.parent.mkdir(parents=True, exist_ok=True)
            temp = DATA.with_suffix('.tmp')
            with temp.open('w', encoding='utf-8') as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush(); os.fsync(stream.fileno())
            if DATA.exists():
                try:
                    read_data(DATA)
                    backup_temp = DATA.with_suffix('.backup.tmp')
                    with DATA.open('rb') as original, backup_temp.open('wb') as backup:
                        shutil.copyfileobj(original, backup)
                        backup.flush(); os.fsync(backup.fileno())
                    os.replace(backup_temp, DATA.with_suffix('.bak'))
                except ValueError:
                    pass
            os.replace(temp, DATA)
        except OSError as exc:
            QMessageBox.critical(None, '保存失败', f'数据未保存，请检查磁盘空间或权限：\n{exc}')

    def setup_tray(self):
        self.quitting = False
        self.tray = QSystemTrayIcon(self.app_icon, self)
        self.tray.setToolTip('玻璃日历')
        menu = QMenu()
        menu.addAction('显示日历', self.show_calendar)
        self.fixed_action = QAction('固定位置和大小', self, checkable=True)
        self.fixed_action.setChecked(self.pin_button.isChecked())
        self.fixed_action.toggled.connect(self.pin_button.setChecked); menu.addAction(self.fixed_action)
        self.bottom_action = QAction('桌面置底', self, checkable=True)
        self.bottom_action.setChecked(self.data.get('settings', {}).get('bottom', False))
        self.bottom_action.toggled.connect(self.set_bottom); menu.addAction(self.bottom_action)
        self.start_action = QAction('开机启动', self, checkable=True)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, 'GlassCalendar')
            self.start_action.setChecked(True)
        except OSError:
            pass
        if STARTUP_RETRY.exists(): self.start_action.setChecked(True)
        self.start_action.toggled.connect(self.set_autostart); menu.addAction(self.start_action)
        self.sync_start_button(self.start_action.isChecked())
        self.start_button.toggled.connect(self.set_autostart)
        if self.start_action.isChecked(): self.set_autostart(True)
        menu.addAction('最小化', self.showMinimized)
        menu.addAction('隐藏日历', self.close)
        menu.addSeparator(); menu.addAction('退出', self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_calendar() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()
        if self.bottom_action.isChecked(): self.set_bottom(True)

    def set_bottom(self, enabled):
        self.data.setdefault('settings', {})['bottom'] = enabled
        self.save()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnBottomHint, enabled)
        self.show()

    def set_autostart(self, enabled):
        try:
            if enabled and not DATA.exists() and not DATA.with_suffix('.bak').exists():
                self.save()
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    command = f'"{sys.executable}"' if getattr(sys, 'frozen', False) else f'"{sys.executable.replace("python.exe", "pythonw.exe")}" "{Path(__file__).resolve()}"'
                    command += ' --autostart'
                    winreg.SetValueEx(key, 'GlassCalendar', 0, winreg.REG_SZ, command)
                    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
                    script = 'WScript.Sleep 45000\r\nCreateObject("WScript.Shell").Run "' + command.replace('"', '""') + '", 0, False\r\n'
                    if not STARTUP_RETRY.exists() or STARTUP_RETRY.read_bytes() != script.encode('utf-16'):
                        temp = STARTUP_RETRY.with_suffix('.tmp')
                        temp.write_bytes(script.encode('utf-16'))
                        os.replace(temp, STARTUP_RETRY)
                else:
                    try: winreg.DeleteValue(key, 'GlassCalendar')
                    except FileNotFoundError: pass
                    STARTUP_RETRY.unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(None, '设置失败', f'无法修改开机启动：{exc}')
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                    winreg.QueryValueEx(key, 'GlassCalendar')
                enabled = True
            except OSError:
                enabled = False
        if hasattr(self, 'start_action'):
            self.start_action.blockSignals(True); self.start_action.setChecked(enabled); self.start_action.blockSignals(False)
        self.sync_start_button(enabled)

    def sync_start_button(self, enabled):
        self.start_button.blockSignals(True); self.start_button.setChecked(enabled); self.start_button.blockSignals(False)
        self.start_button.setText('自启：开' if enabled else '自启：关')
        self.start_button.setToolTip('点击关闭开机自启' if enabled else '点击开启开机自启')

    def switch_theme(self):
        self.theme = 'brooklyn' if self.theme == 'glass' else 'glass'
        self.data.setdefault('settings', {})['theme'] = self.theme
        self.save()
        self.apply_theme()
        self.refresh()
        self.update()

    def apply_theme(self):
        brooklyn = self.theme == 'brooklyn'
        self.theme_button.setText('风格  ▾' if brooklyn else '风格')
        self.theme_button.setMinimumWidth(108 if brooklyn else 0)
        self.theme_button.setToolTip('切换回玻璃风格' if brooklyn else '切换到布鲁克林涂鸦风格')
        self.theme_button.setChecked(brooklyn)
        self.title_label.setVisible(not brooklyn)
        self.brooklyn_header.setVisible(brooklyn)
        self.sparkles.setVisible(not brooklyn)
        for button in (self.pin_button, self.start_button, self.minimize_button, self.close_button):
            button.setVisible(not brooklyn)
        self.root_layout.setContentsMargins(int(self.width() * .052) if brooklyn else 27, 17, int(self.width() * .03) if brooklyn else 27, 19)
        self.root_layout.setSpacing(0 if brooklyn else 7)
        self.bottom_gap.changeSize(0, int(self.height() * .025) if brooklyn else 0)
        self.nav_layout.removeWidget(self.month_label)
        self.top_layout.removeWidget(self.month_label)
        if brooklyn:
            self.top_layout.insertWidget(3, self.month_label)
            self.brooklyn_header.setFixedSize(max(210, min(int(self.width() * .36), self.width() - 330)), max(80, min(120, int(self.height() * .13))))
        else:
            self.nav_layout.insertWidget(0, self.month_label)
        self.month_label.setStyleSheet('font-size: 26px; font-weight: 700; color: #fff9e9;' if brooklyn else 'font-size: 30px; font-weight: 700;')
        self.hint_label.setVisible(not brooklyn)
        self.setStyleSheet(self.base_style + ('''
            QWidget { font-family: "%s"; }
            QPushButton#tool { color: #fff8e8; border: 1px solid rgba(242,225,196,100); background: rgba(20,22,25,115); }
            QPushButton#tool:hover { border: 1px solid rgba(255,218,103,220); background: rgba(93,73,42,130); }
            QPushButton#tool:checked { border: 1px solid rgba(255,211,93,200); background: rgba(153,110,35,120); }
            QCheckBox { color: #fff9e9; font-family: "%s"; }
            QCheckBox::indicator { width: 19px; height: 19px; border: 1px solid rgba(255,248,230,200); background: transparent; }
            QCheckBox::indicator:checked { background: transparent; }
        ''' % (BROOKLYN_TEXT_FONT, BROOKLYN_TEXT_FONT) if brooklyn else ''))
        panel_style = ('QWidget#glassSection { background: rgba(16,17,22,85); border: 1px solid rgba(244,237,216,115); border-radius: 11px; }'
                       if brooklyn else self.glass_panel_style)
        self.bottom_layout.setStretch(0, 1 if brooklyn else 4)
        self.bottom_layout.setStretch(1, 1 if brooklyn else 5)
        for panel in self.section_panels: panel.setStyleSheet(panel_style)
        for label, color in zip(self.section_labels, ('#fff9e9', '#fff9e9') if brooklyn else ('#fff0ba', '#d2f3e9')):
            label.brooklyn = brooklyn
            label.setMinimumWidth(175 if brooklyn else 0)
            label.setStyleSheet(f'border: none; background: transparent; font-size: {29 if brooklyn else 20}px; font-weight: 700; color: {color};')
            label.update()
        self.weekdays_layout.setSpacing(0 if brooklyn else self.glass_weekday_spacing)
        for i, label in enumerate(self.weekday_labels):
            if brooklyn:
                edge = '1px solid rgba(244,237,216,125)'
                label.setStyleSheet(f'color: #fff9e9; font-size: 20px; font-weight: 700; border-top: {edge}; border-right: {edge};' + (f' border-left: {edge};' if i == 0 else ''))
                label.setMinimumHeight(28)
            else:
                label.setStyleSheet('color: rgba(241,246,255,235); font-size: 16px; font-weight: 700;')
                label.setMinimumHeight(0)

    def set_fixed(self, enabled):
        self.drag = None
        settings = self.data.setdefault('settings', {})
        rect = self.geometry(); settings['geometry'] = [rect.x(), rect.y(), rect.width(), rect.height()]
        settings['fixed'] = enabled
        self.grip.setVisible(not enabled)
        self.pin_button.setText('已固定' if enabled else '固定')
        self.pin_button.setToolTip('已锁定位置和大小；点击解锁' if enabled else '点击锁定位置和大小')
        if hasattr(self, 'fixed_action'):
            self.fixed_action.blockSignals(True); self.fixed_action.setChecked(enabled); self.fixed_action.blockSignals(False)
        self.save()

    def show_calendar(self):
        self.showNormal(); self.raise_(); self.activateWindow()

    def quit_app(self):
        self.save_geometry(); self.quitting = True; self.tray.hide(); QApplication.quit()

    def closeEvent(self, event):
        self.save_geometry()
        if not self.quitting and QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore(); self.hide()
            self.tray.showMessage('玻璃日历', '日历仍在后台运行，可从系统托盘重新打开或退出。')
        else:
            event.accept(); QApplication.quit()

    def check_reminders(self):
        now = datetime.now().astimezone()
        due_items = []
        for group in ('todos', 'goals'):
            for item in self.data[group]:
                if item.get('done') or not item.get('due') or item.get('notified'): continue
                try:
                    due = datetime.fromisoformat(item['due'])
                    due = due.astimezone()
                except (ValueError, TypeError):
                    continue
                if due <= now:
                    due_items.append(item)
        if due_items:
            message = '、'.join(item['text'] for item in due_items[:3])
            if len(due_items) > 3: message += f' 等 {len(due_items)} 项'
            self.tray.showMessage('截止时间到了', message, QSystemTrayIcon.MessageIcon.Information, 10000)
            for item in due_items: item['notified'] = True
            self.save()

    def save_geometry(self):
        rect = self.pending_geometry if self.pending_geometry is not None and self.geometry() == self.fallback_geometry else self.geometry()
        self.data.setdefault('settings', {})['geometry'] = [rect.x(), rect.y(), rect.width(), rect.height()]
        self.save()

    def button(self, text, action, name='tool'):
        b = QPushButton(text)
        b.setObjectName(name)
        b.clicked.connect(action)
        return b

    def build(self):
        self.setStyleSheet('''
            QWidget { color: #f5f7fb; font-family: "ZCOOL KuaiLe"; font-size: 15px; font-weight: 600; }
            QPushButton { border: none; background: transparent; color: #f5f7fb; }
            QPushButton#tool { font-family: "Segoe UI Symbol"; font-weight: 600; padding: 4px 9px; border: 1px solid rgba(224,242,255,45); border-radius: 9px; background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(238,249,255,36),stop:1 rgba(150,193,225,12)); }
            QPushButton#tool:hover { border: 1px solid rgba(240,251,255,110); background: rgba(226,245,255,55); }
            QPushButton#tool:checked { border: 1px solid rgba(206,242,228,115); background: rgba(202,231,215,75); }
            QPushButton#day { text-align: left; border: 1px solid rgba(255,255,255,22); border-radius: 0; padding: 8px; background: transparent; }
            QPushButton#day:hover { background: rgba(255,255,255,35); }
            QPushButton#today { text-align: left; border: 1px solid rgba(255,255,255,140); border-radius: 0; padding: 8px; background: rgba(255,255,255,28); }
            QCheckBox { spacing: 8px; color: #fff2c1; font-family: "ZCOOL KuaiLe"; font-size: 16px; font-weight: 600; }
            QScrollArea { border: none; background: transparent; }
            QScrollBar:vertical { background: transparent; width: 6px; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,70); border-radius: 3px; }
        ''')
        self.base_style = self.styleSheet()
        self.glass_panel_style = 'QWidget#glassSection { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 rgba(33,54,79,78),stop:0.5 rgba(24,42,65,54),stop:1 rgba(25,42,63,76)); border: 1px solid rgba(219,240,255,85); border-radius: 11px; }'
        self.section_panels, self.section_labels = [], []
        root = QVBoxLayout(self); self.root_layout = root; root.setContentsMargins(27, 17, 27, 19); root.setSpacing(7)
        top = QHBoxLayout(); self.top_layout = top
        self.title_label = QLabel('玻 璃 日 历'); self.title_label.setStyleSheet('font-size: 16px; font-weight: 700; color: rgba(255,255,255,230);')
        self.brooklyn_header = BrooklynHeader(self.brooklyn_header_art)
        self.sparkles = doodle_accent()
        top.addWidget(self.title_label); top.addWidget(self.sparkles); top.addWidget(self.brooklyn_header); top.addStretch()
        self.theme_button = self.button('风格', self.switch_theme); self.theme_button.setCheckable(True); top.addWidget(self.theme_button)
        fixed = self.data.get('settings', {}).get('fixed', False)
        self.pin_button = self.button('已固定' if fixed else '固定', lambda: None); self.pin_button.setCheckable(True); self.pin_button.setChecked(fixed)
        self.pin_button.setToolTip('已锁定位置和大小；点击解锁' if fixed else '点击锁定位置和大小')
        self.pin_button.toggled.connect(self.set_fixed); top.addWidget(self.pin_button)
        self.start_button = self.button('自启：关', lambda: None); self.start_button.setCheckable(True); top.addWidget(self.start_button)
        top.addWidget(self.button('⋯', lambda: self.tray.contextMenu().exec(self.mapToGlobal(QPoint(self.width() - 180, 40)))))
        self.minimize_button = self.button('—', self.showMinimized); top.addWidget(self.minimize_button)
        self.close_button = self.button('×', self.close); top.addWidget(self.close_button)
        root.addLayout(top)
        nav = QHBoxLayout(); self.nav_layout = nav
        self.month_label = QLabel(); self.month_label.setStyleSheet('font-size: 30px; font-weight: 700;')
        nav.addWidget(self.month_label); nav.addSpacing(10)
        nav.addWidget(self.button('‹', lambda: self.shift(-1)))
        nav.addWidget(self.button('›', lambda: self.shift(1)))
        nav.addWidget(self.button('今天', self.today)); nav.addStretch()
        self.hint_label = QLabel('轻点日期，写下计划'); self.hint_label.setStyleSheet('color: rgba(245,247,251,220); font-size: 15px; font-weight: 600;')
        nav.addWidget(self.hint_label); root.addLayout(nav)
        weekdays = QHBoxLayout(); self.weekdays_layout = weekdays; self.glass_weekday_spacing = weekdays.spacing(); self.weekday_labels = []
        for text in ['周一', '周二', '周三', '周四', '周五', '周六', '周日']:
            label = QLabel(text); label.setAlignment(Qt.AlignmentFlag.AlignCenter); label.setStyleSheet('color: rgba(241,246,255,235); font-size: 16px; font-weight: 700;')
            self.weekday_labels.append(label); weekdays.addWidget(label)
        root.addLayout(weekdays)
        self.grid = QGridLayout(); self.grid.setSpacing(0); root.addLayout(self.grid, 5)
        bottom = QHBoxLayout(); self.bottom_layout = bottom; bottom.setSpacing(10)
        self.todo_layout = self.section(bottom, '待办事项', 'todos', '#fff0ba')
        self.goal_layout = self.section(bottom, '我的目标', 'goals', '#d2f3e9')
        bottom.setStretch(0, 4); bottom.setStretch(1, 5)
        root.addSpacing(0); self.bottom_gap = root.itemAt(root.count() - 1).spacerItem()
        root.addLayout(bottom, 3)
        grip = QHBoxLayout(); grip.addStretch(); self.grip = QSizeGrip(self); self.grip.setVisible(not fixed); grip.addWidget(self.grip); root.addLayout(grip)
        self.apply_theme(); self.refresh()

    def section(self, parent, title, key, color):
        panel = QWidget(); panel.setObjectName('glassSection'); panel.setMaximumHeight(240)
        panel.setStyleSheet(self.glass_panel_style)
        outer = QVBoxLayout(panel); outer.setContentsMargins(14, 10, 14, 8)
        head = QHBoxLayout(); label = SectionTitle(title, key, self.brooklyn_underlines); label.setStyleSheet(f'border: none; background: transparent; font-size: 20px; font-weight: 700; color: {color};')
        self.section_panels.append(panel); self.section_labels.append(label)
        head.addWidget(label); head.addStretch(); head.addWidget(self.button('＋', lambda: self.add_item(key)))
        outer.addLayout(head)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setStyleSheet('border: none; background: transparent;')
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); content.setStyleSheet('border: none; background: transparent;')
        layout = QVBoxLayout(content); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(3)
        scroll.setWidget(content); outer.addWidget(scroll); parent.addWidget(panel)
        return layout

    def clear(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

    def refresh(self):
        self.month_label.setText(f'{self.current.year}年{self.current.month}月' if self.theme == 'brooklyn' else f'{self.current.year}.{self.current.month:02d}')
        self.clear(self.grid)
        for r in range(6): self.grid.setRowStretch(r, 0)
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(self.current.year, self.current.month)
        for r, week in enumerate(weeks):
            self.grid.setRowStretch(r, 1)
            for c, day in enumerate(week):
                key = day.isoformat(); text = self.data['events'].get(key, '')
                b = DayButton(day, day.month == self.current.month, text, self.data['marks'].get(key), theme=self.theme)
                b.clicked.connect(lambda checked=False, d=day: self.edit_event(d))
                self.grid.addWidget(b, r, c)
        for c in range(7): self.grid.setColumnStretch(c, 1)
        for key, layout in [('todos', self.todo_layout), ('goals', self.goal_layout)]:
            self.clear(layout)
            for i, item in enumerate(self.data[key]):
                row = QWidget(); row.setStyleSheet('border: none; background: transparent;')
                h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
                due = item.get('due', '')
                check = HandwrittenCheckBox(item['text'] + (f'  ·  {due[:16].replace("T", " ")}' if due else ''), self.theme == 'brooklyn'); check.setChecked(item.get('done', False))
                check.setMinimumWidth(0); check.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                check.setToolTip(item['text'] + (f'\n截止：{due[:16].replace("T", " ")}' if due else ''))
                check.toggled.connect(lambda value, k=key, n=i: self.toggle(k, n, value))
                h.addWidget(check, 1)
                edit = self.button('✎', lambda checked=False, k=key, n=i: self.edit_item(k, n)); edit.setFixedWidth(30); edit.setToolTip('编辑'); h.addWidget(edit)
                delete = self.button('×', lambda checked=False, k=key, n=i: self.delete_item(k, n)); delete.setFixedWidth(30); delete.setToolTip('删除'); h.addWidget(delete)
                layout.addWidget(row)
            layout.addStretch()

    def shift(self, amount):
        month = self.current.year * 12 + self.current.month - 1 + amount
        self.current = date(month // 12, month % 12 + 1, 1); self.refresh()

    def today(self):
        self.current = date.today().replace(day=1); self.refresh()

    def exec_dialog(self, dialog):
        result = dialog.exec()
        self.desktop_rim.sync()
        return result

    def edit_event(self, day):
        key = day.isoformat()
        dialog = DayDialog(self, day, self.data['events'].get(key, ''), self.data['marks'].get(key))
        if self.exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            text, mark = dialog.value()
            if text.strip(): self.data['events'][key] = text.strip()
            else: self.data['events'].pop(key, None)
            if mark['circle'] or mark['fill']:
                previous = self.data['marks'].get(key, {}).get('variant')
                choices = [i for i in range(8) if i != previous]
                mark['variant'] = random.choice(choices)
                self.data['marks'][key] = mark
            else: self.data['marks'].pop(key, None)
            self.save(); self.refresh()

    def add_item(self, key):
        dialog = ItemDialog(self, '添加' + ('待办' if key == 'todos' else '目标'))
        if self.exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            item = dialog.value()
            if item['text']:
                item['done'] = False; self.data[key].append(item); self.save(); self.refresh(); self.check_reminders()

    def edit_item(self, key, index):
        item = self.data[key][index]
        dialog = ItemDialog(self, '编辑', item)
        if self.exec_dialog(dialog) == QDialog.DialogCode.Accepted:
            value = dialog.value()
            if value['text']:
                if value['due'] != item.get('due', ''): item.pop('notified', None)
                item.update(value); self.save(); self.refresh(); self.check_reminders()

    def delete_item(self, key, index):
        item = self.data[key][index]
        if self.confirm_delete(item['text']):
            self.data[key].pop(index); self.save(); self.refresh()

    def confirm_delete(self, text):
        dialog = QMessageBox(QMessageBox.Icon.Question, '删除事项', f'确定删除“{text}”吗？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        dialog.setStyleSheet('QMessageBox { background: #f7f6f2; } QLabel { color: #253447; font-family: "Microsoft YaHei UI"; font-size: 14px; } QPushButton { background: #dfe8e8; color: #253447; border: none; border-radius: 6px; padding: 7px 16px; min-width: 55px; } QPushButton:hover { background: #cbdcdc; }')
        dialog.button(QMessageBox.StandardButton.Yes).setText('删除')
        dialog.button(QMessageBox.StandardButton.Cancel).setText('取消')
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return self.exec_dialog(dialog) == QMessageBox.StandardButton.Yes

    def toggle(self, key, index, value):
        item = self.data[key][index]; item['done'] = value
        if not value: item.pop('notified', None)
        self.save()

    def mousePressEvent(self, event):
        if not self.data.get('settings', {}).get('fixed', False) and event.button() == Qt.MouseButton.LeftButton and event.position().y() < 58:
            self.drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self.drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, event):
        if self.drag is not None: self.save_geometry()
        self.drag = None


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    try:
        migrate_data(LEGACY_DATA, DATA)
    except OSError as exc:
        QMessageBox.critical(None, '无法迁移数据', f'原数据未被修改，请检查磁盘空间或权限：\n{exc}')
        sys.exit(1)
    if '--autostart' in sys.argv and not DATA.exists() and not DATA.with_suffix('.bak').exists():
        sys.exit(0)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(DATA.parent / 'calendar.lock'))
    if not lock.tryLock(100):
        if '--autostart' not in sys.argv:
            QMessageBox.information(None, '玻璃日历', '玻璃日历已经在运行，请从系统托盘打开。')
        sys.exit(0)
    window = GlassCalendar(); window.show()
    sys.exit(app.exec())

