#!/usr/bin/env python3
"""Verify the A10 XNU patch series against a pristine CI-equivalent baseline.

The CI (blacktop/darwin-xnu-build) applies *.patch with `git apply --check`
and SKIPS a patch that does not apply - a broken port patch would still
produce a green build with a stock kernel.  This script reproduces the CI
sequence locally and fails loudly:

  1. reset the xnu tree to the vanilla xnu-8796.141.3 checkout
  2. apply the upstream ios-sweet series (0001..0083)
  3. apply the A10 port series (0084..0091)
  4. check the A10 markers are really present in the patched tree

usage: verify_a10_patches.py [--tree DIR] [--upstream DIR] [--a10 DIR]
"""
import argparse
import os
import subprocess
import sys

MARKERS = [
    ("pexpert/pexpert/arm64/SWEET.h", "SWEET_GICV2", "0084 GICv2 support"),
    ("pexpert/pexpert/arm64/SWEET.h", "#define __ARM_16K_PG__", "0085 16K knob must be gone"),
    ("makedefs/MakeInc.def", "SWEET_4K_USERVA", "0086 4K user VA"),
    ("osfmk/arm64/arm_vm_init.c", "SWEET_EARLY_FB_PA   0xEC000000ULL", "0087 early framebuffer"),
    ("bsd/dev/sweet_emmc.c", "A10 Samsung DW-MMC0", "0088/0090 A10 eMMC driver"),
    ("bsd/dev/sweet_emmc.c", "HDATA_WIDTH", "0090 FIFO width handling"),
    ("bsd/kern/bsd_init.c", "SWEET root selection", "0091 root diagnostics"),
    ("bsd/vfs/vfs_subr.c", "SWEET root mount: bdev", "0091 mount diagnostics"),
]

# upstream ios-sweet series (the A10/a30s port patches are applied separately)
UPSTREAM_MAX = 83


def git(tree, *args, check=False):
    r = subprocess.run(("git", "-C", tree) + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit("git %s failed:\n%s" % (" ".join(args), r.stderr))
    return r


def apply(tree, patch, reverse=False):
    args = ["apply"]
    if reverse:
        args.append("-R")
    args.append(patch)
    chk = git(tree, *(args + ["--check"]))
    if chk.returncode != 0:
        return False, chk.stderr.strip().splitlines()[:6]
    res = git(tree, *args)
    if res.returncode != 0:
        return False, res.stderr.strip().splitlines()[:6]
    return True, []


HFS_SHA = "31d5ec238dfab485e78a9a30b8b92d13e8914f03"   # hfs-627.100.6, paired with xnu-8796.141.3


def stage_hfs(tree):
    """Mirror xnu/patches/0026-stage-hfs.sh (the CI stage script) locally."""
    import shutil
    import tempfile

    target = os.path.join(tree, "bsd", "sweet_hfs")
    if os.path.isdir(target):
        return
    tmp = tempfile.mkdtemp(prefix="hfsstage_")
    subprocess.run(["git", "init", "-q", tmp], check=True)
    subprocess.run(["git", "-C", tmp, "fetch", "-q", "--depth", "1",
                    "https://github.com/apple-oss-distributions/hfs", HFS_SHA], check=True)
    subprocess.run(["git", "-C", tmp, "checkout", "-q", "FETCH_HEAD"], check=True)
    os.makedirs(target, exist_ok=True)
    for d in ("core", "hfs_encodings"):
        shutil.copytree(os.path.join(tmp, d), os.path.join(target, d))
    # SWEET has no AppleKeyStore: the stage script guards the include and the
    # key-unwrapping prototype with CONFIG_PROTECT
    p = os.path.join(target, "core", "hfs_iokit.h")
    text = open(p, encoding="utf-8").read()
    include = "#include <AppleKeyStore/AppleKeyStoreFSServices.h>\n"
    first = "int hfs_unwrap_key("
    end = "__END_DECLS"
    text = text.replace(include, "#if CONFIG_PROTECT\n" + include + "#endif\n")
    i, j = text.index(first), text.index(end)
    text = text[:i] + "#if CONFIG_PROTECT\n" + text[i:j].rstrip("\n") + "\n#endif\n\n" + text[j:]
    open(p, "w", encoding="utf-8").write(text)
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", default=r"C:\Users\User\iOS-Port\ci\xnu-8796-base")
    ap.add_argument("--upstream", default=r"C:\Users\User\iOS-Port\ref\ios-sweet-a30s-test\xnu\patches")
    ap.add_argument("--a10", default=r"C:\Users\User\iOS-Port\ci\a10-patches")
    ap.add_argument("--keep", action="store_true", help="do not reset the tree first")
    a = ap.parse_args()

    if not a.keep:
        print("reset tree to vanilla xnu-8796 ...")
        git(a.tree, "reset", "--hard", "HEAD", check=True)
        # full clean: the HFS sources are re-staged by stage_hfs() below, and
        # patch 0026 adds its own files into the same directory
        git(a.tree, "clean", "-fdq", check=True)

    print("applying upstream series 0001-%04d ..." % UPSTREAM_MAX)
    stage_hfs(a.tree)
    ups = sorted(f for f in os.listdir(a.upstream)
                 if f.endswith(".patch") and int(f.split("-")[0]) <= UPSTREAM_MAX)
    bad = []
    for name in ups:
        ok, err = apply(a.tree, os.path.join(a.upstream, name))
        if not ok:
            bad.append((name, err))
    if bad:
        for name, err in bad:
            print("  FAIL %s" % name)
            for line in err:
                print("      " + line)
        raise SystemExit("upstream series does not apply cleanly")
    print("  upstream ok (%d patches)" % len(ups))

    print("applying A10 series ...")
    a10s = sorted(f for f in os.listdir(a.a10) if f.endswith(".patch"))
    failed = []
    for name in a10s:
        ok, err = apply(a.tree, os.path.join(a.a10, name))
        print("  %-6s %s" % ("OK" if ok else "FAIL", name))
        if not ok:
            failed.append((name, err))
    if failed:
        for name, err in failed:
            print("  FAIL %s" % name)
            for line in err:
                print("      " + line)
        raise SystemExit("A10 series does not apply cleanly")

    print("checking A10 markers ...")
    missing = []
    for rel, needle, what in MARKERS:
        path = os.path.join(a.tree, rel)
        text = open(path, encoding="utf-8", errors="replace").read()
        if needle.startswith("#define "):
            ok = needle not in text
            want = "absent"
        else:
            ok = needle in text
            want = "present"
        print("  %-6s %-46s %s" % ("OK" if ok else "FAIL", what, want))
        if not ok:
            missing.append((rel, needle))
    if missing:
        raise SystemExit("markers missing: %s" % missing)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
