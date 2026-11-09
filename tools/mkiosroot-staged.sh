#!/bin/bash
# Build the SWEET iOS root HFS+ image from IPSW components on macOS.
# Staging keeps the peak disk usage low: each APFS component is downloaded,
# mounted, copied and deleted before the next one is fetched.
#
#   mkiosroot-staged.sh WORKDIR IPSW_URL OUT.img [SIZE]
#
# Requires macOS hdiutil/newfs_hfs (no apfs-fuse, no hfsprogs).  The APFS
# components and the HFS+ output are both mounted with "-owners on": without
# it macOS mounts them as the build user and every uid/gid would be lost.
set -euo pipefail

[[ $# -ge 3 ]] || { sed -n '2,13p' "$0"; exit 2; }

abspath() { python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1"; }
work=$(abspath "$1"); ipsw="$2"; out=$(abspath "$3"); size=${4:-12G}
size=$(printf '%s' "$size" | tr '[:upper:]' '[:lower:]')   # mkfile wants 12g
[[ ! -e "$out" ]] || { echo "refusing to overwrite $out" >&2; exit 1; }

repo_root=$(cd "$(dirname "$0")/.." && pwd)
remotezip="$repo_root/tools/remotezip.py"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 1; }; }
need hdiutil; need newfs_hfs; need rsync; need python3
RSYNC=$(command -v rsync)
NEWFS_HFS=$(command -v newfs_hfs)

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
OS_DMG=$(awk '$1=="os"{print $2}' components.txt)
SYSOS_DMG=$(awk '$1=="sysos"{print $2}' components.txt)
APPOS_DMG=$(awk '$1=="appos"{print $2}' components.txt)

get_component() { # dmgspec
    if [ ! -f "$work/$1" ]; then
        echo "fetching $1 ..."
        python3 "$remotezip" "$ipsw" "$1" "$work"
    fi
}

# --- the HFS+ output --------------------------------------------------------
# HFSX (-s) matches iOS: the root volume is case-sensitive.
mkfile -n "$size" "$out"
dev=$(hdiutil attach -owners on -imagekey diskimage-class=CRawDiskImage -nomount "$out" |
      awk 'NR==1{print $1}')
echo "newfs_hfs on $dev"
sudo "$NEWFS_HFS" -s -J -v iOSRoot "$dev"
hdiutil detach "$dev"

mkdir -p "$work/root"
hdiutil attach -owners on -imagekey diskimage-class=CRawDiskImage -mountpoint "$work/root" "$out"
mount | grep -i 'hfs' || true

copy_into() { # src dst
    sudo mkdir -p "$2"
    sudo "$RSYNC" -aH --numeric-ids "$1/" "$2/"
}

apfs_attach() { # dmg mountpoint
    mkdir -p "$2"
    hdiutil attach -owners on -nobrowse -readonly -mountpoint "$2" "$1" >/dev/null
}
apfs_detach() { # mountpoint
    hdiutil detach "$1" >/dev/null
}

# --- 1) the OS volume -------------------------------------------------------
get_component "$OS_DMG"
apfs_attach "$work/$OS_DMG" "$work/mnt-os"
echo "--- os top level ---"
ls -la "$work/mnt-os" | head -20
copy_into "$work/mnt-os" "$work/root"
apfs_detach "$work/mnt-os"
rm -f "$work/$OS_DMG"
df -h "$work"

# --- 2) the SystemOS cryptex ------------------------------------------------
get_component "$SYSOS_DMG"
apfs_attach "$work/$SYSOS_DMG" "$work/mnt-sysos"
echo "--- sysos top level ---"
ls -la "$work/mnt-sysos" | head -20
copy_into "$work/mnt-sysos" "$work/root/private/preboot/Cryptexes/OS"
apfs_detach "$work/mnt-sysos"
rm -f "$work/$SYSOS_DMG"
df -h "$work"

# --- 3) the AppOS cryptex ---------------------------------------------------
get_component "$APPOS_DMG"
apfs_attach "$work/$APPOS_DMG" "$work/mnt-appos"
echo "--- appos top level ---"
ls -la "$work/mnt-appos" | head -20
copy_into "$work/mnt-appos" "$work/root/private/preboot/Cryptexes/App"
apfs_detach "$work/mnt-appos"
rm -f "$work/$APPOS_DMG"
df -h "$work"

# --- 4) dyld shared cache + cryptex fallbacks -------------------------------
dsc=private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld
sudo mkdir -p "$work/root/System/Library/Caches/com.apple.dyld"
for f in "$work/root/$dsc"/*; do
    [ -f "$f" ] || continue
    base=${f##*/}
    [ -e "$work/root/System/Library/Caches/com.apple.dyld/$base" ] && continue
    sudo ln "$f" "$work/root/System/Library/Caches/com.apple.dyld/$base"
done
for c in OS App; do
    (cd "$work/root/private/preboot/Cryptexes/$c" &&
     find . ! -type d ! -path './System/Library/Caches/com.apple.dyld/*') |
    while IFS= read -r f; do
        f=${f#./}
        [ -e "$work/root/$f" ] && continue
        [ -L "$work/root/$f" ] && continue
        sudo mkdir -p "$work/root/$(dirname "$f")"
        src="$work/root/private/preboot/Cryptexes/$c/$f"
        if [ -L "$src" ]; then
            sudo cp -P "$src" "$work/root/$f"
        else
            sudo ln "$src" "$work/root/$f"
        fi
    done
done

# --- 5) logd migration markers + system keybag ------------------------------
db="$work/root/private/var/db"
sudo install -d -m 0751 "$db/diagnostics"
sudo install -d -m 0755 "$db/uuidtext" "$db/diagnostics/timesync"
sudo chown 272:272 "$db/diagnostics" "$db/uuidtext" "$db/diagnostics/timesync"
for d in diagnostics uuidtext diagnostics/timesync; do
    sudo install -m 0644 /dev/null "$db/$d/.migrated"
    sudo chown 272:272 "$db/$d/.migrated"
done
sudo install -d -m 0700 "$work/root/private/var/keybags"
sudo chown 0:0 "$work/root/private/var/keybags"
cat > "$work/systembag.kb" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>KeyBagKeys</key><data>U1dFRVQ=</data></dict></plist>
EOF
sudo install -m 0600 "$work/systembag.kb" "$work/root/private/var/keybags/systembag.kb"
sudo chown 0:0 "$work/root/private/var/keybags/systembag.kb"
rm -f "$work/systembag.kb"

# --- 6) SWEET bring-up tweaks (data-volume skeleton, Setup skip, prefs) -----
sudo python3 "$repo_root/tools/sweet-root-tweaks.py" "$work/root"
echo "--- private/var after tweaks ---"
sudo ls -la "$work/root/private/var" | head -30
sudo stat -f '%u:%g %Sp %N' "$work/root/private/var/mobile" \
    "$work/root/private/var/mobile/Library/Preferences"

df -h "$work/root"
sudo du -sh "$work/root"
sync
hdiutil detach "$work/root"

# --- fsck the raw image -----------------------------------------------------
dev=$(hdiutil attach -owners on -imagekey diskimage-class=CRawDiskImage -nomount "$out" |
      awk 'NR==1{print $1}')
sudo fsck_hfs -f -y "$dev" || true
sudo fsck_hfs -f -n "$dev"
hdiutil detach "$dev"
ls -la "$out"
