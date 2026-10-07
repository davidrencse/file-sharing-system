"""Telegram Saved Messages transport and a portable folder index.

This format is intentionally separate from tgfs. A single snapshot is the source of
truth, so users should refresh on the other computer before making changes there.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Callable

INDEX_TAG = "#telegram-relay-index-v1"
CHUNK_SIZE = 8 * 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024
Progress = Callable[[str, int, int], None]


def valid_name(name: str) -> bool:
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    return (
        bool(name)
        and name not in {".", ".."}
        and not name.endswith((" ", "."))
        and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
        and name.split(".", 1)[0].upper() not in reserved
    )


def parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Invalid remote path")
    result = path.split("/")[1:]
    if result == [""]:
        return []
    if any(not valid_name(p) for p in result):
        raise ValueError("Invalid remote path")
    return result


def child(parent: str, name: str) -> str:
    if not valid_name(name):
        raise ValueError(f"Unsafe file name: {name!r}")
    parts(parent)
    return parent.rstrip("/") + "/" + name


def validate_index(raw: object) -> dict:
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(raw.get("entries"), dict)
    ):
        raise ValueError("Unsupported or damaged transfer index")
    entries = raw["entries"]
    pending = raw.get("pending_deletes", [])
    if not isinstance(pending, list) or any(
        not isinstance(i, int) or i <= 0 for i in pending
    ):
        raise ValueError("Damaged pending deletions")
    if len(entries) > 100_000:
        raise ValueError("Transfer index is too large")
    folded = set()
    for path, entry in entries.items():
        if not isinstance(path, str) or not parts(path) or not isinstance(entry, dict):
            raise ValueError("Damaged transfer index")
        if path.casefold() in folded:
            raise ValueError("Folder contains names that collide on Windows")
        folded.add(path.casefold())
        if entry.get("type") == "folder":
            continue
        if (
            entry.get("type") != "file"
            or not isinstance(entry.get("size"), int)
            or entry["size"] < 0
        ):
            raise ValueError("Damaged file entry")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(
            r"[a-f0-9]{64}", entry["sha256"]
        ):
            raise ValueError("Damaged file checksum")
        if not isinstance(entry.get("chunks"), list) or any(
            not isinstance(i, int) or i <= 0 for i in entry["chunks"]
        ):
            raise ValueError("Damaged chunk references")
        if len(entry["chunks"]) != (entry["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE:
            raise ValueError("Damaged file size or chunk count")
    for path in entries:
        parent = "/" + "/".join(parts(path)[:-1])
        if parent != "/" and entries.get(parent, {}).get("type") != "folder":
            raise ValueError("Folder hierarchy is damaged")
    return raw


def unique_path(index: dict, parent: str, name: str) -> str:
    stem, suffix = os.path.splitext(name)
    proposed = child(parent, name)
    n = 2
    used = {path.casefold() for path in index["entries"]}
    while proposed.casefold() in used:
        proposed = child(parent, f"{stem} ({n}){suffix}")
        n += 1
    return proposed


def validate_source_tree(source: Path) -> None:
    """Reject names Windows cannot faithfully receive before sending any blobs."""
    for current, dirs, files in os.walk(
        source, followlinks=False, onerror=_raise_walk_error
    ):
        names = []
        for name in [*dirs, *files]:
            if (Path(current) / name).is_symlink():
                continue
            if not valid_name(name):
                raise ValueError(f"Name cannot be used on Windows: {name}")
            names.append(name.casefold())
        if len(names) != len(set(names)):
            raise ValueError(
                f"Names differ only by case in {current}; Windows cannot keep both"
            )


def _raise_walk_error(error: OSError) -> None:
    raise error


def available_local(parent: Path, name: str) -> Path:
    if not valid_name(name):
        raise ValueError("Unsafe downloaded name")
    stem, suffix = os.path.splitext(name)
    result = parent / name
    n = 2
    while result.exists() or result.is_symlink():
        result = parent / f"{stem} ({n}){suffix}"
        n += 1
    return result


class Relay:
    def __init__(self, client, progress: Progress | None = None):
        self.client = client
        self.progress = progress or (lambda _name, _done, _total: None)
        self.index: dict = {"version": 1, "entries": {}}
        self.snapshot_id: int | None = None

    async def _latest_snapshot(self):
        # Recent messages are available before Telegram's search index catches up.
        for query in ({"limit": 100}, {"search": INDEX_TAG, "limit": 30}):
            async for message in self.client.iter_messages("me", **query):
                if message.message == INDEX_TAG and message.file:
                    return message
        # Telegram search can lag. A full scan is slower, but prevents a fresh
        # device from treating an existing transfer space as empty.
        async for message in self.client.iter_messages("me"):
            if message.message == INDEX_TAG and message.file:
                return message
        return None

    async def refresh(self) -> dict:
        message = await self._latest_snapshot()
        if message:
            blob = await self.client.download_media(message, file=bytes)
            if not blob or len(blob) > MAX_INDEX_BYTES:
                raise ValueError("Transfer index is missing or too large")
            self.index = validate_index(json.loads(blob))
            self.snapshot_id = message.id
            return self.index
        self.index = {"version": 1, "entries": {}}
        self.snapshot_id = None
        return self.index

    async def _save(self) -> None:
        blob = json.dumps(
            self.index, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(blob) > MAX_INDEX_BYTES:
            raise ValueError("Transfer index is too large")
        latest = await self._latest_snapshot()
        if (latest.id if latest else None) != self.snapshot_id:
            raise RuntimeError(
                "Files changed on another computer. Refresh, then try again."
            )
        stream = io.BytesIO(blob)
        stream.name = "telegram-relay-index.json"
        # Keep old snapshots as recovery points if sending fails or devices race.
        saved = await self.client.send_file(
            "me", stream, caption=INDEX_TAG, force_document=True
        )
        self.snapshot_id = saved.id

    async def new_folder(self, parent: str, name: str) -> str:
        if (
            parent != "/"
            and self.index["entries"].get(parent, {}).get("type") != "folder"
        ):
            raise ValueError("Open a folder first")
        path = unique_path(self.index, parent, name)
        self.index["entries"][path] = {"type": "folder"}
        try:
            await self._save()
        except Exception:
            del self.index["entries"][path]
            raise
        return path

    async def _send_file(self, source: Path, parent: str) -> None:
        if source.is_symlink():
            return
        if not source.is_file():
            raise FileNotFoundError(f"File vanished during upload: {source}")
        if not valid_name(source.name):
            raise ValueError(f"File name cannot be used on Windows: {source.name}")
        path = unique_path(self.index, parent, source.name)
        size = source.stat().st_size
        digest = hashlib.sha256()
        ids = []
        done = 0
        try:
            with source.open("rb") as handle:
                while block := handle.read(CHUNK_SIZE):
                    digest.update(block)
                    stream = io.BytesIO(block)
                    stream.name = secrets.token_hex(12) + ".bin"
                    message = await self.client.send_file(
                        "me", stream, force_document=True
                    )
                    ids.append(message.id)
                    done += len(block)
                    self.progress(source.name, done, size)
            if done != size:
                raise IOError(f"File changed while uploading: {source}")
            self.index["entries"][path] = {
                "type": "file",
                "size": size,
                "sha256": digest.hexdigest(),
                "chunks": ids,
            }
            await self._save()
        except Exception:
            self.index["entries"].pop(path, None)
            if ids:
                await self.client.delete_messages("me", ids)
            raise

    async def send(self, source: Path, parent: str) -> None:
        source = Path(source)
        if (
            parent != "/"
            and self.index["entries"].get(parent, {}).get("type") != "folder"
        ):
            raise ValueError(
                "Destination folder no longer exists. Refresh and choose a folder."
            )
        if source.is_symlink():
            raise ValueError("Symbolic links cannot be sent")
        if not valid_name(source.name):
            raise ValueError(f"Name cannot be used on Windows: {source.name}")
        if source.is_file():
            await self._send_file(source, parent)
            return
        if not source.is_dir():
            raise FileNotFoundError(source)
        validate_source_tree(source)
        root = await self.new_folder(parent, source.name)
        try:
            for current, dirs, files in os.walk(
                source, followlinks=False, onerror=_raise_walk_error
            ):
                dirs[:] = sorted(
                    d for d in dirs if not (Path(current) / d).is_symlink()
                )
                relative = Path(current).relative_to(source).parts
                remote = root
                for component in relative:
                    remote = child(remote, component)
                for name in dirs:
                    await self.new_folder(remote, name)
                for name in sorted(files):
                    await self._send_file(Path(current) / name, remote)
        except Exception as upload_error:
            try:
                await self.remove(root)
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"Upload failed and {root} may be incomplete; remove it manually: {cleanup_error}"
                ) from upload_error
            raise

    async def _receive_file(self, path: str, dest: Path) -> None:
        entry = self.index["entries"][path]
        temporary = dest.with_name(dest.name + "." + secrets.token_hex(4) + ".part")
        digest = hashlib.sha256()
        done = 0
        try:
            with temporary.open("xb") as handle:
                for message_id in entry["chunks"]:
                    message = await self.client.get_messages("me", ids=message_id)
                    if not message or not message.file:
                        raise FileNotFoundError(
                            f"Telegram chunk {message_id} is missing"
                        )
                    block = await self.client.download_media(message, file=bytes)
                    if block is None:
                        raise IOError(f"Could not download chunk {message_id}")
                    handle.write(block)
                    digest.update(block)
                    done += len(block)
                    self.progress(dest.name, done, entry["size"])
            if done != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise IOError("Downloaded file failed its checksum")
            if dest.exists() or dest.is_symlink():
                raise FileExistsError(dest)
            temporary.rename(dest)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    async def receive(self, path: str, destination: Path) -> Path:
        parts(path)
        entry = self.index["entries"].get(path)
        if not entry:
            raise FileNotFoundError(path)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        target = available_local(destination, parts(path)[-1])
        if entry["type"] == "file":
            await self._receive_file(path, target)
            return target
        target.mkdir()
        try:
            prefix = path + "/"
            descendants = sorted(
                (p for p in self.index["entries"] if p.startswith(prefix)),
                key=lambda p: (len(parts(p)), p),
            )
            for remote in descendants:
                relative = parts(remote)[len(parts(path)) :]
                local = target.joinpath(*relative)
                # The index is validated before path construction. Check again here to
                # protect against an in-memory index changed by a future caller.
                if any(not valid_name(x) for x in relative):
                    raise ValueError("Unsafe path in transfer index")
                if self.index["entries"][remote]["type"] == "folder":
                    local.mkdir(parents=True, exist_ok=True)
                else:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    await self._receive_file(remote, local)
        except BaseException:
            shutil.rmtree(target)
            raise
        return target

    async def remove(self, path: str) -> None:
        parts(path)
        if path not in self.index["entries"]:
            raise FileNotFoundError(path)
        affected = [
            p for p in self.index["entries"] if p == path or p.startswith(path + "/")
        ]
        ids = [i for p in affected for i in self.index["entries"][p].get("chunks", [])]
        previous = deepcopy(self.index)
        for p in affected:
            del self.index["entries"][p]
        self.index.setdefault("pending_deletes", []).extend(ids)
        try:
            await self._save()
        except Exception:
            self.index = previous
            raise
        await self.retry_pending_deletes()

    async def retry_pending_deletes(self) -> None:
        ids = self.index.get("pending_deletes", [])
        if not ids:
            return
        try:
            for start in range(0, len(ids), 100):
                await self.client.delete_messages("me", ids[start : start + 100])
        except Exception as exc:
            raise RuntimeError(
                "Item removed, but Telegram could not finish deleting its chunks. Refresh to retry cleanup."
            ) from exc
        self.index["pending_deletes"] = []
        try:
            await self._save()
        except Exception:
            self.index["pending_deletes"] = ids
            raise

    def list_folder(self, path: str) -> list[tuple[str, dict]]:
        parts(path)
        if path != "/" and self.index["entries"].get(path, {}).get("type") != "folder":
            raise FileNotFoundError(path)
        return sorted(
            (
                (p, e)
                for p, e in self.index["entries"].items()
                if "/" + "/".join(parts(p)[:-1]) == path
            ),
            key=lambda item: (item[1]["type"] != "folder", item[0].casefold()),
        )
