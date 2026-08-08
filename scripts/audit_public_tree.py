"""Fail on publishable files that resemble secrets or identifying infrastructure."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "release-assets"}
SKIP_FILES = {Path(__file__).name}
TEXT_SUFFIXES = {
    "",
    ".bat",
    ".css",
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".service",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PATTERNS = {
    "absolute user directory": re.compile(r"(?i)[A-Z]:[\\/](?:Users|Documents and Settings)[\\/]"),
    "private key header": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "credential token": re.compile(r"\b(?:gh[pousr]_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_-]{8,}"),
    "OpenAI-style key": re.compile(r"\bsk-(?!example|placeholder)[A-Za-z0-9_-]{16,}"),
    "embedded URL identity": re.compile(r"(?i)https?://[^/\s:@]+:[^@\s/]+@"),
}
URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\"'<>`()\[\]]+")
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def _allowed_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    if normalized in {
        "127.0.0.1",
        "localhost",
        "testserver",
        "github.com",
        "img.shields.io",
        "developers.openai.com",
        "ferretgeek.github.io",
    }:
        return True
    return normalized.endswith((".example", ".example.com", ".test"))


def _scan_line(relative: Path, number: int, line: str) -> list[tuple[Path, int, str]]:
    findings = [(relative, number, name) for name, pattern in PATTERNS.items() if pattern.search(line)]
    for raw in URL_PATTERN.findall(line):
        if "{" in raw or "}" in raw:
            continue
        try:
            host = urlsplit(raw.rstrip(".,;:")).hostname or ""
        except ValueError:
            findings.append((relative, number, "invalid URL"))
            continue
        if not _allowed_host(host):
            findings.append((relative, number, "unapproved URL host"))
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_loopback and not address.is_unspecified:
            findings.append((relative, number, "non-loopback IP address"))
    for email in EMAIL_PATTERN.findall(line):
        if not email.lower().endswith("@example.com"):
            findings.append((relative, number, "non-example email address"))
    return findings


def main() -> None:
    findings: list[tuple[Path, int, str]] = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name in SKIP_FILES or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "LICENSE"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        relative = path.relative_to(ROOT)
        for number, line in enumerate(text.splitlines(), start=1):
            findings.extend(_scan_line(relative, number, line))
    for relative, number, category in findings:
        print(f"{relative}:{number}: {category}")
    if findings:
        raise SystemExit(f"Public-tree audit found {len(findings)} issue(s)")
    print(f"Public-tree audit: {scanned} text files, 0 findings")


if __name__ == "__main__":
    main()
