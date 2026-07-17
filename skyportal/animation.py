"""Skyportal terminal branding and startup animation."""

import os
import time
from math import hypot
from typing import Dict, List, Tuple

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

_PIXEL_STYLES: Dict[str, Tuple[str, str]] = {
    "O": ("██", "bold #052e7a"),
    "D": ("██", "bold #0757bc"),
    "B": ("██", "bold #0787e8"),
    "C": ("██", "bold #15c7ed"),
    "L": ("██", "bold #8be9ff"),
    "W": ("██", "bold #effcff"),
    "G": ("██", "bold #59d8f6"),
    "E": ("██", "bold #062a73"),
    "P": ("██", "bold #7de3ef"),
    "@": ("SP", "bold white on #2563eb"),
}


def _paint_ellipse(
    canvas: List[List[str]],
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    token: str,
) -> None:
    for y, row in enumerate(canvas):
        for x in range(len(row)):
            distance = ((x - center_x) / radius_x) ** 2 + ((y - center_y) / radius_y) ** 2
            if distance <= 1:
                row[x] = token


def _paint_line(
    canvas: List[List[str]],
    start: Tuple[float, float],
    end: Tuple[float, float],
    radius: float,
    token: str,
) -> None:
    start_x, start_y = start
    end_x, end_y = end
    length_squared = (end_x - start_x) ** 2 + (end_y - start_y) ** 2
    for y, row in enumerate(canvas):
        for x in range(len(row)):
            if length_squared == 0:
                distance = hypot(x - start_x, y - start_y)
            else:
                projection = (
                    (x - start_x) * (end_x - start_x) + (y - start_y) * (end_y - start_y)
                ) / length_squared
                projection = max(0.0, min(1.0, projection))
                nearest_x = start_x + projection * (end_x - start_x)
                nearest_y = start_y + projection * (end_y - start_y)
                distance = hypot(x - nearest_x, y - nearest_y)
            if distance <= radius:
                row[x] = token


def _paint_rectangle(
    canvas: List[List[str]],
    left: int,
    top: int,
    right: int,
    bottom: int,
    token: str,
) -> None:
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            canvas[y][x] = token


def _build_astronaut() -> Tuple[str, ...]:
    """Build a chibi astronaut with an oversized helmet and thumbs-up pose."""
    canvas = [[" " for _ in range(38)] for _ in range(30)]

    _paint_ellipse(canvas, 14.0, 20.5, 8.2, 6.8, "O")
    _paint_ellipse(canvas, 14.0, 20.5, 7.3, 6.0, "D")
    _paint_ellipse(canvas, 14.0, 19.8, 6.5, 5.2, "B")

    _paint_line(canvas, (9.0, 19.0), (5.0, 22.0), 3.0, "O")
    _paint_line(canvas, (5.0, 22.0), (7.0, 24.5), 3.0, "O")
    _paint_line(canvas, (9.0, 19.0), (5.0, 22.0), 2.0, "B")
    _paint_line(canvas, (5.0, 22.0), (7.0, 24.5), 2.0, "C")
    _paint_ellipse(canvas, 7.4, 24.2, 2.7, 2.2, "O")
    _paint_ellipse(canvas, 7.4, 24.2, 1.8, 1.4, "L")

    _paint_line(canvas, (18.5, 18.2), (27.5, 14.5), 3.0, "O")
    _paint_line(canvas, (18.5, 18.2), (27.5, 14.5), 2.0, "B")
    _paint_ellipse(canvas, 29.5, 13.7, 3.6, 3.1, "O")
    _paint_ellipse(canvas, 29.5, 13.7, 2.7, 2.2, "C")
    _paint_line(canvas, (30.5, 13.0), (31.4, 9.3), 2.2, "O")
    _paint_line(canvas, (30.5, 13.0), (31.4, 9.3), 1.3, "L")
    _paint_ellipse(canvas, 31.5, 8.9, 1.5, 1.4, "O")
    _paint_ellipse(canvas, 31.5, 8.9, 0.8, 0.7, "L")

    _paint_line(canvas, (11.8, 24.0), (8.0, 28.0), 3.2, "O")
    _paint_line(canvas, (11.8, 24.0), (8.0, 28.0), 2.2, "B")
    _paint_ellipse(canvas, 6.8, 28.0, 3.6, 1.9, "O")
    _paint_ellipse(canvas, 6.8, 27.7, 2.7, 1.1, "L")
    _paint_line(canvas, (16.6, 24.0), (22.5, 27.0), 3.2, "O")
    _paint_line(canvas, (16.6, 24.0), (22.5, 27.0), 2.2, "C")
    _paint_ellipse(canvas, 24.0, 27.4, 3.7, 2.0, "O")
    _paint_ellipse(canvas, 24.0, 27.1, 2.7, 1.2, "L")

    _paint_rectangle(canvas, 10, 18, 18, 23, "O")
    _paint_rectangle(canvas, 11, 19, 17, 22, "D")
    for x, y in ((12, 20), (14, 20), (16, 20), (12, 22), (16, 22)):
        canvas[y][x] = "L"
    canvas[22][14] = "@"

    _paint_ellipse(canvas, 2.2, 8.5, 2.5, 3.2, "O")
    _paint_ellipse(canvas, 2.2, 8.5, 1.5, 2.3, "C")
    _paint_ellipse(canvas, 25.8, 8.5, 2.5, 3.2, "O")
    _paint_ellipse(canvas, 25.8, 8.5, 1.5, 2.3, "B")
    _paint_ellipse(canvas, 14.0, 8.0, 12.5, 8.3, "O")
    _paint_ellipse(canvas, 14.0, 8.0, 11.6, 7.6, "D")
    _paint_ellipse(canvas, 14.0, 7.8, 10.7, 6.9, "C")
    _paint_ellipse(canvas, 14.0, 8.2, 9.8, 6.1, "L")
    _paint_ellipse(canvas, 14.0, 8.5, 9.1, 5.5, "G")

    _paint_ellipse(canvas, 9.0, 4.8, 3.0, 1.2, "W")
    _paint_ellipse(canvas, 7.0, 6.2, 1.1, 1.5, "L")
    _paint_ellipse(canvas, 10.2, 8.1, 1.5, 1.8, "E")
    _paint_ellipse(canvas, 18.0, 8.1, 1.5, 1.8, "E")
    canvas[7][10] = "W"
    canvas[7][18] = "W"
    canvas[11][7] = "P"
    canvas[11][21] = "P"
    for x, y in ((11, 11), (12, 12), (13, 13), (14, 13), (15, 13), (16, 12), (17, 11)):
        canvas[y][x] = "E"

    for x, y in ((5, 18), (8, 20), (20, 19), (23, 17), (28, 14), (30, 14), (9, 27), (21, 26)):
        if canvas[y][x] not in (" ", "O", "E"):
            canvas[y][x] = "L"

    return tuple("".join(row).rstrip() for row in canvas)


_ASTRONAUT_PIXELS = _build_astronaut()


def _pixel_art(scale: float = 1.0) -> Text:
    """Render the astronaut's layered blue pixels at a horizontal spin scale."""
    source_width = max(len(line) for line in _ASTRONAUT_PIXELS)
    target_width = max(1, int(source_width * scale))
    result = Text()
    for line_number, line in enumerate(_ASTRONAUT_PIXELS):
        padded = line.ljust(source_width)
        for index in range(target_width):
            source_index = min(source_width - 1, int(index / scale))
            token = padded[source_index]
            cell, style = _PIXEL_STYLES.get(token, ("  ", ""))
            result.append(cell, style=style)
        if line_number != len(_ASTRONAUT_PIXELS) - 1:
            result.append("\n")
    return result


def _astronaut_renderable(scale: float = 1.0, compact: bool = False) -> RenderableType:
    return Align.center(_pixel_art(scale * (0.72 if compact else 1.0)))


def _final_banner(width: int) -> RenderableType:
    """Return a responsive final frame that remains after the animation."""
    if width < 76:
        return Group(
            _astronaut_renderable(compact=True),
            Text(),
            Align.center(_brand_wordmark()),
            Align.center(Text("YOUR AI COMMAND CENTER", style="bold #3b82f6")),
        )

    return Group(
        _astronaut_renderable(),
        Text(),
        Align.center(_brand_wordmark()),
        Text(),
        Align.center(Text("Y O U R   A I   C O M M A N D   C E N T E R", style="bold #3b82f6")),
        Align.center(Text("Agent  /  Servers  /  Compute", style="dim")),
    )


def _brand_wordmark() -> Text:
    """Render the compact logo and wordmark used by the command center."""
    wordmark = Text()
    wordmark.append("S", style="bold #3b82f6")
    wordmark.append("  Skyportal", style="bold")
    return wordmark


def show_static_banner(console: Console) -> None:
    """Render the final branded banner without animation."""
    console.print(_final_banner(console.width))


def show_startup_animation(console: Console, delay: float = 0.035) -> None:
    """Spin in the astronaut and materialize the Skyportal wordmark."""
    if not console.is_terminal:
        console.print("[bold cyan]Skyportal[/bold cyan] — your AI command center")
        return

    if os.environ.get("SKYPORTAL_NO_ANIMATION"):
        show_static_banner(console)
        console.print()
        return

    try:
        speed = max(0.0, float(os.environ.get("SKYPORTAL_ANIMATION_SPEED", "1")))
    except ValueError:
        speed = 1.0

    compact = console.width < 76
    spin = (0.08, 0.20, 0.42, 0.72, 1.0, 0.62, 0.22, 0.58, 0.86, 1.0)
    with Live(
        _astronaut_renderable(spin[0], compact),
        console=console,
        refresh_per_second=30,
        transient=True,
    ) as live:
        for scale in spin:
            live.update(_astronaut_renderable(scale, compact), refresh=True)
            time.sleep(delay * speed)

        if console.width >= 76:
            live.update(
                Group(
                    _astronaut_renderable(),
                    Text(),
                    Align.center(_brand_wordmark()),
                ),
                refresh=True,
            )
            time.sleep(delay * speed)

    show_static_banner(console)
    console.print()
