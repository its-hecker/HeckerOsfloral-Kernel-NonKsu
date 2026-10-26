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

## GitHub Actions boot image builds

Open **Actions → Build non-KSU boot image → Run workflow** in this repository.
The hosted Ubuntu job compiles this kernel and uploads a device-specific `boot.img`,
SHA-256 checksums and JSON build provenance as an Actions artifact. Choose `coral` or
`flame` and supply the matching ROM baseline archive's direct HTTPS URL and SHA-256.
Run it separately for each device and release. It does not publish a GitHub release.

The baseline comes from a **completed normal, KSU-enabled HeckerOS ROM build**.
A boot image alone is insufficient: the job also needs that build's kernel configuration,
exported module CRCs, kernel release, exact Clang/linker and Android packaging tools.
The exporter collects these automatically. The complete Android source tree stays on
your ROM build machine; Actions only receives the smaller baseline archive.

On the ROM build machine, get just the exporter tools:

```bash
git clone --depth=1 --filter=blob:none --sparse \
  https://github.com/its-hecker/HeckerOsfloral-Kernel-NonKsu ~/hecker-boot-tools
git -C ~/hecker-boot-tools sparse-checkout set tools/hecker
```

From the normal Coral ROM source directory, after its build completes:

```bash
python3 ~/hecker-boot-tools/tools/hecker/actions_boot.py export \
  --android-root "$PWD" --product-out "$PWD/out/target/product/coral" \
  --device coral --output ~/HeckerOS-coral-baseline.tar.gz
```

For Flame, use its normal build checkout and change both occurrences of `coral` to
`flame`. If your build uses a different output directory, pass its actual product output.
The exporter searches the checkout's Clang prebuilts for the exact compiler/linker
identity in the built kernel's `compile.h`. Use `--clang-dir /absolute/compiler/path`
if the compiler is elsewhere. The source kernel must still be the unchanged normal
HeckerOS pin `48d6881fb8537f62811209927022e467d37e3a98`.

Put the resulting archive at a direct HTTPS download URL reachable without login.
Copy the SHA-256 printed by the exporter (also saved in the adjacent `.sha256` file),
then enter the URL, hash and device in **Run workflow**. The archive contains the ROM's
boot image, build properties, configuration/ABI information and build tools; it does not
include the ROM build's private signing keys. Allow space for the compiler snapshot and
temporary files when exporting. Python 3.12 or newer is required for safe archive extraction.

The job uses the baseline configuration with only KSU settings removed, retains LTO/CFI
and other normal settings, compiles `Image.lz4` and modules, and keeps the original boot's
recovery ramdisk, DTB, command line, addresses and OS patch metadata. It checks the actual
compiled symbols and **every non-root exported baseline CRC**, plus kernel release,
before uploading anything. It regenerates and verifies an unsigned AVB hash footer when
one was present. A baseline with a signed AVB footer needs its release signing key and
must use the native Android build path below; the hosted path refuses to strip that signature.

Artifacts are intended for an **unlocked bootloader and the matching device/ROM release**.
The source and packaging checks also run automatically on changes to these tools. A
successful check-only run does not compile a kernel; the manual run with a real baseline
performs the ARM64 build. No baseline ROM outputs are committed to this repository.

## Native Android boot image builds

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

After an OTA, use the optional image built for that release. Check the selected Actions
run for its compilation result and downloadable artifacts. Real-device boot, recovery,
Wi-Fi/module and GameSpace charging tests remain necessary before distributing an image.
Host configuration/source/packaging tests do not establish a tested flashable image.

## Source checks

```bash
python3 tools/hecker/verify_nonksu.py
python3 -m unittest discover -s tools/hecker/tests -v
make O=/tmp/hecker-floral-config ARCH=arm64 CC=cc HOSTCC=cc floral_defconfig
```

The last command is a host Kconfig sanity check, not a target ARM64 compilation.
