#!/bin/bash
# Build the SWEET iOS root HFS+ image from IPSW components, staging the
# downloads so the peak disk usage stays low.
#
#   mkiosroot-staged.sh WORKDIR IPSW_URL OUT.img [SIZE]
#
# Upstream tools/mkiosroot.sh mounts the three APFS images at once, which
# needs ~14 GB of free disk (downloads + output).  Here each image is
# mounted, copied and deleted before the next one is fetched, so the peak is
# the HFS+ output plus the largest single component (~9 GB).
set -euo pipefail

[[ $# -ge 3 ]] || { sed -n '2,10p' "$0"; exit 2; }
work=$(realpath -m "$1"); ipsw="$2"; out="$3"; size=${4:-12G}
[[ ! -e "$out" ]] || { echo "refusing to overwrite $out" >&2; exit 1; }

repo_root=$(cd "$(dirname "$0")/.." && pwd)
remotezip="$repo_root/tools/remotezip.py"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need apfs-fuse; need mkfs.hfsplus; need rsync; need python3

mkdir -p "$work"
cd "$work"

# --- resolve the component names from BuildManifest.plist -------------------
python3 "$remotezip" "$ipsw" "BuildManifest.plist" "$work" >/dev/null
python3 - "$work/BuildManifest.plist" > components.txt <<'PY'
import plistlib, sys
m = plistlib.load(open(sys.argv[1], "rb"))
want = {"OS": "os", "Cryptex1,SystemOS": "sysos", "Cryptex1,AppOS": "appos"}
found = {}
for data in m.get("BuildIdentities", []):
    for k, v in data.get("Manifest", {}).items():
        if k in want and k not in found:
            path = v.get("Info", {}).get("Path", "")
            if path.endswith(".dmg"):
                found[k] = path
for k, alias in want.items():
    if k not in found:
        raise SystemExit("component not found in BuildManifest: %s" % k)
    print("%s %s" % (alias, found[k]))
PY
cat components.txt

get_component() { # alias path
    if [ ! -f "$work/$2" ]; then
        echo "fetching $2 ..."
        python3 "$remotezip" "$ipsw" "$2" "$work"
    fi
}

# --- the HFS+ output --------------------------------------------------------
truncate -s "$size" "$out"
mkfs.hfsplus -s -J -v iOSRoot "$out"
mkdir -p "$work/root"
sudo mount -t hfsplus -o loop,force "$out" "$work/root"

apfs_mount() { # file alias
    ln -sf "$(realpath "$1")" "$work/$2.dmg"
    mkdir -p "$work/$2"
    sudo "$(command -v apfs-fuse)" "$work/$2.dmg" "$work/$2"
}
apfs_umount() {
    sudo umount "$work/$1" 2>/dev/null || true
    sudo fusermount -u "$work/$1" 2>/dev/null || true
    rm -f "$work/$1.dmg"
}

copy_into() { # src dst
    sudo mkdir -p "$2"
    sudo rsync -aH --numeric-ids "$1/" "$2/"
}

# --- 1) the OS volume -------------------------------------------------------
get_component os "$(awk '$1=="os"{print $2}' components.txt)"
apfs_mount "$work/$(awk '$1=="os"{print $2}' components.txt)" os
copy_into "$work/os/root" "$work/root"
apfs_umount os
rm -f "$work/$(awk '$1=="os"{print $2}' components.txt)"
sudo rm -rf "$work/os"

# --- 2) the SystemOS cryptex ------------------------------------------------
get_component sysos "$(awk '$1=="sysos"{print $2}' components.txt)"
apfs_mount "$work/$(awk '$1=="sysos"{print $2}' components.txt)" sysos
copy_into "$work/sysos/root" "$work/root/private/preboot/Cryptexes/OS"
apfs_umount sysos
rm -f "$work/$(awk '$1=="sysos"{print $2}' components.txt)"
sudo rm -rf "$work/sysos"

# --- 3) the AppOS cryptex ---------------------------------------------------
get_component appos "$(awk '$1=="appos"{print $2}' components.txt)"
apfs_mount "$work/$(awk '$1=="appos"{print $2}' components.txt)" appos
copy_into "$work/appos/root" "$work/root/private/preboot/Cryptexes/App"
apfs_umount appos
rm -f "$work/$(awk '$1=="appos"{print $2}' components.txt)"
sudo rm -rf "$work/appos"

# --- 4) dyld shared cache + cryptex fallbacks -------------------------------
dsc=private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld
sudo mkdir -p "$work/root/System/Library/Caches/com.apple.dyld"
for f in "$work/root/$dsc"/*; do
    sudo ln "$f" "$work/root/System/Library/Caches/com.apple.dyld/${f##*/}"
done
for c in OS App; do
    (cd "$work/root/private/preboot/Cryptexes/$c" && sudo find . ! -type d ! -path './System/Library/Caches/com.apple.dyld/*') |
    while IFS= read -r f; do
        f=${f#./}
        if ! sudo test -e "$work/root/$f" && ! sudo test -L "$work/root/$f"; then
            sudo mkdir -p "$work/root/$(dirname "$f")"
            src="$work/root/private/preboot/Cryptexes/$c/$f"
            if sudo test -L "$src"; then sudo cp -P "$src" "$work/root/$f"; else sudo ln "$src" "$work/root/$f"; fi
        fi
    done
done

# --- 5) logd migration markers + system keybag ------------------------------
db="$work/root/private/var/db"
sudo install -d -o 272 -g 272 -m 0751 "$db/diagnostics"
sudo install -d -o 272 -g 272 -m 0755 "$db/uuidtext" "$db/diagnostics/timesync"
for d in diagnostics uuidtext diagnostics/timesync; do
    sudo install -o 272 -g 272 -m 0644 /dev/null "$db/$d/.migrated"
done
sudo install -d -m 0700 -o 0 -g 0 "$work/root/private/var/keybags"
printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>' \
    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">' \
    '<plist version="1.0"><dict><key>KeyBagKeys</key><data>U1dFRVQ=</data></dict></plist>' |
    sudo install -m 0600 -o 0 -g 0 /dev/stdin "$work/root/private/var/keybags/systembag.kb"

df -h "$work/root"
sudo umount "$work/root"
fsck.hfsplus -f -y "$out" | grep -v 'Illegal name' || true
fsck.hfsplus -f -n "$out"
ls -la "$out"
