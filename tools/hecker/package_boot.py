#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Publish a per-device optional boot only after checking the installed ROM ABI."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess

ROOT_SYMBOL = re.compile(r"^(?:__)?(?:ksu_|susfs_)|^ksud(?:_|$)|^kernelsu", re.I)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def symbols(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) < 2 or not re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
            raise ValueError("Malformed Module.symvers: " + str(path))
        crc, name = fields[:2]
        if name in result and result[name] != crc.lower():
            raise ValueError("Conflicting exported symbol: " + name)
        result[name] = crc.lower()
    if not result:
        raise ValueError("Empty Module.symvers: " + str(path))
    return result


def boot(path):
    path = Path(path)
    data = path.read_bytes()
    if len(data) < 1660 or data[:8] != b"ANDROID!":
        raise ValueError("Not an Android boot image: " + str(path))
    fields = struct.unpack_from("<10I", data, 8)
    kernel_size, _, ramdisk_size = fields[:3]
    page, version = fields[7:9]
    if version != 2 or page != 4096:
        raise ValueError("Expected the Floral v2/4096 boot format")
    ramdisk_start = page + ((kernel_size + page - 1) // page) * page
    if not kernel_size or not ramdisk_size or ramdisk_start + ramdisk_size > len(data):
        raise ValueError("Truncated kernel or recovery ramdisk")
    recovery_size = struct.unpack_from("<I", data, 1632)[0]
    dtb_size = struct.unpack_from("<I", data, 1648)[0]
    align = lambda value: ((value + page - 1) // page) * page
    dtb_start = ramdisk_start + align(ramdisk_size) + align(fields[4]) + align(recovery_size)
    if not dtb_size or dtb_start + dtb_size > len(data):
        raise ValueError("Missing or truncated Floral DTB")
    return {"kernel": data[page:page + kernel_size],
            "dtb_sha256": hashlib.sha256(data[dtb_start:dtb_start + dtb_size]).hexdigest(),
            "dtb_address": struct.unpack_from("<Q", data, 1652)[0],
            "ramdisk_sha256": hashlib.sha256(data[ramdisk_start:ramdisk_start + ramdisk_size]).hexdigest(),
            "layout": list(fields[i] for i in (1, 3, 5, 6, 7, 8, 9)),
            "name": data[48:64].hex(), "cmdline": (data[64:576] + data[608:1632]).hex()}


def properties(output):
    result = {}
    for relative in ("system/build.prop", "system/etc/build.prop", "product/build.prop",
                     "product/etc/build.prop", "vendor/build.prop", "vendor/etc/build.prop"):
        path = output / relative
        if path.is_file():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    result[key.strip()] = value.strip()
    return result


def baseline(rooted, device):
    rooted = Path(rooted).resolve()
    props = properties(rooted)
    device_keys = ("ro.product.device", "ro.product.vendor.device", "ro.product.product.device", "ro.product.system.device")
    if device not in ("coral", "flame") or device not in {props.get(k) for k in device_keys}:
        raise ValueError("The baseline ROM properties do not identify " + device)
    obj = rooted / "obj/KERNEL_OBJ"
    config = (obj / ".config").read_text()
    if not re.search(r"(?m)^CONFIG_KSU=y$", config):
        raise ValueError("The baseline must be the matching KSU-enabled ROM build")
    if "CONFIG_MODVERSIONS=y" not in config.splitlines():
        raise ValueError("Module version checks are required for a boot-only replacement")
    boot(rooted / "boot.img")
    symbols(obj / "Module.symvers")
    if not (obj / "include/config/kernel.release").read_text().strip():
        raise ValueError("Missing baseline kernel release")
    return props, obj, config


def check(rooted, nonksu, device):
    rooted, nonksu = Path(rooted).resolve(), Path(nonksu).resolve()
    if device not in ("coral", "flame") or rooted == nonksu:
        raise ValueError("Use distinct rooted/non-KSU outputs for Coral or Flame")
    props, base_obj, base_config = baseline(rooted, device)
    next_obj = nonksu / "obj/KERNEL_OBJ"
    next_config = (next_obj / ".config").read_text()
    if not re.search(r"(?m)^CONFIG_KSU=y$", base_config):
        raise ValueError("The baseline must be the matching KSU-enabled ROM build")
    if re.search(r"(?m)^CONFIG_KSU(?:_[A-Z0-9_]+)?=", next_config):
        raise ValueError("Root integration is enabled in the non-KSU configuration")
    if not all("CONFIG_MODVERSIONS=y" in s.splitlines() for s in (base_config, next_config)):
        raise ValueError("Module version checks are required for a boot-only replacement")
    releases = [(p / "include/config/kernel.release").read_text().strip() for p in (base_obj, next_obj)]
    if not releases[0] or releases[0] != releases[1]:
        raise ValueError("Kernel release/vermagic differs; existing ROM modules may not load")
    base_symbols, next_symbols = symbols(base_obj / "Module.symvers"), symbols(next_obj / "Module.symvers")
    bad = [name for name, crc in base_symbols.items() if not ROOT_SYMBOL.search(name)
           and next_symbols.get(name) != crc]
    if bad:
        raise ValueError("ROM module ABI differs; refuse a boot-only release: " + ", ".join(bad[:12]))
    leaked = [name for name in next_symbols if ROOT_SYMBOL.search(name)]
    system_map = (next_obj / "System.map").read_text().splitlines()
    if not system_map:
        raise ValueError("The compiled kernel symbol map is empty")
    for line in system_map:
        fields = line.split()
        if len(fields) >= 3 and ROOT_SYMBOL.search(fields[2]):
            leaked.append(fields[2])
    if leaked:
        raise ValueError("Root symbols remain: " + ", ".join(leaked[:12]))
    base_boot, next_boot = boot(rooted / "boot.img"), boot(nonksu / "boot.img")
    for key in ("layout", "name", "cmdline", "dtb_sha256", "dtb_address"):
        if base_boot[key] != next_boot[key]:
            raise ValueError("Boot metadata differs from the baseline ROM: " + key)
    if next_boot["kernel"] != (nonksu / "kernel").read_bytes():
        raise ValueError("The packaged boot does not contain the newly built non-KSU kernel")
    return {"device": device, "variant": "nonksu", "kernel_release": releases[1],
            "rom_fingerprint": props.get("ro.build.fingerprint", ""),
            "rooted_boot_sha256": digest(rooted / "boot.img"),
            "nonksu_boot_sha256": digest(nonksu / "boot.img"),
            "rooted_ramdisk_sha256": base_boot["ramdisk_sha256"],
            "nonksu_ramdisk_sha256": next_boot["ramdisk_sha256"],
            "nonksu_config_sha256": digest(next_obj / ".config"),
            "nonksu_symvers_sha256": digest(next_obj / "Module.symvers"),
            "abi": "Matching release and CRCs for every non-root exported baseline symbol",
            "compatibility": "This device and matching ROM build only; shared kernel source does not certify a universal boot image"}


def package(rooted, nonksu, device, source, destination):
    from verify_nonksu import check as check_source
    check_source(source)
    record = check(rooted, nonksu, device)
    record["kernel_base_revision"] = json.loads((source / "NONKSU_SOURCE.json").read_text())["revision"]
    record["kernel_revision"] = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    destination.mkdir(parents=True, exist_ok=True)
    stem = "HeckerOS_" + device + "_NonKSU_boot_" + record["nonksu_boot_sha256"][:12]
    image, metadata = destination / (stem + ".img"), destination / (stem + ".json")
    shutil.copyfile(nonksu / "boot.img", image)
    metadata.write_text(json.dumps(record, indent=2) + "\n")
    (destination / (stem + ".sha256")).write_text(
        digest(image) + "  " + image.name + "\n" + digest(metadata) + "  " + metadata.name + "\n")
    print("Optional boot release verified:", image)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rooted-out", type=Path, required=True)
    parser.add_argument("--nonksu-out", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--device", choices=("coral", "flame"), required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if args.preflight:
        baseline(args.rooted_out, args.device)
        print("Matching rooted ROM baseline and module ABI inputs: ready")
    else:
        if args.nonksu_out is None:
            parser.error("--nonksu-out is required for packaging")
        package(args.rooted_out.resolve(), args.nonksu_out.resolve(), args.device, args.source_root.resolve(),
                args.destination or args.rooted_out.resolve() / "nonksu-release")
