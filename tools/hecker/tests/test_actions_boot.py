# SPDX-License-Identifier: Apache-2.0
"""Exercise real AOSP repacking and untrusted baseline/compatibility boundaries."""

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from actions_boot import (ROOT_CONFIG, avb_settings, copy_tree, download_bundle,
                          export_bundle, extract_bundle, repack, validate_bundle)
from package_boot import boot, digest
from test_package_boot import image
from verify_nonksu import SOURCE

MKBOOTIMG = os.environ.get("TEST_MKBOOTIMG")
AVB_TOOL = os.environ.get("TEST_AVB_TOOL")


class BundleGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_refuses_wrong_device_and_kernel_before_running_tools(self):
        for metadata in ({"schema": 1, "device": "flame", "kernel_base_revision": SOURCE},
                         {"schema": 1, "device": "coral", "kernel_base_revision": "0" * 40}):
            (self.root / "baseline.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "Wrong device"):
                validate_bundle(self.root, "coral")

    def test_rejects_added_or_changed_files(self):
        path = self.root / "payload"
        path.write_text("expected")
        metadata = {"schema": 1, "device": "coral", "kernel_base_revision": SOURCE,
                    "files": {"payload": digest(path)}}
        (self.root / "baseline.json").write_text(json.dumps(metadata))
        path.write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_bundle(self.root, "coral")
        (self.root / "extra").touch()
        with self.assertRaisesRegex(ValueError, "inventory"):
            validate_bundle(self.root, "coral")

    def test_safe_extraction_rejects_traversal_links_and_duplicate_members(self):
        for index, (name, link) in enumerate((("../escaped", None), ("link", "../../escaped"),
                                              ("/absolute", None), ("duplicate", None))):
            archive = self.root / (str(index) + ".tar.gz")
            with tarfile.open(archive, "w:gz") as output:
                member = tarfile.TarInfo(name)
                if link:
                    member.type, member.linkname = tarfile.SYMTYPE, link
                    output.addfile(member)
                else:
                    member.size = 1
                    output.addfile(member, io.BytesIO(b"x"))
                    if name == "duplicate":
                        output.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises((ValueError, tarfile.FilterError)):
                extract_bundle(archive, self.root / ("out" + str(index)), "coral")
        self.assertFalse((self.root / "escaped").exists())

    def test_rejects_bad_sha_and_http_without_network(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            download_bundle("https://example.com/baseline", "bad", self.root / "download")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            download_bundle("http://example.com/baseline", "0" * 64, self.root / "download")

    def test_download_checks_sha_before_extraction(self):
        response = io.BytesIO(b"wrong baseline")
        response.url = "https://example.com/baseline"
        with patch("actions_boot.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                download_bundle(response.url, "0" * 64, self.root / "download")
        self.assertFalse((self.root / "download").exists())

    def test_copy_preserves_internal_compiler_links_and_rejects_external_links(self):
        source = self.root / "source"
        source.mkdir()
        (source / "clang").write_text("compiler")
        (source / "clang++").symlink_to(source / "clang")
        target = self.root / "target"
        copy_tree(source, target)
        self.assertTrue((target / "clang++").is_symlink())
        self.assertEqual((target / "clang++").resolve(), target / "clang")
        (source / "external").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "escapes"):
            copy_tree(source, self.root / "bad-target")

    def test_removes_root_config_only(self):
        config = "CONFIG_KSU=y\nCONFIG_KSU_SUSFS=y\n# CONFIG_KSU_DEBUG is not set\nCONFIG_KPROBES=y\nCONFIG_WIREGUARD=y\nCONFIG_LOCALVERSION=\"CNB\"\n"
        result = "".join(line for line in config.splitlines(keepends=True) if not ROOT_CONFIG.match(line))
        self.assertEqual(result, 'CONFIG_KPROBES=y\nCONFIG_WIREGUARD=y\nCONFIG_LOCALVERSION="CNB"\n')

    def test_refuses_unknown_boot_trailer(self):
        path = self.root / "boot.img"
        path.write_bytes(image(b"kernel") + b"unknown-signature")
        with self.assertRaisesRegex(ValueError, "trailer"):
            avb_settings(self.root / "unused-avbtool.py", path)

    def test_exports_and_relocates_a_complete_baseline_without_signing_keys(self):
        android = self.root / "android"
        product = android / "out/target/product/coral"
        obj = product / "obj/KERNEL_OBJ"
        (obj / "include/config").mkdir(parents=True)
        (obj / "include/generated").mkdir()
        (obj / ".config").write_text("CONFIG_KSU=y\nCONFIG_MODVERSIONS=y\n")
        (obj / "Module.symvers").write_text("0x00000001 module_layout vmlinux EXPORT_SYMBOL\n")
        (obj / "include/config/kernel.release").write_text("4.14.357+\n")
        (obj / "include/generated/compile.h").write_text('#define LINUX_COMPILER "Android clang version 20.0.0, LLD 20.0.0"\n')
        (product / "boot.img").write_bytes(image(b"ROOT-KERNEL"))
        (product / "kernel").write_bytes(b"ROOT-KERNEL")
        (product / "vendor").mkdir()
        (product / "vendor/build.prop").write_text("ro.product.vendor.device=coral\n")
        (android / "release-private-key.pem").write_text("must never be exported")
        clang = android / "prebuilts/clang/host/linux-x86/clang-test"
        (clang / "bin").mkdir(parents=True)
        for name, identity in (("clang", "Android clang version 20.0.0"), ("ld.lld", "LLD 20.0.0")):
            path = clang / "bin" / name
            path.write_text("#!/bin/sh\necho '" + identity + "'\n")
            path.chmod(0o755)
        (clang / "bin/clang++").symlink_to("clang")
        tools = android / "system/tools/mkbootimg"
        tools.mkdir(parents=True)
        (tools / "mkbootimg.py").touch()
        (tools / "unpack_bootimg.py").touch()
        (android / "external/avb").mkdir(parents=True)
        (android / "external/avb/avbtool.py").touch()
        archive = self.root / "baseline.tar.gz"
        with patch("actions_boot.git", side_effect=lambda root, *args: "" if args[0] == "status" else SOURCE):
            export_bundle(android, product, "coral", archive)
        shutil.rmtree(android)
        relocated = self.root / "relocated"
        record = extract_bundle(archive, relocated, "coral")
        self.assertEqual(record["device"], "coral")
        self.assertTrue((relocated / "toolchain/bin/clang++").is_symlink())
        self.assertFalse(list(relocated.rglob("*private-key*")))
        self.assertEqual((relocated / "baseline/boot.img").read_bytes(), image(b"ROOT-KERNEL"))


@unittest.skipUnless(MKBOOTIMG and AVB_TOOL, "Set TEST_MKBOOTIMG and TEST_AVB_TOOL for real AOSP tests")
class RealAospRepacking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundle"
        (self.bundle / "baseline").mkdir(parents=True)
        shutil.copytree(MKBOOTIMG, self.bundle / "mkbootimg", ignore=shutil.ignore_patterns(".git"))
        shutil.copyfile(AVB_TOOL, self.bundle / "avbtool.py")
        for name, content in (("kernel", b"old kernel"), ("ramdisk", b"device recovery and ramdisk"),
                              ("dtb", b"device DTB"), ("second", b"second loader"),
                              ("recovery_dtbo", b"recovery dtbo"), ("replacement", b"new non-KSU kernel")):
            (self.root / name).write_bytes(content)
        self.original = self.bundle / "baseline/boot.img"
        self.invoke(self.bundle / "mkbootimg/mkbootimg.py", "--header_version", "2", "--pagesize", "4096",
                    "--base", "0", "--kernel_offset", "0x8000", "--ramdisk_offset", "0x1000000",
                    "--dtb_offset", "0x1f00000", "--board", "floral", "--os_version", "17.0.0",
                    "--os_patch_level", "2026-10", "--cmdline", 'console=ttyMSM0 with spaces quote="value" ' + "x" * 600,
                    "--kernel", self.root / "kernel", "--ramdisk", self.root / "ramdisk", "--dtb", self.root / "dtb",
                    "--second", self.root / "second", "--recovery_dtbo", self.root / "recovery_dtbo",
                    "--output", self.original)

    def invoke(self, tool, *args):
        return subprocess.check_output([sys.executable, str(tool), *map(str, args)], stderr=subprocess.STDOUT)

    def add_footer(self, signed=False):
        args = ["add_hash_footer", "--image", self.original, "--partition_name", "boot",
                "--partition_size", "1048576", "--salt", "0123456789abcdef", "--rollback_index", "1234",
                "--rollback_index_location", "2", "--prop", "com.android.build.boot.os_version:17",
                "--prop", "com.android.build.boot.security_patch:2026-10-05", "--do_not_use_ab"]
        if signed:
            args += ["--algorithm", "SHA256_RSA2048", "--key", Path(AVB_TOOL).parent / "test/data/testkey_rsa2048.pem"]
        self.invoke(self.bundle / "avbtool.py", *args)

    def assert_repacked(self):
        output = self.root / "boot.img"
        repack(self.bundle, self.root / "replacement", output, self.root)
        base, new = boot(self.original), boot(output)
        self.assertEqual(new["kernel"], (self.root / "replacement").read_bytes())
        for key in ("layout", "cmdline", "name", "dtb_sha256", "dtb_address", "ramdisk_sha256"):
            self.assertEqual(base[key], new[key], key)
        for old_new, path in (("old", self.original), ("new", output)):
            self.invoke(self.bundle / "mkbootimg/unpack_bootimg.py", "--boot_img", path, "--out", self.root / old_new)
        for name in ("ramdisk", "dtb", "second", "recovery_dtbo"):
            self.assertEqual((self.root / "old" / name).read_bytes(), (self.root / "new" / name).read_bytes())
        return output

    def test_preserves_recovery_and_full_header_without_avb(self):
        self.assert_repacked()

    def test_regenerates_and_verifies_avb_footer_with_same_properties_and_policy(self):
        self.add_footer()
        output = self.assert_repacked()
        self.assertEqual(avb_settings(self.bundle / "avbtool.py", self.original), avb_settings(self.bundle / "avbtool.py", output))
        self.assertFalse((self.bundle / "__pycache__").exists())

    def test_refuses_signed_avb_without_exporting_a_private_key(self):
        self.add_footer(signed=True)
        with self.assertRaisesRegex(ValueError, "release key"):
            avb_settings(self.bundle / "avbtool.py", self.original)

    def test_rejects_a_corrupt_baseline_avb_hash(self):
        self.add_footer()
        data = bytearray(self.original.read_bytes())
        data[4096] ^= 1
        self.original.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "does not verify"):
            avb_settings(self.bundle / "avbtool.py", self.original)


if __name__ == "__main__":
    unittest.main()
