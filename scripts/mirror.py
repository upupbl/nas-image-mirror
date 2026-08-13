#!/usr/bin/env python3
"""Validate and mirror OCI images from a declarative YAML inventory."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
PLATFORM_RE = re.compile(r"^[a-z0-9]+/[a-z0-9_]+(?:/[a-z0-9._-]+)?$")
TARGET_RE = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r":[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
)


class MirrorError(RuntimeError):
    pass


@dataclass(frozen=True)
class Destination:
    name: str
    registry: str
    namespace: str
    username: str
    password: str

    def image_ref(self, relative_target: str) -> str:
        parts = [self.registry.rstrip("/")]
        if self.namespace.strip("/"):
            parts.append(self.namespace.strip("/"))
        parts.append(relative_target.lstrip("/"))
        return "/".join(parts)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MirrorError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MirrorError("Manifest root must be a mapping")
    return data


def validate_manifest(data: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[str] = []
    if data.get("version") != 1:
        errors.append("version must be 1")

    images = data.get("images")
    if not isinstance(images, list) or not images:
        errors.append("images must be a non-empty list")
        images = []

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(images, start=1):
        label = f"images[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} must be a mapping")
            continue

        image_id = raw.get("id")
        source = raw.get("source")
        targets = raw.get("targets")
        platforms = raw.get("required_platforms", [])
        copy_platforms = raw.get("copy_platforms", {})
        copy_compression = raw.get("copy_compression", {})

        if not isinstance(image_id, str) or not ID_RE.fullmatch(image_id):
            errors.append(f"{label}.id is invalid")
        elif image_id in seen:
            errors.append(f"duplicate image id: {image_id}")
        else:
            seen.add(image_id)

        if not isinstance(source, str) or not source or any(ch.isspace() for ch in source):
            errors.append(f"{label}.source must be one image reference")

        if not isinstance(targets, dict) or not targets:
            errors.append(f"{label}.targets must be a non-empty mapping")
            targets = {}
        else:
            for destination, target in targets.items():
                if not isinstance(destination, str) or not ID_RE.fullmatch(destination):
                    errors.append(f"{label}.targets has invalid destination {destination!r}")
                if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
                    errors.append(f"{label}.targets.{destination} must be repository/name:tag")

        if not isinstance(platforms, list) or any(
            not isinstance(platform, str) or not PLATFORM_RE.fullmatch(platform)
            for platform in platforms
        ):
            errors.append(f"{label}.required_platforms must contain os/architecture values")
            platforms = []
        elif len(platforms) != len(set(platforms)):
            errors.append(f"{label}.required_platforms contains duplicates")

        if not isinstance(copy_platforms, dict):
            errors.append(f"{label}.copy_platforms must be a destination mapping")
            copy_platforms = {}
        else:
            for destination, selected in copy_platforms.items():
                if destination not in targets:
                    errors.append(
                        f"{label}.copy_platforms.{destination} has no matching target"
                    )
                if not isinstance(selected, list) or not selected or any(
                    not isinstance(platform, str)
                    or not PLATFORM_RE.fullmatch(platform)
                    for platform in selected
                ):
                    errors.append(
                        f"{label}.copy_platforms.{destination} must contain "
                        "os/architecture values"
                    )
                elif len(selected) != len(set(selected)):
                    errors.append(
                        f"{label}.copy_platforms.{destination} contains duplicates"
                    )

        if not isinstance(copy_compression, dict):
            errors.append(f"{label}.copy_compression must be a destination mapping")
            copy_compression = {}
        else:
            for destination, compression in copy_compression.items():
                if destination not in copy_platforms:
                    errors.append(
                        f"{label}.copy_compression.{destination} requires "
                        "copy_platforms"
                    )
                if compression != "gzip":
                    errors.append(
                        f"{label}.copy_compression.{destination} must be gzip"
                    )

        normalized.append(
            {
                "id": image_id,
                "source": source,
                "targets": targets,
                "required_platforms": platforms,
                "copy_platforms": copy_platforms,
                "copy_compression": copy_compression,
            }
        )

    if errors:
        raise MirrorError("Manifest validation failed:\n- " + "\n- ".join(errors))
    return normalized


def load_images(path: Path) -> list[dict[str, Any]]:
    return validate_manifest(load_yaml(path))


def changed_images(
    current: list[dict[str, Any]], manifest_path: Path, base_ref: str | None
) -> list[dict[str, Any]]:
    if not base_ref or set(base_ref) == {"0"}:
        return current
    try:
        result = subprocess.run(
            ["git", "show", f"{base_ref}:{manifest_path.as_posix()}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        previous_data = yaml.safe_load(result.stdout)
        previous = validate_manifest(previous_data)
    except (subprocess.CalledProcessError, yaml.YAMLError, MirrorError):
        print(f"Base manifest at {base_ref} is unavailable; selecting all images.")
        return current

    previous_by_id = {item["id"]: item for item in previous}
    return [item for item in current if previous_by_id.get(item["id"]) != item]


def images_by_id(
    images: list[dict[str, Any]], requested_ids: str
) -> list[dict[str, Any]]:
    image_ids = [item.strip() for item in requested_ids.split(",") if item.strip()]
    if not image_ids:
        return images

    seen: set[str] = set()
    duplicates: set[str] = set()
    for image_id in image_ids:
        if image_id in seen:
            duplicates.add(image_id)
        seen.add(image_id)
    if duplicates:
        raise MirrorError(
            f"Duplicate requested image id(s): {', '.join(sorted(duplicates))}"
        )

    by_id = {image["id"]: image for image in images}
    unknown = sorted(set(image_ids) - set(by_id))
    if unknown:
        raise MirrorError(f"Unknown requested image id(s): {', '.join(unknown)}")
    return [by_id[image_id] for image_id in image_ids]


def docker_transport(reference: str) -> str:
    return reference if reference.startswith("docker://") else f"docker://{reference}"


def run_json(command: list[str]) -> Any:
    result = subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def inspect_platforms(source: str) -> set[str]:
    raw = run_json(["skopeo", "inspect", "--raw", docker_transport(source)])
    manifests = raw.get("manifests") if isinstance(raw, dict) else None
    if isinstance(manifests, list):
        found: set[str] = set()
        for manifest in manifests:
            platform = manifest.get("platform", {}) if isinstance(manifest, dict) else {}
            os_name = platform.get("os")
            architecture = platform.get("architecture")
            variant = platform.get("variant")
            if os_name and architecture:
                value = f"{os_name}/{architecture}"
                if variant:
                    value += f"/{variant}"
                found.add(value)
        return found

    details = run_json(["skopeo", "inspect", docker_transport(source)])
    os_name = details.get("Os")
    architecture = details.get("Architecture")
    variant = details.get("Variant")
    if not os_name or not architecture:
        return set()
    value = f"{os_name}/{architecture}"
    if variant:
        value += f"/{variant}"
    return {value}


def platform_matches(required: str, available: str) -> bool:
    if required == available:
        return True
    required_parts = required.split("/", 2)
    available_parts = available.split("/", 2)
    return len(required_parts) == 2 and required_parts == available_parts[:2]


def check_required_platforms(source: str, required: list[str]) -> set[str]:
    available = inspect_platforms(source)
    missing = {
        platform
        for platform in required
        if not any(platform_matches(platform, candidate) for candidate in available)
    }
    if missing:
        raise MirrorError(
            f"{source} is missing required platform(s): {', '.join(sorted(missing))}; "
            f"available: {', '.join(sorted(available)) or 'unknown'}"
        )
    return available


def destination_from_env(name: str) -> Destination:
    prefix = name.upper().replace("-", "_")
    registry = os.getenv(f"{prefix}_REGISTRY", "").strip()
    namespace = os.getenv(f"{prefix}_NAMESPACE", "").strip()
    username = os.getenv(f"{prefix}_REGISTRY_USER", "").strip()
    password = os.getenv(f"{prefix}_REGISTRY_PASSWORD", "")

    missing = [
        key
        for key, value in {
            f"{prefix}_REGISTRY": registry,
            f"{prefix}_REGISTRY_USER": username,
            f"{prefix}_REGISTRY_PASSWORD": password,
        }.items()
        if not value
    ]
    if missing:
        raise MirrorError(f"Destination {name} is missing: {', '.join(missing)}")
    if "://" in registry:
        raise MirrorError(f"{prefix}_REGISTRY must not include http:// or https://")
    return Destination(name, registry, namespace, username, password)


def login(destination: Destination) -> None:
    command = [
        "skopeo",
        "login",
        "--username",
        destination.username,
        "--password-stdin",
        destination.registry,
    ]
    subprocess.run(command, input=destination.password, text=True, check=True)


def regctl_login(destination: Destination) -> None:
    command = [
        "regctl",
        "registry",
        "login",
        destination.registry,
        "--user",
        destination.username,
        "--pass-stdin",
    ]
    subprocess.run(command, input=destination.password, text=True, check=True)


def copy_image(
    source: str, target: str, destination: Destination, attempts: int, delay: int
) -> None:
    command = [
        "skopeo",
        "copy",
        "--all",
        "--retry-times",
        "3",
        "--dest-creds",
        f"{destination.username}:{destination.password}",
        docker_transport(source),
        docker_transport(target),
    ]
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(command, check=True)
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise
            wait = delay * attempt
            print(f"Copy attempt {attempt}/{attempts} failed; retrying in {wait}s.")
            time.sleep(wait)


def temporary_platform_target(target: str, platform: str) -> str:
    repository, separator, tag = target.rpartition(":")
    if not separator or "/" not in repository:
        raise MirrorError(f"Target must include a registry and explicit tag: {target}")
    suffix = platform.replace("/", "-")
    return f"{repository}:{tag}-mirror-{suffix}"


def copy_selected_platforms(
    source: str,
    target: str,
    platforms: list[str],
    compression: str | None,
    attempts: int,
    delay: int,
) -> None:
    temporary_targets = [
        temporary_platform_target(target, platform) for platform in platforms
    ]
    try:
        for platform, temporary_target in zip(platforms, temporary_targets):
            if compression:
                source_ref = platform_digest_reference(source, platform)
                with tempfile.TemporaryDirectory(prefix="mirror-platform-") as work:
                    local_target = f"dir:{work}"
                    prepare_command = [
                        "skopeo",
                        "copy",
                        "--retry-times",
                        "3",
                        "--dest-compress",
                        "--dest-compress-format",
                        compression,
                        "--format",
                        "v2s2",
                        docker_transport(source_ref),
                        local_target,
                    ]
                    upload_command = [
                        "skopeo",
                        "copy",
                        "--retry-times",
                        "3",
                        "--format",
                        "v2s2",
                        local_target,
                        docker_transport(temporary_target),
                    ]
                    run_with_retries(
                        prepare_command,
                        attempts,
                        delay,
                        "Platform conversion",
                    )
                    run_with_retries(
                        upload_command,
                        attempts,
                        delay,
                        "Platform upload",
                    )
                continue
            else:
                command = [
                    "regctl",
                    "image",
                    "copy",
                    "--platform",
                    platform,
                    source,
                    temporary_target,
                ]
            run_with_retries(command, attempts, delay, "Platform copy")

        subprocess.run(["regctl", "index", "create", target], check=True)
        for platform, temporary_target in zip(platforms, temporary_targets):
            subprocess.run(
                [
                    "regctl",
                    "index",
                    "add",
                    target,
                    "--ref",
                    temporary_target,
                    "--desc-platform",
                    platform,
                ],
                check=True,
            )
    except subprocess.CalledProcessError:
        raise


def run_with_retries(
    command: list[str], attempts: int, delay: int, operation: str
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(command, check=True, timeout=4500)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == attempts:
                raise
            wait = delay * attempt
            print(
                f"{operation} attempt {attempt}/{attempts} failed; "
                f"retrying in {wait}s.",
                flush=True,
            )
            time.sleep(wait)


def platform_digest_reference(source: str, required: str) -> str:
    raw = run_json(["skopeo", "inspect", "--raw", docker_transport(source)])
    manifests = raw.get("manifests") if isinstance(raw, dict) else None
    if not isinstance(manifests, list):
        raise MirrorError(f"{source} is not a multi-platform image")
    matches: list[tuple[str, str]] = []
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        platform = manifest.get("platform", {})
        os_name = platform.get("os") if isinstance(platform, dict) else None
        architecture = (
            platform.get("architecture") if isinstance(platform, dict) else None
        )
        if not os_name or not architecture:
            continue
        available = f"{os_name}/{architecture}"
        variant = platform.get("variant")
        if variant:
            available += f"/{variant}"
        digest = manifest.get("digest")
        if isinstance(digest, str) and platform_matches(required, available):
            matches.append((available, digest))
    if not matches:
        raise MirrorError(f"{source} has no manifest matching {required}")
    exact = next((digest for available, digest in matches if available == required), None)
    digest = exact or matches[0][1]
    return f"{source.split('@', 1)[0]}@{digest}"


def append_summary(rows: list[tuple[str, str, str, str]]) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("## OCI image mirror\n\n")
        summary.write("| Status | Destination | Source | Target |\n")
        summary.write("| --- | --- | --- | --- |\n")
        for status, destination, source, target in rows:
            summary.write(f"| {status} | `{destination}` | `{source}` | `{target}` |\n")


def mirror(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    images = load_images(manifest_path)
    if args.image_ids:
        images = images_by_id(images, args.image_ids)
    elif args.scope == "changed":
        images = changed_images(images, manifest_path, args.base_ref)

    requested_destinations = [item.strip() for item in args.destinations.split(",") if item.strip()]
    if not requested_destinations:
        raise MirrorError("At least one destination is required")

    print(f"Selected {len(images)} image(s); destinations: {', '.join(requested_destinations)}")
    if not images:
        return 0

    destinations: dict[str, Destination] = {}
    if not args.dry_run:
        for name in requested_destinations:
            destination = destination_from_env(name)
            login(destination)
            destinations[name] = destination
        compatibility_destinations = {
            name
            for image in images
            for name in image["copy_platforms"]
            if name in requested_destinations
        }
        for name in sorted(compatibility_destinations):
            regctl_login(destinations[name])

    rows: list[tuple[str, str, str, str]] = []
    failed = False
    for image in images:
        source = image["source"]
        targets = image["targets"]
        applicable = [name for name in requested_destinations if name in targets]
        if not applicable:
            print(f"SKIP {image['id']}: no target for selected destination(s)")
            continue

        if args.dry_run:
            for name in applicable:
                selected = image["copy_platforms"].get(name)
                compression = image["copy_compression"].get(name)
                suffix = f" platforms={','.join(selected)}" if selected else ""
                if compression:
                    suffix += f" compression={compression}"
                print(f"DRY-RUN {source} -> {name}:{targets[name]}{suffix}")
            continue

        try:
            available = check_required_platforms(source, image["required_platforms"])
            print(f"{source} platforms: {', '.join(sorted(available)) or 'unknown'}")
        except (MirrorError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            print(f"ERROR inspecting {source}: {exc}", file=sys.stderr)
            for name in applicable:
                rows.append(("Inspect failed", name, source, targets[name]))
            failed = True
            continue

        for name in applicable:
            destination = destinations[name]
            target = destination.image_ref(targets[name])
            print(f"COPY {source} -> {target}")
            try:
                selected = image["copy_platforms"].get(name)
                compression = image["copy_compression"].get(name)
                if selected:
                    print(
                        f"Compatibility copy for {name}: "
                        f"{', '.join(selected)}"
                    )
                    copy_selected_platforms(
                        source,
                        target,
                        selected,
                        compression,
                        args.attempts,
                        args.retry_delay,
                    )
                else:
                    copy_image(
                        source, target, destination, args.attempts, args.retry_delay
                    )
                rows.append(("Success", name, source, target))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                rows.append(("Copy failed", name, source, target))
                failed = True

    append_summary(rows)
    succeeded = sum(row[0] == "Success" for row in rows)
    failures = len(rows) - succeeded
    print(f"Completed: {succeeded} succeeded, {failures} failed.")
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate images.yml")
    validate.add_argument("--manifest", default="images.yml")

    mirror_parser = subparsers.add_parser("mirror", help="mirror selected images")
    mirror_parser.add_argument("--manifest", default="images.yml")
    mirror_parser.add_argument("--scope", choices=("all", "changed"), default="all")
    mirror_parser.add_argument("--base-ref")
    mirror_parser.add_argument(
        "--image-ids",
        default="",
        help="comma-separated image ids to mirror; overrides --scope",
    )
    mirror_parser.add_argument("--destinations", default="home")
    mirror_parser.add_argument("--attempts", type=int, default=3)
    mirror_parser.add_argument("--retry-delay", type=int, default=10)
    mirror_parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "validate":
            images = load_images(Path(args.manifest))
            print(f"Valid manifest: {len(images)} image(s)")
            return 0
        return mirror(args)
    except MirrorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
