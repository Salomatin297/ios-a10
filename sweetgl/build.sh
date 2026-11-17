#!/bin/sh
# A10 sweetgl: build sweetgl.dylib (Mesa panfrost + GLES + EAGL shim for
# CoreAnimation) for iOS 16 arm64, on macOS with Xcode.
#
#   build.sh MESA_DIR OUT_DIR
#
# MESA_DIR is mesa-26.2.4 after mesa-fixup.py; mesa_clc, vtn_bindgen2 and
# panfrost_compile (host tools, built first by the workflow) must be in PATH.
# The dylib is ad-hoc signed and zero-padded to 64 MiB, so the file on the
# phone keeps one size and can be rewritten in place later.
#
# Sources are the already-adapted tree in this directory (derived from
# joranv1/ios-sailfish tools/sweetgl, pinned 6f27e41; see README.md).
set -e
H=$(cd "$(dirname "$0")" && pwd)
M=$(cd "$1" && pwd)
mkdir -p "$2"
O=$(cd "$2" && pwd)
B=$O/mesa-ios

SDK=$(xcrun -sdk iphoneos --show-sdk-path)
CC=$(xcrun -sdk iphoneos -f clang)
CXX=$(xcrun -sdk iphoneos -f clang++)
AR=$(xcrun -sdk iphoneos -f ar)
STRIP=$(xcrun -sdk iphoneos -f strip)
cat > "$O/ios-cross.txt" <<EOF
[binaries]
c = ['$CC', '-arch', 'arm64', '-isysroot', '$SDK', '-miphoneos-version-min=16.0']
cpp = ['$CXX', '-arch', 'arm64', '-isysroot', '$SDK', '-miphoneos-version-min=16.0']
objc = ['$CC', '-arch', 'arm64', '-isysroot', '$SDK', '-miphoneos-version-min=16.0']
ar = '$AR'
strip = '$STRIP'
pkg-config = 'false'

[built-in options]
c_args = ['-I$H/include']
cpp_args = ['-I$H/include']

[host_machine]
system = 'darwin'
subsystem = 'ios'
cpu_family = 'aarch64'
cpu = 'arm64'
endian = 'little'
EOF

if [ ! -f "$B/build.ninja" ]; then
	meson setup "$B" "$M" --cross-file "$O/ios-cross.txt" \
		--buildtype=release -Db_ndebug=true -Ddefault_library=static \
		-Dplatforms= -Degl=disabled -Dglx=disabled -Dgbm=disabled \
		-Dgallium-drivers=panfrost -Dvulkan-drivers= \
		-Dllvm=disabled -Dgles1=disabled -Dgles2=enabled -Dopengl=true \
		-Dzstd=disabled -Dzlib=disabled -Dexpat=disabled -Dxmlconfig=disabled \
		-Dvalgrind=disabled -Dlibunwind=disabled -Dlmsensors=disabled \
		-Dbuild-tests=false -Dshader-cache=disabled -Dperfetto=false \
		-Dgallium-va=disabled -Dgallium-rusticl=false -Dtools= \
		-Dhtml-docs=disabled -Ddisplay-info=disabled -Dteflon=false \
		-Dmesa-clc=system -Dprecomp-compiler=system
fi
# no Mesa frontend is enabled, so the default targets skip libmesa, glapi
# and the state tracker: build every static library instead
ninja -C "$B" $(ninja -C "$B" -t targets all | grep -o '^src/[^:]*[.]a' | sort -u)

# every static library of the build; the linker takes what it needs
LIBS=$(find "$B/src" -name '*.a' | sort)
echo "$LIBS" > "$O/libs.txt"
BRIDGE=$(find "$B/src" -name 'libglapi_bridge.a' | head -1)
[ -n "$BRIDGE" ] || { echo "no libglapi_bridge.a in the Mesa build"; exit 1; }
nm -g "$BRIDGE" > "$O/bridge.nm"

# Mesa's configure-time defines (HAVE_PTHREAD, ...) as the state tracker gets them
python3 -I -c '
import json, shlex, sys
cc = json.load(open(sys.argv[1]))
e = [x for x in cc if x["file"].endswith("state_tracker/st_manager.c")][0]
print("\n".join(shlex.quote(a) for a in shlex.split(e["command"]) if a.startswith("-D")))
' "$B/compile_commands.json" > "$O/mesa-defines.rsp"
python3 -I "$H/gen-dispatch.py" "$H/ios-dispatch.txt" "$M/include" "$O/bridge.nm" "$O/dispatch.c"

XCC="$CC -arch arm64 -isysroot $SDK -miphoneos-version-min=16.0"
INC="-I$H/include -I$H -I$O -I$M/include -I$M/src -I$M/src/mesa -I$M/src/mapi
	-I$M/src/gallium/include -I$M/src/gallium/auxiliary -I$M/src/gallium/drivers
	-I$M/src/gallium/winsys -I$B/src -I$B/src/mesa -I$B/src/mapi"
for f in "$H/sweetgl" "$H/eagl" "$H/layerdump" "$O/dispatch" "$H/sweet_drm_pan"; do
	$XCC -O2 -Wall -fvisibility=hidden @"$O/mesa-defines.rsp" $INC -c "$f.c" -o "$O/$(basename "$f").o"
done
printf '_sweetgl_init\n_sweetgl_create\n_sweetgl_make_current\n_sweetgl_destroy\n_sweetgl_selftest\n' > "$O/exports.txt"
$CXX -arch arm64 -isysroot "$SDK" -miphoneos-version-min=16.0 -dynamiclib -dead_strip \
	-install_name /usr/lib/sweetgl.dylib -exported_symbols_list "$O/exports.txt" \
	-o "$O/sweetgl.dylib" "$O/sweetgl.o" "$O/sweet_drm_pan.o" "$O/eagl.o" "$O/layerdump.o" "$O/dispatch.o" \
	$LIBS \
	-framework IOKit -framework CoreFoundation -framework IOSurface -framework OpenGLES \
	-framework CoreGraphics -lobjc -lc++
codesign -f -s - "$O/sweetgl.dylib"
SIZE=$(stat -f %z "$O/sweetgl.dylib")
[ "$SIZE" -le 67108864 ] || { echo "sweetgl.dylib is $SIZE bytes, over 64 MiB"; exit 1; }
cp "$O/sweetgl.dylib" "$O/sweetgl-unpadded.dylib"
python3 -I -c 'import sys; open(sys.argv[1], "r+b").truncate(64 << 20)' "$O/sweetgl.dylib"
echo "built $O/sweetgl.dylib ($SIZE bytes, padded to 64 MiB)"
