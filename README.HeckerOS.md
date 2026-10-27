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

## Standalone GitHub Actions builds

**Actions → Build Floral kernel and boot image** compiles this kernel directly on every
push to `main` (except documentation-only changes). **Run workflow** can also start it
manually with all fields left at their defaults. No ROM source checkout, completed ROM
build, exported baseline, ROM configuration or ROM compiler snapshot is required.

The job fetches pinned AOSP Clang 22 `clang-r596125`, identified in the published
26 October 2026 HeckerOS kernel, and uses the tree's own `floral_defconfig`. The older
`build.config.common` compiler pin describes Google's legacy build setup. It keeps the normal
Floral features, LTO and CFI enabled. Hosted builds use supported ThinLTO with two
linker workers to reduce peak memory; the source defconfig and native Android build
path retain their existing defaults. A successful run uploads:

- `Image.lz4`, the compiled shared ARM64 kernel;
- `modules.tar.gz`, `Module.symvers`, `System.map`, `floral.config` and `kernel.release`;
- `build.json` with source/compiler revisions and `SHA256SUMS`.

### Optional complete boot image

A kernel tree builds the kernel binary. A complete Android `boot.img` also contains the
recovery ramdisk and device/release boot metadata, which are not stored in this kernel
tree. To produce that image in the same run, select Coral or Flame and enter a direct
HTTPS URL to that device's existing matching `boot.img` plus its SHA-256. No other ROM
files are required. Leave both boot fields empty to compile the kernel alone.

Actions replaces only the kernel in the supplied boot image using pinned AOSP packaging
tools. It preserves the recovery ramdisk, DTB, command line, addresses and OS patch
metadata, and regenerates/verifies an unsigned AVB hash footer if present. Signed AVB
requires the release key and uses the native Android path below. Packaging is intended
for the matching device/release and an unlocked bootloader. Validate boot, recovery and
ROM module loading on the phone before distribution; standalone compilation does not
compare exported symbol CRCs against the installed ROM's modules.

### Optional full ROM ABI verification

The separate **Build boot with full ROM ABI verification** workflow remains available
when you want to compare every non-root exported CRC, kernel release and the exact ROM
compiler/configuration. Its baseline export is an additional verification path, not a
requirement for compiling this kernel or for the default workflow.

On the normal ROM build machine, run this repository's exporter against the completed
KSU-enabled ROM build, then supply the exported archive URL and SHA-256 to that separate
workflow:

```bash
python3 /path/to/HeckerOsfloral-Kernel-NonKsu/tools/hecker/actions_boot.py export \
  --android-root "$PWD" --product-out "$PWD/out/target/product/coral" \
  --device coral --output ~/HeckerOS-coral-baseline.tar.gz
```

Use Flame's checkout and paths for Flame. The exporter automatically locates the exact
Clang/linker from the built kernel's `compile.h`; `--clang-dir` overrides its search.
Python 3.12 or newer is required for that baseline archive extraction path.

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
