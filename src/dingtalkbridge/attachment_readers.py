"""Read actual image and document bytes into paginated MCP content blocks."""

from __future__ import annotations

import base64
import hashlib
from datetime import date, datetime, time
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .attachments import AttachmentError, _digest

MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_ORIGINAL_BYTES = 5 * 1024 * 1024
MAX_CELL_COUNT = 5000


def _image(data: bytes, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return an actual viewable image and its original/display dimensions."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(data)) as source:
        source.seek(0)
        oriented = ImageOps.exif_transpose(source)
        frame = Image.new("RGB", oriented.size, "white")
        rgba = oriented.convert("RGBA")
        frame.paste(rgba, mask=rgba.getchannel("A"))
        original = list(frame.size)
        frame.thumbnail((2048, 2048))
        target = io.BytesIO()
        frame.save(target, "JPEG", quality=88)
        if len(target.getvalue()) > MAX_IMAGE_BYTES:
            target = io.BytesIO()
            frame.save(target, "JPEG", quality=65)
        if len(target.getvalue()) > MAX_IMAGE_BYTES:
            raise AttachmentError("attachment_image_exceeds_response_size")
        block = {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(target.getvalue()).decode("ascii")}
        details = {"label": label, "original_dimensions": original, "display_dimensions": list(frame.size)}
        return block, details


def _pages(selection: str | None, total: int) -> list[int]:
    """Convert one-based page/slide selections into validated zero-based positions."""
    if selection is None:
        return list(range(total))
    if not isinstance(selection, str) or not re.fullmatch(r"[0-9,\- ]{1,128}", selection):
        raise AttachmentError("pages_must_use_one_based_numbers_or_ranges")
    positions = set()
    for segment in selection.split(","):
        parts = segment.strip().split("-")
        if len(parts) not in {1, 2} or any(not part.strip().isdigit() for part in parts):
            raise AttachmentError("invalid_page_range")
        first, last = int(parts[0]), int(parts[-1])
        if not 1 <= first <= last <= total:
            raise AttachmentError("page_out_of_range")
        positions.update(range(first - 1, last))
    return sorted(positions)


def _batch(positions: list[int], cursor: str | None, limit: int) -> tuple[list[int], dict[str, Any]]:
    """Page through a selection and expose exactly where continuation resumes."""
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r"[0-9]{1,18}", cursor)):
        raise AttachmentError("invalid_attachment_cursor")
    offset = int(cursor or "0")
    if offset > len(positions):
        raise AttachmentError("attachment_cursor_out_of_range")
    selected = positions[offset:offset + limit]
    next_offset = offset + len(selected)
    return selected, {"has_more": next_offset < len(positions),
                      "next_cursor": str(next_offset) if next_offset < len(positions) else None}


def _chunks(text: str, maximum: int = 12000) -> list[str]:
    """Split long text without losing any characters beyond a per-response boundary."""
    return [text[offset:offset + maximum] for offset in range(0, len(text), maximum)] or [""]


def _pdf(path: Path, view: str, pages: str | None, cursor: str | None, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read selected PDF pages as text and/or rendered images, including scanned pages."""
    import pypdfium2 as pdfium
    from pypdf import PdfReader
    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise AttachmentError("attachment_pdf_password_required")
    selected, continuation = _batch(_pages(pages, len(reader.pages)), cursor, limit)
    document = pdfium.PdfDocument(str(path)) if view != "text" else None
    records, media = [], []
    try:
        for position in selected:
            text = reader.pages[position].extract_text() or ""
            entry = {"page": position + 1, "text": text, "text_available": bool(text.strip())}
            # A large text page is readable through the text reader's separate part selection.
            if len(text) > 100000:
                raise AttachmentError("pdf_page_text_too_large_use_view_image")
            if document is not None:
                page = document[position]
                bitmap = page.render(scale=min(2, 2048 / max(page.get_size())))
                try:
                    buffer = io.BytesIO()
                    bitmap.to_pil().save(buffer, "PNG")
                    block, image_info = _image(buffer.getvalue(), f"page {position + 1}")
                    media.append(block)
                    entry["image"] = image_info
                finally:
                    bitmap.close()
                    page.close()
            if not text.strip() and view == "text":
                entry["reading_hint"] = "scanned_page_use_view_image_or_OCR"
            records.append(entry)
    finally:
        if document is not None:
            document.close()
    return {"format": "pdf", "page_count": len(reader.pages), "pages": records, **continuation}, media


def _table(table: Any) -> list[list[str]]:
    """Preserve row and column boundaries of Word or PowerPoint tables."""
    return [[cell.text for cell in row.cells] for row in table.rows]


def _docx(path: Path, pages: str | None, cursor: str | None, limit: int, include_images: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read Word blocks in document order, retaining tables and inline images."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    document = Document(path)
    blocks = []
    for index, block in enumerate(document.iter_inner_content()):
        if isinstance(block, Table):
            text = json.dumps(_table(block), ensure_ascii=False)
            kind = "table"
        else:
            text, kind = block.text, "paragraph"
        relations = []
        for drawing in block._element.xpath('.//*[local-name()="blip"]'):
            relation = drawing.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
            if relation in document.part.related_parts:
                relations.append(relation)
        for part_index, part in enumerate(_chunks(text)):
            blocks.append({"block": index + 1, "part": part_index + 1, "kind": kind, "text": part,
                           "style": block.style.name if isinstance(block, Paragraph) else None,
                           "images": relations if part_index == 0 else []})
    selected, continuation = _batch(_pages(pages, len(blocks)), cursor, limit)
    records, media = [], []
    for position in selected:
        entry = dict(blocks[position])
        relations = entry.pop("images")
        entry["images"] = []
        if include_images:
            for relation in relations:
                image, info = _image(document.part.related_parts[relation].blob, f"block {entry['block']}")
                media.append(image)
                entry["images"].append(info)
        else:
            entry["embedded_image_count"] = len(relations)
        records.append(entry)
    return {"format": "docx", "unit": "block_part", "block_part_count": len(blocks), "blocks": records,
            "headers": [section.header.paragraphs[0].text for section in document.sections if section.header.paragraphs],
            **continuation}, media


def _shapes(shapes: Any) -> Any:
    """Visit grouped PowerPoint shapes in their stored order."""
    for shape in shapes:
        if shape.shape_type == 6:
            yield from _shapes(shape.shapes)
        else:
            yield shape


def _pptx(path: Path, pages: str | None, cursor: str | None, limit: int, include_images: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read slide text, tables, notes, embedded pictures and chart series."""
    from pptx import Presentation
    presentation = Presentation(path)
    selected, continuation = _batch(_pages(pages, len(presentation.slides)), cursor, limit)
    records, media = [], []
    for position in selected:
        slide = presentation.slides[position]
        entry = {"slide": position + 1, "text": [], "tables": [], "charts": [], "images": [], "notes": ""}
        for shape in _shapes(slide.shapes):
            if shape.has_text_frame:
                entry["text"].append(shape.text_frame.text)
            if shape.has_table:
                entry["tables"].append(_table(shape.table))
            if shape.has_chart:
                entry["charts"].append({"series": [{"name": series.name, "values": list(series.values)} for series in shape.chart.series]})
            if shape.shape_type == 13 and include_images:
                image, info = _image(shape.image.blob, f"slide {position + 1} picture")
                media.append(image)
                entry["images"].append(info)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            entry["notes"] = slide.notes_slide.notes_text_frame.text
        records.append(entry)
    return {"format": "pptx", "slide_count": len(presentation.slides), "slides": records, **continuation}, media


def _value(value: Any) -> Any:
    """Keep typed spreadsheet values JSON-safe, including dates and times."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _xlsx(path: Path, sheet: str | None, cell_range: str | None, cursor: str | None, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read exact Excel cells with formulas and cached values, paginated by row."""
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries, get_column_letter
    # File handles allow cached originals without relying on filename suffixes.
    with path.open("rb") as source, path.open("rb") as values_source:
        workbook = load_workbook(source, read_only=False, data_only=False, keep_links=False)
        cached = load_workbook(values_source, read_only=False, data_only=True, keep_links=False)
        try:
            overview = [{"name": tab.title, "rows": tab.max_row, "columns": tab.max_column,
                         "merged_ranges": [str(value) for value in tab.merged_cells.ranges], "charts": len(tab._charts)} for tab in workbook]
            name = sheet or workbook.sheetnames[0]
            if name not in workbook.sheetnames:
                raise AttachmentError("attachment_sheet_not_found")
            tab, values = workbook[name], cached[name]
            selected_range = cell_range or f"A1:{get_column_letter(min(tab.max_column, 30))}{tab.max_row}"
            try:
                left, top, right, bottom = range_boundaries(selected_range)
                if any(value is None for value in (left, top, right, bottom)) or not 1 <= left <= right <= 16384 or not 1 <= top <= bottom <= 1048576:
                    raise ValueError()
            except (ValueError, TypeError):
                raise AttachmentError("invalid_excel_cell_range") from None
            if (right - left + 1) * limit > MAX_CELL_COUNT:
                raise AttachmentError("excel_range_too_wide_reduce_columns_or_limit")
            selected, continuation = _batch(list(range(top, bottom + 1)), cursor, limit)
            rows = []
            for row_number in selected:
                cells = []
                for column in range(left, right + 1):
                    cell = tab.cell(row_number, column)
                    cells.append({"cell": cell.coordinate, "type": cell.data_type,
                                  "value": _value(values.cell(row_number, column).value) if cell.data_type == "f" else _value(cell.value),
                                  "formula": cell.value if cell.data_type == "f" else None, "number_format": cell.number_format})
                rows.append({"row": row_number, "cells": cells})
            return {"format": "xlsx", "sheets": overview, "sheet": name, "cell_range": selected_range,
                    "columns_omitted_from_default_range": cell_range is None and tab.max_column > 30,
                    "rows": rows, **continuation}, []
        finally:
            workbook.close()
            cached.close()


def _xls(path: Path, sheet: str | None, cell_range: str | None, cursor: str | None, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read legacy XLS cells and cached values with their original coordinates."""
    import xlrd
    from openpyxl.utils.cell import range_boundaries, get_column_letter
    workbook = xlrd.open_workbook(str(path), on_demand=True)
    try:
        names = workbook.sheet_names()
        name = sheet or names[0]
        if name not in names:
            raise AttachmentError("attachment_sheet_not_found")
        tab = workbook.sheet_by_name(name)
        selected_range = cell_range or f"A1:{get_column_letter(max(1, min(tab.ncols, 30)))}{max(1, tab.nrows)}"
        try:
            left, top, right, bottom = range_boundaries(selected_range)
            if not all(isinstance(value, int) and value > 0 for value in (left, top, right, bottom)) or left > right or top > bottom:
                raise ValueError()
        except (ValueError, TypeError):
            raise AttachmentError("invalid_excel_cell_range") from None
        if (right - left + 1) * limit > MAX_CELL_COUNT:
            raise AttachmentError("excel_range_too_wide_reduce_columns_or_limit")
        selected, continuation = _batch(list(range(top, bottom + 1)), cursor, limit)
        rows = []
        for row_number in selected:
            cells = []
            for column in range(left, right + 1):
                value = tab.cell_value(row_number - 1, column - 1) if row_number <= tab.nrows and column <= tab.ncols else None
                cells.append({"cell": f"{get_column_letter(column)}{row_number}", "value": _value(value)})
            rows.append({"row": row_number, "cells": cells})
        return {"format": "xls", "sheets": names, "sheet": name, "cell_range": selected_range,
                "formula_representation": "cached_values_only", "rows": rows, **continuation}, []
    finally:
        workbook.release_resources()


def _convert(path: Path, suffix: str, sheet: str | None = None, cell_range: str | None = None) -> Path:
    """Render an Office document using an isolated conversion process and cache its PDF."""
    digest = hashlib.sha256((_digest(path) + suffix + str(sheet) + str(cell_range)).encode()).hexdigest()[:16]
    target = path.parent / f"render-{digest}.pdf"
    if target.is_file():
        return target
    converter = Path(__file__).with_name("convert_office.ps1")
    with tempfile.TemporaryDirectory(prefix="office-", dir=path.parent) as temporary:
        source = Path(temporary) / ("source" + suffix)
        output = Path(temporary) / "render.pdf"
        shutil.copyfile(path, source)
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(converter), "-Source", str(source), "-Target", str(output)]
        if sheet:
            command.extend(("-Sheet", sheet))
        if cell_range:
            command.extend(("-CellRange", cell_range))
        try:
            process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise AttachmentError("office_converter_unavailable_or_timed_out") from None
        if process.returncode or not output.is_file():
            raise AttachmentError("office_conversion_failed_check_local_office_installation")
        output.replace(target)
    return target


def read(path: Path, name: str, mime_type: str, *, view: str = "auto", pages: str | None = None,
         sheet: str | None = None, cell_range: str | None = None, cursor: str | None = None,
         limit: int = 1) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Dispatch actual attachment bytes to the appropriate image/document reader."""
    if view not in {"auto", "text", "image", "original"}:
        raise AttachmentError("invalid_attachment_view")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise AttachmentError("attachment_limit_must_be_between_1_and_20")
    suffix = Path(name).suffix.lower()
    if view == "original":
        if path.stat().st_size > MAX_ORIGINAL_BYTES:
            raise AttachmentError("original_exceeds_response_size_use_paginated_content")
        if mime_type.startswith("image/"):
            from PIL import Image
            with Image.open(path) as image:
                mime_type = Image.MIME.get(image.format, mime_type)
        resource = {"type": "resource", "resource": {"uri": "attachment://original", "mimeType": mime_type,
                    "blob": base64.b64encode(path.read_bytes()).decode("ascii")}}
        return {"format": suffix.lstrip("."), "mime_type": mime_type, "bytes": path.stat().st_size,
                "has_more": False, "next_cursor": None}, [resource]
    if (sheet is not None or cell_range is not None) and suffix not in {".xls", ".xlsx", ".xlsm"}:
        raise AttachmentError("sheet_and_cell_range_require_excel")
    if mime_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        if view == "text":
            raise AttachmentError("image_requires_view_auto_or_image_for_visual_reading")
        from PIL import Image
        records, media = [], []
        with Image.open(path) as source:
            count = getattr(source, "n_frames", 1)
            selected, continuation = _batch(_pages(pages, count), cursor, limit)
            for index in selected:
                source.seek(index)
                buffer = io.BytesIO()
                source.save(buffer, "PNG")
                block, details = _image(buffer.getvalue(), f"frame {index + 1}")
                records.append({"frame": index + 1, **details})
                media.append(block)
        return {"format": "image", "frame_count": count, "frames": records, **continuation}, media
    if suffix == ".pdf":
        return _pdf(path, view, pages, cursor, limit)
    if suffix in {".doc", ".ppt"} or view == "image" and suffix in {".docx", ".pptx", ".xlsx", ".xls", ".xlsm"}:
        converted = _convert(path, suffix, sheet, cell_range)
        result, media = _pdf(converted, view, pages, cursor, limit)
        result["converted_from"] = suffix.lstrip(".")
        return result, media
    if suffix == ".docx":
        return _docx(path, pages, cursor, limit, view == "auto")
    if suffix == ".pptx":
        result, media = _pptx(path, pages, cursor, limit, view == "auto")
        result["reading_hint"] = "Use view=image for slide layout and drawn diagrams."
        return result, media
    if suffix in {".xlsx", ".xlsm"}:
        return _xlsx(path, sheet, cell_range, cursor, max(limit, 1))
    if suffix == ".xls":
        return _xls(path, sheet, cell_range, cursor, limit)
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        data = path.read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            try:
                text = data.decode("gb18030")
            except UnicodeError:
                raise AttachmentError("attachment_text_encoding_unsupported") from None
        chunks = _chunks(text)
        selected, continuation = _batch(list(range(len(chunks))), cursor, limit)
        return {"format": suffix.lstrip("."), "parts": [{"part": index + 1, "text": chunks[index]} for index in selected], **continuation}, []
    raise AttachmentError("attachment_format_unsupported_use_view_original")
