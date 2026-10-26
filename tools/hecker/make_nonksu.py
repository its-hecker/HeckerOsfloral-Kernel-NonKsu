#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Derive the non-KSU variant from the exact kernel pinned by both ROMs."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

SOURCE = "48d6881fb8537f62811209927022e467d37e3a98"
REPOSITORY = "its-hecker/heckeros-floral-kernel"
REMOVED = (".gitmodules", "KernelSU", "drivers/kernelsu", "fs/susfs.c",
           "include/linux/susfs.h", "include/linux/susfs_def.h")
CONFIG = re.compile(r"\bCONFIG_KSU(?:_[A-Z0-9_]+)?\b")
ROOT_CODE = re.compile(rb"\b(?:CONFIG_KSU(?:_[A-Z0-9_]+)?|ksu_\w*|__ksu_\w*|susfs_\w*|SUSFS_\w*)\b")


def tracked(root):
    return subprocess.check_output(["git", "-C", str(root), "ls-tree", "-r", "--name-only", SOURCE],
                                   text=True).splitlines()


def original(root, path):
    return subprocess.check_output(["git", "-C", str(root), "show", SOURCE + ":" + path])


def apply(root):
    root = root.resolve()
    matches = subprocess.check_output(
        ["git", "-C", str(root), "grep", "-l", "CONFIG_KSU", SOURCE, "--", "*.c", "*.h"], text=True)
    sources = [line.split(":", 1)[1] for line in matches.splitlines()
               if line.split(":", 1)[1] not in REMOVED]
    # Use the kernel's own mature preprocessor conditional remover. Only root
    # configuration symbols are resolved; unrelated C preprocessor branches stay.
    with tempfile.TemporaryDirectory(prefix="hecker-unifdef-") as temporary:
        executable = Path(temporary) / "unifdef"
        subprocess.run(["cc", "-O2", str(root / "scripts/unifdef.c"), "-o", str(executable)], check=True)
        changes = {}
        for path in sources:
            before = original(root, path)
            if (root / path).read_bytes() != before:
                raise ValueError("Preserving an unfamiliar source edit: " + path)
            symbols = sorted(set(CONFIG.findall(before.decode("utf-8"))))
            result = subprocess.run([str(executable), *["-U" + symbol for symbol in symbols]],
                                    input=before, capture_output=True)
            if result.returncode not in (0, 1):
                raise ValueError(path + ": " + result.stderr.decode())
            if ROOT_CODE.search(result.stdout):
                raise ValueError("Review an unguarded root hook in " + path)
            changes[path] = result.stdout
        for path, needle in (("drivers/Kconfig", 'source "drivers/kernelsu/Kconfig"\n'),
                             ("drivers/Makefile", '# KernelSU driver\nobj-$(CONFIG_KSU) += kernelsu/\n'),
                             ("fs/Makefile", 'obj-$(CONFIG_KSU_SUSFS) += susfs.o\n')):
            before = original(root, path)
            if (root / path).read_bytes() != before or before.count(needle.encode()) != 1:
                raise ValueError("Review changed root build integration: " + path)
            changes[path] = before.replace(needle.encode(), b"", 1)
        path = "arch/arm64/configs/floral_defconfig"
        before = original(root, path)
        if (root / path).read_bytes() != before:
            raise ValueError("Review changed Floral defconfig")
        changes[path] = b"".join(line for line in before.splitlines(keepends=True)
                                 if not CONFIG.search(line.decode()) and line.strip() != b"# KernelSU")
        records = {}
        for path, after in changes.items():
            before = original(root, path)
            if after.endswith(b"\n\n") and not before.endswith(b"\n\n"):
                after = after.rstrip(b"\n") + b"\n"
            (root / path).write_bytes(after)
            records[path] = {"source_sha256": hashlib.sha256(before).hexdigest(),
                             "nonksu_sha256": hashlib.sha256(after).hexdigest()}
        for path in REMOVED:
            p = root / path
            if p.is_symlink() or p.is_file():
                p.unlink()
            elif p.is_dir():
                # The import deliberately never initializes the root submodule.
                if list(p.iterdir()):
                    raise ValueError("Refusing to remove a populated submodule: " + path)
                p.rmdir()
        provenance = {"repository": REPOSITORY, "revision": SOURCE,
                      "variant": "nonksu", "devices": ["coral", "flame"],
                      "removed": list(REMOVED), "changes": records,
                      "preserved": "All non-root kernel code/configuration, including the GameSpace charging inhibitor",
                      "validation": "Source checks only; full target kernel/boot builds and phone validation required"}
        (root / "NONKSU_SOURCE.json").write_text(json.dumps(provenance, indent=2) + "\n")
        print(f"Removed root integration from {len(changes)} source/build/config files and {len(REMOVED)} paths.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    apply(parser.parse_args().source_root)
