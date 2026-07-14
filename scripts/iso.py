"""ISO assembly for BuildSystem.

Mixin: `_IsoMixin` adds `build_iso` to BuildSystem.  Reads
`self._dir_chroot`, `self._dir_image`, `self._dir_log`,
`self._password`, and `self._config` (for codename / version) — all
provided by `BuildSystem`.
"""

import glob
import logging
import os
import secrets
import shutil
import subprocess
from typing import TYPE_CHECKING

import tui
import utils

if TYPE_CHECKING:
    from utils import BuildConfig

logger = logging.getLogger('athena.iso')


# Common English first names used as the random live-user account.
# `secrets.choice` picks one at every ISO build so the username is
# not predictable across releases.  Lowercase only — Debian useradd
# rejects mixed case for portability.  Curated list, not a dictionary
# import, so the namespace stays small and readable.
_LIVE_USERNAMES = [
    'agatha',  'alan',     'alice',    'amelia',   'arthur',
    'beatrice','benjamin', 'blake',    'caroline', 'charlie',
    'chloe',   'daisy',    'david',    'dorian',   'edward',
    'eleanor', 'emma',     'felix',    'finn',     'fiona',
    'george',  'grace',    'gregor',   'harriet',  'henry',
    'hugo',    'iris',     'isaac',    'james',    'jane',
    'julian',  'kate',     'kevin',    'laura',    'leon',
    'linus',   'lucy',     'maggie',   'marcus',   'max',
    'megan',   'milo',     'nathan',   'nora',     'oliver',
    'ophelia', 'owen',     'patrick',  'penny',    'philip',
    'quentin', 'quinn',    'rachel',   'rufus',    'ruby',
    'sam',     'sarah',    'simon',    'sophie',   'thomas',
    'theodore','victor',   'violet',   'walter',   'william',
    'yvonne',  'zara',
]


class _IsoMixin:
    # Instance attributes set by `BuildSystem.__init__` (the composer
    # that mixes this in).  Type-only stubs for mypy; no runtime
    # assignment here.
    _dir_chroot: str
    _dir_image: str
    _dir_log: str
    _password: str
    _config: 'BuildConfig'

    def build_iso(self, container=None) -> bool:
        """Create a bootable hybrid BIOS/EFI ISO from the assembled chroot.

        Steps:
          1. Locate the latest kernel (vmlinuz-*) and initramfs (initrd.img-*)
             installed in chroot/boot/ by the linux-image package.
          2. Create a staging tree under dir_image/staging/.
          3. Copy kernel and initramfs into staging/boot/.
          4. Write staging/boot/grub/grub.cfg configured for live-boot — the
             'boot=live' parameter tells live-boot to mount the squashfs as root
             with an overlayfs for writes.
          5. Create staging/live/filesystem.squashfs from the chroot via
             mksquashfs, excluding runtime virtual directories (proc, sys, dev,
             run, tmp) that live-boot mounts fresh at boot.
          6. Run grub-mkrescue to produce a hybrid BIOS+EFI ISO.

        Subprocess transcripts for mksquashfs and grub-mkrescue are
        routed through ``logger.debug``, captured by the file handler
        attached in build.main() — see the unified run log under
        dir_log/build-<timestamp>.log.

        Returns:
            True on success, False if any step fails.
        """
        logger.info(
            f"build_iso (live): chroot={self._dir_chroot} → {self._dir_image}/"
        )
        # ── Step 1: locate kernel and initramfs ───────────────────────────────
        logger.info("step 1/6: locate kernel + initramfs in chroot/boot/")
        _boot = os.path.join(self._dir_chroot, 'boot')
        # version-aware selection + initrd paired to the chosen
        # kernel by suffix (a lexicographic sorted()[-1] picks 6.1.0-9 over
        # 6.1.0-47, and independent vmlinuz/initrd sorts can mismatch).
        _pair = utils.select_latest_kernel(_boot)
        if _pair is None:
            if not glob.glob(os.path.join(_boot, 'vmlinuz-*')):
                tui.console.print("ERROR: no kernel found in chroot/boot/ — is linux-image installed?")
                logger.error("build_iso: no vmlinuz-* in chroot/boot/")
            else:
                tui.console.print("ERROR: no initramfs matching the latest kernel in chroot/boot/ — is initramfs-tools installed?")
                logger.error("build_iso: no paired initrd.img-* for the latest vmlinuz in chroot/boot/")
            return False
        _kernel = os.path.join(_boot, _pair[0])
        _initrd = os.path.join(_boot, _pair[1])
        tui.console.print(f"Kernel  : {os.path.basename(_kernel)}")
        tui.console.print(f"Initrd  : {os.path.basename(_initrd)}")

        # ── Step 2: create staging tree ───────────────────────────────────────
        logger.info("step 2/6: create staging/{boot,boot/grub,live}/")
        _staging      = os.path.join(self._dir_image, 'staging')
        _staging_boot = os.path.join(_staging, 'boot')
        _staging_grub = os.path.join(_staging, 'boot', 'grub')
        _staging_live = os.path.join(_staging, 'live')

        # wipe the staging tree first so an asset removed since the
        # last run (a dropped background/test file) doesn't ship forever —
        # grub-mkrescue images the WHOLE tree.  sudo: a prior run's
        # live/filesystem.squashfs is root-owned (mksquashfs ran as root).
        # Mirrors iso_installer._prepare_staging.
        if os.path.isdir(_staging):
            _wipe = subprocess.run(
                ['sudo', '-S', 'rm', '-rf', _staging],
                input=self._password + '\n', capture_output=True, text=True)
            if _wipe.returncode != 0:
                tui.console.print(
                    f"ERROR: failed to wipe staging {_staging} — "
                    "stale files may ship")
                logger.error(
                    f"build_iso: wipe staging rc={_wipe.returncode}: "
                    f"{_wipe.stderr.strip()[:200]}")
                return False

        for _d in [_staging_boot, _staging_grub, _staging_live]:
            os.makedirs(_d, exist_ok=True)

        # ── Step 3: copy kernel and initramfs ─────────────────────────────────
        logger.info(
            f"step 3/6: copy {os.path.basename(_kernel)} + "
            f"{os.path.basename(_initrd)} → staging/boot/"
        )
        shutil.copy2(_kernel, os.path.join(_staging_boot, 'vmlinuz'))
        shutil.copy2(_initrd, os.path.join(_staging_boot, 'initrd.img'))
        tui.console.print("Kernel and initramfs copied to staging")

        # ── Step 4: write grub.cfg ────────────────────────────────────────────
        logger.info("step 4/6: write boot/grub/grub.cfg + stage background")
        # 'boot=live' is the live-boot trigger; live-boot locates the squashfs
        # under /live/filesystem.squashfs on the boot device and mounts it as
        # the root filesystem with overlayfs.
        cfg = self._config
        # build_distribution / build_version are already _strip_quotes'd when
        # BuildConfig loads them (utils.py), so use them directly.
        _name     = cfg.build_distribution
        _version  = cfg.build_version
        # Pick a random live-user name per build so SSH attackers do not
        # have a fixed `root` (or `user`) target — the username becomes
        # the first secret an attacker has to guess.  live-config (already
        # in the package set) creates the user at first boot from this
        # kernel cmdline arg.
        _live_user = secrets.choice(_LIVE_USERNAMES)
        # Graphics mode + Asgard background.  Mirrors installer/boot/grub.cfg
        # — same gated-loadfont pattern + insmod all_video to give gfxterm
        # a framebuffer to switch into + gfxmode=auto so firmware picks
        # a mode it actually has.  Pre-fix, this grub.cfg had no video
        # setup at all and GRUB's default-fallback ran with no
        # all_video loaded → "no suitable video mode found booting in
        # blind mode" → boot stalled.  Fixed 2026-05-22.
        _grub_cfg = (
            'if loadfont /boot/grub/fonts/unicode.pf2 ; then\n'
            '    set gfxmode=auto\n'
            '    insmod all_video\n'
            '    insmod gfxterm\n'
            '    insmod png\n'
            '    terminal_output gfxterm\n'
            '    background_image /boot/grub/grub-background.png\n'
            'fi\n'
            '\n'
            'set default=0\n'
            'set timeout=5\n'
            '\n'
            f'menuentry "{_name} {_version} (live as {_live_user})" {{\n'
            # boot=live   — triggers live-boot to find and mount the squashfs root
            # components  — tells live-boot to activate all its hook scripts
            # console=tty0 — ensures kernel messages go to the screen (visible in QEMU)
            #
            # NO nomodeset on the default entry: GNOME requires KMS — with
            # nomodeset there is no DRM device, gdm's Wayland AND Xorg
            # attempts both die and the screen just flashes (caught live
            # 2026-06-11, first GNOME boot). VMs get KMS via
            # virtio-gpu/qxl/vmwgfx/bochs-simpledrm; the era this flag
            # protected (console-only live image) is over.  The
            # safe-graphics entry below keeps nomodeset for hardware with
            # genuinely broken KMS — console boot only, no GNOME.
            f'    linux  /boot/vmlinuz boot=live components username={_live_user} console=tty0\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
            '\n'
            f'menuentry "{_name} {_version} (live, safe graphics — console only)" {{\n'
            f'    linux  /boot/vmlinuz boot=live components username={_live_user} console=tty0 nomodeset\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
            '\n'
            # live-media-check verifies the burned media against md5sum.txt
            # (written at staging root) before booting — catches a corrupt USB
            # write up front rather than failing confusingly later (MAT-08).
            f'menuentry "Check media and boot {_name} {_version}" {{\n'
            f'    linux  /boot/vmlinuz boot=live components live-media-check username={_live_user} console=tty0\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
        )
        with open(os.path.join(_staging_grub, 'grub.cfg'), 'w') as fh:
            fh.write(_grub_cfg)
        tui.console.print(f"grub.cfg written (live user: {_live_user})")

        # Stage the Asgard splash PNG alongside grub.cfg so the
        # background_image line above resolves at boot time.  Reuses
        # the same asset the installer ISO uses (Pattern B drop-in
        # static asset — see installer/boot/regenerate-bg.py for the
        # generator).  Missing source is non-fatal: GRUB's `if
        # loadfont` guard falls back to text mode and boot continues.
        _bg_src = os.path.join(
            self._config.working_dir, 'installer', 'boot',
            'grub-background.png',
        )
        if os.path.isfile(_bg_src):
            try:
                shutil.copy2(
                    _bg_src,
                    os.path.join(_staging_grub, 'grub-background.png'),
                )
                tui.console.print(
                    f"Boot asset: {_bg_src} → boot/grub/grub-background.png"
                )
            except OSError as e:
                logger.warning(f"copy grub-background.png: {e}")
        else:
            logger.info(
                f"grub-background.png not at {_bg_src} — boot menu "
                f"will render in text mode"
            )

        # ── Step 4b: identity-residue scan of the chroot root (MAT-03) ──────
        # The squashfs is opaque to every later audit (SEC-07 covered
        # text/config staging surfaces only; _audit_staged_iso can't see
        # inside the blob), so Debian identity residue in the BOOTED root
        # (os-release, debconf db, menus) would ship undetected.  Scan the
        # chroot tree BEFORE the expensive mksquashfs — reuses the staged-ISO
        # walker + the same allowlist (which carves out legally-retained
        # upstream attribution under usr/share/doc/).  Root-only-readable
        # files (shadow etc.) are skipped by the walker — they are
        # credentials, not identity surface.  Gated by [Audit] IdentityScan
        # like the staged-ISO scan.
        if getattr(self._config, 'audit_identity_scan', True):
            from iso_installer import _audit_staged_iso
            if not _audit_staged_iso(self._dir_chroot, self._dir_image):
                tui.console.print(
                    "ERROR: identity residue in the live chroot root — "
                    "fix or allowlist (audit/identity-allowlist) before the "
                    "squashfs ships it", tui.COLOR_ERROR)
                return False
        else:
            logger.warning(
                "build_iso: chroot identity scan SKIPPED via "
                "[Audit] IdentityScan = false — Debian residue can ship "
                "inside the squashfs without flagging")

        # ── Step 5: create squashfs ───────────────────────────────────────────
        logger.info(
            f"step 5/6: mksquashfs xz {self._dir_chroot} → staging/live/filesystem.squashfs"
        )
        # Runtime virtual directories (proc, sys, dev, run, tmp) must NOT be
        # excluded as directories — live-boot's initramfs bind-mounts /dev,
        # /proc, /sys, /run into the new root and needs those directories to
        # exist as mount points inside the squashfs.  We only exclude their
        # CONTENTS (which are empty after _umount_chroot_fs anyway) by passing
        # each entry inside the dir rather than the dir itself.
        # -noappend overwrites any previous squashfs.
        _squashfs   = os.path.join(_staging_live, 'filesystem.squashfs')

        # Collect any actual files inside the runtime dirs to exclude (normally
        # empty after unmounting, but defensive in case something was left).
        _exclude_args = []
        _used_glob = False
        for _d in ['proc', 'sys', 'dev', 'run', 'tmp']:
            _dir_path = os.path.join(self._dir_chroot, _d)
            if not os.path.isdir(_dir_path):
                continue
            try:
                for _entry in os.listdir(_dir_path):
                    _exclude_args += ['-e', os.path.join(_dir_path, _entry)]
            except PermissionError:
                # dev/* may have root-owned nodes — exclude the whole dir's
                # contents via a glob pattern as a fallback
                _exclude_args += ['-e', os.path.join(_dir_path, '*')]
                _used_glob = True

        # mksquashfs treats `-e dir/*` literally without -wildcards,
        # so the glob fallback above would match nothing and silently ship
        # the unlistable dir's contents.  Enable wildcards only when the
        # fallback fired (the flag governs all following -e patterns, which
        # are plain paths otherwise — harmless either way, but keep it tight).
        _cmd = (
            ['sudo', '-S', 'mksquashfs', self._dir_chroot, _squashfs,
             '-comp', 'xz', '-noappend']
            + (['-wildcards'] if _used_glob else [])
            + _exclude_args
        )
        # Subprocess transcript routed through logger.debug — the file
        # handler attached by setup_file_logging() captures it in the
        # unified run log, replacing the legacy mksquashfs.log file.
        # Spinner so the operator sees the long mksquashfs xz pass is
        # still going (typical: several minutes on a ~2 GB chroot).
        _spin = tui.Spinner(
            f"Creating squashfs (xz, {self._dir_chroot} → {_squashfs})"
        )
        try:
            _proc = subprocess.run(
                _cmd, input=self._password + '\n',
                capture_output=True, text=True
            )
        finally:
            _spin.done()
        for _line in _proc.stdout.splitlines():
            logger.debug(_line)
        for _line in _proc.stderr.splitlines():
            logger.debug(_line)

        if _proc.returncode != 0:
            tui.console.print("ERROR: mksquashfs failed — see unified run log")
            logger.error(f"build_iso: mksquashfs exited {_proc.returncode}")
            return False

        _sq_mb = os.path.getsize(_squashfs) // (2 ** 20)
        tui.console.print(f"squashfs created: {_sq_mb} MB")

        # ── Step 5.5: on-media integrity manifest ─────────────────────────────
        # Write md5sum.txt over the fully-staged tree (incl. the squashfs) so a
        # `live-media-check` boot can detect a corrupt USB/DVD write.  Best-
        # effort: a manifest failure must not block an otherwise-good ISO.
        if utils.write_iso_md5sum_manifest(_staging, self._password):
            tui.console.print("md5sum.txt written (live-media-check ready)")
        else:
            tui.console.print(
                "WARNING: md5sum.txt not written — live-media-check unavailable")

        # ── Step 6: run grub-mkrescue ─────────────────────────────────────────
        logger.info("step 6/6: grub-mkrescue (in build container) → ISO")
        # fix path (b): run grub-mkrescue inside the build
        # container so the ISO embeds bookworm's GRUB toolchain instead
        # of the host's.  Eliminates the "host runs trixie → ISO
        # bootloader self-reports 2.12-9+deb13u1" leakage path.
        # Tag the filename with the snapshot pin so ISOs built from different
        # snapshots are distinguishable.  Empty when snapshots off.
        _snap = utils.snapshot_iso_tag(cfg)
        _iso_name = (f"athena-{_version}-{_snap}-amd64.iso" if _snap
                     else f"athena-{_version}-amd64.iso")
        _iso_path   = os.path.join(self._dir_image, _iso_name)

        if container is None:
            tui.console.print(
                "ERROR: grub-mkrescue requires the build container "
                "(run `container local init` first)"
            )
            logger.error("build_iso: container is None")
            return False

        _spin = tui.Spinner(
            f"Running grub-mkrescue (bookworm container) → {_iso_name}"
        )
        try:
            _ok, _stdout, _stderr = container.run_grub_mkrescue(
                _staging, _iso_path, self._password,
            )
        finally:
            _spin.done()
        for _line in _stdout.splitlines():
            logger.debug(_line)
        for _line in _stderr.splitlines():
            logger.debug(_line)

        if not _ok:
            tui.console.print("ERROR: grub-mkrescue failed — see unified run log")
            _tail = _stderr.strip().splitlines()[-3:] if _stderr.strip() else []
            logger.error(f"build_iso: grub-mkrescue failed; stderr_tail={_tail}")
            return False

        _iso_mb = os.path.getsize(_iso_path) // (2 ** 20)
        tui.console.print(f"ISO built: {_iso_path} ({_iso_mb} MB)")

        # Sidecar file with the random live-user name.  The boot menu
        # entry shows it too, but the operator may need it before
        # booting (e.g. to write a kickstart on a separate machine);
        # one-line file at <iso>.user keeps it close to the ISO.
        _user_path = _iso_path + '.user'
        try:
            with open(_user_path, 'w') as fh:
                fh.write(_live_user + '\n')
            tui.console.print(f"Live user: {_live_user}  (also at {_user_path})")
        except OSError as e:
            logger.warning(f"Could not write live-user sidecar {_user_path}: {e}")

        return True
