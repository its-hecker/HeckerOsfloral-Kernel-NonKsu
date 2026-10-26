# SPDX-License-Identifier: Apache-2.0
"""Reject non-KSU boot releases that would mismatch the ROM or retain root."""

from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_boot import check


def image(kernel, version=2, os_version=123):
    header = bytearray(4096)
    header[:8] = b"ANDROID!"
    struct.pack_into("<10I", header, 8, len(kernel), 0x8000, 7, 0x1000000,
                     0, 0, 0x100, 4096, version, os_version)
    struct.pack_into("<I", header, 1648, len(b"FLORAL-DTB"))
    return (bytes(header) + kernel + b"\0" * ((-len(kernel)) % 4096)
            + b"ramdisk" + b"\0" * (4096 - 7) + b"FLORAL-DTB")


class PackageBootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rooted = Path(self.tmp.name) / "rooted/coral"
        self.nonksu = Path(self.tmp.name) / "nonksu/coral"
        for root, config, kernel in ((self.rooted, "CONFIG_KSU=y\n", b"ROOT-KERNEL"),
                                     (self.nonksu, "", b"NONKSU-KERNEL")):
            obj = root / "obj/KERNEL_OBJ"
            (obj / "include/config").mkdir(parents=True)
            (obj / "include/config/kernel.release").write_text("4.14.399-CNB\n")
            (obj / ".config").write_text(config + "CONFIG_MODVERSIONS=y\n")
            (obj / "Module.symvers").write_text("0x00000001 module_layout vmlinux EXPORT_SYMBOL\n"
                                               "0x00000002 normal_export vmlinux EXPORT_SYMBOL\n")
            (obj / "System.map").write_text("00008000 T _text\n")
            (root / "boot.img").write_bytes(image(kernel))
            (root / "kernel").write_bytes(kernel)
        props = self.rooted / "vendor/build.prop"
        props.parent.mkdir()
        props.write_text("ro.product.vendor.device=coral\nro.build.fingerprint=HeckerOS/coral/release\n")
        self.object = self.nonksu / "obj/KERNEL_OBJ"

    def test_accepts_matching_module_abi_and_a_new_nonksu_kernel(self):
        path = self.rooted / "obj/KERNEL_OBJ/Module.symvers"
        path.write_text(path.read_text() + "0x00000003 ksu_compat vmlinux EXPORT_SYMBOL\n")
        record = check(self.rooted, self.nonksu, "coral")
        self.assertEqual(record["device"], "coral")
        self.assertNotEqual(record["rooted_boot_sha256"], record["nonksu_boot_sha256"])

    def test_rejects_crc_changes_and_missing_nonroot_exports(self):
        for content in ("0x000000ff module_layout vmlinux EXPORT_SYMBOL\n",
                        "0x00000001 module_layout vmlinux EXPORT_SYMBOL\n"):
            with self.subTest(content=content):
                (self.object / "Module.symvers").write_text(content)
                with self.assertRaisesRegex(ValueError, "module ABI differs"):
                    check(self.rooted, self.nonksu, "coral")

    def test_rejects_a_different_kernel_release(self):
        (self.object / "include/config/kernel.release").write_text("4.14.399-other\n")
        with self.assertRaisesRegex(ValueError, "release/vermagic"):
            check(self.rooted, self.nonksu, "coral")

    def test_rejects_root_configuration_and_root_symbols(self):
        config = self.object / ".config"
        saved = config.read_text()
        config.write_text(saved + "CONFIG_KSU=y\n")
        with self.assertRaisesRegex(ValueError, "Root integration"):
            check(self.rooted, self.nonksu, "coral")
        config.write_text(saved)
        (self.object / "System.map").write_text("00008000 T ksu_handle_execveat\n")
        with self.assertRaisesRegex(ValueError, "Root symbols"):
            check(self.rooted, self.nonksu, "coral")

    def test_rejects_unversioned_modules(self):
        (self.object / ".config").write_text("# CONFIG_MODVERSIONS is not set\n")
        with self.assertRaisesRegex(ValueError, "Module version checks"):
            check(self.rooted, self.nonksu, "coral")

    def test_rejects_a_stale_kernel_inside_boot(self):
        (self.nonksu / "boot.img").write_bytes(image(b"OLD-KERNEL"))
        with self.assertRaisesRegex(ValueError, "newly built non-KSU kernel"):
            check(self.rooted, self.nonksu, "coral")

    def test_rejects_the_other_device_or_wrong_boot_metadata(self):
        with self.assertRaisesRegex(ValueError, "do not identify flame"):
            check(self.rooted, self.nonksu, "flame")
        (self.nonksu / "boot.img").write_bytes(image(b"NONKSU-KERNEL", os_version=124))
        with self.assertRaisesRegex(ValueError, "Boot metadata differs"):
            check(self.rooted, self.nonksu, "coral")

    def test_rejects_truncated_or_wrong_format_images(self):
        for content in (b"ANDROID!", image(b"NONKSU-KERNEL", version=3)):
            with self.subTest(content=content[:48]):
                (self.nonksu / "boot.img").write_bytes(content)
                with self.assertRaises(ValueError):
                    check(self.rooted, self.nonksu, "coral")

    def test_accepts_flame_only_with_flame_baseline_properties(self):
        (self.rooted / "vendor/build.prop").write_text("ro.product.vendor.device=flame\n")
        self.assertEqual(check(self.rooted, self.nonksu, "flame")["device"], "flame")


if __name__ == "__main__":
    unittest.main()
