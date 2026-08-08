"""Validate publication assets, local links, and privacy-sensitive invariants."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise AssertionError(f"Not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


def verify_images() -> None:
    dashboard = ROOT / "docs/images/dashboard.png"
    social = ROOT / "docs/images/social-preview.png"
    assert png_size(dashboard) == (1280, 720)
    assert png_size(social) == (1280, 640)
    assert social.stat().st_size < 1_000_000
    assert not (ROOT / "docs/images/dashboard-source.png").exists()
    for name in ("favicon.svg", "favicon-32.png", "favicon.ico"):
        assert (ROOT / "demo/assets" / name).read_bytes() == (ROOT / "stress_tool/static" / name).read_bytes()
    assert (ROOT / "demo/assets/dashboard.png").read_bytes() == dashboard.read_bytes()


def verify_links() -> None:
    missing: list[str] = []
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for markdown in ROOT.rglob("*.md"):
        for raw in pattern.findall(markdown.read_text(encoding="utf-8")):
            target = raw.strip().split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            if not (markdown.parent / unquote(target)).resolve().exists():
                missing.append(f"{markdown.relative_to(ROOT)}: {raw}")
    assert not missing, "Missing local links:\n" + "\n".join(missing)


def verify_dashboard_contract() -> None:
    html = (ROOT / "stress_tool/static/index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([A-Za-z][\w:-]*)"', html))
    referenced = set(re.findall(r"\$\('([A-Za-z][\w:-]*)'\)", html))
    assert not (referenced - ids), f"Missing HTML IDs: {sorted(referenced - ids)}"
    assert "--canvas: #17191d" in html
    assert all(value in html for value in ('data-palette="iris"', 'value="jade"', 'value="sunrise"'))
    assert "favicon.svg" in html and "favicon-32.png" in html and "favicon.ico" in html
    assert "api_key: config.api_key" not in html
    assert "base_url: config.base_url" not in html


def verify_pricing_snapshot() -> None:
    table = json.loads((ROOT / "pricing_table.json").read_text(encoding="utf-8"))
    expected = {
        "gpt-5.6-sol": (5.0, 0.5, 6.25, 30.0),
        "gpt-5.6-terra": (2.0, 0.2, 2.5, 12.0),
        "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
    }
    for model, values in expected.items():
        item = table["models"][model]
        assert tuple(item[key] for key in ("input", "cached_input", "cache_write", "output")) == values


def verify_private_artifacts_absent() -> None:
    forbidden = (
        "stress_state.json",
        "stress_profiles.json",
        "密钥和模型.txt",
        "benchmark_results",
        "backups",
        ".serena",
        ".playwright-mcp",
    )
    assert not [name for name in forbidden if (ROOT / name).exists()]


def main() -> None:
    verify_images()
    verify_links()
    verify_dashboard_contract()
    verify_pricing_snapshot()
    verify_private_artifacts_absent()
    print("Publication assets and privacy invariants: OK")


if __name__ == "__main__":
    main()
