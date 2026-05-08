"""ISO assembly for BuildSystem.

Mixin: `_IsoMixin` adds `build_iso` to BuildSystem.  Reads
`self._dir_chroot`, `self._dir_image`, `self._dir_log`,
`self._password`, and `self._config` (for codename / version) — all
provided by `BuildSystem`.
"""

import glob
import os
import shutil
import subprocess

import tui


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

        Logs for the long-running steps are written to dir_log/mksquashfs.log
        and dir_log/grub-mkrescue.log.

        Returns:
            True on success, False if any step fails.
        """
        # ── Step 1: locate kernel and initramfs ───────────────────────────────
        _boot = os.path.join(self._dir_chroot, 'boot')
        _kernels = sorted(glob.glob(os.path.join(_boot, 'vmlinuz-*')))
        _initrds = sorted(glob.glob(os.path.join(_boot, 'initrd.img-*')))

        if not _kernels:
            tui.console.print("ERROR: no kernel found in chroot/boot/ — is linux-image installed?")
            tui.console.error("build_iso: no vmlinuz-* in chroot/boot/")
            return False
        if not _initrds:
            tui.console.print("ERROR: no initramfs found in chroot/boot/ — is initramfs-tools installed?")
            tui.console.error("build_iso: no initrd.img-* in chroot/boot/")
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
        _grub_cfg = (
            'set default=0\n'
            'set timeout=5\n'
            '\n'
            f'menuentry "{_codename} {_version}" {{\n'
            # boot=live   — triggers live-boot to find and mount the squashfs root
            # components  — tells live-boot to activate all its hook scripts
            # console=tty0 — ensures kernel messages go to the screen (visible in QEMU)
            # nomodeset   — disables KMS; prevents blank/garbled screen in QEMU/VMs
            '    linux  /boot/vmlinuz boot=live components username=root console=tty0 nomodeset\n'
            '    initrd /boot/initrd.img\n'
            '}\n'
        )
        with open(os.path.join(_staging_grub, 'grub.cfg'), 'w') as fh:
            fh.write(_grub_cfg)
        tui.console.print("grub.cfg written")

        # ── Step 5: create squashfs ───────────────────────────────────────────
        # Runtime virtual directories (proc, sys, dev, run, tmp) must NOT be
        # excluded as directories — live-boot's initramfs bind-mounts /dev,
        # /proc, /sys, /run into the new root and needs those directories to
        # exist as mount points inside the squashfs.  We only exclude their
        # CONTENTS (which are empty after _umount_chroot_fs anyway) by passing
        # each entry inside the dir rather than the dir itself.
        # -noappend overwrites any previous squashfs.
        _squashfs   = os.path.join(_staging_live, 'filesystem.squashfs')
        _squash_log = os.path.join(self._dir_log, 'mksquashfs.log')

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
        with open(_squash_log, 'w') as fh:
            _proc = subprocess.run(
                _cmd, input=self._password + '\n',
                stdout=fh, stderr=subprocess.STDOUT, text=True
            )

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: mksquashfs failed — see {_squash_log}")
            tui.console.error(f"build_iso: mksquashfs exited {_proc.returncode}")
            return False

        _sq_mb = os.path.getsize(_squashfs) // (2 ** 20)
        tui.console.print(f"squashfs created: {_sq_mb} MB")

        # ── Step 6: run grub-mkrescue ─────────────────────────────────────────
        # grub-mkrescue produces a hybrid image bootable on BIOS and UEFI
        # systems.  It requires grub-pc-bin, grub-efi-amd64-bin, and xorriso
        # to be installed on the host.
        _iso_name   = f"athena-{_version}-amd64.iso"
        _iso_path   = os.path.join(self._dir_image, _iso_name)
        _grub_log   = os.path.join(self._dir_log, 'grub-mkrescue.log')

        tui.console.print("Running grub-mkrescue...")
        with open(_grub_log, 'w') as fh:
            _proc = subprocess.run(
                ['grub-mkrescue', '-o', _iso_path, _staging],
                stdout=fh, stderr=subprocess.STDOUT, text=True
            )

        if _proc.returncode != 0:
            tui.console.print(f"ERROR: grub-mkrescue failed — see {_grub_log}")
            tui.console.error(f"build_iso: grub-mkrescue exited {_proc.returncode}")
            return False

        _iso_mb = os.path.getsize(_iso_path) // (2 ** 20)
        tui.console.print(f"ISO built: {_iso_path} ({_iso_mb} MB)")
        return True
