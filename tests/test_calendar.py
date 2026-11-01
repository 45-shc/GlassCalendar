import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import json
import sys
import tempfile
import unittest
import uuid
import winreg
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication
import main


class FakeTray:
    def __init__(self): self.messages = []
    def showMessage(self, *args): self.messages.append(args)


class CalendarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        startup_dir = Path(self.temp.name) / 'startup'
        self.startup_dir_patch = patch.object(main, 'STARTUP_DIR', startup_dir)
        self.startup_retry_patch = patch.object(main, 'STARTUP_RETRY', startup_dir / 'GlassCalendar-retry.vbs')
        self.startup_dir_patch.start(); self.startup_retry_patch.start()
        self.addCleanup(self.startup_dir_patch.stop); self.addCleanup(self.startup_retry_patch.stop)
        self.old_data = main.DATA
        main.DATA = Path(self.temp.name) / 'data.json'
        with patch.object(main.GlassCalendar, 'setup_tray'):
            self.window = main.GlassCalendar()
        self.window.timer.stop()
        self.window.tray = FakeTray()

    def tearDown(self):
        self.window.timer.stop()
        main.DATA = self.old_data
        self.temp.cleanup()

    def test_save_reload_and_backup(self):
        self.window.data['events']['2026-11-08'] = '开会'
        self.window.save()
        self.window.data['events']['2026-11-08'] = '提交'
        self.window.save()
        self.assertEqual(json.loads(main.DATA.read_text(encoding='utf-8'))['events']['2026-11-08'], '提交')
        self.assertEqual(json.loads(main.DATA.with_suffix('.bak').read_text(encoding='utf-8'))['events']['2026-11-08'], '开会')

    def test_migration_preserves_data_and_does_not_replace_existing(self):
        legacy = Path(self.temp.name) / 'old' / 'data.json'
        target = Path(self.temp.name) / 'new' / 'data.json'
        legacy.parent.mkdir()
        legacy.write_text('{"events":{"2026-11-08":"保留"}}', encoding='utf-8')
        legacy.with_suffix('.bak').write_text('{"events":{}}', encoding='utf-8')
        main.migrate_data(legacy, target)
        self.assertEqual(main.read_data(target)['events']['2026-11-08'], '保留')
        self.assertTrue(target.with_suffix('.bak').exists())
        legacy.write_text('{}', encoding='utf-8')
        main.migrate_data(legacy, target)
        self.assertEqual(main.read_data(target)['events']['2026-11-08'], '保留')

    def test_remind_once_and_skip_completed_or_future(self):
        past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec='seconds')
        future = (datetime.now() + timedelta(days=1)).isoformat(timespec='seconds')
        self.window.data['todos'] = [
            {'text': '到期事项', 'due': past, 'done': False},
            {'text': '已完成', 'due': past, 'done': True},
            {'text': '未来事项', 'due': future, 'done': False},
        ]
        self.window.check_reminders(); self.window.check_reminders()
        self.assertEqual(len(self.window.tray.messages), 1)
        self.assertTrue(self.window.data['todos'][0]['notified'])
        self.assertNotIn('notified', self.window.data['todos'][1])
        self.assertNotIn('notified', self.window.data['todos'][2])

    def test_month_navigation_crosses_year(self):
        from datetime import date
        self.window.current = date(2026, 12, 1)
        self.window.shift(1)
        self.assertEqual(self.window.current, date(2027, 1, 1))

    def test_calendar_shrinks_after_six_week_month(self):
        from datetime import date
        self.window.current = date(2026, 11, 1)
        self.window.refresh()
        self.assertEqual(self.window.grid.rowStretch(5), 1)
        self.window.current = date(2026, 12, 1)
        self.window.refresh()
        self.assertEqual(self.window.grid.rowStretch(5), 0)

    def test_unchecking_rearms_reminder(self):
        self.window.data['todos'] = [{'text': '复核', 'done': True, 'notified': True}]
        self.window.toggle('todos', 0, False)
        self.assertFalse(self.window.data['todos'][0]['done'])
        self.assertNotIn('notified', self.window.data['todos'][0])

    def test_item_dialog_due_round_trip(self):
        item = {'text': '交作业', 'due': '2026-11-08T18:30:00'}
        dialog = main.ItemDialog(self.window, '测试', item)
        self.assertEqual(dialog.value(), item)
        dialog.has_due.setChecked(False)
        self.assertEqual(dialog.value()['due'], '')

    def test_event_add_edit_and_delete(self):
        from datetime import date
        day = date(2026, 11, 8)
        with patch.object(main.DayDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.DayDialog, 'value', return_value=('会议', {'circle': True, 'fill': True, 'color': '#f6c975'})):
            self.window.edit_event(day)
        self.assertEqual(self.window.data['events'][day.isoformat()], '会议')
        self.assertTrue(self.window.data['marks'][day.isoformat()]['circle'])
        self.assertTrue(self.window.data['marks'][day.isoformat()]['fill'])
        with patch.object(main.DayDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.DayDialog, 'value', return_value=('', {'circle': False, 'fill': False, 'color': '#f6c975'})):
            self.window.edit_event(day)
        self.assertNotIn(day.isoformat(), self.window.data['events'])
        self.assertNotIn(day.isoformat(), self.window.data['marks'])

    def test_date_click_opens_editable_dialog_and_saves(self):
        from PySide6.QtCore import QTimer
        from datetime import date
        day = date(2026, 11, 8)
        self.window.current = day.replace(day=1)
        self.window.refresh()
        self.window.show(); self.app.processEvents()
        button = next(b for b in self.window.findChildren(main.DayButton) if b.day == day and b.isVisible())
        def edit():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, main.DayDialog)
            dialog.notes.setPlainText('实际编辑测试')
            dialog.accept()
        QTimer.singleShot(0, edit)
        button.click()
        self.assertEqual(self.window.data['events'][day.isoformat()], '实际编辑测试')
        self.assertEqual(json.loads(main.DATA.read_text(encoding='utf-8'))['events'][day.isoformat()], '实际编辑测试')

    def test_day_mark_persists_without_event(self):
        from datetime import date
        day = date(2026, 11, 9)
        with patch.object(main.DayDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.DayDialog, 'value', return_value=('', {'circle': True, 'fill': False, 'color': '#a6d9d0'})):
            self.window.edit_event(day)
        saved = json.loads(main.DATA.read_text(encoding='utf-8'))
        self.assertEqual(saved['marks'][day.isoformat()]['color'], '#a6d9d0')
        self.assertTrue(saved['marks'][day.isoformat()]['circle'])

    def test_day_dialog_restores_text_and_marks(self):
        from datetime import date
        mark = {'circle': True, 'fill': True, 'color': '#a6d9d0'}
        dialog = main.DayDialog(self.window, date(2026, 11, 8), '会议\n提交报告', mark)
        self.assertEqual(dialog.value(), ('会议\n提交报告', mark))

    def test_saved_mark_changes_variant_without_losing_its_color(self):
        from datetime import date
        day = date(2026, 11, 8)
        key = day.isoformat()
        self.window.data['marks'][key] = {'circle': True, 'fill': True, 'color': '#a6d9d0', 'variant': 3}
        with patch.object(main.DayDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.DayDialog, 'value', return_value=('事项', {'circle': True, 'fill': True, 'color': '#a6d9d0'})):
            self.window.edit_event(day)
        mark = json.loads(main.DATA.read_text(encoding='utf-8'))['marks'][key]
        self.assertNotEqual(mark['variant'], 3)
        self.assertEqual(mark['color'], '#a6d9d0')

    def test_mark_render_is_stable_across_repaints(self):
        from datetime import date
        mark = {'circle': True, 'fill': True, 'color': '#a6d9d0', 'variant': 3}
        button = main.DayButton(date(2026, 11, 8), True, '事项', mark)
        button.resize(120, 90)
        first = button.grab().toImage()
        second = button.grab().toImage()
        self.assertEqual(first, second)

    def test_glass_corners_stay_transparent(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        self.window.resize(900, 780)
        image = QImage(self.window.size(), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        self.window.render(image)
        for x, y in ((0, 0), (899, 0), (0, 779), (899, 779)):
            self.assertEqual(image.pixelColor(x, y).alpha(), 0)
        self.assertLess(image.pixelColor(450, 400).alpha(), 255)

    def test_generated_marker_assets_can_be_tinted(self):
        from PySide6.QtGui import QColor
        for variant in range(2):
            brush = main.marker_brush(variant, QColor('#f2a9a4'))
            self.assertFalse(brush.isNull())
            self.assertTrue(brush.hasAlphaChannel())

    def test_application_icon_is_available(self):
        self.assertFalse(self.window.app_icon.isNull())

    def test_calendar_uses_normal_window_for_editing(self):
        from PySide6.QtCore import Qt
        self.assertEqual(self.window.windowFlags() & Qt.WindowType.WindowType_Mask, Qt.WindowType.Window)

    def test_bottom_toggle_keeps_normal_window_type(self):
        from PySide6.QtCore import Qt
        self.window.set_bottom(True)
        self.assertEqual(self.window.windowFlags() & Qt.WindowType.WindowType_Mask, Qt.WindowType.Window)
        self.window.set_bottom(False)
        self.assertEqual(self.window.windowFlags() & Qt.WindowType.WindowType_Mask, Qt.WindowType.Window)

    def test_cancel_day_edit_preserves_existing_data(self):
        from datetime import date
        day = date(2026, 11, 8)
        self.window.data['events'][day.isoformat()] = '原有内容'
        with patch.object(main.DayDialog, 'exec', return_value=main.QDialog.DialogCode.Rejected):
            self.window.edit_event(day)
        self.assertEqual(self.window.data['events'][day.isoformat()], '原有内容')

    def test_add_edit_complete_and_delete_item(self):
        with patch.object(main.ItemDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.ItemDialog, 'value', return_value={'text': '完成论文', 'due': ''}):
            self.window.add_item('todos')
        self.assertEqual(self.window.data['todos'][0]['text'], '完成论文')
        with patch.object(main.ItemDialog, 'exec', return_value=main.QDialog.DialogCode.Accepted), \
             patch.object(main.ItemDialog, 'value', return_value={'text': '修改论文', 'due': ''}):
            self.window.edit_item('todos', 0)
        self.window.toggle('todos', 0, True)
        self.assertEqual(self.window.data['todos'][0]['text'], '修改论文')
        self.assertTrue(self.window.data['todos'][0]['done'])
        with patch.object(self.window, 'confirm_delete', return_value=True):
            self.window.delete_item('todos', 0)
        self.assertEqual(self.window.data['todos'], [])

    def test_long_todo_delete_button_is_visible_and_confirmation_works(self):
        from PySide6.QtCore import QTimer
        self.window.resize(700, 620)
        self.window.data['todos'].append({'text': '一个很长的待办事项' * 12, 'due': '2026-12-11T23:59:00', 'done': False})
        self.window.refresh(); self.window.show(); self.app.processEvents()
        scroll = self.window.todo_layout.parentWidget().parentWidget().parentWidget()
        row = self.window.todo_layout.itemAt(0).widget()
        delete = row.findChildren(main.QPushButton)[-1]
        self.assertLessEqual(delete.mapTo(scroll, delete.rect().bottomRight()).x(), scroll.width())
        observed = []
        def confirm():
            dialog = QApplication.activeModalWidget()
            observed.append(dialog)
            if isinstance(dialog, main.QMessageBox):
                dialog.button(main.QMessageBox.StandardButton.Yes).click()
            elif dialog is not None:
                dialog.accept()
        QTimer.singleShot(0, confirm)
        delete.click()
        self.assertTrue(observed)
        self.assertEqual(self.window.data['todos'], [])

    def test_backup_restores_after_invalid_primary(self):
        self.window.data['events']['2026-11-01'] = '保留的内容'
        self.window.save(); self.window.save()
        main.DATA.write_text('{broken', encoding='utf-8')
        with patch.object(main.GlassCalendar, 'setup_tray'), patch.object(main.QMessageBox, 'warning'):
            recovered = main.GlassCalendar()
        recovered.timer.stop()
        self.assertEqual(recovered.data['events']['2026-11-01'], '保留的内容')
        recovered.save()
        self.assertEqual(json.loads(main.DATA.with_suffix('.bak').read_text(encoding='utf-8'))['events']['2026-11-01'], '保留的内容')

    def test_missing_primary_recovers_existing_backup(self):
        self.window.data['events']['2026-11-01'] = '需要保留'
        self.window.save(); self.window.save()
        main.DATA.unlink()
        with patch.object(main.GlassCalendar, 'setup_tray'), patch.object(main.QMessageBox, 'warning'):
            recovered = main.GlassCalendar()
        recovered.timer.stop()
        self.assertEqual(recovered.data['events']['2026-11-01'], '需要保留')

    def test_wrong_json_structure_recovers_backup(self):
        self.window.data['events']['2026-11-01'] = '需要保留'
        self.window.save(); self.window.save()
        main.DATA.write_text('[]', encoding='utf-8')
        with patch.object(main.GlassCalendar, 'setup_tray'), patch.object(main.QMessageBox, 'warning'):
            recovered = main.GlassCalendar()
        recovered.timer.stop()
        self.assertEqual(recovered.data['events']['2026-11-01'], '需要保留')

    def test_failed_flush_keeps_previous_data(self):
        self.window.data['events']['2026-11-01'] = '原来的数据'
        self.window.save()
        self.window.data['events']['2026-11-01'] = '尚未安全写入'
        with patch.object(main.os, 'fsync', side_effect=OSError('disk error')), patch.object(main.QMessageBox, 'critical') as error:
            self.window.save()
        error.assert_called_once()
        self.assertEqual(json.loads(main.DATA.read_text(encoding='utf-8'))['events']['2026-11-01'], '原来的数据')

    def test_failed_backup_flush_preserves_original_and_backup(self):
        self.window.data['events']['2026-11-01'] = '已保存内容'
        self.window.save(); self.window.save()
        self.window.data['events']['2026-11-01'] = '未完成保存'
        with patch.object(main.os, 'fsync', side_effect=[None, OSError('backup failed')]), patch.object(main.QMessageBox, 'critical') as error:
            self.window.save()
        error.assert_called_once()
        self.assertEqual(json.loads(main.DATA.read_text(encoding='utf-8'))['events']['2026-11-01'], '已保存内容')
        self.assertEqual(json.loads(main.DATA.with_suffix('.bak').read_text(encoding='utf-8'))['events']['2026-11-01'], '已保存内容')

    def test_fixed_button_locks_drag_and_restores_after_restart(self):
        from PySide6.QtCore import QPoint, QPointF, Qt
        self.window.pin_button.click()
        self.assertTrue(self.window.data['settings']['fixed'])
        self.assertTrue(self.window.grip.isHidden())
        class Mouse:
            def button(self): return Qt.MouseButton.LeftButton
            def position(self): return QPointF(10, 10)
            def globalPosition(self): return QPointF(100, 100)
        self.window.mousePressEvent(Mouse())
        self.assertIsNone(self.window.drag)
        with patch.object(main.GlassCalendar, 'setup_tray'):
            recovered = main.GlassCalendar()
        recovered.timer.stop()
        self.assertTrue(recovered.pin_button.isChecked())
        recovered.pin_button.click()
        self.assertFalse(recovered.data['settings']['fixed'])

    def test_temporarily_missing_screen_preserves_and_restores_geometry(self):
        desired = QRect(2000, 50, 900, 780)
        self.window.pending_geometry = desired
        self.window.fallback_geometry = self.window.geometry()
        self.window.save_geometry()
        self.assertEqual(self.window.data['settings']['geometry'], [2000, 50, 900, 780])
        class Screen:
            def availableGeometry(self): return QRect(1920, 0, 1920, 1080)
        with patch.object(QApplication, 'screens', return_value=[Screen()]):
            self.window.restore_pending_geometry()
        self.assertEqual(self.window.geometry(), desired)
        self.assertIsNone(self.window.pending_geometry)

    def test_startup_with_missing_screen_uses_temporary_position(self):
        original = [2000, 50, 900, 780]
        main.DATA.write_text(json.dumps({'settings': {'geometry': original}}), encoding='utf-8')
        class Screen:
            def availableGeometry(self): return QRect(0, 0, 1600, 900)
        screen = Screen()
        with patch.object(QApplication, 'screens', return_value=[screen]), patch.object(QApplication, 'primaryScreen', return_value=screen), patch.object(main.GlassCalendar, 'setup_tray'), patch.object(main.QTimer, 'singleShot'):
            recovered = main.GlassCalendar()
        recovered.timer.stop()
        self.assertEqual(recovered.geometry().topLeft().x(), 30)
        self.assertEqual(recovered.pending_geometry, QRect(*original))
        recovered.save_geometry()
        self.assertEqual(json.loads(main.DATA.read_text(encoding='utf-8'))['settings']['geometry'], original)

    def test_user_move_prevents_late_geometry_restore(self):
        self.window.pending_geometry = QRect(2000, 50, 900, 780)
        self.window.fallback_geometry = self.window.geometry()
        self.window.move(self.window.x() + 40, self.window.y())
        moved = self.window.geometry()
        self.window.restore_pending_geometry()
        self.assertIsNone(self.window.pending_geometry)
        self.assertEqual(self.window.geometry(), moved)
        self.window.save_geometry()
        self.assertEqual(self.window.data['settings']['geometry'][:2], [moved.x(), moved.y()])

    def test_autostart_writes_and_removes_only_its_own_value(self):
        key_path = 'Software\\GlassCalendarTest_' + uuid.uuid4().hex
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path).Close()
        try:
            with patch.object(main, 'RUN_KEY', key_path):
                self.window.set_autostart(True)
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, 'GlassCalendar')
                self.assertIn('main.py', value)
                script = main.STARTUP_RETRY.read_text(encoding='utf-16')
                self.assertIn('WScript.Sleep 45000', script)
                self.assertIn('--autostart', script)
                self.assertIn(value.replace('"', '""'), script)
                with patch.object(main.os, 'replace') as replace:
                    self.window.set_autostart(True)
                    replace.assert_not_called()
                self.window.set_autostart(False)
                self.assertFalse(main.STARTUP_RETRY.exists())
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    with self.assertRaises(FileNotFoundError):
                        winreg.QueryValueEx(key, 'GlassCalendar')
        finally:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)

    def test_autostart_toolbar_and_tray_stay_in_sync(self):
        key_path = 'Software\\GlassCalendarTest_' + uuid.uuid4().hex
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path).Close()
        try:
            with patch.object(main, 'RUN_KEY', key_path):
                self.window.setup_tray()
                self.assertFalse(self.window.start_button.isChecked())
                self.window.start_button.click()
                self.assertTrue(self.window.start_action.isChecked())
                self.assertEqual(self.window.start_button.text(), '自启：开')
                self.window.start_action.trigger()
                self.assertFalse(self.window.start_button.isChecked())
                self.assertEqual(self.window.start_button.text(), '自启：关')
                self.window.tray.hide()
        finally:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)

    def test_enabled_autostart_updates_to_current_executable(self):
        key_path = 'Software\\GlassCalendarTest_' + uuid.uuid4().hex
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, 'GlassCalendar', 0, winreg.REG_SZ, 'old-path')
        try:
            with patch.object(main, 'RUN_KEY', key_path):
                self.window.setup_tray()
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, 'GlassCalendar')
                self.assertIn('main.py', value)
                self.assertTrue(self.window.start_button.isChecked())
                self.window.tray.hide()
        finally:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)

    def test_startup_retry_keeps_autostart_enabled_without_run_value(self):
        key_path = 'Software\\GlassCalendarTest_' + uuid.uuid4().hex
        winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path).Close()
        main.STARTUP_DIR.mkdir()
        main.STARTUP_RETRY.write_text('old startup', encoding='utf-16')
        try:
            with patch.object(main, 'RUN_KEY', key_path):
                self.window.setup_tray()
                self.assertTrue(self.window.start_button.isChecked())
                self.assertIn('--autostart', main.STARTUP_RETRY.read_text(encoding='utf-16'))
                self.window.tray.hide()
        finally:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)

    def test_bottom_setting_is_saved(self):
        with patch.object(self.window, 'setWindowFlag'), patch.object(self.window, 'show'):
            self.window.set_bottom(True)
        self.assertTrue(json.loads(main.DATA.read_text(encoding='utf-8'))['settings']['bottom'])


if __name__ == '__main__': unittest.main()
