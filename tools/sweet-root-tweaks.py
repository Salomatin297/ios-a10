#!/usr/bin/env python3
"""Apply the SWEET bring-up changes to an iOS root made by tools/mkiosroot.sh.

  sudo sweet-root-tweaks.py MOUNTED_ROOT

MOUNTED_ROOT is the HFS+ image mounted read-write (Linux: run
`fsck.hfsplus -fy` on the image, then `mount -t hfsplus -o loop,force`).
Run as root (it writes root/mobile owned files). Idempotent. This is what
turned the plain mkiosroot.sh output into the images that reach the home
screen (image versions v8-v14 in NOTES.md):

  launchd.plist   _PanicOnCrash -> _PanicOnCrasX (a crashing daemon must not
                  panic the system); CommCenter jobs removed (no baseband: it
                  crash-loops and Setup blocks on its CoreTelephony XPC)
  data volume     /private/var/mobile (501), its Library/Preferences, the
                  containers tree and /private/var/{root,tmp}; an image built
                  from the IPSW alone (mkiosroot-staged.sh) has none of these
  containers      /private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}
                  (on iOS they live on the data volume)
  migration       LastSystemVersion so DataMigration does not run for an hour
                  on every boot
  audio           VirtualAudio HAL plug-in disabled (mediaserverd crash)
  MediaToolbox    StartRecorderServer=false (camera filter prewarm calls a
                  NULL OpenGL function without a GPU -> mediaserverd crash loop)
  SpringBoard     BKNoWatchdogs (one slow CPU path, no scene-create watchdog
                  kills), no cloud-configuration UI, auto-dim/idle sleep off
  QuartzCore      framebuffer/IOSurface compression off (software rendering,
                  no GPU), as in UniXNU's Data seed
  Setup           skipped: SetupDone, SetupFinishedAllSteps, SetupVersion 11,
                  mini-buddy flags off
  activation      mobileactivationd data ark seeded; the actual Activated state
                  comes from its Hactivation branch, selected by the /chosen
                  security-status properties in tools/mkadt.py
"""
import glob, os, plistlib, shutil, sys

BUILD, VERSION = '20H392', '16.7.16'
MOBILE = 501


def load(path):
    with open(path, 'rb') as f:
        return plistlib.load(f)


def save(path, data, uid, gid, mode, fmt=plistlib.FMT_BINARY):
    with open(path, 'wb') as f:
        plistlib.dump(data, f, fmt=fmt)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def merge_pref(root, name, values):
    """Merge keys into /var/mobile/Library/Preferences/NAME.plist (mobile, 0600)."""
    path = os.path.join(root, 'private/var/mobile/Library/Preferences', name + '.plist')
    data = load(path) if os.path.exists(path) else {}
    data.update(values)
    save(path, data, MOBILE, MOBILE, 0o600)
    print('pref', name, sorted(values))


def ensure(root, rel, uid, gid, mode):
    """Create REL under ROOT and force its ownership/mode (idempotent)."""
    path = os.path.join(root, rel)
    if not os.path.isdir(path):
        os.makedirs(path)
        print('mkdir', rel)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def data_volume(root):
    """Create the data-volume skeleton that a restore would have made.

    mkiosroot.sh copies the OS volume plus the cryptexes; on a real device
    /private/var is the data volume, so the home directory, the container
    roots and /private/var/tmp are missing.  launchd starts mobile daemons
    with HOME=/private/var/mobile, so it has to exist (501:501) before the
    first boot.
    """
    ensure(root, 'private/var', 0, 0, 0o755)
    ensure(root, 'private/var/mobile', MOBILE, MOBILE, 0o755)
    for sub in ('Library', 'Library/Preferences', 'Library/Preferences/ByHost',
                'Library/Caches', 'Library/Logs', 'Media', 'Documents',
                'Downloads', 'Containers', 'tmp'):
        ensure(root, 'private/var/mobile/' + sub, MOBILE, MOBILE, 0o755)
    ensure(root, 'private/var/root', 0, 0, 0o700)
    ensure(root, 'private/var/tmp', 0, 0, 0o1777)
    for sub in ('run', 'Library', 'PersonaVolumes', 'preboot'):
        ensure(root, 'private/var/' + sub, 0, 0, 0o755)
    ensure(root, 'private/var/keybags', 0, 0, 0o700)
    ensure(root, 'private/var/keybags/backup', 0, 0, 0o700)


def launchd(root):
    path = os.path.join(root, 'System/Library/xpc/launchd.plist')
    st = os.stat(path)
    plist = load(path)
    jobs = plist['LaunchDaemons']
    renamed = 0
    for job in jobs.values():
        if '_PanicOnCrash' in job:
            job['_PanicOnCrasX'] = job.pop('_PanicOnCrash')
            renamed += 1
    removed = [k for k, j in jobs.items() if j.get('Label') == 'com.apple.CommCenter']
    for k in removed:
        del jobs[k]
    save(path, plist, st.st_uid, st.st_gid, st.st_mode & 0o7777)
    print(f'launchd.plist: {renamed} _PanicOnCrash renamed, {len(removed)} CommCenter jobs removed')


def containers(root):
    # containermanagerd creates the per-app containers on demand; it only
    # needs these roots to exist (image v9 on sweet booted with exactly this).
    for sub in ('Bundle', 'Data/System', 'Shared/SystemGroup', 'Temp'):
        ensure(root, 'private/var/containers/' + sub, 0, 0, 0o755)


def find_container(root, kind, identifier):
    for meta in glob.glob(os.path.join(root, 'private/var/containers', kind, '*',
                                       '.com.apple.mobile_container_manager.metadata.plist')):
        if load(meta).get('MCMMetadataIdentifier') == identifier:
            return os.path.dirname(meta)
    return None


def activation(root):
    c = find_container(root, 'Data/System', 'com.apple.mobileactivationd')
    if c is None:
        print('warning: no mobileactivationd system container in the image; skipped data ark')
        return
    d = os.path.join(c, 'Library/internal')
    os.makedirs(d, exist_ok=True)
    os.chown(d, MOBILE, MOBILE)
    os.chmod(d, 0o755)
    save(os.path.join(d, 'data_ark.plist'), {
        '-ActivationState': 'Activated', '-BrickState': False,
        '-BuildVersion': BUILD, '-ProductVersion': VERSION,
        '-DataMigrationCompleted': True, '-PreSydroDataMigrationCompleted': True,
    }, MOBILE, MOBILE, 0o600)
    print('data ark', d)


def audio(root):
    src = os.path.join(root, 'Library/Audio/Plug-Ins/HAL/VirtualAudio.plugin')
    dst_dir = os.path.join(root, 'Library/Audio/Plug-Ins/HAL.disabled')
    if os.path.exists(src):
        os.makedirs(dst_dir, exist_ok=True)
        shutil.move(src, dst_dir)
        print('VirtualAudio.plugin disabled')
    for conf in glob.glob(os.path.join(root, 'System/Library/Frameworks/MediaToolbox.framework/*/SubServerConfiguration.plist')):
        st = os.stat(conf)
        data = load(conf)
        data['StartRecorderServer'] = False
        save(conf, data, st.st_uid, st.st_gid, st.st_mode & 0o7777, plistlib.FMT_XML)
        print('StartRecorderServer off', os.path.relpath(conf, root))


def main():
    if len(sys.argv) != 2 or not os.path.isdir(os.path.join(sys.argv[1], 'System/Library/xpc')):
        sys.exit(__doc__)
    root = sys.argv[1]
    data_volume(root)
    launchd(root)
    containers(root)
    activation(root)
    audio(root)
    merge_pref(root, 'com.apple.migration', {
        'LastSystemVersion': BUILD,
        'DMUserDataDisposition': {'SyntheticDidUpgrade': False, 'buildVersion': BUILD}})
    merge_pref(root, 'com.apple.springboard', {
        'BKNoWatchdogs': True,
        'FBLaunchWatchdogExceptions': {'com.apple.purplebuddy': 100.0},
        'MCForceShowCloudConfigurationUI': False,
        # No touch panel and no idle timer: keep the panel on instead of
        # dimming and locking ~30 s after SpringBoard starts (UniXNU).
        'SBDisableAutoDim': True,
        'SBDontDimOrLockOnAC': True,
        'SBDisableIdleSleep': True})
    # Software rendering without a GPU: keep CoreAnimation off the extra
    # framebuffer/compression paths (UniXNU's Data seed does the same).
    for domain in ('backboardd', '.GlobalPreferences'):
        merge_pref(root, domain, {
            'CAEnableDeepFramebuffer': False,
            'CAEnableFramebufferCompression': False,
            'CAEnableIOSurfaceCompression': False})
    merge_pref(root, 'com.apple.purplebuddy', {
        'SetupDone': True, 'SetupFinishedAllSteps': True, 'SetupVersion': 11,
        'daemonizeNeedsToRun': False})
    merge_pref(root, 'com.apple.purplebuddy.notbackedup', {
        k: False for k in ('ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy',
                           'ShouldRunKeychainSyncMiniBuddy', 'ShouldRunMesaMiniBuddy',
                           'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
                           'SilentICDPUpgrade')})
    merge_pref(root, 'com.apple.managedconfiguration.notbackedup', {
        'LockdownActivationIndicatesCloudConfigurationAvailable': False})


if __name__ == '__main__':
    main()
