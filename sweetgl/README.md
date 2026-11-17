# sweetgl — Mesa GLES on the A10's Mali-G71

`sweetgl.dylib` is Mesa's panfrost Gallium driver plus a GLES frontend and an
EAGL shim, built as an iOS 16 arm64 library. Loaded into `backboardd` with
`DYLD_INSERT_LIBRARIES`, it replaces Apple's software GLES renderer that
CoreAnimation otherwise falls back to, so QuartzCore's GL renderer
(`CA::OGL::GLESContext`) draws the UI on the GPU.

The A10 has no DRM and no dma-buf. `sweet_drm_pan.c` answers the panfrost DRM
calls in user space over the `SweetMali` IOKit user client from xnu patch
0122:

| DRM entry point | SweetMali |
| --- | --- |
| `DRM_IOCTL_PANFROST_CREATE_BO` | selector 10 `[size, flags]` → `[handle, gpu va, user va, size]` |
| `DRM_IOCTL_PANFROST_MMAP_BO` | offset = `handle << 32`, mapped by the kernel client |
| `DRM_IOCTL_GEM_CLOSE` | selector 11 |
| `DRM_IOCTL_PANFROST_SUBMIT` | selector 17 (job queue) or 13 (synchronous) |
| `DRM_IOCTL_PANFROST_WAIT_BO` | selector 18 `[seq, block]` |
| `DRM_IOCTL_PANFROST_GET_PARAM` | selector 15 |
| IOSurface import | selector 12; exposed to Mesa as a prime fd (a dup of `/dev/null`) |
| syncobjs / sync files | sequence numbers of the job that signals them |

Mesa's tiler heap is cut to 32 MiB and BO mappings are the kernel client's
`createMappingInTask` mappings, never `munmap`ed (see `mesa-fixup.py`).

## Derivation

* `sweetgl.c`, `eagl.c`, `eagl.h`, `layerdump.c`, `gen-dispatch.py`,
  `ios-dispatch.txt`: from [joranv1/ios-sailfish](https://github.com/joranv1/ios-sailfish)
  `tools/sweetgl`, pinned at `6f27e41ff249f2680540ebe8ee5ab982c5e641c9`
  (MIT). Changed for panfrost: panfrost includes and screen creation,
  `PAN_MESA_DEBUG=nofp16`, `MESA_GLES_VERSION_OVERRIDE=3.1`, `get_param` /
  `set_background_context` hooks (Mesa 26.2's `st_manager` calls them),
  `glTextureBarrier` → `glFlush`, and IOSurfaces imported as prime fds
  (`WINSYS_HANDLE_TYPE_FD`) instead of KMS handles.
* `sweet_drm_pan.c`, `sweet_drm.h`: the A510F panfrost DRM emulation, adapted
  for the A10 (same `SweetMali` selector ABI).
* `include/xf86drm.h`, `include/sys/eventfd.h`, `include/sys/timerfd.h`:
  the libdrm/Linux subset Mesa's panfrost references.

## Build

Needs macOS with Xcode, Meson/Ninja, Homebrew LLVM, Bison/Flex, Python with
Mako, and Mesa's host tools (`mesa_clc`, `vtn_bindgen2`, `panfrost_compile`).

```sh
git clone --depth 1 -b mesa-26.2.4 https://gitlab.freedesktop.org/mesa/mesa.git mesa
python3 sweetgl/mesa-fixup.py mesa
sh sweetgl/build.sh mesa out
```

The workflow `.github/workflows/sweetgl.yml` does the same on `macos-15` and
uploads `out/sweetgl.dylib` (padded to 64 MiB) and
`out/sweetgl-unpadded.dylib` as `a10-sweetgl`.

## Delivery

The HFSX root is full, so the padded dylib is written in place over the unused
`/usr/standalone/update/ramdisk/arm64SURamDisk.dmg` (one extent; the padded
library fits inside it). Keep the file size; the Mach-O header stays at offset 0
and dyld ignores the zero padding. Then boot with:

```
sweetenv=backboardd:DYLD_INSERT_LIBRARIES=/usr/standalone/update/ramdisk/arm64SURamDisk.dmg,SWEETGL_TEST=1
```

`SWEETGL_TEST=1` runs an offscreen triangle at load and logs
`MESA GLES ON THE MALI WORKS` (or `WRONG`) plus `GL_VERSION` /
`GL_RENDERER` / `GLSL`. Other switches:

| variable | effect |
| --- | --- |
| `SWEETGL_TRACE=N` | log draws/shaders (expensive) |
| `SWEETGL_GLERR=1` | check `glGetError` after every call |
| `SWEETGL_SYNC=1` | synchronous submits (no kernel job queue) |
| `SWEETGL_WAITBO_BLOCK=1` | blocking `WAIT_BO` polls |
| `SWEETGL_LAYERDUMP=1` | SpringBoard only: layer dump instead of the shim |

The kernel side is xnu patch 0122 (`SweetMali`), 0123 (`sweetenv`) and 0159
(AMFI dyld policy). Without `sweetmali=1` the shim cannot open the service and
Mesa screen creation fails; without 0159 dyld ignores `DYLD_INSERT_LIBRARIES`.
