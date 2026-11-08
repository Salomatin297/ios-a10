# ios-a10

**Bringing Apple's iOS 16 kernel and userland to the Samsung Galaxy A10 (SM-A105F).**

This is the Galaxy A10 branch of the ios-sweet work. It ports Apple's open-source XNU kernel (xnu-8796.141.3, the iOS 16 generation) to the Exynos 7884B, with the stock iOS 16.7.16 (20H392) iPhone X userland as the target. The original ios-sweet sources are the starting point; the platform layer here is Exynos-specific.

> **Status: 2026-11-08, milestone M2 complete — M3 in progress.**
> The A10 boots XNU through the IOKit platform expert, CPU bring-up and `bsd_init`; probe13 reached `bsd_autoconf`/`kminit` and brought up the loopback and tunnel interfaces. Probe14 now points the BSD root at the internal eMMC (`rd=emmcroot0`, partition `userdata`, read-only) to validate the DW-MMC driver and GPT before an HFS+ root image is written. No iOS userland, `launchd` or GUI runs on the phone yet.

## Device

| | |
| --- | --- |
| Model | Samsung Galaxy A10, SM-A105F |
| SoC | Exynos 7884B: 2× Cortex-A73 + 6× Cortex-A53, ARMv8.0 (no LSE, no PAN) |
| RAM | 2 GB. The kernel payload uses bank 2: `0xC0000000` + `0x40000000` |
| Storage | 32 GB eMMC (DW-MMC with 64-bit FIFO), plus a microSD card for flashing |
| Display | 720×1600 S6D7AT0B panel; the bootloader leaves a framebuffer at `0xEC000000` |
| Interrupts | GIC-400 (GICv2): distributor `0x12301000`, CPU interface `0x12302000` |
| eMMC controller | `0x13500000`, CMDQ `0x13508000`, INTID 178 |
| USB | Data lines are broken on this unit: no adb/fastboot. Every iteration is a TWRP ZIP on microSD |

The device is an experimental bring-up target. Android is already erased; the stock boot/recovery/modem/persist partitions were backed up before any work. iOS is intended to live in the existing `userdata` partition, without changing the GPT.

## Milestones

| Milestone | State |
| --- | --- |
| **M0** — EL1, live FDT, GICv2, framebuffer | ✅ confirmed on device (probe #1/#2) |
| **M1** — eMMC read-only, 64-bit FIFO | ✅ confirmed on device (probe #8, 2026-11-07) |
| **M2** — XNU boots on the A10 to the console | ✅ confirmed on device (probe #13, 2026-11-08): XNU reaches `bsd_init`/`kminit` |
| **M3** — HFS+ root + `launchd` | 🔄 probe14 validates the eMMC root path (`userdata`, read-only) |
| **M4** — iOS GUI (SpringBoard, touch, buttons) | ⏳ blocked on M3 |

### What works today

- A shim packaged as an Android `boot.img` v1 enters EL1, reads the live FDT passed by SBOOT and loads an XNU payload with a generated Apple Device Tree.
- XNU starts on the A10 with 4K pages, GICv2 interrupts, the generic timer and a framebuffer console at 720×1600.
- The kernel reaches the IOKit platform expert; `AppleCLCD` publishes the display and the boot log is visible on the panel.
- The `SweetCPU` personality matches the A10 CPU node, `ml_processor_register()` succeeds and the boot CPU enters `initCPU()`.
- XNU completes CPU startup, `bsd_init` and `kminit`; `bsd_autoconf` configures `lo0`, `gif0` and `stf0` (probe #13).
- The DW-MMC eMMC path reads CID, EXT_CSD, MBR (`0xAA55`) and the GPT header (`EFI PART`) using 64-bit FIFO elements.
- The kernel contains no ARMv8.1 LSE instructions; CI scans for them.

### What does not work yet

- **SMP:** the A10 firmware accepts `PSCI CPU_ON` but secondary cores do not acknowledge it. Bring-up is single-CPU for now.
- **iOS root:** there is no HFS+ volume yet, so `IOFindBSDRoot` finds nothing to mount. Probe14 tests the eMMC root path read-only; mounting `userdata` and starting `launchd` are M3 work.
- **GUI:** no SpringBoard, touch, buttons, battery, Wi-Fi, Bluetooth, cellular, audio, camera or GPU.
- **USB:** no data connection; logs are read by photographing the screen or from the TWRP/ZIP log on the microSD card.

## How it boots

```
Samsung SBOOT (unlocked bootloader)
  -> boot partition: Android boot.img v1
     -> our shim: EL1, live FDT, copy XNU + ADT + boot_args
        -> XNU-8796 -> IOKit platform expert -> framebuffer console
           -> [M2] CPU startup, kperf, bsd_init
              -> [M3] HFS+ root in userdata -> launchd
                 -> [M4] backboardd + SpringBoard
```

Each probe is a self-contained TWRP ZIP. The installer backs up the current `boot` partition to `a10/backup/boot.before-probeNN.img`, writes the new image and keeps a log in `a10/probeNN.log`. `a10_restore_boot.zip` walks the backup chain and restores the newest known-good image.

## Building

The kernel is built by GitHub Actions on `macos-15` with [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build): the workflow fetches xnu-8796.141.3, applies `xnu/patches/` in order and verifies A10 markers in the patched tree. A silently skipped patch fails the job.

```sh
gh workflow run xnu.yml -R Salomatin297/ios-a10
gh run download RUN_ID -R Salomatin297/ios-a10 -n xnu-kernel -D ci/xnu-kernel-RUN_ID
```

The downloaded `kernel.development.sweet` is packed into the boot image by the local bring-up scripts (`tools/mkadt-a10.py` for the A10 device tree, `tools/mkpayload.py` for the payload, and the A10 shim). The resulting image is wrapped into `a10_probeNN.zip` with the same TWRP installer used for the eMMC probes.

### Flash on the phone

1. Copy `a10_probeNN.zip` to the root of a FAT32 microSD card.
2. TWRP → Mount → enable **Micro SD card**.
3. Install → select the ZIP → swipe. Do **not** enable ZIP signature verification.
4. Reboot and photograph the screen.
5. Return the card: the previous boot image is in `a10/backup/`, the installer log in `a10/probeNN.log`.

Rollback: `a10_restore_boot.zip` searches for the newest `boot.before-probeNN.img`, then the known probe backups, then the stock image.

## A10 patch series

The A10 platform layer is a continuation of the ios-sweet patch series:

| Patch | Content |
| --- | --- |
| `0084` | GIC-400 (GICv2) instead of GICv3; memory-mapped GICC/GICD; virtual timer INTID 27; no PAN |
| `0085` | Remove `__ARM_16K_PG__`; 4K pages for ARMv8.0 cores |
| `0086` | 4K user VA, `-mcpu=cortex-a53+crypto` (no LSE) |
| `0087` | Early framebuffer at `0xEC000000` |
| `0088` | Read-only DW-MMC probe |
| `0089` | Startup progress markers |
| `0090` | DW-MMC eMMC driver with 64-bit FIFO writes and block reads |
| `0091` | `emmcroot0` root selection and mount diagnostics |
| `0092` | `IOFindBSDRoot` hook for `emmcroot0` → `sweet_emmc_rootdev()` |
| `0093` | `SweetCPU` personality for `arm,cortex-a53`, CPU match logging, IOKit/startup traces |
| `0094` | Correct GICv2 `GICD_SGIR` self-SGI filter (`TargetListFilter` is bits `[25:24]`) and CPU-timeout diagnostics |

CI checks a marker for every A10 patch, including the SGIR fix, so a patch that does not apply cleanly cannot produce a green build.

## Differences from ios-sweet

| | ios-sweet (Redmi Note 10 Pro) | ios-a10 (Galaxy A10) |
| --- | --- | --- |
| SoC | Qualcomm SM7150, 8 cores | Exynos 7884B, 2× A73 + 6× A53 |
| Interrupts | GICv3 | GIC-400, GICv2 |
| Page size | 16K | 4K |
| CPU baseline | 6× A55 via PSCI | 1 boot CPU; secondary `CPU_ON` not acknowledged |
| Boot | `fastboot boot`, nothing flashed | microSD/TWRP ZIP writes the `boot` partition |
| Display | 1080×2400 panel presented as 1125×2436 | 720×1600 panel, kernel framebuffer console |
| Storage | UFS driver | DW-MMC eMMC driver |
| Status | iOS home screen | XNU console / IOKit; no userland yet |

## Key hardware facts

- Boot CPU: the first Cortex-A53 of cluster 1, MPIDR `0x100` (not `0x0`).
- PSCI v0.1 through `smc`; `CPU_ON = 0xC4000003`. The firmware accepts it but does not start secondaries for our payload.
- GICv2 `GICD_SGIR`: SGI INTID `[3:0]`, `CPUTargetList` `[23:16]`, `TargetListFilter` `[25:24]`; `0b10` targets the requesting CPU interface.
- Kernel window: `0xC4000000..0xE8F00000`; panic log at `0xE8F00000`.
- Current boot arguments: `debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress wdt=-1`.

## Repository layout

| Path | Content |
| --- | --- |
| `xnu/patches/` | XNU patch series, applied in order by CI (A10 work starts at `0084`) |
| `.github/workflows/xnu.yml` | macOS kernel build, LSE scan and A10 marker verification |
| `tools/mkadt-a10.py` | Generates the A10 Apple Device Tree |
| `tools/mkpayload.py` | Packs kernel + ADT + boot arguments into the shim payload |
| `tools/verify_a10_patches.py` | Replays the CI patch sequence locally and fails on any skipped patch |
| `shim/` | ios-sweet's original shim sources (the A10 shim is a separate local bring-up tree) |
| `NOTES.md` | Active engineering handoff: probe results, addresses, next actions |

Device dumps, partition backups, Apple firmware, IPSW files and generated root images are not in git.

## Credits

Built on Apple's open-source releases (XNU, IOHIDFamily, libpthread, HFS), [ios-sweet](https://github.com/dipa-studio/ios-sweet), the [ios-a30s](https://github.com/xJezykk/ios-a30s) Exynos 7904 port, [blacktop/darwin-xnu-build](https://github.com/blacktop/darwin-xnu-build), [ipsw](https://github.com/blacktop/ipsw), Samsung/Exynos kernel sources and TWRP. iOS itself is Apple's and is not distributed here.

This is an experimental port, not a usable phone OS. Apple's security model is largely disabled, and nothing personal should be stored on the device.
