import ctypes
import math
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QPoint
from PySide6.QtGui import QMoveEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import delete
import glass_refraction as glass
import main
from glass_refraction import DesktopRim


class RefractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rounded_thumbnail_bands_use_one_continuous_zoom(self):
        def fill(pointer, kind, *values):
            ctypes.cast(pointer, ctypes.POINTER(kind))[0] = kind(*values)
            return 1

        widget = QWidget(); widget.resize(900, 780); widget.show()
        rim = DesktopRim(widget, lambda: 12345)
        rim.poll.stop(); rim.timer.stop()
        thumbnail_properties = []
        def capture_properties(_handle, pointer):
            props = ctypes.cast(pointer, ctypes.POINTER(glass.THUMBNAIL)).contents
            thumbnail_properties.append((props.opacity, props.visible))
            return 0
        with patch.object(glass.user32, 'IsWindow', return_value=1), \
             patch.object(glass.user32, 'GetClientRect', side_effect=lambda _, p: fill(p, glass.RECT, 0, 0, 900, 780)), \
             patch.object(glass.user32, 'ClientToScreen', side_effect=lambda _, p: fill(p, glass.POINT, 100, 80)), \
             patch.object(glass.user32, 'GetWindowRect', side_effect=lambda _, p: fill(p, glass.RECT, 0, 0, 1920, 1080)), \
             patch.object(glass.user32, 'SetWindowRgn', return_value=1), \
             patch.object(glass.user32, 'SetWindowPos', return_value=1), \
             patch.object(glass.gdi32, 'CreateRoundRectRgn', return_value=99), \
             patch.object(glass.dwm, 'DwmRegisterThumbnail', side_effect=lambda _a, _b, p: 0 if fill(p, glass.wt.HANDLE, 7) else -1), \
             patch.object(glass.dwm, 'DwmQueryThumbnailSourceSize', side_effect=lambda _h, p: 0 if fill(p, glass.SIZE, 1920, 1080) else -1), \
             patch.object(glass.dwm, 'DwmUpdateThumbnailProperties', side_effect=capture_properties) as update, \
             patch.object(glass.dwm, 'DwmUnregisterThumbnail', return_value=0):
            rim.sync()
            self.assertGreater(len(rim.last_mapping), 1)
            self.assertEqual(len(rim.last_mapping), len(rim.handles))
            self.assertEqual(update.call_count, len(rim.last_mapping))
            self.assertTrue(all(props == (255, True) for props in thumbnail_properties))
            for (left, top, right, bottom), source, zoom, valid in rim.last_mapping:
                self.assertTrue(valid)
                self.assertAlmostEqual(zoom, .98)
                expected = (109 + left * zoom, 87.8 + top * zoom,
                            109 + right * zoom, 87.8 + bottom * zoom)
                for actual, want in zip(source, expected):
                    self.assertLessEqual(abs(actual - want), 1)
            self.assertTrue(rim.underlay.isVisible())
            rim.clear()
        rim.timer.stop(); widget.close()
        delete(rim.underlay); delete(widget)

    def test_bands_stay_inside_round_glass_and_cover_the_interior(self):
        self.assertIs(main.glass_radius, glass.glass_radius)
        for width, height, scale in ((700, 620, 1), (900, 780, 1), (1800, 1560, 2)):
            with self.subTest(width=width, scale=scale):
                radius = round(glass.glass_radius(width / scale) * scale)
                bands = glass.glass_bands(width, height, radius, scale)
                self.assertTrue(bands)
                for left, top, right, bottom in bands:
                    self.assertTrue(0 <= left < right <= width and 0 <= top < bottom <= height)
                    for x in (left + .5, right - .5):
                        for y in (top + .5, bottom - .5):
                            dx = max(0, radius - min(x, width - x))
                            dy = max(0, radius - min(y, height - y))
                            self.assertLessEqual(math.hypot(dx, dy), radius + 1)
                for y in range(scale, height - scale):
                    self.assertTrue(any(top <= y < bottom and left <= width // 2 < right
                                        for left, top, right, bottom in bands))
                for x, y in ((0, 0), (width - 1, 0), (0, height - 1),
                             (width - 1, height - 1)):
                    self.assertFalse(any(left <= x < right and top <= y < bottom
                                         for left, top, right, bottom in bands))
                for x, y in ((scale, height // 2), (width - scale - 1, height // 2),
                             (width // 2, scale), (width // 2, height - scale - 1)):
                    self.assertTrue(any(left <= x < right and top <= y < bottom
                                        for left, top, right, bottom in bands))

    def test_missing_source_hides_underlay(self):
        widget = QWidget(); widget.show()
        rim = DesktopRim(widget, lambda: 0)
        rim.poll.stop(); rim.timer.stop()
        rim.underlay.show()
        rim.sync()
        self.assertFalse(rim.underlay.isVisible())
        self.assertFalse(rim.handles)
        self.assertEqual(rim.last_mapping, [])
        rim.timer.stop(); widget.close()
        delete(rim.underlay); delete(widget)

    def test_cleanup_after_underlay_has_been_deleted(self):
        widget = QWidget()
        rim = DesktopRim(widget, lambda: 0)
        rim.poll.stop(); rim.timer.stop()
        delete(rim.underlay)
        rim.clear()
        self.assertFalse(rim.handles)
        delete(widget)

    def test_continuous_move_refreshes_before_drag_ends(self):
        class ProbeRim(DesktopRim):
            def __init__(self, widget):
                self.positions = []
                self.latest_position = 0
                super().__init__(widget, lambda: 0)
                self.poll.stop()
            def eventFilter(self, obj, event):
                if isinstance(event, QMoveEvent): self.latest_position = event.pos().x()
                return super().eventFilter(obj, event)
            def sync(self):
                self.positions.append(self.latest_position)

        widget = QWidget()
        rim = ProbeRim(widget)
        for i in range(40):
            QCoreApplication.sendEvent(widget, QMoveEvent(QPoint(i + 1, 0), QPoint(i, 0)))
            QTest.qWait(5)
        self.assertTrue(rim.positions, '拖动过程中应更新折射，而不是等拖动结束')
        QTest.qWait(50)
        self.assertEqual(rim.positions[-1], 40)
        rim.timer.stop()
        rim.clear()
        delete(rim.underlay); delete(widget)


if __name__ == '__main__':
    unittest.main()
