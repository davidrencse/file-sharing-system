import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import telegram_relay.core as core
from telegram_relay.core import Relay, child, validate_index


class FakeTelegram:
    def __init__(self):
        self.messages = {}
        self.next_id = 1
        self.fail_delete_once = False
        self.fail_next_data_upload = False
        self.search_unavailable = False

    async def send_file(self, chat, stream, caption="", force_document=False):
        if self.fail_next_data_upload and caption != "#telegram-relay-index-v1":
            self.fail_next_data_upload = False
            raise ConnectionError("simulated upload failure")
        mid = self.next_id
        self.next_id += 1
        self.messages[mid] = (stream.read(), caption)
        return SimpleNamespace(id=mid)

    async def iter_messages(self, chat, limit=None, search=None):
        if search and self.search_unavailable:
            return
        yielded = 0
        for mid in sorted(self.messages, reverse=True):
            caption = self.messages[mid][1]
            if search is None or caption == search:
                yield SimpleNamespace(id=mid, message=caption, file=True)
                yielded += 1
                if limit is not None and yielded >= limit:
                    return

    async def download_media(self, message, file):
        return self.messages[message.id][0]

    async def get_messages(self, chat, ids):
        return SimpleNamespace(id=ids, file=True) if ids in self.messages else None

    async def delete_messages(self, chat, ids):
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise ConnectionError("simulated delete failure")
        for mid in ids:
            self.messages.pop(mid, None)


class RelayTests(unittest.TestCase):
    def test_multiple_chunks_and_empty_file(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            old_size = core.CHUNK_SIZE
            core.CHUNK_SIZE = 4
            try:

                async def scenario():
                    client = FakeTelegram()
                    sender = Relay(client)
                    (tmp / "large.bin").write_bytes(b"abcdefghijkl")
                    (tmp / "empty.bin").write_bytes(b"")
                    await sender.send(tmp / "large.bin", "/")
                    await sender.send(tmp / "empty.bin", "/")
                    receiver = Relay(client)
                    await receiver.refresh()
                    self.assertEqual(
                        len(receiver.index["entries"]["/large.bin"]["chunks"]), 3
                    )
                    out = tmp / "out"
                    await receiver.receive("/large.bin", out)
                    await receiver.receive("/empty.bin", out)
                    self.assertEqual((out / "large.bin").read_bytes(), b"abcdefghijkl")
                    self.assertEqual((out / "empty.bin").read_bytes(), b"")

                asyncio.run(scenario())
            finally:
                core.CHUNK_SIZE = old_size

    def test_folder_roundtrip_between_two_clients(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)

            async def scenario():
                client = FakeTelegram()
                sender = Relay(client)
                source = tmp / "Project"
                (source / "empty").mkdir(parents=True)
                (source / "docs").mkdir()
                (source / "docs" / "note.txt").write_text(
                    "hello" * 100, encoding="utf-8"
                )
                await sender.send(source, "/")
                receiver = Relay(client)
                await receiver.refresh()
                target = await receiver.receive("/Project", tmp / "received")
                self.assertTrue((target / "empty").is_dir())
                self.assertEqual(
                    (target / "docs" / "note.txt").read_text(encoding="utf-8"),
                    "hello" * 100,
                )
                target2 = await receiver.receive("/Project", tmp / "received")
                self.assertEqual(target2.name, "Project (2)")
                await receiver.remove("/Project")
                await sender.refresh()
                self.assertEqual(sender.list_folder("/"), [])

            asyncio.run(scenario())

    def test_reject_traversal_and_bad_checksum(self):
        with self.assertRaises(ValueError):
            child("/", "../outside")
        with self.assertRaises(ValueError):
            validate_index(
                {"version": 1, "entries": {"/x/../outside": {"type": "folder"}}}
            )

        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)

            async def scenario():
                client = FakeTelegram()
                relay = Relay(client)
                original = tmp / "data.bin"
                original.write_bytes(b"actual")
                await relay.send(original, "/")
                chunk = relay.index["entries"]["/data.bin"]["chunks"][0]
                client.messages[chunk] = (b"tampered", "")
                with self.assertRaises(IOError):
                    await relay.receive("/data.bin", tmp / "output")
                self.assertFalse((tmp / "output" / "data.bin").exists())
                self.assertFalse(list((tmp / "output").glob("*.part")))

            asyncio.run(scenario())

    def test_stale_index_refuses_to_overwrite_other_computer(self):
        async def scenario():
            client = FakeTelegram()
            first = Relay(client)
            second = Relay(client)
            await first.refresh()
            await second.refresh()
            await first.new_folder("/", "from-first")
            with self.assertRaisesRegex(RuntimeError, "Refresh"):
                await second.new_folder("/", "from-second")
            self.assertNotIn("/from-second", second.index["entries"])
            await second.refresh()
            await second.new_folder("/", "from-second")
            await first.refresh()
            self.assertEqual(
                set(first.index["entries"]), {"/from-first", "/from-second"}
            )

        asyncio.run(scenario())

    def test_file_cannot_create_orphan_entry_after_parent_was_removed(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "note.txt"
            source.write_text("text")

            async def scenario():
                client = FakeTelegram()
                first = Relay(client)
                await first.new_folder("/", "shared")
                second = Relay(client)
                await second.refresh()
                await second.remove("/shared")
                await first.refresh()
                before = dict(client.messages)
                with self.assertRaisesRegex(ValueError, "no longer exists"):
                    await first.send(source, "/shared")
                self.assertEqual(client.messages, before)
                fresh = Relay(client)
                await fresh.refresh()
                self.assertEqual(fresh.list_folder("/"), [])

            asyncio.run(scenario())

    def test_case_collision_rejected_before_any_upload(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            folder = tmp / "Pair"
            folder.mkdir()
            client = FakeTelegram()
            relay = Relay(client)
            with patch(
                "telegram_relay.core.os.walk",
                return_value=[(str(folder), [], ["A.txt", "a.txt"])],
            ):
                with self.assertRaisesRegex(ValueError, "differ only by case"):
                    asyncio.run(relay.send(folder, "/"))
            self.assertEqual(client.messages, {})
            with self.assertRaisesRegex(ValueError, "collide on Windows"):
                validate_index(
                    {
                        "version": 1,
                        "entries": {"/A": {"type": "folder"}, "/a": {"type": "folder"}},
                    }
                )

    def test_failed_deletion_can_be_retried(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            source = tmp / "note.txt"
            source.write_text("text")

            async def scenario():
                client = FakeTelegram()
                relay = Relay(client)
                await relay.send(source, "/")
                chunk = relay.index["entries"]["/note.txt"]["chunks"][0]
                client.fail_delete_once = True
                with self.assertRaisesRegex(RuntimeError, "Refresh"):
                    await relay.remove("/note.txt")
                self.assertNotIn("/note.txt", relay.index["entries"])
                self.assertIn(chunk, relay.index["pending_deletes"])
                another = Relay(client)
                await another.refresh()
                await another.retry_pending_deletes()
                self.assertNotIn(chunk, client.messages)
                self.assertEqual(another.index["pending_deletes"], [])

            asyncio.run(scenario())

    def test_failed_folder_upload_rolls_back_partial_tree(self):
        with tempfile.TemporaryDirectory() as root:
            tmp = Path(root)
            folder = tmp / "Project"
            folder.mkdir()
            (folder / "data.txt").write_text("text")

            async def scenario():
                client = FakeTelegram()
                client.fail_next_data_upload = True
                relay = Relay(client)
                with self.assertRaises(ConnectionError):
                    await relay.send(folder, "/")
                another = Relay(client)
                await another.refresh()
                self.assertEqual(another.list_folder("/"), [])

            asyncio.run(scenario())

    def test_unreadable_folder_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "Project"
            folder.mkdir()

            def failed_walk(*args, onerror, **kwargs):
                onerror(PermissionError("blocked subfolder"))
                yield from ()

            client = FakeTelegram()
            relay = Relay(client)
            with patch("telegram_relay.core.os.walk", side_effect=failed_walk):
                with self.assertRaisesRegex(PermissionError, "blocked subfolder"):
                    asyncio.run(relay.send(folder, "/"))
            self.assertEqual(client.messages, {})

    def test_file_disappearing_during_folder_upload_rolls_back(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "Project"
            folder.mkdir()
            client = FakeTelegram()
            relay = Relay(client)
            with (
                patch("telegram_relay.core.validate_source_tree"),
                patch(
                    "telegram_relay.core.os.walk",
                    return_value=[(str(folder), [], ["vanished.txt"])],
                ),
            ):
                with self.assertRaisesRegex(FileNotFoundError, "vanished"):
                    asyncio.run(relay.send(folder, "/"))
            receiver = Relay(client)
            asyncio.run(receiver.refresh())
            self.assertEqual(receiver.list_folder("/"), [])

    def test_full_history_finds_index_when_search_is_delayed(self):
        async def scenario():
            client = FakeTelegram()
            first = Relay(client)
            await first.new_folder("/", "kept")
            for _ in range(101):
                await client.send_file("me", SimpleNamespace(read=lambda: b"blob"))
            client.search_unavailable = True
            second = Relay(client)
            await second.refresh()
            self.assertIn("/kept", second.index["entries"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
