# SPDX-License-Identifier: Apache-2.0
"""Standalone builds require no ROM baseline and still reject root in real outputs."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from standalone import build, check_compiled


class StandaloneGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "arch/arm64/boot").mkdir(parents=True)
        (self.root / "arch/arm64/boot/Image.lz4").write_bytes(b"compiled kernel")
        self.config = "CONFIG_MODULES=y\nCONFIG_MODVERSIONS=y\nCONFIG_LTO_CLANG=y\nCONFIG_CFI_CLANG=y\n"
        (self.root / ".config").write_text(self.config)
        (self.root / "Module.symvers").write_text("0x00000001 module_layout vmlinux EXPORT_SYMBOL\n")
        (self.root / "System.map").write_text("00008000 T _text\n")

    def test_checks_compiled_outputs_without_a_rom_output_directory(self):
        check_compiled(self.root)

    def test_rejects_root_in_config_or_map(self):
        (self.root / ".config").write_text(self.config + "CONFIG_KSU=y\n")
        with self.assertRaisesRegex(ValueError, "Root configuration"):
            check_compiled(self.root)
        (self.root / ".config").write_text(self.config)
        (self.root / "System.map").write_text("00008000 T ksu_handle_execveat\n")
        with self.assertRaisesRegex(ValueError, "root symbols"):
            check_compiled(self.root)

    def test_rejects_disabling_cfi_or_lto(self):
        for name in ("CONFIG_LTO_CLANG", "CONFIG_CFI_CLANG"):
            with self.subTest(name=name):
                (self.root / ".config").write_text(self.config.replace(name + "=y", "# " + name + " is not set"))
                with self.assertRaisesRegex(ValueError, "Required Floral setting"):
                    check_compiled(self.root)

    def test_rejects_half_specified_boot_inputs_before_fetching_or_compiling(self):
        for url, sha in (("https://example.com/boot.img", ""), ("", "a" * 64)):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "Supply both"):
                    build(self.root, self.root / "tools", self.root / "out", self.root / "dist", 2, "coral", url, sha)

    def test_rejects_stale_outputs_before_fetching(self):
        with self.assertRaisesRegex(ValueError, "fresh tools"):
            build(self.root, self.root, self.root / "out", self.root / "dist", 2, "coral")


if __name__ == "__main__":
    unittest.main()
