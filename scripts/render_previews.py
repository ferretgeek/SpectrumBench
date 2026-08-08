"""Build publication-ready screenshots from a real local dashboard capture."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"
SOURCE = IMAGES / "dashboard-source.png"


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf"),
        Path(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
    )
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _rounded_image(image: Image.Image, size: tuple[int, int], radius: int) -> Image.Image:
    fitted = ImageOps.fit(image.convert("RGB"), size, Image.Resampling.LANCZOS, centering=(0.5, 0.15))
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    fitted.putalpha(mask)
    return fitted


def render() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    source = Image.open(SOURCE).convert("RGB")

    dashboard = ImageOps.fit(source, (1280, 720), Image.Resampling.LANCZOS, centering=(0.5, 0.1))
    dashboard.save(IMAGES / "dashboard.png", optimize=True)

    social = Image.new("RGB", (1280, 640), "#17191d")
    draw = ImageDraw.Draw(social)
    draw.rectangle((0, 0, 1280, 8), fill="#5558f7")
    draw.rectangle((320, 0, 640, 8), fill="#36bfd3")
    draw.rectangle((640, 0, 960, 8), fill="#ef5579")
    draw.rectangle((960, 0, 1280, 8), fill="#e5a633")
    draw.ellipse((-150, 400, 280, 830), fill="#22252c")

    logo = Image.open(ROOT / "stress_tool" / "static" / "favicon-32.png").convert("RGBA")
    logo = logo.resize((74, 74), Image.Resampling.LANCZOS)
    social.paste(logo, (70, 70), logo)
    draw.text((166, 76), "SPECTRUMBENCH", font=_font(21, bold=True), fill="#a9aaff")
    draw.text((70, 177), "光谱测速台", font=_font(54, bold=True), fill="#f7f6f2")
    draw.text((70, 250), "把速度、等待与额度，\n变成可复核的证据。", font=_font(27), fill="#c9c8c3", spacing=12)
    draw.text((70, 376), "Single-stream  ·  Exact usage  ·  Auditable", font=_font(17, bold=True), fill="#7dd9e7")
    draw.rounded_rectangle((70, 438, 450, 492), radius=14, fill="#292d34", outline="#3c414a", width=2)
    draw.text((92, 452), "LOCAL FIRST  /  SERVER READY", font=_font(16, bold=True), fill="#ecebe7")
    draw.text((70, 558), "github.com/ferretgeek/SpectrumBench", font=_font(17), fill="#92938f")

    preview = _rounded_image(source, (660, 372), 25)
    shadow = Image.new("RGBA", social.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle((566, 132, 1250, 528), radius=32, fill=(0, 0, 0, 78))
    social = Image.alpha_composite(social.convert("RGBA"), shadow)
    social.alpha_composite(preview, (578, 112))
    border = ImageDraw.Draw(social)
    border.rounded_rectangle((578, 112, 1237, 483), radius=25, outline="#50545d", width=2)

    social = social.convert("RGB")
    social.save(IMAGES / "social-preview.png", optimize=True)


if __name__ == "__main__":
    render()
