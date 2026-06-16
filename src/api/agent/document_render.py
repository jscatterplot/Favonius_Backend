"""Pure document extraction + rendering for the agent document-fill path.

No DB, no auth, no Anthropic — just bytes in, bytes/text out. Every
third-party import (``docx``, ``docxtpl``, ``pypdf``, ``reportlab``) is
**lazy** (inside the function that needs it) so the rest of the agent — and
test envs that never touch document-fill — never load them.

Two jobs:

* **Extract** (``extract``) — sniff the kind, pull the full text, and detect
  any fillable fields/placeholders. A finished old report has no placeholders
  (``fields == []``); the agent refreshes it via *targeted replacements*
  instead (anchored old→new edits).
* **Render** (``render``) — produce the filled document, same kind as the
  input. DOCX and fillable (AcroForm) PDFs keep their original layout
  (``fidelity='preserved'``); flat PDFs are re-rendered from text
  (``fidelity='degraded'`` — layout NOT preserved).

All text the model sees is length-capped and control-char-stripped here; the
caller frames it as data, not instructions.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Hard cap on extracted text handed to the LLM (chars). Generous for a report
# while bounding token cost / prompt-injection surface.
MAX_TEXT_CHARS = 200_000

# Jinja-style placeholder used by docxtpl, e.g. ``{{ customer_name }}``. We
# capture the leading identifier as the field name (ignoring ``.attr`` /
# filters) so the detected-field set matches what the LLM is told to fill.
_JINJA_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")

# DOCX magic = a ZIP whose first entry is the OOXML content-types part.
_ZIP_MAGIC = b"PK\x03\x04"
_PDF_MAGIC = b"%PDF"


class DocumentRenderError(Exception):
    """Extraction or rendering failed for a reason worth surfacing.

    The caller maps this to a graceful tool error / classified failure — it
    never crashes the agent loop.
    """


@dataclass(frozen=True)
class DetectedField:
    """One fillable slot found in a template."""

    name: str
    source: str  # 'jinja' | 'acroform'


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of :func:`extract`."""

    kind: str  # 'docx' | 'pdf'
    text: str
    fields: list[DetectedField] = field(default_factory=list)
    pdf_form_type: Optional[str] = None  # 'acroform' | 'flat' | None (docx)
    truncated: bool = False  # text exceeded MAX_TEXT_CHARS and was cut


# ── Kind sniffing ──────────────────────────────────────────────────────────


def sniff_kind(body: bytes) -> Optional[str]:
    """Return ``'docx'`` / ``'pdf'`` from magic bytes, or ``None`` if neither.

    DOCX is a ZIP; we additionally require the OOXML marker so a generic ZIP
    (or an .xlsx/.pptx) isn't mistaken for a Word document.
    """
    if body[:4] == _PDF_MAGIC:
        return "pdf"
    if _looks_like_docx(body):
        return "docx"
    return None


def _looks_like_docx(body: bytes) -> bool:
    """True iff ``body`` is a ZIP containing a Word ``word/document.xml`` part."""
    if body[:4] != _ZIP_MAGIC:
        return False
    import zipfile  # stdlib, lazy to keep the hot path lean

    try:
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(n == "word/document.xml" for n in names)


def _sanitize_text(text: str) -> tuple[str, bool]:
    """Strip control chars (except tab/newline), cap length; flag if truncated.

    Returns ``(cleaned_text, truncated)`` so callers can tell the agent the
    document was longer than it can see — otherwise fields/anchors past the cap
    are silently undetected and any fill for them is silently dropped.
    """
    cleaned = "".join(ch for ch in text if ch in ("\t", "\n") or ord(ch) >= 32)
    truncated = len(cleaned) > MAX_TEXT_CHARS
    return (cleaned[:MAX_TEXT_CHARS] if truncated else cleaned), truncated


# ── Extraction ───────────────────────────────────────────────────────────────


def extract(body: bytes, kind: str) -> ExtractResult:
    """Extract text + detected fields from a DOCX or PDF blob."""
    if kind == "docx":
        return _extract_docx(body)
    if kind == "pdf":
        return _extract_pdf(body)
    raise DocumentRenderError(f"unsupported kind {kind!r}")


def _extract_docx(body: bytes) -> ExtractResult:
    try:
        from docx import Document  # lazy
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise DocumentRenderError("python-docx is not installed") from exc
    try:
        doc = Document(io.BytesIO(body))
    except Exception as exc:  # noqa: BLE001 - any malformed docx
        raise DocumentRenderError(f"could not open docx: {exc}") from exc

    parts: list[str] = [p.text for p in _iter_docx_paragraphs(doc)]
    text, truncated = _sanitize_text("\n".join(parts))

    seen: set[str] = set()
    fields: list[DetectedField] = []
    for name in _JINJA_RE.findall(text):
        if name not in seen:
            seen.add(name)
            fields.append(DetectedField(name=name, source="jinja"))
    return ExtractResult(
        kind="docx", text=text, fields=fields, pdf_form_type=None, truncated=truncated
    )


def _iter_docx_paragraphs(doc: Any):
    """Yield every paragraph in body, tables, and section headers/footers."""

    def _iter_container(container: Any):
        for para in getattr(container, "paragraphs", []) or []:
            yield para
        for table in getattr(container, "tables", []) or []:
            for row in table.rows:
                for cell in row.cells:
                    yield from _iter_container(cell)

    yield from _iter_container(doc)
    for section in getattr(doc, "sections", []) or []:
        for hf in (section.header, section.footer):
            if hf is not None:
                yield from _iter_container(hf)


def _extract_pdf(body: bytes) -> ExtractResult:
    try:
        from pypdf import PdfReader  # lazy
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise DocumentRenderError("pypdf is not installed") from exc
    try:
        reader = PdfReader(io.BytesIO(body))
    except Exception as exc:  # noqa: BLE001
        raise DocumentRenderError(f"could not open pdf: {exc}") from exc

    text_parts: list[str] = []
    for page in reader.pages:
        try:
            text_parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page shouldn't sink extraction
            continue
    text, truncated = _sanitize_text("\n".join(text_parts))

    fields: list[DetectedField] = []
    form_type = "flat"
    try:
        acro = reader.get_fields()
    except Exception:  # noqa: BLE001
        acro = None
    # ``is not None`` (not truthiness): a PDF carrying an AcroForm with zero
    # fields ({}) is still a form — keep it 'acroform' so render() preserves the
    # layout (fill is a no-op, replacements still apply) instead of re-rendering.
    if acro is not None:
        form_type = "acroform"
        for name in acro.keys():
            fields.append(DetectedField(name=str(name), source="acroform"))
    return ExtractResult(
        kind="pdf", text=text, fields=fields, pdf_form_type=form_type, truncated=truncated
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def render(
    *,
    kind: str,
    pdf_form_type: Optional[str],
    body: bytes,
    field_values: dict[str, Any],
    replacements: list[dict[str, Any]],
    full_text: Optional[str] = None,
) -> tuple[bytes, str]:
    """Render the filled document. Returns ``(bytes, fidelity)``.

    ``fidelity`` is ``'preserved'`` when the original layout is kept (DOCX,
    AcroForm PDF) or ``'degraded'`` when re-rendered from text (flat PDF).
    """
    if kind == "docx":
        return _render_docx(body, field_values, replacements), "preserved"
    if kind == "pdf":
        if pdf_form_type == "acroform":
            filled = _fill_acroform_pdf(body, field_values)
            if replacements:
                filled = _apply_pdf_replacements(filled, replacements)
            return filled, "preserved"
        # Flat PDF: apply targeted replacements to the extracted text and
        # re-render. Layout is NOT preserved — best-effort. Named fields are not
        # supported (no AcroForm slots); callers must use replacements only.
        if field_values:
            raise DocumentRenderError(
                "flat PDF rendering does not support field_values; use replacements"
            )
        text = full_text if full_text is not None else _extract_pdf(body).text
        for rep in replacements:
            find = str(rep.get("find") or "")
            if find:
                text = text.replace(find, str(rep.get("replace") or ""))
        return _render_flat_pdf_from_text(text), "degraded"
    raise DocumentRenderError(f"unsupported kind {kind!r}")


def _render_docx(
    body: bytes, field_values: dict[str, Any], replacements: list[dict[str, Any]]
) -> bytes:
    """Fill ``{{ jinja }}`` placeholders (docxtpl) + apply targeted replacements."""
    intermediate = body
    if field_values:
        try:
            from docxtpl import DocxTemplate  # lazy
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise DocumentRenderError("docxtpl is not installed") from exc
        try:
            tpl = DocxTemplate(io.BytesIO(body))
            tpl.render({str(k): ("" if v is None else str(v)) for k, v in field_values.items()})
            buf = io.BytesIO()
            tpl.save(buf)
            intermediate = buf.getvalue()
        except Exception as exc:  # noqa: BLE001
            raise DocumentRenderError(f"docx template render failed: {exc}") from exc

    if not replacements:
        return intermediate

    try:
        from docx import Document  # lazy
    except ImportError as exc:  # pragma: no cover
        raise DocumentRenderError("python-docx is not installed") from exc
    try:
        doc = Document(io.BytesIO(intermediate))
        pairs = [
            (str(r.get("find") or ""), str(r.get("replace") or ""))
            for r in replacements
            if str(r.get("find") or "")
        ]
        for para in _iter_docx_paragraphs(doc):
            _replace_in_paragraph(para, pairs)
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()
    except DocumentRenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentRenderError(f"docx replacement failed: {exc}") from exc


def _replace_in_paragraph(para: Any, pairs: list[tuple[str, str]]) -> None:
    """Apply ``(find, replace)`` edits to one paragraph, preserving format.

    Fast path: if ``find`` lives entirely inside a single run, edit that run
    (full formatting preserved). Fallback: when ``find`` is split across runs,
    collapse the paragraph's runs into the first run with the replaced text
    (paragraph style preserved; intra-paragraph run formatting is lost — the
    documented best-effort behaviour for split anchors).
    """
    runs = para.runs
    if not runs:
        return
    for find, replace in pairs:
        if not find or find not in para.text:
            continue
        # Fast path — fully inside one run.
        single = next((r for r in runs if find in r.text), None)
        if single is not None:
            single.text = single.text.replace(find, replace)
            continue
        # Fallback — collapse runs, replace on the joined text.
        joined = "".join(r.text for r in runs)
        joined = joined.replace(find, replace)
        runs[0].text = joined
        for r in runs[1:]:
            r.text = ""


def _apply_pdf_replacements(body: bytes, replacements: list[dict[str, Any]]) -> bytes:
    """Apply anchored find/replace edits to static PDF text (best-effort).

    Operates on text-showing operators in page content streams so AcroForm
    field fills and layout are preserved. Complex encodings may not match.
    """
    pairs = [
        (str(r.get("find") or ""), str(r.get("replace") or ""))
        for r in replacements
        if str(r.get("find") or "")
    ]
    if not pairs:
        return body
    try:
        from pypdf import PdfReader, PdfWriter  # lazy
        from pypdf.generic import ArrayObject, ContentStream, NameObject, TextStringObject
    except ImportError as exc:  # pragma: no cover
        raise DocumentRenderError("pypdf is not installed") from exc

    def _replace_string(s: str) -> str:
        for find, replace in pairs:
            s = s.replace(find, replace)
        return s

    def _patch_content_stream(cs: ContentStream) -> None:
        for i, (operands, operator) in enumerate(cs.operations):
            if operator in (b"Tj", b"'", b'"') and operands:
                if isinstance(operands[0], str):
                    new_s = _replace_string(operands[0])
                    if new_s != operands[0]:
                        operands = (TextStringObject(new_s),) + tuple(operands[1:])
                        cs.operations[i] = (operands, operator)
            elif operator == b"TJ" and operands:
                arr = operands[0]
                if isinstance(arr, (list, tuple)):
                    new_arr = list(arr)
                    changed = False
                    for j, item in enumerate(new_arr):
                        if isinstance(item, str):
                            new_item = _replace_string(item)
                            if new_item != item:
                                new_arr[j] = TextStringObject(new_item)
                                changed = True
                    if changed:
                        # Re-wrap as an ArrayObject (not a plain list) — pypdf's
                        # ContentStream serializer calls write_to_stream on each
                        # operand, which a bare list does not implement.
                        cs.operations[i] = (
                            (ArrayObject(new_arr),) + tuple(operands[1:]),
                            operator,
                        )

    try:
        reader = PdfReader(io.BytesIO(body))
        writer = PdfWriter()
        writer.append(reader)
        for page in writer.pages:
            contents = page.get_contents()
            if contents is None:
                continue
            if isinstance(contents, ArrayObject):
                patched: list[Any] = []
                for stream in contents:
                    cs = ContentStream(stream, reader)
                    _patch_content_stream(cs)
                    patched.append(cs)
                page[NameObject("/Contents")] = ArrayObject(patched)
            else:
                cs = (
                    contents
                    if isinstance(contents, ContentStream)
                    else ContentStream(contents, reader)
                )
                _patch_content_stream(cs)
                page[NameObject("/Contents")] = cs
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except DocumentRenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentRenderError(f"pdf replacement failed: {exc}") from exc


def _fill_acroform_pdf(body: bytes, field_values: dict[str, Any]) -> bytes:
    """Fill AcroForm fields by name, preserving the PDF layout."""
    try:
        from pypdf import PdfReader, PdfWriter  # lazy
    except ImportError as exc:  # pragma: no cover
        raise DocumentRenderError("pypdf is not installed") from exc
    try:
        reader = PdfReader(io.BytesIO(body))
        writer = PdfWriter()
        writer.append(reader)
        values = {str(k): ("" if v is None else str(v)) for k, v in field_values.items()}
        for page in writer.pages:
            try:
                writer.update_page_form_field_values(page, values)
            except Exception:  # noqa: BLE001 - field may not exist on every page
                continue
        # Ask viewers to regenerate field appearances so filled values render.
        try:
            writer.set_need_appearances_writer(True)
        except Exception:  # noqa: BLE001 - older pypdf API variants
            pass
        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except DocumentRenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentRenderError(f"acroform fill failed: {exc}") from exc


def _render_flat_pdf_from_text(text: str) -> bytes:
    """Re-render plain text as a simple PDF (reportlab). Layout NOT preserved."""
    try:
        from reportlab.lib.pagesizes import A4  # lazy (already a core dep)
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:  # pragma: no cover
        raise DocumentRenderError("reportlab is not installed") from exc
    try:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4)
        styles = getSampleStyleSheet()
        flow: list[Any] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped:
                # Escape XML-significant chars — reportlab Paragraph parses markup.
                safe = stripped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                flow.append(Paragraph(safe, styles["Normal"]))
            else:
                flow.append(Spacer(1, 8))
        if not flow:
            flow.append(Paragraph("(empty document)", styles["Normal"]))
        doc.build(flow)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        raise DocumentRenderError(f"flat pdf render failed: {exc}") from exc


__all__ = [
    "DocumentRenderError",
    "DetectedField",
    "ExtractResult",
    "MAX_TEXT_CHARS",
    "sniff_kind",
    "extract",
    "render",
]
