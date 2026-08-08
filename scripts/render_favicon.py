"""Render PNG/ICO companions for the checked-in SVG brand mark."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "stress_tool" / "static"


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * ratio) for a, b in zip(start, end, strict=True))


def render(size: int = 256) -> Image.Image:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pixels = gradient.load()
    for y in range(size):
        for x in range(size):
            ratio = min(max((x + y) / (2 * max(size - 1, 1)), 0), 1)
            mid = _mix((85, 88, 247), (131, 92, 246), min(ratio * 2, 1))
            color = _mix(mid, (239, 85, 121), max((ratio - 0.5) * 2, 0))
            pixels[x, y] = (*color, 255)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (3 * scale, 3 * scale, 61 * scale, 61 * scale),
        radius=17 * scale,
        fill=255,
    )
    image.alpha_composite(Image.composite(gradient, Image.new("RGBA", image.size), mask))
    draw = ImageDraw.Draw(image)
    points = [(12, 37), (20, 37), (24, 22), (31, 48), (37, 27), (41, 37), (52, 37)]
    points = [(round(x * scale), round(y * scale)) for x, y in points]
    draw.line(points, fill=(239, 251, 255, 255), width=max(round(5 * scale), 1), joint="curve")
    r = 3 * scale
    draw.ellipse((51 * scale - r, 37 * scale - r, 51 * scale + r, 37 * scale + r), fill="white")
    return image


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    master = render()
    master.resize((32, 32), Image.Resampling.LANCZOS).save(STATIC / "favicon-32.png", optimize=True)
    master.save(
        STATIC / "favicon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
