"""Render the README's terminal diagnosis demo GIF."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 620
OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "assets" / "skyportal-diagnose.gif"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
BOLD_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BACKGROUND = "#eef2ff"
TERMINAL = "#ffffff"
BORDER = "#c7d2fe"
TEXT = "#172033"
MUTED = "#667085"
BLUE = "#3b82f6"
GREEN = "#059669"

FONT = ImageFont.truetype(FONT_PATH, 25)
BOLD = ImageFont.truetype(BOLD_FONT_PATH, 25)
SMALL = ImageFont.truetype(FONT_PATH, 19)

PROMPT = "diagnose latest deployment"
RESULTS = (
    "Deployment detected",
    "Compared against previous release",
    "GPU utilization dropped 27%",
)


def frame(typed: str, visible_results: int, show_root_cause: bool) -> Image.Image:
    """Draw one terminal frame."""
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((35, 35, WIDTH - 35, HEIGHT - 35), 22, fill=TERMINAL, outline=BORDER, width=2)

    draw.ellipse((65, 66, 81, 82), fill="#ff6b6b")
    draw.ellipse((91, 66, 107, 82), fill="#fbbf24")
    draw.ellipse((117, 66, 133, 82), fill="#34d399")
    draw.text((WIDTH // 2, 74), "Skyportal Agent", font=SMALL, fill=MUTED, anchor="mm")
    draw.line((35, 105, WIDTH - 35, 105), fill=BORDER, width=2)

    x = 78
    y = 145
    draw.text((x, y), "skyportal", font=BOLD, fill=BLUE)
    prompt_x = x + draw.textlength("skyportal", font=BOLD)
    draw.text((prompt_x, y), " [connected] > ", font=FONT, fill=GREEN)
    command_x = prompt_x + draw.textlength(" [connected] > ", font=FONT)
    draw.text((command_x, y), typed, font=FONT, fill=TEXT)
    if len(typed) < len(PROMPT):
        cursor_x = command_x + draw.textlength(typed, font=FONT)
        draw.rectangle((cursor_x + 2, y + 4, cursor_x + 14, y + 29), fill=TEXT)

    line_y = 215
    for index, result in enumerate(RESULTS[:visible_results]):
        draw.text((x, line_y + index * 49), "✓", font=BOLD, fill=GREEN)
        draw.text((x + 42, line_y + index * 49), result, font=FONT, fill=TEXT)

    if show_root_cause:
        root_y = line_y + len(RESULTS) * 49 + 12
        draw.text((x, root_y), "✓", font=BOLD, fill=GREEN)
        draw.text((x + 42, root_y), "Root cause", font=BOLD, fill=TEXT)
        draw.rounded_rectangle(
            (x + 40, root_y + 48, WIDTH - 80, root_y + 142),
            12,
            fill="#eff6ff",
            outline="#bfdbfe",
            width=2,
        )
        draw.text(
            (x + 66, root_y + 66),
            "Deployment 8a3c1 changed the CUDA version",
            font=BOLD,
            fill=TEXT,
        )
        draw.text((x + 66, root_y + 105), "Likely confidence: 94%", font=SMALL, fill=BLUE)

    return image


def main() -> None:
    """Build typewriter, progress, and final-result frames."""
    frames = []
    durations = []

    for character_count in range(0, len(PROMPT) + 1, 2):
        frames.append(frame(PROMPT[:character_count], 0, False))
        durations.append(75)
    if len(PROMPT) % 2:
        frames.append(frame(PROMPT, 0, False))
        durations.append(450)
    else:
        durations[-1] = 450

    for result_count in range(1, len(RESULTS) + 1):
        frames.append(frame(PROMPT, result_count, False))
        durations.append(650)

    frames.append(frame(PROMPT, len(RESULTS), True))
    durations.append(3600)

    frames[0].save(
        OUTPUT,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )


if __name__ == "__main__":
    main()
