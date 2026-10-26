#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Export a matching ROM baseline and build its optional non-KSU boot on Actions."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

from package_boot import baseline, boot, check, digest, package
from verify_nonksu import SOURCE, check as check_source

MAX_ARCHIVE = 4 * 1024**3
MAX_EXPANDED = 10 * 1024**3
ROOT_CONFIG = re.compile(r"^(?:# )?CONFIG_KSU(?:_[A-Z0-9_]+)?(?:=| is not set)")


def run(args, **kwargs):
    return subprocess.check_output([str(a) for a in args], text=True, **kwargs).strip()


def git(root, *args):
    return run(["git", "-C", root, *args])


def compiler_identity(clang):
    env = dict(os.environ, LD_LIBRARY_PATH=str(clang / "lib64") + ":" + os.environ.get("LD_LIBRARY_PATH", ""))
    cc = run([clang / "bin/clang", "-v"], stderr=subprocess.STDOUT, env=env)
    cc = "\n".join(line.rstrip() for line in cc.splitlines() if " version " in line)
    ld = run([clang / "bin/ld.lld", "-v"], env=env).splitlines()[0]
    ld = re.sub(r"\(compatible with [^)]*\)", "", ld).rstrip()
    return cc + ", " + ld


def choose_clang(android, obj, explicit=None):
    text = (obj / "include/generated/compile.h").read_text()
    match = re.search(r'^#define LINUX_COMPILER "(.*)"$', text, re.M)
    if not match or "clang version" not in match[1]:
        raise ValueError("The baseline needs its generated compile.h and a Clang-built kernel")
    candidates = [explicit.resolve()] if explicit else sorted({p.parent.parent.resolve() for p in
        (android / "prebuilts/clang/host/linux-x86").glob("*/bin/clang")})
    for candidate in candidates:
        try:
            if compiler_identity(candidate) == match[1]:
                return candidate, match[1]
        except (OSError, subprocess.CalledProcessError):
            if explicit:
                raise
    raise ValueError("No exact baseline compiler/linker match; pass --clang-dir from the ROM build")


def copy_tree(source, target):
    shutil.copytree(source, target, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    # Preserve internal links without duplicating large Clang binaries. Relocate any
    # absolute internal links and reject dependencies outside the selected toolchain.
    for path in target.rglob("*"):
        if path.is_symlink():
            original = source / path.relative_to(target)
            resolved = original.resolve(strict=True)
            if not resolved.is_relative_to(source.resolve()):
                raise ValueError("Tool link escapes its exported directory: " + str(original))
            path.unlink()
            path.symlink_to(os.path.relpath(target / resolved.relative_to(source.resolve()), path.parent))


def export_bundle(android, product, device, output, clang=None):
    android, product, output = android.resolve(), product.resolve(), output.resolve()
    _, obj, _ = baseline(product, device)
    if boot(product / "boot.img")["kernel"] != (product / "kernel").read_bytes():
        raise ValueError("The baseline boot.img does not contain the ROM build's installed kernel")
    kernel = android / "kernel/google/msm-4.14"
    if git(kernel, "rev-parse", "HEAD") != SOURCE or git(kernel, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Export from the unchanged KSU kernel pinned by the ROM: " + SOURCE)
    clang, identity = choose_clang(android, obj, clang)
    tools = android / "system/tools/mkbootimg"
    avb = android / "external/avb/avbtool.py"
    for path in (tools / "mkbootimg.py", tools / "unpack_bootimg.py", avb):
        if not path.is_file():
            raise ValueError("Missing Android packaging tool: " + str(path))
    if output.exists():
        raise ValueError("Refusing to overwrite an existing baseline archive")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hecker-baseline-") as temporary:
        root = Path(temporary)
        base = root / "baseline"
        for relative in ("boot.img", "obj/KERNEL_OBJ/.config", "obj/KERNEL_OBJ/Module.symvers",
                         "obj/KERNEL_OBJ/include/config/kernel.release", "obj/KERNEL_OBJ/include/generated/compile.h",
                         "system/build.prop", "system/etc/build.prop", "product/build.prop",
                         "product/etc/build.prop", "vendor/build.prop", "vendor/etc/build.prop"):
            source = product / relative
            if source.is_file():
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        copy_tree(clang, root / "toolchain")
        copy_tree(tools, root / "mkbootimg")
        shutil.copyfile(avb, root / "avbtool.py")
        metadata = {"schema": 1, "device": device, "kernel_base_revision": SOURCE,
                    "compiler": identity, "clang_sha256": digest(root / "toolchain/bin/clang"),
                    "linker_sha256": digest(root / "toolchain/bin/ld.lld"),
                    "mkbootimg_revision": git(tools, "rev-parse", "HEAD"),
                    "avb_revision": git(avb.parent, "rev-parse", "HEAD")}
        metadata["files"] = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}
        (root / "baseline.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # A signed footer requires the release key; this public hosted path never exports keys.
        avb_settings(root / "avbtool.py", base / "boot.img")
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(root.iterdir()):
                archive.add(path, arcname=path.name)
    checksum = digest(output)
    output.with_name(output.name + ".sha256").write_text(checksum + "  " + output.name + "\n")
    print("Baseline archive:", output)
    print("SHA-256:", checksum)


def download_bundle(url, sha256, destination):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        raise ValueError("Supply the exported archive's SHA-256 (64 hex characters)")
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Use an HTTPS download URL for the baseline archive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as target:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("Baseline download redirected away from HTTPS")
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise ValueError("Baseline archive exceeds 4 GiB")
            target.write(chunk)
    if digest(destination) != sha256.lower():
        destination.unlink()
        raise ValueError("Baseline archive SHA-256 mismatch")


def extract_bundle(archive, destination, device):
    if destination.exists():
        raise ValueError("Use a new, empty baseline directory")
    destination.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if sum(m.size for m in members) > MAX_EXPANDED or len(members) > 100000:
            raise ValueError("Baseline archive exceeds extraction limits")
        if len({m.name for m in members}) != len(members):
            raise ValueError("Duplicate archive members")
        if any(Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members):
            raise ValueError("Absolute or traversing archive path")
        # Python's data filter rejects traversal, escaping links and special files.
        source.extractall(destination, members=members, filter="data")
    return validate_bundle(destination, device)


def validate_bundle(root, device):
    metadata = json.loads((root / "baseline.json").read_text())
    if metadata.get("schema") != 1 or metadata.get("device") != device or metadata.get("kernel_base_revision") != SOURCE:
        raise ValueError("Wrong device, schema or kernel revision in the baseline archive")
    files = metadata["files"]
    actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p != root / "baseline.json"}
    if actual != set(files):
        raise ValueError("Baseline file inventory differs from its manifest")
    for relative, expected in files.items():
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or digest(path) != expected:
            raise ValueError("Baseline file hash mismatch: " + relative)
    baseline(root / "baseline", device)
    if compiler_identity(root / "toolchain") != metadata["compiler"]:
        raise ValueError("The exported compiler/linker identity changed")
    if digest(root / "toolchain/bin/clang") != metadata["clang_sha256"] or digest(root / "toolchain/bin/ld.lld") != metadata["linker_sha256"]:
        raise ValueError("Compiler/linker binary hash mismatch")
    return metadata


def load_avb(path):
    spec = importlib.util.spec_from_file_location("hecker_avbtool", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        # Loading the exporter-supplied tool must not add unrecorded cache files
        # to the baseline after its inventory has been hashed.
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def avb_settings(tool, image):
    data = image.read_bytes()
    boot(image)
    if data[-64:-60] != b"AVBf":
        # Do not silently discard a footer hidden by appended padding or a legacy signature.
        fields = struct.unpack_from("<10I", data, 8)
        align = lambda n: (n + 4095) // 4096 * 4096
        end = (4096 + sum(align(fields[i]) for i in (0, 2, 4)) +
               align(struct.unpack_from("<I", data, 1632)[0]) + struct.unpack_from("<I", data, 1648)[0])
        if any(data[end:]):
            raise ValueError("Unrecognized boot trailer; use the native Android build")
        return None
    avb = load_avb(tool)
    handler = avb.ImageHandler(str(image), read_only=True)
    try:
        footer, header, descriptors, _ = avb.Avb()._parse_image(handler)
    finally:
        handler._image.close()
    if header.algorithm_type != 0:
        raise ValueError("Signed AVB boot requires the release key; use the native Android bootimage helper")
    hashes = [d for d in descriptors if isinstance(d, avb.AvbHashDescriptor)]
    props = [d for d in descriptors if isinstance(d, avb.AvbPropertyDescriptor)]
    if (not footer or len(hashes) != 1 or hashes[0].partition_name != "boot" or
            len(hashes) + len(props) != len(descriptors) or header.flags or
            hashes[0].flags & ~1 or not hashes[0].digest):
        raise ValueError("Unsupported AVB boot metadata; use the native Android bootimage helper")
    desc = hashes[0]
    if hashlib.new(desc.hash_algorithm, desc.salt + data[:desc.image_size]).digest() != desc.digest:
        raise ValueError("Baseline AVB boot hash does not verify")
    return {"partition_size": len(data), "hash_algorithm": desc.hash_algorithm,
            "salt": desc.salt.hex(), "do_not_use_ab": bool(desc.flags & 1),
            "rollback_index": header.rollback_index,
            "rollback_index_location": header.rollback_index_location,
            "properties": [(d.key, d.value) for d in props]}


def repack(bundle, kernel, output, temporary):
    original = bundle / "baseline/boot.img"
    settings = avb_settings(bundle / "avbtool.py", original)
    unpacked = temporary / "unpacked"
    encoded = subprocess.check_output([sys.executable, str(bundle / "mkbootimg/unpack_bootimg.py"),
        "--boot_img", str(original), "--out", str(unpacked), "--format=mkbootimg", "--null"])
    args = encoded.rstrip(b"\n\0").decode().split("\0")
    args[args.index("--kernel") + 1] = str(kernel)
    subprocess.run([sys.executable, str(bundle / "mkbootimg/mkbootimg.py"), *args, "--output", str(output)], check=True)
    if settings:
        argv = [sys.executable, str(bundle / "avbtool.py"), "add_hash_footer", "--image", str(output),
                "--partition_name", "boot", "--algorithm", "NONE"]
        for key in ("partition_size", "hash_algorithm", "salt", "rollback_index", "rollback_index_location"):
            argv += ["--" + key, str(settings[key])]
        if settings["do_not_use_ab"]:
            argv.append("--do_not_use_ab")
        for index, (key, value) in enumerate(settings["properties"]):
            path = temporary / ("avb-property-" + str(index))
            path.write_bytes(value)
            argv += ["--prop_from_file", key + ":" + str(path)]
        subprocess.run(argv, check=True)
        subprocess.run([sys.executable, str(bundle / "avbtool.py"), "verify_image", "--image", str(output)], check=True)
        if avb_settings(bundle / "avbtool.py", output) != settings:
            raise ValueError("Regenerated AVB metadata differs from the baseline")
    if boot(original)["ramdisk_sha256"] != boot(output)["ramdisk_sha256"]:
        raise ValueError("Recovery ramdisk changed during repacking")
    if output.stat().st_size > 0x04000000:
        raise ValueError("Boot image exceeds the Floral boot partition")


def build(bundle, device, source, output, destination, jobs):
    bundle, source, output, destination = (p.resolve() for p in (bundle, source, output, destination))
    metadata = validate_bundle(bundle, device)
    check_source(source)
    if output.exists() or destination.exists():
        raise ValueError("Use new output and destination directories; stale kernels must not be reused")
    obj = output / "obj/KERNEL_OBJ"
    obj.mkdir(parents=True)
    config = (bundle / "baseline/obj/KERNEL_OBJ/.config").read_text()
    (obj / ".config").write_text("".join(line for line in config.splitlines(keepends=True) if not ROOT_CONFIG.match(line)))
    clang = bundle / "toolchain"
    env = dict(os.environ, PATH=str(clang / "bin") + ":" + os.environ["PATH"],
               LD_LIBRARY_PATH=str(clang / "lib64") + ":" + os.environ.get("LD_LIBRARY_PATH", ""),
               KBUILD_BUILD_USER="HeckerOS", KBUILD_BUILD_HOST="GitHub-Actions")
    env.pop("LOCALVERSION", None)
    args = ["make", "--no-print-directory", "-C", str(source), "O=" + str(obj), "ARCH=arm64", "LLVM=1", "LLVM_IAS=1",
            "CC=clang", "LD=ld.lld", "HOSTLDFLAGS=-fuse-ld=lld", "LZ4=lz4"]
    subprocess.run([*args, "olddefconfig"], env=env, check=True)
    expected_release = (bundle / "baseline/obj/KERNEL_OBJ/include/config/kernel.release").read_text().strip()
    actual_release = run([*args, "-s", "kernelrelease"], env=env).splitlines()[-1]
    if actual_release != expected_release:
        args.append("LOCALVERSION=")
        if run([*args, "-s", "kernelrelease"], env=env).splitlines()[-1] != expected_release:
            raise ValueError("The kernel release differs from the ROM baseline; use the native Android helper")
    subprocess.run([*args, "-j" + str(jobs), "Image.lz4", "modules"], env=env, check=True)
    shutil.copyfile(obj / "arch/arm64/boot/Image.lz4", output / "kernel")
    with tempfile.TemporaryDirectory(prefix="hecker-repack-") as temporary:
        repack(bundle, output / "kernel", output / "boot.img", Path(temporary))
    check(bundle / "baseline", output, device)
    package(bundle / "baseline", output, device, source, destination)
    (destination / "actions-build.json").write_text(json.dumps({
        "schema": 1, "device": device, "kernel_revision": git(source, "rev-parse", "HEAD"),
        "baseline_manifest_sha256": digest(bundle / "baseline.json"), "compiler": metadata["compiler"],
        "clang_sha256": metadata["clang_sha256"], "linker_sha256": metadata["linker_sha256"],
        "mkbootimg_revision": metadata["mkbootimg_revision"], "avb_revision": metadata["avb_revision"],
        "make_arguments": args[5:], "bootloader": "unlocked; matching device and ROM release only",
        "validation": "compiled and packaged; real-device boot, recovery and module tests required"
    }, indent=2) + "\n")
    (destination / "SHA256SUMS").write_text("".join(digest(p) + "  " + p.name + "\n"
        for p in sorted(destination.iterdir()) if p.is_file() and p.name != "SHA256SUMS"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="Run on the completed normal HeckerOS ROM build machine")
    export.add_argument("--android-root", type=Path, default=Path.cwd())
    export.add_argument("--product-out", type=Path, required=True)
    export.add_argument("--clang-dir", type=Path)
    export.add_argument("--output", type=Path, required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--sha256", required=True)
    fetch.add_argument("--archive", type=Path, required=True)
    fetch.add_argument("--destination", type=Path, required=True)
    compile_boot = sub.add_parser("build")
    compile_boot.add_argument("--bundle", type=Path, required=True)
    compile_boot.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    compile_boot.add_argument("--out", type=Path, required=True)
    compile_boot.add_argument("--destination", type=Path, required=True)
    compile_boot.add_argument("--jobs", type=int, default=2, choices=range(1, 9))
    for command in (export, fetch, compile_boot):
        command.add_argument("--device", choices=("coral", "flame"), required=True)
    args = parser.parse_args()
    if args.command == "export":
        export_bundle(args.android_root, args.product_out, args.device, args.output, args.clang_dir)
    elif args.command == "fetch":
        download_bundle(args.url, args.sha256, args.archive)
        extract_bundle(args.archive, args.destination, args.device)
        args.archive.unlink()
    else:
        build(args.bundle, args.device, args.source_root, args.out, args.destination, args.jobs)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit("Boot build refused: " + str(error))
