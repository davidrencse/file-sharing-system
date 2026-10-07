# Telegram Relay

A small desktop app for moving files and folders between Windows 11 and Arch Linux through one Telegram account. Sign in on both computers, send a file or a whole folder on one, press **Refresh** on the other, and download it there. Folder structure and empty folders are preserved. Files are split into 8 MiB Telegram documents, and downloads are checked with SHA-256 before being made available.

This is inspired by [tgfs](https://github.com/davidrencse/telegram-file-storage-system): Telegram Saved Messages holds both the file chunks and a folder index. Relay has its **own index format**, so it does not read or modify a tgfs index.

## Run from source

Install Python 3.10 or newer. On Arch Linux, install `python` and `python-pip`; on Windows, install Python from python.org with the launcher enabled.

```powershell
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m telegram_relay
```

On Arch Linux:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m telegram_relay
```

At first launch, create a Telegram API ID and API hash at [my.telegram.org/apps](https://my.telegram.org/apps). Enter them with your phone number, then the Telegram login code and two-step password if asked. Repeat on the other computer with the same Telegram account. You can use the same API ID and hash. The app remembers them locally and stores your Telegram session in your platform app-data folder. `TELEGRAM_RELAY_HOME` can override that location.

## Build an app bundle

Run the build on each target OS; PyInstaller cannot cross-compile. Each command writes one self-contained application into `dist/`:

```powershell
# Windows 11
.\.venv\Scripts\python -m pip install -e ".[build]"
.\.venv\Scripts\pyinstaller --noconfirm --clean --windowed --onefile --name TelegramRelay launcher.py
```

```sh
# Arch Linux
.venv/bin/python -m pip install -e '.[build]'
.venv/bin/pyinstaller --noconfirm --clean --windowed --onefile --name telegram-relay launcher.py
```

The Windows result is `dist/TelegramRelay.exe`; on Arch Linux it is `dist/telegram-relay`. Both can be copied to another computer of the same OS and architecture. Linux bundles still need the ordinary desktop system libraries used by Qt.

## Practical notes

- This uses a Telegram **user account**, not a bot. Transfers require an internet connection and are limited by Telegram's service limits and rate limits.
- Relay stores files in Telegram cloud Saved Messages. It does not add its own end-to-end encryption. Do not use it for sensitive material unless you encrypt the files first.
- A new index snapshot is sent after each added file or folder. Refresh before working on another computer. The app rejects a write if the index changed since it was loaded, but Telegram has no atomic update operation, so avoid making changes on both computers at exactly the same time.
- Uploads with an existing name get a numbered name. Downloads never overwrite local files; they also get a numbered name.
- Symbolic links are skipped during folder upload. File permissions and modification times are not preserved.
- File and folder names must be valid on both Windows and Linux. Names Windows cannot create are rejected before upload.
- Removing an item deletes its file chunks from Telegram. If deletion fails, press **Refresh** to retry the pending cleanup. Older index snapshots remain, but they cannot restore chunks that were removed. Clearing Saved Messages manually can break downloads.

## Core checks

From the project root, run `PYTHONPATH=src python -m unittest discover -s tests -v` on Linux, or `$env:PYTHONPATH='src'; python -m unittest discover -s tests -v` in PowerShell. These checks use an in-memory Telegram stand-in and do not require account access.
"# file-sharing-system" 
