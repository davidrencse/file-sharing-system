from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .core import Relay, parts, valid_name


def data_dir() -> Path:
    override = os.environ.get("TELEGRAM_RELAY_HOME")
    root = (
        Path(override)
        if override
        else Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(root, 0o700)
    return root


class Worker(QThread):
    ready = Signal(str)
    auth_step = Signal(str)
    listing = Signal(str, object)
    progress = Signal(str, int, int)
    complete = Signal(str)
    failed = Signal(str)

    def __init__(self, home: Path):
        super().__init__()
        self.home = home
        self.loop = None
        self.queue = None
        self.client = None
        self.relay = None
        self.current_path = "/"
        self.credentials = {}
        self.started = threading.Event()
        self.active = False

    def submit(self, action: str, *args) -> None:
        self.started.wait(5)
        if self.isRunning() and self.loop and self.queue and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.queue.put_nowait, (action, args))
        else:
            self.failed.emit("Background worker could not start")

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue()
        self.started.set()
        while True:
            action, args = await self.queue.get()
            if action == "stop":
                break
            self.active = True
            try:
                await getattr(self, "do_" + action)(*args)
            except Exception as exc:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
            finally:
                self.active = False
        if self.client:
            await self.client.disconnect()

    async def do_connect(self, api_id: str, api_hash: str, phone: str) -> None:
        from telethon import TelegramClient

        if not api_id.isdigit() or not api_hash.strip() or not phone.strip():
            raise ValueError("Enter your API ID, API hash, and phone number")
        if self.client:
            await self.client.disconnect()
        self.credentials = {
            "api_id": api_id,
            "api_hash": api_hash.strip(),
            "phone": phone.strip(),
        }
        self.client = TelegramClient(
            str(self.home / "account"),
            int(api_id),
            api_hash.strip(),
            device_model="Telegram Relay",
            flood_sleep_threshold=300,
        )
        await self.client.connect()
        if await self.client.is_user_authorized():
            await self._authorized()
        else:
            await self.client.send_code_request(phone.strip())
            self.auth_step.emit("code")

    async def do_code(self, code: str) -> None:
        from telethon.errors import SessionPasswordNeededError

        try:
            await self.client.sign_in(
                phone=self.credentials["phone"], code=code.strip()
            )
        except SessionPasswordNeededError:
            self.auth_step.emit("password")
            return
        await self._authorized()

    async def do_password(self, password: str) -> None:
        await self.client.sign_in(password=password)
        await self._authorized()

    async def _authorized(self) -> None:
        config = self.home / "config.json"
        config.write_text(json.dumps(self.credentials), encoding="utf-8")
        if os.name != "nt":
            os.chmod(config, 0o600)
            session = self.home / "account.session"
            if session.exists():
                os.chmod(session, 0o600)
        self.relay = Relay(
            self.client, lambda name, done, total: self.progress.emit(name, done, total)
        )
        await self.relay.refresh()
        me = await self.client.get_me()
        self.ready.emit(me.first_name or me.username or "Telegram account")
        await self.do_list("/")

    async def do_list(self, path: str) -> None:
        entries = self.relay.list_folder(path)
        self.current_path = path
        self.listing.emit(path, entries)

    async def do_refresh(self) -> None:
        await self.relay.refresh()
        await self.relay.retry_pending_deletes()
        if (
            self.current_path != "/"
            and self.current_path not in self.relay.index["entries"]
        ):
            self.current_path = "/"
        await self.do_list(self.current_path)
        self.complete.emit("Up to date with Telegram")

    async def do_mkdir(self, name: str) -> None:
        await self.relay.refresh()
        await self.relay.new_folder(self.current_path, name)
        await self.do_list(self.current_path)
        self.complete.emit("Folder created")

    async def do_send(self, sources: list[str]) -> None:
        await self.relay.refresh()
        for source in sources:
            await self.relay.send(Path(source), self.current_path)
            await self.do_list(self.current_path)
        self.complete.emit("Upload finished")

    async def do_receive(self, path: str, destination: str) -> None:
        await self.relay.refresh()
        local = await self.relay.receive(path, Path(destination))
        self.complete.emit(f"Saved to {local}")

    async def do_remove(self, path: str) -> None:
        await self.relay.refresh()
        await self.relay.remove(path)
        await self.do_list(self.current_path)
        self.complete.emit("Removed from Telegram")


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Telegram Relay")
        self.resize(760, 600)
        self.home = data_dir()
        self.worker = Worker(self.home)
        self.worker.ready.connect(self.on_ready)
        self.worker.auth_step.connect(self.on_auth_step)
        self.worker.listing.connect(self.on_listing)
        self.worker.progress.connect(self.on_progress)
        self.worker.complete.connect(self.on_complete)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()
        self.busy = False
        self._build()
        config = self.home / "config.json"
        if config.exists():
            try:
                saved = json.loads(config.read_text(encoding="utf-8"))
                self.api_id.setText(saved["api_id"])
                self.api_hash.setText(saved["api_hash"])
                self.phone.setText(saved["phone"])
                self.connect_account()
            except (KeyError, ValueError, OSError):
                pass

    def _build(self):
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.login = QWidget()
        form = QVBoxLayout(self.login)
        form.setContentsMargins(40, 36, 40, 36)
        title = QLabel("Connect your Telegram account")
        title.setObjectName("title")
        form.addWidget(title)
        help_text = QLabel(
            "Use the same account on both computers. Create an API ID and hash at my.telegram.org/apps, then sign in here."
        )
        help_text.setWordWrap(True)
        form.addWidget(help_text)
        self.api_id = self._field(form, "API ID", "123456")
        self.api_hash = self._field(form, "API hash", "From my.telegram.org/apps")
        self.phone = self._field(form, "Phone number", "+1 555 123 4567")
        self.code = self._field(form, "Login code", "Code sent by Telegram")
        self.code_group = self.code.parentWidget()
        self.code_group.hide()
        self.password = self._field(
            form,
            "Two-step verification password",
            "Your Telegram password",
            secret=True,
        )
        self.password_group = self.password.parentWidget()
        self.password_group.hide()
        self.sign_in = QPushButton("Send login code")
        self.sign_in.clicked.connect(self.connect_account)
        form.addWidget(self.sign_in)
        form.addStretch()
        self.stack.addWidget(self.login)

        self.browser = QWidget()
        body = QVBoxLayout(self.browser)
        body.setContentsMargins(28, 24, 28, 24)
        header = QHBoxLayout()
        heading = QLabel("Your transfer space")
        heading.setObjectName("title")
        header.addWidget(heading)
        header.addStretch()
        self.account = QLabel("")
        header.addWidget(self.account)
        body.addLayout(header)
        self.path_label = QLabel("/")
        self.path_label.setObjectName("path")
        body.addWidget(self.path_label)
        toolbar = QHBoxLayout()
        self.up_button = self._button(toolbar, "Up", self.go_up)
        self._button(toolbar, "Send files", self.pick_files)
        self._button(toolbar, "Send folder", self.pick_folder)
        self._button(toolbar, "New folder", self.new_folder)
        self._button(toolbar, "Refresh", lambda: self.run_action("refresh"))
        body.addLayout(toolbar)
        self.items = QListWidget()
        self.items.itemDoubleClicked.connect(self.open_item)
        self.items.setAlternatingRowColors(True)
        body.addWidget(self.items)
        actions = QHBoxLayout()
        self.download_button = self._button(
            actions, "Download selected", self.download_selected
        )
        self.remove_button = self._button(
            actions, "Remove selected", self.remove_selected
        )
        actions.addStretch()
        body.addLayout(actions)
        self.progress_text = QLabel("Ready. Send a file or folder to begin.")
        body.addWidget(self.progress_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.hide()
        body.addWidget(self.progress_bar)
        self.stack.addWidget(self.browser)
        self.stack.setCurrentWidget(self.login)
        self.path = "/"
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f7f9fc; color: #14263b; font-size: 14px; }
            QLabel#title { font-size: 25px; font-weight: 700; margin-bottom: 10px; }
            QLabel#path { color: #47647e; font-size: 13px; padding: 8px 0; }
            QLineEdit, QListWidget { background: white; border: 1px solid #cbd8e5; border-radius: 7px; padding: 9px; }
            QListWidget { padding: 4px; }
            QListWidget::item { padding: 11px 8px; }
            QListWidget::item:selected { background: #d8eaff; color: #102c49; }
            QPushButton { background: #e5edf5; border: 0; border-radius: 7px; padding: 10px 14px; font-weight: 600; }
            QPushButton:hover { background: #d5e5f4; }
            QPushButton:pressed { background: #bed8ef; }
            QPushButton:disabled { color: #8295a8; }
            QPushButton:focus, QLineEdit:focus, QListWidget:focus { border: 2px solid #2475bf; }
            QProgressBar { border: 0; background: #dbe6f0; border-radius: 5px; height: 9px; text-align: center; }
            QProgressBar::chunk { background: #2475bf; border-radius: 5px; }
        """)

    def _field(self, layout, label: str, placeholder: str, secret=False):
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 7, 0, 7)
        column.addWidget(QLabel(label))
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        if secret:
            field.setEchoMode(QLineEdit.EchoMode.Password)
        column.addWidget(field)
        layout.addWidget(group)
        return field

    def _button(self, layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def connect_account(self):
        self.sign_in.setEnabled(False)
        self.sign_in.setText("Connecting…")
        self.worker.submit(
            "connect",
            self.api_id.text().strip(),
            self.api_hash.text().strip(),
            self.phone.text().strip(),
        )

    def on_auth_step(self, step):
        self.sign_in.setEnabled(True)
        if step == "code":
            self.code_group.show()
            self.sign_in.setText("Confirm code")
            try:
                self.sign_in.clicked.disconnect()
            except RuntimeError:
                pass
            self.sign_in.clicked.connect(
                lambda: self._submit_auth("code", self.code.text())
            )
            self.code.setFocus()
        else:
            self.password_group.show()
            self.sign_in.setText("Unlock account")
            try:
                self.sign_in.clicked.disconnect()
            except RuntimeError:
                pass
            self.sign_in.clicked.connect(
                lambda: self._submit_auth("password", self.password.text())
            )
            self.password.setFocus()

    def _submit_auth(self, action, value):
        self.sign_in.setEnabled(False)
        self.worker.submit(action, value)

    def on_ready(self, name):
        self.account.setText(name)
        self.stack.setCurrentWidget(self.browser)
        self.progress_text.setText(
            "Connected. Refresh on either computer to see the latest files."
        )

    def on_listing(self, path, entries):
        self.path = path
        self.path_label.setText(path)
        self.up_button.setEnabled(path != "/")
        self.items.clear()
        for remote, entry in entries:
            name = parts(remote)[-1]
            detail = (
                "Folder" if entry["type"] == "folder" else self.size_text(entry["size"])
            )
            item = QListWidgetItem(f"{name}    ·    {detail}")
            item.setData(Qt.ItemDataRole.UserRole, (remote, entry["type"]))
            self.items.addItem(item)

    @staticmethod
    def size_text(size):
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024

    def selected(self):
        item = self.items.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def open_item(self, item):
        remote, kind = item.data(Qt.ItemDataRole.UserRole)
        if kind == "folder":
            self.run_action("list", remote)
        else:
            self.download_selected()

    def go_up(self):
        parent = "/" + "/".join(parts(self.path)[:-1])
        self.run_action("list", parent)

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Choose files to send")
        if files:
            self.run_action("send", files)

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to send")
        if folder:
            self.run_action("send", [folder])

    def new_folder(self):
        name, accepted = QInputDialog.getText(self, "New folder", "Folder name")
        if accepted:
            if valid_name(name):
                self.run_action("mkdir", name)
            else:
                QMessageBox.warning(
                    self,
                    "Invalid name",
                    "Use a folder name without slashes or backslashes.",
                )

    def download_selected(self):
        selection = self.selected()
        if not selection:
            return
        destination = QFileDialog.getExistingDirectory(self, "Save into folder")
        if destination:
            self.run_action("receive", selection[0], destination)

    def remove_selected(self):
        selection = self.selected()
        if not selection:
            return
        if (
            QMessageBox.question(
                self,
                "Remove from Telegram",
                f"Remove {parts(selection[0])[-1]} and its contents from this transfer space?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.run_action("remove", selection[0])

    def run_action(self, action, *args):
        if self.busy:
            return
        if action not in {"list", "refresh"}:
            self.busy = True
            self.progress_bar.show()
            self.progress_bar.setRange(0, 0)
            self.progress_text.setText("Working with Telegram…")
        self.worker.submit(action, *args)

    def on_progress(self, name, done, total):
        self.progress_bar.show()
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(done)
        self.progress_text.setText(
            f"{name} · {self.size_text(done)} of {self.size_text(total)}"
        )

    def on_complete(self, message):
        self.busy = False
        self.progress_bar.hide()
        self.progress_text.setText(message)

    def on_failed(self, message):
        self.busy = False
        self.progress_bar.hide()
        self.sign_in.setEnabled(True)
        self.progress_text.setText(message)
        QMessageBox.warning(self, "Telegram Relay", message)

    def closeEvent(self, event):
        if self.busy or self.worker.active:
            event.ignore()
            QMessageBox.information(
                self,
                "Transfer in progress",
                "Wait for the current transfer to finish before closing.",
            )
            return
        if self.worker.isRunning():
            self.worker.submit("stop")
        if not self.worker.wait(5000):
            event.ignore()
            QMessageBox.information(
                self,
                "Still connected",
                "Telegram is still closing the connection. Try again in a moment.",
            )
            return
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Telegram Relay")
    app.setOrganizationName("Telegram Relay")
    app.setFont(QFont("Segoe UI" if sys.platform == "win32" else "Noto Sans", 10))
    window = Window()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
