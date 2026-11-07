#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the A10 xnu needs to boot.

Port of mkadt-a30s.py to the Samsung Galaxy A10 (SM-A105F, Exynos 7884B).
Every MMIO address comes from the A10 live FDT / stock dmesg in dist/A10.

Usage:
  mkadt-a10.py OUT.adt [--dump] [--qemu] [--boot-core 0x100]

--qemu switches the boot-core reg to 0 for the QEMU virt board: there the
single TCG vCPU reports Aff1=0/Aff0=0 (mask MPIDR_AFF1|MPIDR_AFF0 in
ml_get_cpu_number).  The phone boot core must be measured on hardware; on
the A10 it is the first Cortex-A73 (MPIDR 0x80000000 -> reg 0).

A10 facts:
  - SoC Exynos 7884B (ro.boot.hardware), 2x Cortex-A73 + 6x Cortex-A53;
  - eMMC host DW-MMC0 at 0x13500000 + CMDQ window 0x13508000, DT IRQ 146
    (INTID 178 after the GIC's 32 private interrupts);
  - GIC-400 at GICD 0x12301000 / GICC 0x12302000;
  - virtual timer: PPI 11 -> INTID 27, CNTFRQ 26 MHz;
  - framebuffer 0xEC000000, 720x1600, stride 2880 (SBOOT leaves it up);
  - RAM (live /proc/device-tree/reserved-memory + dmesg):
      bank 1  0x80000000 + 0x3DA00000  SBOOT lives here: its DTB at
        0x8DFE0000, the secure logs at 0x91200000, and OUR boot image
        (measured load base 0x9BBF0000; SBOOT drifts it per boot).
      bank 2  0xC0000000 + 0x40000000  kernel RAM: seclog 0xC0000000+0x80000,
        wifibt 0xE9000000, abox 0xE9400000, framebuffer 0xEC000000+0x1400000,
        modem_if 0xF5400000, cp_ram_logging 0xFCC00000, gnss 0xFFC00000.
    The kernel window is bank 2 ONLY: copying the kernel into bank 1 would
    overwrite the shim image (and the code that is doing the copy) when
    SBOOT happens to load it there.  dram-size ends 1 MiB below the
    wifibt reservation; that last MiB is the panic log.
"""

import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: bank 2, 0xC4000000..0xE8F00000 (591 MiB).  The base is
# 32 MiB aligned because start.s maps the kernel with a 32 MiB block; the
# top 1 MiB (0xE8F00000) is the panic log, carved off the dram range.
PHYS_BASE = 0xC4000000
MEM_SIZE = 0x24f00000		# 0xE8F00000 - PHYS_BASE (panic log excluded)
# Panic log buffer, carved off the TOP of the dram range and exposed to
# XNU as /pram + /chosen/embedded-panic-log-size.
PANIC_LOG_SIZE = 0x100000
# /arm-io children are relative to this; it must not be 0 (serial_init
# bails). Exynos peripherals start at 0x10000000 (wdt/PMC/GIC/serial).
SOC_BASE = 0x01000000

# GIC-400: same layout as the A30s.
GICC, GICC_SIZE = 0x12302000, 0x1000
GICD, GICD_SIZE = 0x12301000, 0x1000
GICC_QEMU, GICD_QEMU = 0x08010000, 0x08000000
# fbcon: s3cfb.bootloaderfb=0xEC000000, panel s6d7at0b 720x1600.
FB, FB_SIZE = 0xec000000, 0x1400000
FB_W, FB_H, FB_STRIDE = 720, 1600, 720 * 4
# Exynos DW-MMC 0 hosts the soldered eMMC.  DT interrupts = <0 146 0>;
# XNU wants the GIC INTID, i.e. 146 + 32 = 178.
DWMMC0, DWMMC0_SIZE = 0x13500000, 0x2000
DWMMC0_CMDQ, DWMMC0_CMDQ_SIZE = 0x13508000, 0x200
DWMMC0_IRQ = 178
# Synthetic ECID, NOT the phone's: 'A10' header word, fixed tail.
A10_ECID = 0x4131303000000001
TIMEBASE = 26000000		# CNTFRQ at boot, FDT clock-frequency 0x18cba80
VTIMER_INTID = 27		# FDT timer: type1 PPI 11 (virtual) -> INTID 27
# Masked MPIDR (Aff1<<8 | Aff0) of the boot core.  Probe #1 on the A10 read
# MPIDR_EL1 = 0x80000000 -> Aff1=0, Aff0=0 -> cpu@0, the first Cortex-A73
# (the A73 pair is cluster 0; the A53s are clusters 1 and 2).  So the ADT
# cpu node must carry reg = 0, not the A30s value 0x100.
BOOT_CORE_PHYS_ID = 0x0
BOOT_MANIFEST_HASH = hashlib.sha384(b'a10 boot manifest').digest()

# The real D22AP tree provides /product; kept out of git.  Same optional
# input as the A30s generator: tools/remotezip.py + ipsw img4.
D22AP_ADT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
			 'research', 'd22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'note: {path} missing, no /product node (fine for first boot)',
              file=sys.stderr)
        return None
    for c in adt.parse(open(path, 'rb').read())[0].children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props
                    if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')


u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


def arg_value(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 >= len(sys.argv):
            raise SystemExit(f'{flag} needs a value')
        return sys.argv[i + 1]
    return None


def build():
    qemu = '--qemu' in sys.argv
    boot_core = arg_value('--boot-core')
    boot_core = int(boot_core, 0) if boot_core else BOOT_CORE_PHYS_ID
    panic_base = PHYS_BASE + MEM_SIZE
    gicc, gicd = (GICC_QEMU, GICD_QEMU) if qemu else (GICC, GICD)
    if qemu:
        boot_core = 0
    gicc_rel, gicd_rel = gicc - SOC_BASE, gicd - SOC_BASE

    seed = os.urandom(64)
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(PHYS_BASE)),
        ('dram-size', u64(MEM_SIZE)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        ('firmware-version', cstr('a10-shim')),
        ('system-firmware-version', cstr('a10-shim')),
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        ('crypto-hash-method', cstr('sha2-384')),
        ('display-scale', u32(2)),
        ('unique-chip-id', struct.pack('<Q', A10_ECID)),
        ('chip-id', u32(0x8015)),
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
    ], [
        node('memory-map', [ph()]),
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))]),
    ])

    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], [
        node('cpu0', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('arm,cortex-a73') + cstr('ARM,v8')),
            ('reg', u32(boot_core)),	# MPIDR Aff1:Aff0, not logical
            ('cpu-id', u32(0)),
            ('cluster-id', u32(0)),
            ('cluster-type', u32(0)),
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(0)),
            ('l2-cache-id', u32(0)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running')),
            ('timebase-frequency', u32(TIMEBASE)),
            ('clock-frequency', u32(1600000000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1600000000)),
            ('peripheral-frequency', u32(TIMEBASE)),
            ('fixed-frequency', u32(TIMEBASE)),
        ]),
    ])

    gic = node('gic', [
        ph(),
        ('compatible', cstr('arm,gic-v2')),
        ('interrupt-controller', b''),
        ('virtual-timer-intid', u32(VTIMER_INTID)),
        ('reg', u64(gicc_rel, GICC_SIZE, gicd_rel, GICD_SIZE)),
    ])

    emmc = node('dwmmc0', [
        ph(),
        ('compatible', cstr('samsung,exynos-dw-mshc')),
        ('reg', u64(DWMMC0 - SOC_BASE, DWMMC0_SIZE)),
        ('cmdq-reg', u64(DWMMC0_CMDQ - SOC_BASE, DWMMC0_CMDQ_SIZE)),
        ('interrupt', u32(DWMMC0_IRQ)),
        ('bus-width', u32(8)),
        ('non-removable', u32(1)),
    ])

    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('a10-io')),
        ('compatible', cstr('sweet-io') + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [
        gic,
        emmc,
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr('sweet,display')),
            ('canvas-width', u32(FB_W)),
            ('canvas-height', u32(FB_H)),
        ]),
    ])

    fbcon = node('sweet-fbcon', [
        ph(),
        ('reg', u64(FB, FB_SIZE)),
        ('width', u32(FB_W)),
        ('height', u32(FB_H)),
        ('stride', u32(FB_STRIDE)),
    ])

    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    pram = node('pram', [ph(),
                         ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    product_props = d22ap_product()
    product = [node('product', [ph()] + product_props)] if product_props \
        else []

    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('xiaomi,sweet')
         + cstr('exynos7884') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ], [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    root = build()
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
