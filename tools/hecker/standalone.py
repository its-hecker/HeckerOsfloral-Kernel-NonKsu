#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile the Floral kernel directly, optionally replacing a supplied boot's kernel."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

from actions_boot import compiler_identity, download_bundle, repack
from package_boot import ROOT_SYMBOL, boot, digest, symbols
from verify_nonksu import check as check_source

# Clang 22/r596125 is identified in the published 2026-10-26 HeckerOS kernel.
# build.config.common still describes Google's older standalone build environment.
CLANG_VERSION = "clang-r596125"
CLANG_REPO = "https://android.googlesource.com/platform/prebuilts/clang/host/linux-x86"
CLANG_REVISION = "29182889399818f97baa65ea238c80e8117981eb"
MKBOOTIMG_REPO = "https://android.googlesource.com/platform/system/tools/mkbootimg"
MKBOOTIMG_REVISION = "4a27dd13adf9846e12d585d6fe99a375ebb58396"
AVB_REPO = "https://android.googlesource.com/platform/external/avb"
AVB_REVISION = "ba2dec4b035b0a3b61c5f8f8a74d86bcd450b1ee"


def command(argv, **kwargs):
    subprocess.run([str(a) for a in argv], check=True, **kwargs)


def output(argv, **kwargs):
    return subprocess.check_output([str(a) for a in argv], text=True, **kwargs).strip()


def fetch_tree(url, revision, target, sparse=None):
    if target.exists():
        raise ValueError("Use a fresh tools directory: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    command(["git", "init", target])
    command(["git", "-C", target, "remote", "add", "origin", url])
    if sparse:
        command(["git", "-C", target, "sparse-checkout", "init", "--cone"])
        command(["git", "-C", target, "sparse-checkout", "set", sparse])
    command(["git", "-C", target, "-c", "protocol.version=2", "fetch", "--depth=1", "--filter=blob:none", "origin", revision])
    command(["git", "-C", target, "checkout", "--detach", "FETCH_HEAD"])
    if output(["git", "-C", target, "rev-parse", "HEAD"]) != revision:
        raise ValueError("Unexpected build-tool revision")


def check_compiled(obj):
    config = (obj / ".config").read_text()
    if re.search(r"(?m)^CONFIG_KSU(?:_[A-Z0-9_]+)?=", config):
        raise ValueError("Root configuration enabled in the compiled kernel")
    for setting in ("CONFIG_MODULES=y", "CONFIG_MODVERSIONS=y", "CONFIG_LTO_CLANG=y", "CONFIG_THINLTO=y", "CONFIG_CFI_CLANG=y"):
        if setting not in config.splitlines():
            raise ValueError("Required Floral setting changed: " + setting)
    exported = symbols(obj / "Module.symvers")
    names = [line.split()[-1] for line in (obj / "System.map").read_text().splitlines() if line.split()]
    if not names or any(ROOT_SYMBOL.search(name) for name in [*exported, *names]):
        raise ValueError("Empty kernel map or root symbols in compiled outputs")
    if not (obj / "arch/arm64/boot/Image.lz4").stat().st_size:
        raise ValueError("The ARM64 kernel image is empty")


def build(source, tools, obj, destination, jobs, device, boot_url="", boot_sha256=""):
    source, tools, obj, destination = (p.resolve() for p in (source, tools, obj, destination))
    if bool(boot_url) != bool(boot_sha256):
        raise ValueError("Supply both the base boot image URL and its SHA-256, or leave both empty")
    if boot_url and not re.fullmatch(r"[0-9a-fA-F]{64}", boot_sha256):
        raise ValueError("The base boot image needs a SHA-256 with 64 hex characters")
    if any(p.exists() for p in (tools, obj, destination)):
        raise ValueError("Use fresh tools, output and artifact directories")
    check_source(source)
    fetch_tree(CLANG_REPO, CLANG_REVISION, tools / "clang", CLANG_VERSION)
    clang = tools / "clang" / CLANG_VERSION
    # The first hosted runner was lost during full-program LTO at vmlinux.o. Keep LTO
    # and CFI, use the tree's supported ThinLTO mode, and bound linker workers.
    linker = tools / "ld.lld-limited"
    linker.write_text("#!/bin/sh\nexec " + shlex.quote(str(clang / "bin/ld.lld")) +
                      ' --threads=2 --thinlto-jobs=2 "$@"\n')
    linker.chmod(0o755)
    env = dict(os.environ, PATH=str(clang / "bin") + ":" + os.environ["PATH"],
               LD_LIBRARY_PATH=str(clang / "lib64") + ":" + os.environ.get("LD_LIBRARY_PATH", ""),
               KBUILD_BUILD_USER="HeckerOS", KBUILD_BUILD_HOST="GitHub-Actions")
    env.pop("LOCALVERSION", None)
    args = ["make", "--no-print-directory", "-C", source, "O=" + str(obj), "ARCH=arm64",
            "LLVM=1", "LLVM_IAS=1", "CC=clang", "LD=" + str(linker), "HOSTLDFLAGS=-fuse-ld=lld", "LZ4=lz4"]
    print("Standalone compiler:", compiler_identity(clang), flush=True)
    command([*args, "floral_defconfig"], env=env)
    command([source / "scripts/config", "--file", obj / ".config", "--enable", "THINLTO"])
    command([*args, "olddefconfig"], env=env)
    command([*args, "-j" + str(jobs), "Image.lz4", "modules"], env=env)
    check_compiled(obj)
    destination.mkdir(parents=True)
    for relative, name in (("arch/arm64/boot/Image.lz4", "Image.lz4"), (".config", "floral.config"),
                           ("Module.symvers", "Module.symvers"), ("System.map", "System.map"),
                           ("include/config/kernel.release", "kernel.release")):
        shutil.copyfile(obj / relative, destination / name)
    with tarfile.open(destination / "modules.tar.gz", "w:gz") as archive:
        for module in sorted(obj.rglob("*.ko")):
            archive.add(module, arcname=str(module.relative_to(obj)))
    record = {"schema": 1, "variant": "nonksu", "kernel_revision": output(["git", "-C", source, "rev-parse", "HEAD"]),
              "defconfig": "floral_defconfig", "compiler": compiler_identity(clang),
              "compiler_repository": CLANG_REPO, "compiler_revision": CLANG_REVISION,
              "compiler_directory": CLANG_VERSION, "compiler_sha256": digest(clang / "bin/clang"),
              "lto": "ThinLTO", "linker_flags": ["--threads=2", "--thinlto-jobs=2"],
              "kernel_release": (destination / "kernel.release").read_text().strip(),
              "kernel_sha256": digest(destination / "Image.lz4"),
              "validation": "ARM64 compilation and root-absence checks; real-device tests pending"}
    if boot_url:
        fetch_tree(MKBOOTIMG_REPO, MKBOOTIMG_REVISION, tools / "mkbootimg")
        fetch_tree(AVB_REPO, AVB_REVISION, tools / "avb")
        with tempfile.TemporaryDirectory(prefix="hecker-base-boot-") as temporary:
            root = Path(temporary)
            (root / "baseline").mkdir()
            base = root / "baseline/boot.img"
            download_bundle(boot_url, boot_sha256, base)
            boot(base)
            (root / "mkbootimg").symlink_to(tools / "mkbootimg", target_is_directory=True)
            (root / "avbtool.py").symlink_to(tools / "avb/avbtool.py")
            image = destination / "boot.img"
            repack(root, destination / "Image.lz4", image, root)
            before, after = boot(base), boot(image)
            if after["kernel"] != (destination / "Image.lz4").read_bytes():
                raise ValueError("Repacked boot contains the wrong kernel")
            for key in ("layout", "name", "cmdline", "dtb_sha256", "dtb_address", "ramdisk_sha256"):
                if before[key] != after[key]:
                    raise ValueError("Repacked boot metadata changed: " + key)
            record.update(device=device, base_boot_sha256=boot_sha256.lower(), boot_sha256=digest(image),
                          mkbootimg_revision=MKBOOTIMG_REVISION, avb_revision=AVB_REVISION,
                          module_abi="Not compared against ROM modules; test on the matching device and ROM before release",
                          compatibility="Provided base boot's device/release, unlocked bootloader; device testing required")
    (destination / "build.json").write_text(json.dumps(record, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text("".join(digest(p) + "  " + p.name + "\n"
        for p in sorted(destination.iterdir()) if p.name != "SHA256SUMS"))
    print("Standalone build artifacts:", destination, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--device", choices=("coral", "flame"), default="coral")
    parser.add_argument("--boot-url", default="")
    parser.add_argument("--boot-sha256", default="")
    args = parser.parse_args()
    try:
        build(args.source_root, args.tools, args.out, args.destination, args.jobs, args.device, args.boot_url, args.boot_sha256)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit("Standalone kernel build failed: " + str(error))
