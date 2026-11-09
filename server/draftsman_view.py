"""Geometry for navigating Altium's Draftsman view before a screenshot.

Draftsman shows the pages of a drawing stacked top to bottom in one scrolled
view and has no script command to go to a page or zoom to an area. What a
script can do: ask where a point of a page is on screen
(``IDraftsman_DocumentView.PointToScreen``), fit the page that holds the view
centre (``PcbDrawing:FitSheet``) and zoom in x2 around the view centre
(``PcbDrawing:Zoom Action=In``). The server scrolls the view with real wheel
input over it (Shift+wheel: horizontally); posted wheel messages are ignored
by the WPF view. This module holds the arithmetic; rectangles are screen
pixels (left, top, right, bottom).
"""

from __future__ import annotations

Rect = tuple[int, int, int, int]

# Pixels the view scrolls per wheel notch (measured; refined per call)
DEFAULT_PIXELS_PER_NOTCH = 50.0
MAX_ZOOM_STEPS = 6


def parse_state(text: str) -> dict:
    """Parse 'pages=5;page=l,t,r,b;area=l,t,r,b' (or 'error=...')."""
    out: dict = {}
    for part in (text or "").split(";"):
        key, _, value = part.partition("=")
        key = key.strip()
        if not key:
            continue
        if key in ("page", "area"):
            nums = [int(v) for v in value.split(",") if v.strip()]
            if len(nums) == 4:
                out[key] = (nums[0], nums[1], nums[2], nums[3])
        elif key == "pages":
            out[key] = int(value)
        else:
            out[key] = value
    return out


def rect_spec(page_mm: tuple[float, float], area_mm: list | None = None) -> str:
    """Page rectangle (and area) in thousandths of mm for the bridge."""
    nums = [0.0, 0.0, page_mm[0], page_mm[1]]
    if area_mm:
        x1, y1, x2, y2 = (float(v) for v in area_mm)
        nums += [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    return ",".join(str(round(v * 1000)) for v in nums)


def centre(rect: Rect) -> tuple[float, float]:
    return (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2


def scroll_notches(
    target: Rect, view: Rect, pixels_per_notch: float, axis: str = "y"
) -> int:
    """Wheel notches that bring the target's centre to the view's centre.

    A positive notch (wheel up) moves the content down the screen, or right
    with Shift held (axis "x").
    """
    i = 0 if axis == "x" else 1
    d = centre(view)[i] - centre(target)[i]
    return round(d / max(pixels_per_notch, 1.0))


def inside(rect: Rect, view: Rect, tolerance: int = 4) -> bool:
    return (
        rect[0] >= view[0] - tolerance
        and rect[1] >= view[1] - tolerance
        and rect[2] <= view[2] + tolerance
        and rect[3] <= view[3] + tolerance
    )


def zoomed(rect: Rect, view: Rect, steps: int) -> tuple[float, float, float, float]:
    """Where rect lands after zooming in x2 ``steps`` times around the view centre."""
    cx, cy = centre(view)
    f = 2**steps
    return (
        cx + (rect[0] - cx) * f,
        cy + (rect[1] - cy) * f,
        cx + (rect[2] - cx) * f,
        cy + (rect[3] - cy) * f,
    )


def zoom_steps(
    area: Rect, view: Rect, margin: float = 0.04, max_steps: int = MAX_ZOOM_STEPS
) -> int:
    """Most x2 zoom-in steps after which the area still lies inside the view."""
    w, h = view[2] - view[0], view[3] - view[1]
    inner = (
        view[0] + w * margin,
        view[1] + h * margin,
        view[2] - w * margin,
        view[3] - h * margin,
    )
    best = 0
    for steps in range(1, max_steps + 1):
        z = zoomed(area, view, steps)
        if (
            z[0] >= inner[0]
            and z[1] >= inner[1]
            and z[2] <= inner[2]
            and z[3] <= inner[3]
        ):
            best = steps
        else:
            break
    return best


def crop_rect(area: Rect, view: Rect, margin: float = 0.05) -> Rect | None:
    """Area plus a margin, clipped to the view; None when it is off the view."""
    w, h = area[2] - area[0], area[3] - area[1]
    mx, my = max(10, round(w * margin)), max(10, round(h * margin))
    out = (
        max(view[0], area[0] - mx),
        max(view[1], area[1] - my),
        min(view[2], area[2] + mx),
        min(view[3], area[3] + my),
    )
    if out[2] - out[0] < 20 or out[3] - out[1] < 20:
        return None
    return out
