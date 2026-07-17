"""Tests for Skyportal terminal branding."""

from io import StringIO

from rich.console import Console

from skyportal.animation import (
    _ASTRONAUT_PIXELS,
    _PIXEL_STYLES,
    _final_banner,
    _pixel_art,
    show_startup_animation,
)


def test_astronaut_has_chibi_face_suit_and_badge():
    pixels = "\n".join(_ASTRONAUT_PIXELS)

    assert len(_ASTRONAUT_PIXELS) == 30
    assert pixels.count("E") >= 10
    assert pixels.count("W") >= 3
    assert pixels.count("@") == 1
    assert all(token in _PIXEL_STYLES for token in set(pixels) - {" ", "\n"})


def test_pixel_art_is_full_size_and_compresses_for_spin():
    full = _pixel_art()
    edge = _pixel_art(0.08)

    assert full.cell_len > edge.cell_len
    assert "SP" in full.plain
    assert full.plain.count("\n") == 29


def test_compact_banner_fits_narrow_terminal():
    output = StringIO()
    console = Console(file=output, width=70, force_terminal=False)

    console.print(_final_banner(console.width))

    assert max(len(line) for line in output.getvalue().splitlines()) <= 70
    assert "S  Skyportal" in output.getvalue()
    assert "YOUR AI COMMAND CENTER" in output.getvalue()


def test_non_terminal_startup_uses_plain_banner():
    output = StringIO()
    console = Console(file=output, width=80, force_terminal=False)

    show_startup_animation(console)

    assert output.getvalue() == "Skyportal — your AI command center\n"
