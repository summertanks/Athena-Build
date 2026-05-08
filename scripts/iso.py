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

import tui

logger = logging.getLogger('athena')


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

    def build_iso(self) -> bool:
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
        # ── Step 1: locate kernel and initramfs ───────────────────────────────
        _boot = os.path.join(self._dir_chroot, 'boot')
        _kernels = sorted(glob.glob(os.path.join(_boot, 'vmlinuz-*')))
        _initrds = sorted(glob.glob(os.path.join(_boot, 'initrd.img-*')))

        if not _kernels:
            tui.console.print("ERROR: no kernel found in chroot/boot/ — is linux-image installed?")
            logger.error("build_iso: no vmlinuz-* in chroot/boot/")
            return False
        if not _initrds:
            tui.console.print("ERROR: no initramfs found in chroot/boot/ — is initramfs-tools installed?")
            logger.error("build_iso: no initrd.img-* in chroot/boot/")
            return False

        # Use the latest kernel version (highest sort order).
        _kernel = _kernels[-1]
        _initrd = _initrds[-1]
        tui.console.print(f"Kernel  : {os.path.basename(_kernel)}")
        tui.console.print(f"Initrd  : {os.path.basename(_initrd)}")

        # ── Step 2: create staging tree ───────────────────────────────────────
        _staging      = os.path.join(self._dir_image, 'staging')
        _staging_boot = os.path.join(_staging, 'boot')
        _staging_grub = os.path.join(_staging, 'boot', 'grub')
        _staging_live = os.path.join(_staging, 'live')

        for _d in [_staging_boot, _staging_grub, _staging_live]:
            os.makedirs(_d, exist_ok=True)

        # ── Step 3: copy kernel and initramfs ─────────────────────────────────
        shutil.copy2(_kernel, os.path.join(_staging_boot, 'vmlinuz'))
        shutil.copy2(_initrd, os.path.join(_staging_boot, 'initrd.img'))
        tui.console.print("Kernel and initramfs copied to staging")

        # ── Step 4: write grub.cfg ────────────────────────────────────────────
        # 'boot=live' is the live-boot trigger; live-boot locates the squashfs
        # under /live/filesystem.squashfs on the boot device and mounts it as
        # the root filesystem with overlayfs.
        cfg = self._config
        # Strip any stray quotes that may be embedded in the config values
        # (e.g. VERSION = "0.1" parsed with the surrounding quotes intact).
        _codename = cfg.build_codename.strip('"').strip("'")
        _version  = cfg.build_version.strip('"').strip("'")
        # Pick a random live-user name per build so SSH attackers do not
        # have a fixed `root` (or `user`) target — the username becomes
        # the first secret an attacker has to guess.  live-config (already
        # in the package set) creates the user at first boot from this
        # kernel cmdline arg.
        _live_user = secrets.choice(_LIVE_USERNAMES)
        _grub_cfg = (
            'set default=0\n'
            'set timeout=5\n'
            '\n'
            f'menuentry "{_codename} {_version} (live as {_live_user})" {{\n'
            # boot=live   — triggers live-boot to find and mount the squashfs root
            # components  — tells live-boot to activate all its hook scripts
            # console=tty0 — ensures kernel messages go to the screen (visible in QEMU)
            # nomodeset   — disables KMS; prevents blank/garbled screen in QEMU/VMs
            f'    linux  /boot/vmlinuz boot=live components username={_live_user} console=tty0 nomodeset\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
        )
        with open(os.path.join(_staging_grub, 'grub.cfg'), 'w') as fh:
            fh.write(_grub_cfg)
        tui.console.print(f"grub.cfg written (live user: {_live_user})")

        # ── Step 5: create squashfs ───────────────────────────────────────────
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

        tui.console.print("Creating squashfs — this may take several minutes...")
        _cmd = (
            ['sudo', '-S', 'mksquashfs', self._dir_chroot, _squashfs,
             '-comp', 'xz', '-noappend'] + _exclude_args
        )
        # Subprocess transcript routed through logger.debug — the file
        # handler attached by setup_file_logging() captures it in the
        # unified run log, replacing the legacy mksquashfs.log file.
        _proc = subprocess.run(
            _cmd, input=self._password + '\n',
            capture_output=True, text=True
        )
        for _line in _proc.stdout.splitlines():
            logger.debug(_line)
        for _line in _proc.stderr.splitlines():
            logger.debug(_line)

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: mksquashfs failed — see unified run log")
            logger.error(f"build_iso: mksquashfs exited {_proc.returncode}")
            return False

        _sq_mb = os.path.getsize(_squashfs) // (2 ** 20)
        tui.console.print(f"squashfs created: {_sq_mb} MB")

        # ── Step 6: run grub-mkrescue ─────────────────────────────────────────
        # grub-mkrescue produces a hybrid image bootable on BIOS and UEFI
        # systems.  It requires grub-pc-bin, grub-efi-amd64-bin, and xorriso
        # to be installed on the host.
        _iso_name   = f"athena-{_version}-amd64.iso"
        _iso_path   = os.path.join(self._dir_image, _iso_name)

        tui.console.print("Running grub-mkrescue...")
        # Subprocess transcript routed through logger.debug — see comment
        # above the mksquashfs invocation.
        _proc = subprocess.run(
            ['grub-mkrescue', '-o', _iso_path, _staging],
            capture_output=True, text=True
        )
        for _line in _proc.stdout.splitlines():
            logger.debug(_line)
        for _line in _proc.stderr.splitlines():
            logger.debug(_line)

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: grub-mkrescue failed — see unified run log")
            logger.error(f"build_iso: grub-mkrescue exited {_proc.returncode}")
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
