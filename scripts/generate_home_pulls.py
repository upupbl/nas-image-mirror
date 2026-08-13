#!/usr/bin/env python3
"""Generate copy-ready pull commands for every home registry target."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mirror import load_images


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "images.yml"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "pulls" / "home"
DEFAULT_REGISTRY = "registry.runsh.de"


def home_entries(manifest: Path, registry: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for image in load_images(manifest):
        target = image["targets"].get("home")
        if target:
            entries.append((image["id"], f"{registry.rstrip('/')}/{target}"))
    return entries


def render_readme(entries: list[tuple[str, str]]) -> str:
    lines = [
        "# Home 镜像拉取目录",
        "",
        "此文件由 `images.yml` 自动生成，请勿手动修改。每条命令都包含明确的 tag；",
        "同一个地址会由 Docker 自动选择 AMD64 或 ARM64（前提是该镜像提供对应架构）。",
        "当前仓库允许匿名只读拉取，不需要先执行 `docker login`。",
        "",
        f"共 {len(entries)} 个镜像。",
        "",
        "| 清单 ID | 完整镜像地址 | 拉取命令 |",
        "| --- | --- | --- |",
    ]
    for image_id, reference in entries:
        lines.append(
            f"| `{image_id}` | `{reference}` | `docker pull {reference}` |"
        )
    lines.extend(
        [
            "",
            "纯命令版本见 [`docker-pull-commands.txt`](docker-pull-commands.txt)。",
            "",
        ]
    )
    return "\n".join(lines)


def render_commands(entries: list[tuple[str, str]]) -> str:
    lines = [
        "# Generated from images.yml; copy one command as needed.",
        "# This file is not intended to be executed as a bulk pull script.",
    ]
    lines.extend(f"docker pull {reference}" for _, reference in entries)
    return "\n".join(lines) + "\n"


def update_file(path: Path, content: str, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return True
    if check:
        print(f"Generated file is stale: {path.relative_to(REPOSITORY_ROOT)}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"Updated {path.relative_to(REPOSITORY_ROOT)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--registry", default=DEFAULT_REGISTRY)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    entries = home_entries(args.manifest, args.registry)
    results = [
        update_file(args.output / "README.md", render_readme(entries), args.check),
        update_file(
            args.output / "docker-pull-commands.txt",
            render_commands(entries),
            args.check,
        ),
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
