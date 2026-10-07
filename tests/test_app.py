import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from telegram_relay.app import Window


class DesktopSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setApplicationName("Telegram Relay Test")

    def test_window_starts_and_worker_stops(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"TELEGRAM_RELAY_HOME": home}):
                window = Window()
                self.app.processEvents()
                self.assertEqual(window.windowTitle(), "Telegram Relay")
                self.assertEqual(window.sign_in.text(), "Send login code")
                self.assertEqual(window.items.count(), 0)
                self.assertTrue(window.worker.isRunning())
                window.close()
                self.assertFalse(window.worker.isRunning())

    def test_close_waits_for_active_worker_action(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"TELEGRAM_RELAY_HOME": home}):
                window = Window()

                async def pause():
                    await asyncio.sleep(0.2)

                window.worker.do_pause = pause
                window.worker.submit("pause")
                deadline = time.monotonic() + 2
                while not window.worker.active and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(window.worker.active)
                with patch("telegram_relay.app.QMessageBox.information"):
                    self.assertFalse(window.close())
                self.assertTrue(window.worker.isRunning())
                while window.worker.active and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.01)
                self.assertTrue(window.close())
                self.assertFalse(window.worker.isRunning())


if __name__ == "__main__":
    unittest.main()
