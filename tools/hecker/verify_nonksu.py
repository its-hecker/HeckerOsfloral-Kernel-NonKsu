#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Reject root hooks and unintended changes in the kernel's non-KSU variant."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

SOURCE = "48d6881fb8537f62811209927022e467d37e3a98"


def check(root):
    root = root.resolve()
    record = json.loads((root / "NONKSU_SOURCE.json").read_text())
    if record["revision"] != SOURCE or record["variant"] != "nonksu":
        raise ValueError("Unexpected non-KSU source provenance")
    for relative in record["removed"]:
        path = root / relative
        if path.exists() or path.is_symlink():
            raise ValueError("Root integration remains: " + relative)
    for relative, hashes in record["changes"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != hashes["nonksu_sha256"]:
            raise ValueError("Unreviewed non-KSU source change: " + relative)
    pattern = r"CONFIG_KSU|\bksu_[[:alnum:]_]*|\b__ksu_[[:alnum:]_]*|\bsusfs_[[:alnum:]_]*|\bSUSFS_[[:alnum:]_]*|drivers/kernelsu"
    result = subprocess.run(["git", "-C", str(root), "grep", "-I", "-n", "-E", pattern, "--",
                             "arch", "drivers", "fs", "include", "init", "ipc", "kernel", "mm", "net", "security"],
                            capture_output=True, text=True)
    if result.returncode != 1:
        raise ValueError("Root source scan failed:\n" + result.stdout + result.stderr)
    if subprocess.run(["git", "-C", str(root), "cat-file", "-e", SOURCE],
                      capture_output=True).returncode == 0:
        differences = subprocess.check_output(["git", "-C", str(root), "diff", "--name-only", SOURCE],
                                              text=True).splitlines()
        allowed = set(record["changes"]) | set(record["removed"]) | {"NONKSU_SOURCE.json", "README.HeckerOS.md"}
        unexpected = [p for p in differences if p not in allowed and
                      not p.startswith((".github/", "tools/hecker/"))]
        if unexpected:
            raise ValueError("Unrelated kernel modifications: " + ", ".join(unexpected))
        source_config = subprocess.check_output(["git", "-C", str(root), "show",
                                                SOURCE + ":arch/arm64/configs/floral_defconfig"], text=True)
        expected = "".join(line for line in source_config.splitlines(keepends=True)
                           if "CONFIG_KSU" not in line and line.strip() != "# KernelSU")
        if (root / "arch/arm64/configs/floral_defconfig").read_text() != expected:
            raise ValueError("Non-root Floral configuration changed")
        charge = "drivers/power/supply/google/sm8150_bms.c"
        original = subprocess.check_output(["git", "-C", str(root), "show", SOURCE + ":" + charge])
        if (root / charge).read_bytes() != original:
            raise ValueError("The existing GameSpace charging driver changed")
    print("Non-KSU source: root submodule/hooks/SUSFS absent; recorded source hashes, non-root config and charging driver preserved: PASS")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    check(parser.parse_args().source_root)
