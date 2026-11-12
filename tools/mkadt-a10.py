#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the A10 xnu needs to boot.

Port of mkadt-a30s.py to the Samsung Galaxy A10 (SM-A105F, Exynos 7884B).
Every MMIO address comes from the A10 live FDT / stock dmesg in dist/A10.

Usage:
  mkadt-a10.py OUT.adt [--dump] [--qemu] [--boot-core 0x100]

--qemu switches the boot-core reg to 0 for the QEMU virt board: there the
single TCG vCPU reports Aff1=0/Aff0=0 (mask MPIDR_AFF1|MPIDR_AFF0 in
ml_get_cpu_number).  The phone boots on the first Cortex-A53 of cluster 1:
stock dmesg says "Booting Linux on physical CPU 0x100", and probe #9 with
reg=0 panicked in ml_get_cpu_number_local() because the live MPIDR
0x80000100 did not match any ADT cpu node.

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
# IOMobileFramebuffer.framework reads the canvas from the disp0 node; present
# the native panel: 720x1560 at display-scale 2 (360x780 points).  These are
# fixed here on purpose; the kernel no longer defaults to an iPhone X canvas.
CANVAS_W, CANVAS_H = 720, 1560
# Environment overrides exist for experiments only.
CANVAS_W = int(os.environ.get("A10_CANVAS_W", CANVAS_W))
CANVAS_H = int(os.environ.get("A10_CANVAS_H", CANVAS_H))
DISPLAY_SCALE = int(os.environ.get("A10_DISPLAY_SCALE", 2))
# /product/artwork-scale-factor decides which @Nx asset set UIKit loads.
# Keep the real D22AP value (3) unless an experiment overrides it.
ARTWORK_SCALE = int(os.environ.get("A10_ARTWORK_SCALE", 0))
# Exynos DW-MMC 0 hosts the soldered eMMC.  DT interrupts = <0 146 0>;
# XNU wants the GIC INTID, i.e. 146 + 32 = 178.
DWMMC0, DWMMC0_SIZE = 0x13500000, 0x2000
DWMMC0_CMDQ, DWMMC0_CMDQ_SIZE = 0x13508000, 0x200
DWMMC0_IRQ = 178
# Synthetic ECID, NOT the phone's: 'A10' header word, fixed tail.
A10_ECID = 0x4131303000000001
TIMEBASE = 26000000		# CNTFRQ at boot, FDT clock-frequency 0x18cba80
VTIMER_INTID = 27		# FDT timer: type1 PPI 11 (virtual) -> INTID 27
# Masked MPIDR (Aff1<<8 | Aff0) of the boot core.  The A10 FDT puts the two
# Cortex-A73s in cluster 0 (cpu@0/1) and the six A53s in clusters 1-2
# (cpu@100..103, cpu@200/201).  Stock dmesg: "Booting Linux on physical CPU
# 0x100" -> Aff1=1, Aff0=0: the boot core is the first A53, not an A73.
# Probe #9 declared reg=0 and hit the machine_routines.c:1244 assert because
# the live MPIDR (0x80000100) matched no topology entry.
BOOT_CORE_PHYS_ID = 0x100
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
            props = [(k, v) for k, f, v in c.props
                     if not f and k not in ('name', 'AAPL,phandle')]
            # A10_ARTWORK_SCALE=2 selects the @2x asset set when the canvas is
            # rendered at 2x for speed (the real D22AP tree says 3).
            if ARTWORK_SCALE:
                props = [(k, struct.pack('<I', ARTWORK_SCALE) if
                          k == 'artwork-scale-factor' else v) for k, v in props]
            return props
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
        ('display-scale', u32(DISPLAY_SCALE)),
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

    # CPU nodes. The boot core is enumerated first: ml_parse_cpu_topology
    # assigns logical ids in DT order, and SweetCPU::startAll requires the
    # boot CPU to be logical 0. Only the boot node has state = "running";
    # ml_is_boot_cpu() treats that (and only that) as the boot CPU. The
    # A10 has 2x Cortex-A73 (cluster 0, little-endian MPIDRs 0x000/0x001)
    # + 6x Cortex-A53 (clusters 1-2, 0x100..0x103 and 0x200/0x201);
    # cpus=N in the boot-args still selects how many enter the topology.
    # cluster-type is the ASCII code XNU compares against 'E'/'P'.  With
    # patch 0126 (MAX_CPU_CLUSTERS=2) 'E'/'P' gave one E pset (A53) and one
    # P pset (A73); patch 0128 reverted MAX_CPU_CLUSTERS to 1 and made the
    # kernel ignore the property, so A10_CLUSTER_TYPE=smp emits the plain
    # single-cluster form (cluster-type 0, cluster-id 0) for every core.
    cluster_type = os.environ.get('A10_CLUSTER_TYPE', 'amp').lower()
    if cluster_type in ('smp', '0', 'none'):
        cluster_type = 'smp'
    elif cluster_type not in ('amp', 'ep'):
        raise SystemExit('A10_CLUSTER_TYPE must be amp or smp')

    if qemu:
        cpu_list = [(boot_core, 'arm,cortex-a53')]
    else:
        a10_cpus = [
            (0x100, 'arm,cortex-a53'),
            (0x101, 'arm,cortex-a53'),
            (0x102, 'arm,cortex-a53'),
            (0x103, 'arm,cortex-a53'),
            (0x200, 'arm,cortex-a53'),
            (0x201, 'arm,cortex-a53'),
            (0x000, 'arm,cortex-a73'),
            (0x001, 'arm,cortex-a73'),
        ]
        # A10_CPU_ORDER=0x100,0x103,0x102,... overrides the enumeration
        # order (and therefore which cores cpus=N selects) without a kernel
        # rebuild.  The boot core is always enumerated first.
        order = os.environ.get('A10_CPU_ORDER')
        if order:
            by_phys = dict(a10_cpus)
            want = [int(part, 0) for part in order.split(',') if part.strip()]
            missing = [hex(phys) for phys in want if phys not in by_phys]
            if missing:
                raise SystemExit('A10_CPU_ORDER: unknown MPIDRs ' + ','.join(missing))
            a10_cpus = [(phys, by_phys[phys]) for phys in want]
        cpu_list = [(boot_core, 'arm,cortex-a53')] + [
            (phys, compat) for phys, compat in a10_cpus if phys != boot_core]

    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], [
        node('cpu%d' % i, [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr(compat) + cstr('ARM,v8')),
            ('reg', u32(phys)),	# MPIDR Aff1:Aff0, not logical
            ('cpu-id', u32(i)),
            # Logical cluster id after the E/P collapse (HAS_CLUSTER is off).
            ('cluster-id', u32(0 if cluster_type == 'smp'
                               or compat == 'arm,cortex-a53' else 1)),
            ('cluster-type', u32(0 if cluster_type == 'smp'
                                 else (ord('E') if compat == 'arm,cortex-a53'
                                       else ord('P')))),
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(phys & 0xff)),
            # Every E core is in the logical E cluster (id 0), every P core in
            # the logical P cluster (id 1).  This matches cpu_cluster_id, which
            # pset_create() uses.
            ('l2-cache-id', u32(0 if compat == 'arm,cortex-a53' else 1)),
            ('l3-cache-id', u32(0)),
            ('state', cstr('running' if i == 0 else 'stopped')),
            ('timebase-frequency', u32(TIMEBASE)),
            ('clock-frequency', u32(1600000000)),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1600000000)),
            ('peripheral-frequency', u32(TIMEBASE)),
            ('fixed-frequency', u32(TIMEBASE)),
        ]) for i, (phys, compat) in enumerate(cpu_list)
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
            ('canvas-width', u32(CANVAS_W)),
            ('canvas-height', u32(CANVAS_H)),
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
