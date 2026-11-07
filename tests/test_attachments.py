"""Verify actual image/document payloads through the public MCP tool entry points."""

import base64
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from dingtalkbridge.config import Config
from dingtalkbridge.mcp import MCPServer, serve
from dingtalkbridge.store import Store


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        """Create a private temporary archive and an explicitly allowed download directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(data_dir=self.root / "archive", attachment_dirs=(str(self.root),))
        self.store = Store(self.config.database_path, self.config)
        self.server = MCPServer(self.store)

    def tearDown(self):
        """Close the test archive and remove only its temporary files."""
        self.store.close()
        self.temporary.cleanup()

    def attach(self, path, image=False):
        """Archive the same native locator structure used by real image and file messages."""
        if image:
            body = {"filename": path.name, "filepath": str(path), "picSize": path.stat().st_size, "mediaId": "fixture-image"}
        else:
            body = {"attachments": [{"filepath": str(path), "extension": json.dumps({
                "f_id": "fixture-file", "s_id": "fixture-space", "f_name": path.name, "f_size": str(path.stat().st_size)})}]}
        identifier = "message-" + path.name
        self.store.ingest({"id": identifier, "conversation_id": "fixture", "text": json.dumps(body),
                           "timestamp": datetime.now(timezone.utc).isoformat(), "source": "windows_native_history",
                           "metadata": {"content_type": "2" if image else "501"}})
        listing = self.server.call_tool("list_attachments", {"message_id": identifier})
        self.assertFalse(listing["isError"])
        item = listing["structuredContent"]["attachments"][0]
        self.assertEqual(item["availability"], "local")
        self.assertNotIn("filepath", json.dumps(listing))
        return item["attachment_id"]

    def read(self, attachment_id, **options):
        """Read an attachment through MCP and require a successful content response."""
        result = self.server.call_tool("read_attachment", {"attachment_id": attachment_id, **options})
        self.assertFalse(result["isError"], result)
        return result

    def test_image_original_and_policy_recheck(self):
        """Return decodable image blocks and exact original bytes, then recheck archive policy."""
        from PIL import Image
        path = self.root / "picture.png"
        Image.new("RGB", (120, 80), "blue").save(path)
        attachment = self.attach(path, image=True)
        result = self.read(attachment)
        image = result["content"][1]
        self.assertEqual(image["type"], "image")
        with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as decoded:
            self.assertEqual(decoded.size, (120, 80))
        original = self.read(attachment, view="original")["content"][1]["resource"]
        self.assertEqual(base64.b64decode(original["blob"]), path.read_bytes())
        path.unlink()
        self.assertFalse(self.read(attachment)["isError"])
        self.store.config = replace(self.config, excluded_conversations=("fixture",))
        self.assertTrue(self.server.call_tool("read_attachment", {"attachment_id": attachment})["isError"])

    def test_rich_card_images_deduplicate_payload_versions(self):
        """Extract authorized V2 images once and retain V1-only support for older rich-text cards."""
        from PIL import Image
        path = self.root / "inline.png"
        Image.new("RGB", (1793, 105), "white").save(path)
        old = {"items": [{"type": "img", "value": {"src": "mediaId://@fixture", "filename": path.name,
                                                       "width": 1793, "height": 105}}]}
        new = {"contents": [{"type": "markdown", "text": {"items": [
            {"type": "image", "data": {"authMediaId": "$fixture", "url": "@fixture", "filename": path.name},
             "style": {"width": 1793, "height": 105}}]}}]}
        extra = {"payload": json.dumps(old), "payloadV2": json.dumps(new)}
        body = {"attachments": [{"type": 3100, "extension": json.dumps(extra)}]}
        record = {"id": "card", "conversation_id": "fixture", "text": json.dumps(body),
                  "timestamp": datetime.now(timezone.utc).isoformat(), "source": "windows_native_history",
                  "metadata": {"content_type": "3100"}}
        self.store.ingest(record)
        listing = self.server.call_tool("list_attachments", {"message_id": "card"})["structuredContent"]["attachments"]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["expected_dimensions"], [1793, 105])
        result = self.read(listing[0]["attachment_id"])
        self.assertEqual(result["content"][1]["type"], "image")
        del extra["payloadV2"]
        body["attachments"][0]["extension"] = json.dumps(extra)
        self.store.connection.execute("UPDATE messages SET text=? WHERE id='card'", (json.dumps(body),))
        self.store.connection.commit()
        self.assertEqual(len(self.server.call_tool("list_attachments", {"message_id": "card"})["structuredContent"]["attachments"]), 1)

    def test_group_folders_paginate_read_files_and_recheck_policy(self):
        """Route 503/type-0 folders to listings and keep returned drive IDs scoped on every read."""
        import msgpack
        from docx import Document
        self.store.bind_native_account("12345")
        body = {"attachments": [{"type": 0, "extension": json.dumps({
            "f_id": "folder", "s_id": "123", "f_name": "Folder", "f_count": "1"})}]}
        self.store.ingest({"id": "folder-message", "conversation_id": "fixture", "text": json.dumps(body),
                           "timestamp": datetime.now(timezone.utc).isoformat(), "source": "windows_native_history",
                           "metadata": {"content_type": "503", "account_id": "12345", "native_message_id": "101"}})
        document = Document()
        document.add_paragraph("read from group folder")
        document.save(self.root / "child.docx")
        revision = "first"

        def query(_store, operation, payload):
            """Model verified CSpace wire shapes while requiring the account's group-space variant."""
            if operation == "group-space":
                unpacker = msgpack.Unpacker(raw=False)
                unpacker.feed(payload)
                self.assertEqual(list(unpacker), ["fixture", 2])
                return 123
            arguments = msgpack.unpackb(payload, raw=False, strict_map_key=False)
            if arguments[5] == "0" and 1 not in arguments:
                model = {1: "next-page", 2: 2, 3: [{1: "folder", 2: 0, 3: "/Folder/", 4: "Folder", 5: "folder"}], 5: True}
            elif arguments[5] == "0":
                self.assertEqual(arguments[1], "next-page")
                model = {2: 2, 3: [{1: "second", 2: 2, 3: "/other.txt", 4: "other.txt", 5: "file"}], 5: False}
            else:
                self.assertEqual(arguments[5], "folder")
                model = {2: 1, 3: [{1: "child", 2: 0, 3: "/Folder/child.docx", 4: "child.docx", 5: "file", 47: "other-id", 91: revision}], 5: False}
            return {1: 0, 3: {1: model}}

        folder = self.server.call_tool("list_attachments", {"message_id": "folder-message"})["structuredContent"]["attachments"][0]
        self.assertEqual(folder["kind"], "directory")
        self.assertFalse(folder["download_on_read"])
        with patch("dingtalkbridge.group_files._rpc", side_effect=query):
            first = self.server.call_tool("list_group_files", {"conversation_id": "fixture", "limit": 1})["structuredContent"]
            second = self.server.call_tool("list_group_files", {"conversation_id": "fixture", "cursor": first["next_cursor"], "limit": 1})
            self.assertFalse(second["structuredContent"]["has_more"])
            drive_folder = first["files"][0]["attachment_id"]
            children = self.read(drive_folder)["structuredContent"]["files"]
            self.assertEqual(len(children), 1)
            direct = self.read(folder["attachment_id"])["structuredContent"]["files"]
            self.assertEqual(direct[0]["attachment_id"], children[0]["attachment_id"])
            result = self.read(children[0]["attachment_id"], view="text")
            from dingtalkbridge.attachments import resolve
            before = resolve(self.store, children[0]["attachment_id"]).fingerprint()
            revision = "changed-with-the-same-name-and-size"
            self.read(drive_folder)
            self.assertNotEqual(resolve(self.store, children[0]["attachment_id"]).fingerprint(), before)
        self.assertIn("read from group folder", result["structuredContent"]["blocks"][0]["text"])
        self.store.config = replace(self.config, excluded_conversations=("fixture",))
        self.assertTrue(self.server.call_tool("read_attachment", {"attachment_id": children[0]["attachment_id"]})["isError"])

    def test_stdio_transmits_a_real_image_larger_than_the_text_response_limit(self):
        """Exercise all MCP envelopes with a binary image exceeding the old text-only ceiling."""
        from PIL import Image
        path = self.root / "large.png"
        Image.frombytes("RGB", (1400, 1000), os.urandom(1400 * 1000 * 3)).save(path)
        attachment = self.attach(path, image=True)
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "image-check", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "read_attachment", "arguments": {"attachment_id": attachment}}},
        ]
        incoming = io.StringIO("\n".join(json.dumps(frame) for frame in frames) + "\n")
        outgoing = io.StringIO()
        serve(self.store, incoming, outgoing)
        lines = outgoing.getvalue().splitlines()
        self.assertGreater(len(lines[1]), 1024 * 1024)
        result = json.loads(lines[1])["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][1]["type"], "image")

    def test_preview_cache_is_replaced_with_full_image_on_read(self):
        """Discard a legacy blurred cache and fetch full image bytes before returning MCP media."""
        from PIL import Image
        from dingtalkbridge.attachments import _directory, resolve
        path = self.root / "photo.png"
        Image.new("RGB", (45, 60), "blue").save(path)
        preview = path.read_bytes()
        attachment_id = self.attach(path, image=True)
        self.read(attachment_id)
        body = {"filename": path.name, "mediaId": "fixture-image", "width": 960, "height": 1280,
                "url": "https://down.dingtalk.com/fixture.png"}
        self.store.connection.execute("UPDATE messages SET text=? WHERE id=?",
                                      (json.dumps(body), "message-" + path.name))
        self.store.connection.commit()
        path.unlink()
        attachment = resolve(self.store, attachment_id)
        manifest_path = _directory(self.store, attachment) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["fingerprint"] = attachment.fingerprint()
        manifest_path.write_text(json.dumps(manifest))
        listing = self.server.call_tool("list_attachments", {"message_id": attachment.message_id})
        item = listing["structuredContent"]["attachments"][0]
        self.assertEqual(item["availability"], "download_required")
        self.assertTrue(item["download_on_read"])
        self.assertEqual(item["expected_dimensions"], [960, 1280])
        self.store.config = replace(self.config, attachment_download_timeout_seconds=9)

        def download_preview(_url, destination, *, timeout):
            """Model a media URL which responds successfully with only a blurred preview."""
            self.assertEqual(timeout, 9)
            destination.write_bytes(preview)

        def download_original(_store, _attachment, destination):
            """Model the client's original-media branch returning the archived image dimensions."""
            Image.new("RGB", (960, 1280), "blue").save(destination, format="PNG")

        with patch("dingtalkbridge.attachments._download", side_effect=download_preview), \
             patch("dingtalkbridge.attachments._client_download", side_effect=download_original) as client:
            result = self.read(attachment_id)
        client.assert_called_once()
        with Image.open(io.BytesIO(base64.b64decode(result["content"][1]["data"]))) as picture:
            self.assertEqual(picture.size, (960, 1280))
        self.assertEqual(self.read(attachment_id)["structuredContent"]["frames"][0]["original_dimensions"], [960, 1280])
        self.assertNotIn("https://", json.dumps(result))

    def test_cache_expiry_preserves_recent_and_busy_entries_then_allows_refetch(self):
        """Expire complete legacy directories through MCP startup while preserving active reads."""
        from dingtalkbridge.attachment_cache import cache_lock, cleanup_cache
        from dingtalkbridge.attachments import _directory, resolve
        path = self.root / "expiry.txt"
        path.write_text("read again after cache cleanup", encoding="utf-8")
        attachment_id = self.attach(path)
        self.read(attachment_id, view="text")
        directory = _directory(self.store, resolve(self.store, attachment_id))
        incoming = directory / "incoming-legacy"
        incoming.mkdir()
        (incoming / path.name).write_bytes(path.read_bytes())
        (directory / "render-legacy.pdf").write_bytes(b"legacy render")
        old = time.time() - 8 * 86400

        def age_cache():
            """Model a pre-existing cache whose files and directories are all beyond retention."""
            for item in [*directory.rglob("*"), directory]:
                os.utime(item, (old, old))

        age_cache()
        with cache_lock(directory):
            self.assertEqual(cleanup_cache(self.config), 0)
        self.read(attachment_id, view="text")
        self.assertEqual(cleanup_cache(self.config), 0)
        age_cache()
        self.assertEqual(cleanup_cache(replace(self.config, attachment_cache_retention_days=0)), 0)
        serve(self.store, io.StringIO(""), io.StringIO())
        self.assertFalse(directory.exists())
        self.assertTrue(path.exists())
        self.assertTrue(self.config.database_path.exists())
        self.assertEqual(self.read(attachment_id, view="text")["structuredContent"]["parts"][0]["text"], path.read_text())

    def test_word_blocks_tables_and_images_paginate(self):
        """Preserve Word block order and inline image bytes across continuation calls."""
        from docx import Document
        from PIL import Image
        picture = self.root / "inline.png"
        Image.new("RGB", (20, 20), "red").save(picture)
        document = Document()
        document.add_paragraph("paragraph one")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "table cell"
        document.add_picture(str(picture))
        path = self.root / "document.docx"
        document.save(path)
        attachment = self.attach(path)
        first = self.read(attachment)
        self.assertIn("paragraph one", first["structuredContent"]["blocks"][0]["text"])
        second = self.read(attachment, cursor=first["structuredContent"]["next_cursor"])
        self.assertEqual(second["structuredContent"]["blocks"][0]["kind"], "table")
        third = self.read(attachment, cursor=second["structuredContent"]["next_cursor"])
        self.assertEqual(third["content"][1]["type"], "image")
        self.assertFalse(third["structuredContent"]["has_more"])

    def test_scanned_pdf_returns_selected_page_images(self):
        """Render the selected page of a scanned PDF even when its text layer is empty."""
        from PIL import Image
        path = self.root / "scan.pdf"
        Image.new("RGB", (100, 150), "white").save(path, save_all=True, append_images=[Image.new("RGB", (100, 150), "blue")])
        attachment = self.attach(path)
        result = self.read(attachment, pages="2", view="auto")
        self.assertEqual(result["structuredContent"]["pages"][0]["page"], 2)
        self.assertFalse(result["structuredContent"]["pages"][0]["text_available"])
        self.assertEqual(result["content"][1]["type"], "image")

    def test_powerpoint_slide_notes_and_tables(self):
        """Read selected PowerPoint slides with their table cells and speaker notes."""
        from pptx import Presentation
        from pptx.util import Inches
        presentation = Presentation()
        for index in range(2):
            slide = presentation.slides.add_slide(presentation.slide_layouts[5])
            slide.shapes.title.text = f"slide {index + 1}"
            slide.notes_slide.notes_text_frame.text = f"notes {index + 1}"
            slide.shapes.add_table(1, 1, Inches(1), Inches(2), Inches(3), Inches(1)).table.cell(0, 0).text = "table value"
        path = self.root / "slides.pptx"
        presentation.save(path)
        result = self.read(self.attach(path), pages="2", view="text")
        slide = result["structuredContent"]["slides"][0]
        self.assertEqual(slide["slide"], 2)
        self.assertIn("notes 2", slide["notes"])
        self.assertEqual(slide["tables"], [[["table value"]]])

    def test_excel_range_formula_and_row_continuation(self):
        """Return exact Excel coordinates, formulas and explicit missing cached values."""
        from openpyxl import Workbook
        workbook = Workbook()
        tab = workbook.active
        tab.title = "Budget"
        tab["C3"], tab["D3"], tab["C4"] = 42, "=C3*2", "next row"
        path = self.root / "budget.xlsx"
        workbook.save(path)
        attachment = self.attach(path)
        first = self.read(attachment, sheet="Budget", cell_range="C3:D4")
        cells = first["structuredContent"]["rows"][0]["cells"]
        self.assertEqual(cells[0]["cell"], "C3")
        self.assertEqual(cells[0]["value"], 42)
        self.assertEqual(cells[1]["formula"], "=C3*2")
        self.assertIsNone(cells[1]["value"])
        second = self.read(attachment, sheet="Budget", cell_range="C3:D4", cursor=first["structuredContent"]["next_cursor"])
        self.assertEqual(second["structuredContent"]["rows"][0]["cells"][0]["value"], "next row")
        self.assertFalse(second["structuredContent"]["has_more"])

    def test_client_download_requests_use_lf_and_resume_with_actual_bytes(self):
        """Keep the private native protocol LF-only on Windows and consume its completed file."""
        path = self.root / "client.txt"
        content = b"client download completed"
        path.write_bytes(content)
        attachment = self.attach(path)
        self.store.bind_native_account("12345")
        body = json.loads(self.store.connection.execute("SELECT text FROM messages WHERE id=?", ("message-client.txt",)).fetchone()[0])
        body["attachments"][0]["url"] = "x:x"
        extra = json.loads(body["attachments"][0]["extension"])
        extra["f_size"] = str(len(content) + 7)
        body["attachments"][0]["extension"] = json.dumps(extra)
        self.store.connection.execute(
            "UPDATE messages SET metadata=?,text=? WHERE id=?",
            (json.dumps({"content_type": "501", "account_id": "12345", "native_message_id": "101"}), json.dumps(body), "message-client.txt"),
        )
        self.store.connection.commit()
        path.unlink()
        listing = self.server.call_tool("list_attachments", {"message_id": "message-client.txt"})
        self.assertTrue(listing["structuredContent"]["attachments"][0]["download_on_read"])
        self.store.config = replace(self.config, attachment_download_timeout_seconds=5)

        with patch.object(self.store, "_collector_status", return_value={"running": True}), \
             patch("dingtalkbridge.attachments.time.monotonic", side_effect=[0, 6]), \
             patch("dingtalkbridge.attachments.time.sleep") as waiting:
            pending = self.server.call_tool("read_attachment", {"attachment_id": attachment, "view": "text"})
        self.assertTrue(pending["isError"])
        self.assertIn("attachment_download_pending_retry_read_attachment", pending["content"][0]["text"])
        waiting.assert_not_called()
        self.assertEqual(len(list((self.config.data_dir / "native-attachment-requests").glob("*.request"))), 1)

        def complete_download(_seconds):
            """Model the native consumer after checking the actual on-disk request encoding."""
            request = next((self.config.data_dir / "native-attachment-requests").glob("*.request"))
            raw = request.read_bytes()
            self.assertNotIn(b"\r", raw)
            account, payload = raw.decode("utf-8").splitlines()
            self.assertEqual(account, "12345")
            arguments = json.loads(json.loads(payload)[0])
            destination = Path(arguments["dest_path_"]) / arguments["f_name_"]
            destination.write_bytes(content)

        with patch.object(self.store, "_collector_status", return_value={"running": True}), \
             patch("dingtalkbridge.attachments.time.sleep", side_effect=complete_download):
            result = self.read(attachment, view="text")
        self.assertEqual(result["structuredContent"]["parts"][0]["text"], content.decode())


if __name__ == "__main__":
    unittest.main()
