# HeckerOS Floral kernel — non-KSU variant

This is the optional kernel without KernelSU Next for Pixel 4 (`flame`) and Pixel 4 XL
(`coral`). It derives from the **exact kernel currently pinned by both HeckerOS builds**:
`its-hecker/heckeros-floral-kernel`, `feat/game-booster-reference`,
`48d6881fb8537f62811209927022e467d37e3a98`.

The KernelSU submodule, driver symlink, Kconfig/Makefile integration, all manual
KernelSU hooks, SUSFS implementation/hooks and root configuration entries are removed.
The existing non-root kernel settings and other code are retained, including WireGuard
and the leased GameSpace charging inhibitor. `NONKSU_SOURCE.json` records the exact
transformation and hashes. Linux and upstream authors' licenses remain intact.

The normal HeckerOS manifests continue selecting the KSU kernel. This repository is
an opt-out variant; it does not change the normal ROM kernel or kernel features.
“Non-KSU” describes removal of the built-in kernel root framework. It does not change
the ROM's existing userdebug/development-ADB configuration or promise an integrity verdict.

## Optional boot image builds

Use the `nonksu` branch of the device's local manifest in a **separate Android source
checkout**, with the same release source pins and build/signing configuration as the
normal ROM. The root-enabled ROM must be built first: the packaging guard compares its
kernel release and every non-root exported module CRC with the non-KSU build. This is
necessary because the ROM's vendor partition keeps its existing kernel modules.

Coral manifest: `https://github.com/its-hecker/HeckerOs-localmanifest`, branch `nonksu`.
Flame manifest: `https://github.com/its-hecker/HeckerOs-Flame-localmanifest`, branch `nonksu`.
Both use this repository at the normal `kernel/google/msm-4.14` path. Use a fresh kernel
checkout so a populated KernelSU submodule is not left over from an earlier sync.

Example for Coral (use Flame's manifest and paths for Flame):

```bash
mkdir -p ~/heckeros-coral-nonksu
cd ~/heckeros-coral-nonksu
repo init -u https://github.com/its-hecker/HeckerOS -b feat/suite-polish --git-lfs
git clone --branch nonksu https://github.com/its-hecker/HeckerOs-localmanifest .repo/local_manifests
repo sync -c -j8 --fail-fast --no-clone-bundle --no-tags
HECKER_KSU_PRODUCT_OUT=~/heckeros-coral/out/target/product/coral \
  HECKER_BUILD_JOBS=8 bash .repo/local_manifests/build-nonksu-boot.sh
```

The helper uses its own `OUT_DIR` (default `out-nonksu`), prepares the normal HeckerOS
feature configuration and runs the native Android `bootimage` target. Thus boot headers,
recovery-as-boot, DTB and AVB packaging use the same Android build rules as the ROM.
It never replaces the rooted build's output. Set `HECKER_NONKSU_OUT_DIR` if required.

After compilation, `tools/hecker/package_boot.py` checks:

- no root config or root symbols in the generated kernel outputs;
- equal kernel release and matching CRCs for all non-root baseline exports;
- the correct device in the baseline ROM properties;
- equal boot header, cmdline, OS patch metadata and DTB;
- the boot actually contains the newly compiled non-KSU kernel.

It writes `HeckerOS_<device>_NonKSU_boot_<hash>.img`, JSON provenance and a SHA-256 file
into the normal ROM output's `nonksu-release/` directory. If compatibility checks fail,
it refuses to prepare a boot-only release; investigate the mismatch rather than ship it.

## Shared kernel versus shared boot

Flame and Coral share this kernel source, but a complete `boot.img` also contains a
recovery ramdisk, hardware metadata and build configuration. Initially distribute
**one matching image for each device and ROM release**. Do not label a boot universal
merely because the kernel source is shared. Compare actual build outputs and test both
phones before consolidating them. Keep the normal boot image available as the counterpart.

After an OTA, use the optional image built for that release. The first ARM64 kernel build,
full boot build, module compatibility check and real-device acceptance remain pending.
Host configuration/source/packaging tests do not establish a tested flashable image.

## Source checks

```bash
python3 tools/hecker/verify_nonksu.py
python3 -m unittest discover -s tools/hecker/tests -v
make O=/tmp/hecker-floral-config ARCH=arm64 CC=cc HOSTCC=cc floral_defconfig
```

The last command is a host Kconfig sanity check, not a target ARM64 compilation.
