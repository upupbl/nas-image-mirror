import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from scripts import mirror


class ManifestTests(unittest.TestCase):
    def test_valid_manifest(self):
        data = {
            "version": 1,
            "images": [
                {
                    "id": "valkey-9",
                    "source": "docker.io/valkey/valkey:9",
                    "targets": {"home": "common/valkey:9"},
                    "required_platforms": ["linux/amd64", "linux/arm64"],
                }
            ],
        }
        images = mirror.validate_manifest(data)
        self.assertEqual(images[0]["id"], "valkey-9")

    def test_copy_platforms_are_destination_specific(self):
        data = {
            "version": 1,
            "images": [
                {
                    "id": "home-assistant",
                    "source": "homeassistant/home-assistant:latest",
                    "targets": {
                        "home": "home-assistant/server:latest",
                        "aliyun": "home-assistant:latest",
                    },
                    "required_platforms": ["linux/amd64", "linux/arm64"],
                    "copy_platforms": {
                        "aliyun": ["linux/amd64", "linux/arm64"]
                    },
                }
            ],
        }
        images = mirror.validate_manifest(data)
        self.assertEqual(
            images[0]["copy_platforms"],
            {"aliyun": ["linux/amd64", "linux/arm64"]},
        )

    def test_copy_platforms_require_matching_target(self):
        item = {
            "id": "alpine",
            "source": "alpine:3.20",
            "targets": {"home": "common/alpine:3.20"},
            "copy_platforms": {"aliyun": ["linux/amd64"]},
        }
        with self.assertRaisesRegex(mirror.MirrorError, "has no matching target"):
            mirror.validate_manifest({"version": 1, "images": [item]})

    def test_duplicate_ids_are_rejected(self):
        item = {
            "id": "same",
            "source": "alpine:3.20",
            "targets": {"home": "common/alpine:3.20"},
        }
        with self.assertRaisesRegex(mirror.MirrorError, "duplicate image id"):
            mirror.validate_manifest({"version": 1, "images": [item, item]})

    def test_target_requires_explicit_tag(self):
        item = {
            "id": "alpine",
            "source": "alpine:3.20",
            "targets": {"home": "common/alpine"},
        }
        with self.assertRaisesRegex(mirror.MirrorError, "repository/name:tag"):
            mirror.validate_manifest({"version": 1, "images": [item]})

    @mock.patch("scripts.mirror.subprocess.run")
    def test_changed_scope_selects_only_modified_entry(self, run):
        old = """version: 1
images:
  - id: alpine
    source: alpine:3.19
    targets: {home: common/alpine:3.19}
  - id: valkey
    source: valkey/valkey:9
    targets: {home: common/valkey:9}
"""
        run.return_value = subprocess.CompletedProcess([], 0, stdout=old, stderr="")
        current = mirror.validate_manifest(
            {
                "version": 1,
                "images": [
                    {
                        "id": "alpine",
                        "source": "alpine:3.20",
                        "targets": {"home": "common/alpine:3.20"},
                    },
                    {
                        "id": "valkey",
                        "source": "valkey/valkey:9",
                        "targets": {"home": "common/valkey:9"},
                    },
                ],
            }
        )
        changed = mirror.changed_images(current, Path("images.yml"), "abc123")
        self.assertEqual([item["id"] for item in changed], ["alpine"])

    def test_image_ids_select_only_requested_entries_in_requested_order(self):
        images = [
            {"id": "alpine", "source": "alpine:3.20"},
            {"id": "postgres-18", "source": "postgres:18"},
        ]
        selected = mirror.images_by_id(images, "postgres-18, alpine")
        self.assertEqual([item["id"] for item in selected], ["postgres-18", "alpine"])

    def test_unknown_image_id_is_rejected(self):
        with self.assertRaisesRegex(mirror.MirrorError, "Unknown requested image id.*missing"):
            mirror.images_by_id([{"id": "alpine"}], "missing")


class DestinationTests(unittest.TestCase):
    def test_nested_home_path(self):
        destination = mirror.Destination("home", "registry.example.com", "", "u", "p")
        self.assertEqual(
            destination.image_ref("immich/server:v1"),
            "registry.example.com/immich/server:v1",
        )

    @mock.patch.dict(
        os.environ,
        {
            "HOME_REGISTRY": "registry.example.com",
            "HOME_REGISTRY_USER": "mirror",
            "HOME_REGISTRY_PASSWORD": "secret",
        },
        clear=True,
    )
    def test_destination_from_env(self):
        destination = mirror.destination_from_env("home")
        self.assertEqual(destination.registry, "registry.example.com")

    @mock.patch("scripts.mirror.subprocess.run")
    def test_copy_passes_destination_credentials_explicitly(self, run):
        destination = mirror.Destination(
            "home", "registry.example.com", "", "mirror", "secret"
        )
        mirror.copy_image(
            "alpine:3.20",
            "registry.example.com/common/alpine:3.20",
            destination,
            attempts=1,
            delay=0,
        )
        command = run.call_args.args[0]
        credential_index = command.index("--dest-creds")
        self.assertEqual(command[credential_index + 1], "mirror:secret")

    @mock.patch("scripts.mirror.subprocess.run")
    def test_selected_platform_copy_builds_clean_index(self, run):
        mirror.copy_selected_platforms(
            "homeassistant/home-assistant:latest",
            "registry.example.com/ns/home-assistant:latest",
            ["linux/amd64", "linux/arm64"],
            compression=None,
            attempts=1,
            delay=0,
        )
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands[0],
            [
                "regctl",
                "image",
                "copy",
                "--platform",
                "linux/amd64",
                "homeassistant/home-assistant:latest",
                "registry.example.com/ns/home-assistant:latest-mirror-linux-amd64",
            ],
        )
        self.assertEqual(
            commands[2],
            [
                "regctl",
                "index",
                "create",
                "registry.example.com/ns/home-assistant:latest",
            ],
        )
        self.assertEqual(commands[3][1:3], ["index", "add"])
        self.assertEqual(commands[3][-2:], ["--desc-platform", "linux/amd64"])
        self.assertEqual(commands[4][1:3], ["index", "add"])
        self.assertEqual(commands[4][-2:], ["--desc-platform", "linux/arm64"])

    @mock.patch("scripts.mirror.run_json")
    def test_platform_digest_reference_prefers_exact_platform(self, run_json):
        run_json.return_value = {
            "manifests": [
                {
                    "digest": "sha256:amd",
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:arm",
                    "platform": {
                        "os": "linux",
                        "architecture": "arm64",
                        "variant": "v8",
                    },
                },
            ]
        }
        self.assertEqual(
            mirror.platform_digest_reference("example/image:latest", "linux/arm64"),
            "example/image:latest@sha256:arm",
        )

    @mock.patch("scripts.mirror.platform_digest_reference")
    @mock.patch("scripts.mirror.tempfile.TemporaryDirectory")
    @mock.patch("scripts.mirror.subprocess.run")
    def test_gzip_copy_recompresses_selected_manifest(
        self, run, temporary_directory, platform_ref
    ):
        platform_ref.side_effect = ["source@sha256:amd", "source@sha256:arm"]
        temporary_directory.return_value.__enter__.side_effect = [
            "/tmp/amd",
            "/tmp/arm",
        ]
        mirror.copy_selected_platforms(
            "source:latest",
            "registry.example.com/ns/image:latest",
            ["linux/amd64", "linux/arm64"],
            compression="gzip",
            attempts=1,
            delay=0,
        )
        first_command = run.call_args_list[0].args[0]
        self.assertEqual(
            first_command,
            [
                "skopeo",
                "copy",
                "--retry-times",
                "3",
                "--dest-compress",
                "--dest-compress-format",
                "gzip",
                "--format",
                "v2s2",
                "docker://source@sha256:amd",
                "dir:/tmp/amd",
            ],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "skopeo",
                "copy",
                "--retry-times",
                "3",
                "--format",
                "v2s2",
                "dir:/tmp/amd",
                "docker://registry.example.com/ns/image:latest-mirror-linux-amd64",
            ],
        )
        self.assertTrue(
            all(call.kwargs["timeout"] == 4500 for call in run.call_args_list[:4])
        )


class PlatformTests(unittest.TestCase):
    @mock.patch("scripts.mirror.run_json")
    def test_manifest_list_platforms(self, run_json):
        run_json.return_value = {
            "manifests": [
                {"platform": {"os": "linux", "architecture": "amd64"}},
                {"platform": {"os": "linux", "architecture": "arm64"}},
            ]
        }
        self.assertEqual(
            mirror.inspect_platforms("alpine:3.20"),
            {"linux/amd64", "linux/arm64"},
        )

    @mock.patch("scripts.mirror.inspect_platforms")
    def test_architecture_requirement_accepts_available_variant(self, inspect_platforms):
        inspect_platforms.return_value = {"linux/amd64", "linux/arm64/v8"}
        self.assertEqual(
            mirror.check_required_platforms(
                "postgres:18", ["linux/amd64", "linux/arm64"]
            ),
            {"linux/amd64", "linux/arm64/v8"},
        )

    @mock.patch("scripts.mirror.inspect_platforms")
    def test_explicit_variant_requirement_stays_strict(self, inspect_platforms):
        inspect_platforms.return_value = {"linux/arm64/v9"}
        with self.assertRaisesRegex(mirror.MirrorError, "linux/arm64/v8"):
            mirror.check_required_platforms("example:latest", ["linux/arm64/v8"])


if __name__ == "__main__":
    unittest.main()
