"""Look at a Draftsman drawing without the screen: find the OutJob container
that publishes it to PDF, find the PDF the run wrote, and render its pages
(or an area of one) to PNG.

Pure Python apart from the publish itself, which main.render_harness_drawing
runs through the bridge (run_output_jobs). Pages are rendered with pypdfium2.
"""

from __future__ import annotations

import configparser
import io
import time
from pathlib import Path

from outjob_parser import parse_outjob

# Draftsman's output type in an OutJob
DRAWING_OUTPUT_TYPES = {"pcbdrawing"}
MAX_WIDTH_PX = 1600  # default width of a rendered page or area
MAX_DPI = 600.0


class RenderError(ValueError):
    pass


def _project_outjobs(drawing: Path) -> list[Path]:
    """OutJobs a project in the drawing's folder lists (the project holding
    the drawing first), then any other OutJob in that folder."""
    folder = drawing.parent
    listed: list[Path] = []
    projects = sorted(folder.glob("*.PrjHar")) + sorted(folder.glob("*.PrjPcb"))
    projects.sort(key=lambda p: drawing.name.lower() not in _read(p).lower())
    for prj in projects:
        for line in _read(prj).splitlines():
            if line.lower().startswith("documentpath=") and line.lower().endswith(
                ".outjob"
            ):
                p = Path(line.split("=", 1)[1].strip())
                p = p if p.is_absolute() else folder / p
                if p.exists() and p not in listed:
                    listed.append(p)
    for p in sorted(folder.glob("*.OutJob")):
        if p not in listed:
            listed.append(p)
    return listed


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def find_publish_container(
    drawing: Path, outjob_path: str = "", container: str = ""
) -> dict:
    """The OutJob and Publish container that print the drawing: {outjob,
    container, output_dir}. A container given by name is used as is;
    otherwise the first Publish container connected to a Draftsman output
    whose document is the drawing or unset (all drawings of the project)."""
    outjobs = [Path(outjob_path)] if outjob_path else _project_outjobs(drawing)
    if not outjobs:
        raise RenderError(
            f"no OutJob found for {drawing.name}: add one to its project with a "
            "Publish (PDF) container connected to a Draftsman output"
        )
    tried = []
    for oj in outjobs:
        try:
            parsed = parse_outjob(oj, include_raw=False)
        except (OSError, ValueError, KeyError, configparser.Error) as e:
            # an unreadable OutJob: try the next one
            tried.append(f"{oj.name}: {e}")
            continue
        containers = {c["name"]: c for c in parsed.get("containers", [])}
        names: list[str] = []
        if container:
            if container in containers:
                names = [container]
        else:
            for out in parsed.get("outputs", []):
                if (out.get("type") or "").lower() not in DRAWING_OUTPUT_TYPES:
                    continue
                doc = out.get("document_path") or ""
                if doc and Path(doc).name.lower() != drawing.name.lower():
                    continue
                for name in out.get("connected_containers") or []:
                    c = containers.get(name)
                    if (
                        c
                        and (c.get("type") or "").lower() == "publish"
                        and name not in names
                    ):
                        names.append(name)
        if names:
            c = containers[names[0]]
            out_dir = _output_dir(c, oj)
            return {
                "outjob": str(oj),
                "container": names[0],
                "output_dir": str(out_dir),
            }
        tried.append(f"{oj.name}: no Publish container prints Draftsman drawings")
    raise RenderError(
        "no OutJob container publishes this drawing to PDF ("
        + "; ".join(tried)
        + "): add a Publish container connected to a Draftsman output, or name one"
    )


def _output_dir(container: dict, outjob: Path) -> Path:
    for key in ("output_path", "relative_output_path"):
        p = container.get(key)
        if p:
            p = Path(p)
            return p.parent if p.suffix.lower() == ".pdf" else p
    base = container.get("base_path") or ""
    return outjob.parent / base


def newest_pdf(folder: Path, since: float) -> Path | None:
    """The newest PDF in the folder written at or after `since` (epoch s)."""
    best = None
    if not folder.exists():
        return None
    for p in folder.glob("*.[pP][dD][fF]"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m >= since - 1 and (best is None or m > best[0]):
            best = (m, p)
    return best[1] if best else None


def wait_for_pdf(folder: Path, since: float, timeout: float = 90.0) -> Path | None:
    """Wait until a PDF written since `since` exists and its size holds still."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        p = newest_pdf(folder, since)
        if p is not None:
            size = p.stat().st_size
            if last == (p, size) and size > 0:
                return p
            last = (p, size)
        time.sleep(0.5)
    return None


def render_pages(
    pdf: Path,
    pages: list[int] | None = None,
    area: dict | None = None,
    dpi: float | None = None,
    max_width_px: int = MAX_WIDTH_PX,
) -> tuple[list[dict], list[bytes]]:
    """PNG images of the requested pages (1-based; default all), or of an
    area {"page": n, "area": [x1, y1, x2, y2]} (mm from the page's bottom-left
    corner, y up). Without dpi a page is fitted to max_width_px wide and an
    area zoomed to max_width_px (at most 600 dpi)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        n = len(doc)
        info: list[dict] = []
        images: list[bytes] = []
        if area:
            page_no = int(area.get("page", 1))
            box = area.get("area")
            if not box or len(box) != 4:
                raise RenderError(
                    'area needs {"page": n, "area": [x1, y1, x2, y2]} (mm)'
                )
            todo = [(page_no, [float(v) for v in box])]
        else:
            todo = [(p, None) for p in (pages or range(1, n + 1))]
        for page_no, box in todo:
            if not 1 <= page_no <= n:
                raise RenderError(f"page {page_no}: the PDF has {n} pages")
            page = doc[page_no - 1]
            w_pt, h_pt = page.get_size()
            w_mm, h_mm = w_pt / 72 * 25.4, h_pt / 72 * 25.4
            if box:
                x1, y1, x2, y2 = (
                    min(box[0], box[2]),
                    min(box[1], box[3]),
                    max(box[0], box[2]),
                    max(box[1], box[3]),
                )
                d = dpi or min(MAX_DPI, max_width_px / max((x2 - x1) / 25.4, 1e-3))
                scale = d / 72
                # crop in points: left, bottom, right, top margins
                crop = (
                    max(x1, 0) / 25.4 * 72,
                    max(y1, 0) / 25.4 * 72,
                    max(w_mm - x2, 0) / 25.4 * 72,
                    max(h_mm - y2, 0) / 25.4 * 72,
                )
                bitmap = page.render(scale=scale, crop=crop)
            else:
                d = dpi or max_width_px / (w_mm / 25.4)
                scale = d / 72
                bitmap = page.render(scale=scale)
            im = bitmap.to_pil()
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            images.append(buf.getvalue())
            entry = {
                "page": page_no,
                "page_mm": [round(w_mm, 1), round(h_mm, 1)],
                "dpi": round(d, 1),
                "px": list(im.size),
            }
            if box:
                entry["area_mm"] = box
            info.append(entry)
        return info, images
    finally:
        doc.close()
