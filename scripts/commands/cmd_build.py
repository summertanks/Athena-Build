"""Artifact builders — the chroot / ISO build command surface.

Builds the live + installer chroots (cmd_build_chroot_*), the live /
installer / disk ISOs (cmd_build_iso_*), and runs the post-build chroot
verification suite (_verify_chroot / cmd_verify_chroot).  Extracted
verbatim from build.py's BuildSession; see commands/base.py for how the
mixin shares session state.
"""
import glob
import logging
import os
import subprocess

import buildsystem
import installer_chroot
import iso_installer
import tui
import utils
from tui import console

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class BuildCommandsMixin(SessionState):
    def cmd_build_chroot_live(self, *args):
        """Assemble the resolved package set into a bootable live chroot.

        Usage: chroot build live [with_debug]   (or bare `chroot build [with_debug]`)

          with_debug — write /etc/systemd/journald.conf.d/50-console.conf so all
                       journal entries forward to /dev/console (ttyS0 in serial
                       boots).  Off by default — production images should not leak
                       logs onto the console.

        Takes the .deb files produced by source build from dir_repo and installs
        them into a chroot tree at dir_chroot using dpkg.  The resulting chroot
        can be packaged into a live ISO via `iso build live`.

        Prerequisites: source build must have completed (source_build_ready flag)
        AND the signing key must verify (signing_key_verified flag, gated up
        front via _ensure_signing_key_verified — see phase 3 for why).
        The sudo password is collected interactively at the start of this command.
        """
        if self._refuse_in_build_mode("chroot build live"):
            return
        # dep_check_ready is in-memory-only — set exclusively by
        # a successful `cache parse` THIS session, which also populates
        # self.cache / self.dep_tree.  Without it the command would pass
        # the persisted source_build_ready gate, collect the sudo
        # password, then crash on the None session state (and the repo
        # audit's stale-file gate runs blind).  Gate up front.
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return
        if not self.flags.source_build_ready:
            console.print("Run 'source build' first")
            return

        # reset flags on entry so a Ctrl+C / exception during this
        # run can't leave stale True values from a previous successful
        # run.  Re-set to True only on the success-tail (line ~5001 for
        # chroot_ready, the verify call sets chroot_verified separately).
        self.flags.chroot_ready = False
        self.flags.chroot_verified = False

        # Verify the project signing key before any sudo / mount / dpkg work
        if not self._ensure_signing_key_verified():
            return

        # Pre-flight audit gates.  Two layers, both
        # ABORT on red unless `no-gate` is in args:
        #   1. source audit — build-state per source (binaries present,
        #      .result fresh, patches not drifted)
        #   2. repo audit — install-time risks (unresolved Depends,
        #      conflict cohorts)
        # Operator can bypass both with `chroot build live no-gate`
        # for emergency / debugging — when the audits are noisy in a
        # known-acceptable way but you want to push through.
        _no_gate = 'no-gate' in args or '--no-gate' in args
        if not _no_gate:
            if not self._preflight_audit_source():
                console.print("Aborted by source audit pre-flight")
                return
            if not self._preflight_audit_repo():
                console.print("Aborted by repo audit pre-flight")
                return
        else:
            console.print(
                "chroot build live: pre-flight audits BYPASSED "
                "(no-gate)",
                tui.COLOR_WARNING,
            )

        # auto-index the repo if InRelease is
        # missing.  `repo index` is no longer operator-visible —
        # chroot build (and mirror publish) own the side-effect.
        # The chroot bring-up needs a signed InRelease so apt under
        # `--no-check-valid-until` can install built packages.
        if not self._ensure_repo_indexed_for_chroot():
            # Auto-index failed → chroot bring-up would die minutes later on
            # the missing InRelease.  Abort now (the helper already printed
            # the cause).
            console.print("Aborted by repo auto-index")
            return

        _debug = 'with_debug' in args
        if _debug:
            console.print("Debug mode: journald will forward to ttyS0 in built chroot")

        assert self.cache is not None and self.dep_tree is not None
        console.print("Initialising build system...")
        try:
            build_system = buildsystem.BuildSystem(self.dep_tree, self.config)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem() raised: {e}")
            return

        # ── the live surface = closure([Live] Groups seeds ∪
        # live.list ∪ required/important), WITH Recommends extras.  The
        # closure (not the credit-based group deltas) decides membership.
        import surfaces
        _live_seeds = surfaces.group_seed_names(
            self.config.pkglist_path, self.config.live_groups)
        _live_seeds |= set(surfaces.read_flat_roots(self.config.livelist_path))
        _live_seeds |= set(self.cache.required) | set(self.cache.important)
        _live_set = surfaces.surface_closure(
            self.dep_tree, _live_seeds, include_recommends_extras=True)
        console.print(
            f"Live surface: groups {sorted(self.config.live_groups)} → "
            f"{len(_live_set)} package(s) (closure incl. Recommends)",
            tui.COLOR_INFO)

        # Bracket the BuildSystem's lifetime so the cached sudo password is
        # scrubbed on every exit path — success, build failure,
        try:
            console.print("Building chroot environment...")
            _result = build_system.build_chroot(
                debug=_debug, install_set=_live_set,
                gate_complete=not _no_gate)
            if not _result:
                console.print("ERROR: chroot build failed — check logs for details")
                logger.error("build_chroot() returned False")
                return

            self.flags.chroot_ready = True

            # Run verification immediately — chroot_verified gates build_iso
            _passed, _failed = self._verify_chroot(build_system.password, self.config.dir_chroot)
            self.flags.chroot_verified = (_failed == 0)
            if _failed > 0:
                logger.error(f"chroot verification: {_failed} of {_passed + _failed} checks failed")
        finally:
            build_system.scrub_password()


    def _generate_tasks_desc(self) -> 'str':
        """Derive the tasksel `.desc` text from the SIGNED
        lockfile's groups (the selection authority); fall back to a fresh
        pkg.list parse with a warning when the lockfile isn't trustworthy.
        Returns '' (skip staging) only when both sources fail."""
        import selection_lock
        import tasksel_desc
        _lock, _status = selection_lock.read_selection_state(self.config)
        if _status == selection_lock.STATUS_OK and _lock is not None:
            _groups = (_lock.get('seeds') or {}).get('pkg', {}) or {}
            _meta = (_lock.get('seeds') or {}).get('pkg_meta', {}) or {}
        else:
            console.print(
                f"iso build: selection.state {_status} — generating the "
                "tasksel menu from pkg.list directly (run `cache parse` to "
                "restore the signed authority)", tui.COLOR_WARNING)
            try:
                _pkg_lines = utils.readfile(
                    self.config.pkglist_path).splitlines()
                _groups = utils.parse_pkg_list_groups(
                    self.config.pkglist_path, lines=_pkg_lines)
                _meta = utils.parse_pkg_list_group_meta(
                    self.config.pkglist_path, lines=_pkg_lines)
            except (OSError, ValueError) as _e:
                logger.error(f"_generate_tasks_desc: {_e}")
                return ''
        # the lockfile seeds are the raw pkg.list vocabulary, which
        # may be VIRTUAL names — tasksel can't resolve a virtual Key entry and
        # silently drops the whole task.  Canonicalise each seed to its real
        # Package via the selection closure (fall back to the raw name when not
        # selected, so nothing is dropped), deduping collisions.
        if self.dep_tree is not None and self.dep_tree.selected_pkgs:
            import surfaces
            _sel = self.dep_tree.selected_pkgs
            _groups = {
                _g: list(dict.fromkeys(
                    surfaces._canonical(_sel, _s) or _s for _s in _seeds))
                for _g, _seeds in _groups.items()
            }
        _text = tasksel_desc.generate_desc(_groups, _meta)
        console.print(
            f"Generated tasksel menu: {max(0, _text.count('Task: '))} "
            "task(s) → /.disk/athena-tasks.desc", tui.COLOR_INFO)
        return _text

    def cmd_build_chroot_disk(self, *args):
        """Assemble the DISK surface chroot — the minimal
        pre-installed system ([Disk] Groups closure, hard deps only, no
        Recommends extras) into buildroot/disk.  Decoupled from the live
        chroot so live (GNOME) and disk (console) can diverge.

        Usage: chroot build disk [with_debug] [no-gate]

        `iso build disk` then packages buildroot/disk into the qcow2.
        """
        if self._refuse_in_build_mode("chroot build disk"):
            return
        # same up-front gate as chroot build live — see there.
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return
        if not self.flags.source_build_ready:
            console.print("Run 'source build' first")
            return
        self.flags.chroot_disk_ready = False
        if not self._ensure_signing_key_verified():
            return
        _no_gate = 'no-gate' in args or '--no-gate' in args
        if not _no_gate:
            if not self._preflight_audit_source():
                console.print("Aborted by source audit pre-flight")
                return
            if not self._preflight_audit_repo():
                console.print("Aborted by repo audit pre-flight")
                return
        else:
            console.print(
                "chroot build disk: pre-flight audits BYPASSED (no-gate)",
                tui.COLOR_WARNING)
        if not self._ensure_repo_indexed_for_chroot():
            # Auto-index failed → abort before the multi-minute disk-chroot
            # bring-up that would die on the missing InRelease.
            console.print("Aborted by repo auto-index")
            return
        _debug = 'with_debug' in args

        console.print("Initialising build system (disk surface)...")
        assert self.cache is not None and self.dep_tree is not None
        try:
            build_system = buildsystem.BuildSystem(
                self.dep_tree, self.config,
                dir_chroot=self.config.dir_chroot_disk)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem(disk) raised: {e}")
            return

        # The disk surface = closure([Disk] Groups seeds ∪ required/
        # important), hard deps only — a minimal console system (ssh
        # rides [base]).  No live.list (not a live boot), no Recommends.
        import surfaces
        _disk_seeds = surfaces.group_seed_names(
            self.config.pkglist_path, self.config.disk_groups)
        _disk_seeds |= set(self.cache.required) | set(self.cache.important)
        _disk_set = surfaces.surface_closure(self.dep_tree, _disk_seeds)
        console.print(
            f"Disk surface: groups {sorted(self.config.disk_groups)} → "
            f"{len(_disk_set)} package(s) (hard closure)", tui.COLOR_INFO)

        try:
            console.print("Building disk chroot environment...")
            _result = build_system.build_chroot(
                debug=_debug, install_set=_disk_set,
                gate_complete=not _no_gate)
            if not _result:
                console.print(
                    "ERROR: disk chroot build failed — check logs")
                logger.error("build_chroot(disk) returned False")
                return
            self.flags.chroot_disk_ready = True
            # completeness IS now gated — build_chroot returns False
            # (above) on a broken / never-installed set, so chroot_disk_ready
            # is only set on a complete chroot.  This _verify_chroot is the
            # separate BOOT-readiness check; it stays informational on the
            # disk surface (no live-boot by design) — failures logged only.
            _passed, _failed = self._verify_chroot(
                build_system.password, self.config.dir_chroot_disk,
                require_live_boot=False)
            if _failed > 0:
                logger.error(
                    f"disk chroot verification: {_failed} of "
                    f"{_passed + _failed} checks failed")
            console.print(
                f"Disk chroot ready at {self.config.dir_chroot_disk}",
                tui.COLOR_HIGHLIGHT)
        finally:
            build_system.scrub_password()

    def cmd_build_chroot_installer(self, *args):
        """Build the d-i installer chroot from the udeb closure.

        Usage: chroot build installer

        Wipes + (re)creates dir_chroot_installer, then `dpkg --unpack`s
        every udeb in udeb_dep_tree.selected_pkgs into it.  Postinsts
        are NOT run at chroot-build time — they run at first boot under
        rootskel + main-menu (this matches how d-i itself works; see
        project memory project_installer_from_source).

        After unpack, applies the data-layer overlays from installer/
        per the engine mapping in installer_chroot._OVERLAY_MAP.  All
        configuration (preseed, cdebconf overrides, branding) lives in
        installer/ and can be edited without touching this engine code.

        Prerequisites:
          - dep_check_ready (so udeb_dep_tree is populated)
          - source_build_ready (so the .udeb files exist in repo/)
        Collects sudo password — dpkg --root + the wipe/bootstrap need
        root to set file ownerships correctly inside the chroot.

        On success sets self.flags.chroot_installer_ready.
        """
        if self._refuse_in_build_mode("chroot build installer"):
            return
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return
        if not self.flags.source_build_ready:
            console.print(
                "Run 'source build installer' first (need .udeb files in repo/)"
            )
            return
        if self.udeb_dep_tree is None:
            console.print(
                "Udeb dep tree not built — re-run 'cache parse' (it populates "
                "udeb_dep_tree alongside the deb tree)"
            )
            return
        if not self.udeb_dep_tree.selected_pkgs:
            console.print(
                "Udeb closure is empty — check installer.list contains udeb "
                "names and cache has the d-i Packages index"
            )
            return

        # Pre-flight closure audit — scoped to the installer (udeb)
        # selection.  Installer chroot uses dpkg --unpack (no apt),
        # so unmet deps don't fail until the installer runs on the
        # target — catch them here.  Two-layer gate —
        # source audit first, then repo audit.  Bypass with `no-gate`.
        _no_gate = 'no-gate' in args or '--no-gate' in args
        if not _no_gate:
            if not self._preflight_audit_source():
                console.print("Aborted by source audit pre-flight")
                return
            if not self._preflight_audit_repo():
                console.print("Aborted by repo audit pre-flight")
                return
        else:
            console.print(
                "chroot build installer: pre-flight audits BYPASSED "
                "(no-gate)",
                tui.COLOR_WARNING,
            )

        self.flags.chroot_installer_ready = False  # reset before work

        # Sudo password — collect once + validate via `sudo -v`; scrub on
        # every exit path.  Single copy in _collect_validated_sudo_password.
        _password = self._collect_validated_sudo_password('chroot build installer')
        if _password is None:
            return

        try:
            console.print("Building installer chroot from udeb closure...")
            _codename = self.config.build_codename.strip('"').strip("'")
            # pool_pkg_names = the set of pkg names the
            # installed system's apt will see at /cdrom/pool.  Union of
            # the canonical deb closure + the pool_extras additions;
            # passed to installer_chroot so the apt-install audit in
            # pre-pkgsel.d / finish-install.d can cross-reference each
            # apt-install target against what actually ships.  Empty/None
            # = skip audit (legacy compat).
            _pool_pkg_names: 'set[str]' = set()
            if self.dep_tree is not None:
                _pool_pkg_names = set(self.dep_tree.canonical_pkgs.keys())
                _pool_pkg_names |= self.dep_tree.pool_extras_pkg_names
            _ok = installer_chroot.build_installer_chroot(
                udeb_tree=self.udeb_dep_tree,
                dir_udebs=self.config.dir_repo_main_udeb,
                dir_chroot_installer=self.config.dir_chroot_installer,
                installer_dir=os.path.join(self.config.working_dir, 'installer'),
                password=_password,
                codename=_codename,
                pool_pkg_names=_pool_pkg_names,
            )
            if not _ok:
                console.print(
                    "ERROR: installer chroot build failed — check log for details"
                )
                logger.error("build_installer_chroot returned False")
                return

            self.flags.chroot_installer_ready = True
            console.print(
                f"Installer chroot ready at {self.config.dir_chroot_installer}",
                tui.COLOR_HIGHLIGHT,
            )
        finally:
            # Single-use credential; overwrite the in-memory copy.
            _password = '*' * len(_password)  # noqa: F841


    # -------------------------------Command: build_iso---------------------

    def cmd_build_iso_live(self, *args):
        """Build a bootable hybrid BIOS/EFI live ISO from the assembled chroot.

        Usage: iso build live [force]

          force — skip the chroot_verified flag check.  After a manual
                  edit of the chroot tree (e.g. dropping in extra config
                  files between `chroot build` and `iso build live`) the
                  in-memory chroot_verified flag is stale even though the
                  on-disk chroot may still be valid.  With force, we
                  re-run verify_chroot against the on-disk chroot using
                  the password just collected for ISO assembly, and
                  proceed only if all 8 checks still pass.

        Packages the chroot produced by chroot build into a squashfs live
        image, writes a GRUB configuration, and runs grub-mkrescue to
        produce a bootable ISO at dir_image/athena-VERSION-amd64.iso.

        Requires on the host: squashfs-tools, grub-pc-bin, grub-efi-amd64-bin,
        xorriso.  These are checked by build-system.sh at startup.

        Prerequisites: chroot must be built AND verified (chroot_verified
        flag), unless `force` is given in which case verify is re-run.
        """
        if self._refuse_in_build_mode("iso build live"):
            return
        _force = 'force' in args
        if not _force and not self.flags.chroot_verified:
            if self.flags.chroot_ready:
                console.print("Chroot built but verification failed — re-run 'chroot verify' after fixing")
            else:
                console.print("Run 'chroot build' first")
            return

        # grub-mkrescue — the FINAL mastering step — runs inside the
        # build container.  Gate up front, before squashfs packing
        # (same late-failure trap as iso build installer, 2026-06-11).
        if self.container is None:
            console.print(
                "Run 'container local init' first — grub-mkrescue (the final "
                "ISO mastering step) runs inside the build container")
            return

        self.flags.iso_live_ready = False  # reset before work; set True only on success
        console.print("Initialising build system for ISO...")
        try:
            build_system = buildsystem.BuildSystem.for_iso(self.config)
        except RuntimeError as e:
            console.print(f"ERROR: build system initialisation failed — {e}")
            logger.error(f"BuildSystem.for_iso() raised: {e}")
            return

        # Same try/finally pattern as cmd_build_chroot_live — scrub the cached
        # sudo password on every exit path so it does not outlive the ISO
        # build command.
        try:
            if _force:
                console.print("Force mode: re-verifying chroot before ISO...")
                _passed, _failed = self._verify_chroot(
                    build_system.password, self.config.dir_chroot)
                if _failed > 0:
                    console.print(
                        f"ERROR: chroot verification failed "
                        f"({_failed} of {_passed + _failed} checks) — "
                        f"refusing to build ISO"
                    )
                    logger.error(
                        f"build_iso force: verify failed "
                        f"{_failed}/{_passed + _failed}"
                    )
                    return
                # Refresh the flag so subsequent (non-force) calls work
                # without re-verifying.
                self.flags.chroot_verified = True

            console.print("Building ISO...")
            _result = build_system.build_iso(container=self.container)
            if not _result:
                console.print("ERROR: ISO build failed — check logs for details")
                logger.error("build_iso() returned False")
                return
            self.flags.iso_live_ready = True
        finally:
            build_system.scrub_password()


    def cmd_build_iso_installer(self, *args):
        """Build the installer ISO from buildroot/installer/ + repo/.

        Usage: iso build installer

        Mastering steps (delegated to iso_installer.build_installer_iso):
          1. Wipe + create dir_image/staging-installer/
          2. Find kernel — first try installer chroot's /boot/vmlinuz-*,
             fall back to extracting from repo/linux-image-*-amd64*.deb
          3. Build monolithic cpio.gz initrd from buildroot/installer/
          4. Copy installer/boot/grub.cfg → staging/boot/grub/grub.cfg
          5. Copy repo/ → staging/pool/ (for /cdrom/pool runtime read)
          6. grub-mkrescue produces hybrid BIOS+EFI ISO

        All configurable bits (boot menu, kernel cmdline) live in
        installer/boot/grub.cfg — operator edits there without touching
        engine code.

        Prerequisites:
          - chroot_installer_ready (so buildroot/installer/ exists)

        Collects sudo password — initrd cpio reads root-owned chroot
        content, pool copy preserves ownership.
        """
        if self._refuse_in_build_mode("iso build installer"):
            return
        if not self.flags.chroot_installer_ready:
            console.print(
                "Run 'chroot build installer' first (need "
                "buildroot/installer/ populated with the udeb closure)"
            )
            return
        # dep_check_ready is in-memory-only (a successful
        # `cache parse` this session, which populates dep_tree/cache).
        # chroot_installer_ready PERSISTS across sessions, so without
        # this gate a fresh session would silently skip the SELECT-LOCK
        # coherence check below (its `dep_tree is not None` wrapper) and
        # later crash on None session state after collecting sudo.
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return

        # grub-mkrescue — the FINAL mastering step — runs inside the
        # build container.  Gate up front: failing there costs the
        # operator the full ~10-minute pool staging first (hit live
        # 2026-06-11).
        if self.container is None:
            console.print(
                "Run 'container local init' first — grub-mkrescue (the final "
                "ISO mastering step) runs inside the build container")
            return

        # refuse to master an ISO whose in-memory selection
        # disagrees with the signed selection.state (catches a force-through
        # parse or a stale resume shipping a set the authority never approved).
        if self.dep_tree is not None:
            import selection_lock as _sl
            _coh, _add, _rem = _sl.closure_matches_lock(
                self.dep_tree, self.udeb_dep_tree, self.config)
            if _coh == _sl.COHERENCE_DELTA:
                console.print(
                    "iso build: REFUSED — the resolved selection disagrees "
                    f"with the signed selection.state "
                    f"({len(_add['bins']) + len(_add['srcs'])} added / "
                    f"{len(_rem['bins']) + len(_rem['srcs'])} removed).  Run "
                    "`cache parse` to reconcile (or `cache restore` / "
                    "`cache purge-state`).", tui.COLOR_ERROR)
                return
            if _coh == _sl.COHERENCE_BADLOCK:
                console.print(
                    "iso build: REFUSED — selection.state is untrusted "
                    "(badsig/malformed).  `cache purge-state` to re-baseline.",
                    tui.COLOR_ERROR)
                return
            if _coh == _sl.COHERENCE_NOLOCK:
                console.print(
                    "iso build: WARNING — no selection.state authority yet; "
                    "run `cache parse` to bootstrap it.", tui.COLOR_WARNING)

        # Verify the project signing key BEFORE any sudo work — apt on the
        # installed target verifies our Release against this key, and
        # _sign_release_files inside build_installer_iso will fail loud if
        # the key isn't present.  Failing here is cheaper.
        import signing
        if not self._ensure_signing_key_verified():
            return

        self.flags.iso_installer_ready = False  # reset before work; set True only on success

        # Sudo password — single copy in _collect_validated_sudo_password.
        _password = self._collect_validated_sudo_password('iso build installer')
        if _password is None:
            return

        try:
            _version  = self.config.build_version.strip('"').strip("'")
            _codename = self.config.build_codename.strip('"').strip("'")
            # Suite == codename for our single-suite distro.  If we ever
            # ship multiple suites (e.g. athena-stable / athena-testing),
            # the suite would come from a separate config field.
            _suite    = _codename
            # Tag the filename with the snapshot pin so an installer ISO from
            # the base snapshot is distinguishable from one built after
            # stepping the snapshot.  Empty when snapshots off.
            _snap = utils.snapshot_iso_tag(self.config)
            _iso_basename = (
                f"athena-installer-{_version}-{_snap}-amd64.iso" if _snap
                else f"athena-installer-{_version}-amd64.iso")
            console.print(
                f"Building installer ISO {_iso_basename}..."
            )
            # base_include and pool_whitelist for the installer ISO
            # both drop `live_exclusive_pkg_names` — packages that
            # only exist in selected_pkgs because Pass IV resolved
            # `live.list` and pulled them in transitively.  The
            # installer ISO has nothing to do with live boot; those
            # binaries (live-boot, live-config, live-tools, etc.)
            # should not ship on the installer disc or end up on the
            # installed target.
            #
            # This was wrong earlier when pkg.list was incomplete:
            # busybox isn't in stock Debian's required/important set,
            # we didn't list it explicitly, so it got pulled in only
            # via live.list's transitive deps and got classified as
            # live-exclusive — base-installer's install_kernel then
            # failed when this filter dropped it from the target set
            #.  Fix: pkg.list now lists every
            # binary d-i actually apt-installs at install time (audit
            # walked buildroot/installer/ for apt-install callsites);
            # busybox is in pkg_closure after Pass III; Pass IV's
            # live.list resolution finds it already present and
            # doesn't add it to live_exclusive.
            assert self.dep_tree is not None
            _live_excl = self.dep_tree.live_exclusive_pkg_names
            _extras    = self.dep_tree.extras_pkg_names
            # phase D follow-up: pool extras (from pool.list,
            # resolved in Pass VII) ship in /cdrom/pool but are NOT
            # installed in any chroot — drop them from base_include so
            # debootstrap doesn't pull them onto the target.  They
            # remain in _pool_whitelist so they're indexed in the
            # cdrom apt pool, available for `apt-get install` on the
            # target post-install (or by grub-installer at install
            # time, the case that motivated the file).
            _pool_extras = self.dep_tree.pool_extras_pkg_names
            # pkg.list groups other than [base] ship in the
            # cdrom pool but are NOT installed at target debootstrap
            # time — tasksel apt-installs the operator-chosen groups
            # at install time from /cdrom/pool.
            _group_extras = self.dep_tree.pkg_group_extras_pkg_names

            # Pre-flight integrity check: catch operator mistakes that
            # would manifest as a silent install-time UX bug (e.g.
            # tasksel shows a checkbox for an empty task, or a group
            # silently has zero packages because every seed was a
            # typo).
            #
            # `pkg_group_pkg_names[g]` is the DELTA of canonical names
            # added by group `g` — it's empty in two distinct cases:
            #   (a) every seed name was already in selected_pkgs from an
            #       earlier group / required / important.  Not a typo;
            #       the group is REDUNDANT but the tasksel task still
            #       works because its Key entries resolve from elsewhere.
            #       Canonical example: [ssh-server] = openssh-server when
            #       openssh-server is also in [base].
            #   (b) one or more seeds failed to resolve (typo, missing
            #       from cache).  Genuine bug — operator must fix.
            # Distinguish the two by re-parsing pkg.list and checking
            # whether each seed is reachable in selected_pkgs.
            _group_pkgs = self.dep_tree.pkg_group_pkg_names
            try:
                _raw_pkg_groups = utils.parse_pkg_list_groups(
                    self.config.pkglist_path,
                )
            except Exception:
                _raw_pkg_groups = {}
            for _g, _names in _group_pkgs.items():
                if _names:
                    continue
                _seeds = list(_raw_pkg_groups.get(_g, []))
                _unresolved = [
                    _s for _s in _seeds
                    if _s not in self.dep_tree.selected_pkgs
                ]
                if _seeds and not _unresolved:
                    console.print(
                        f"INFO: pkg.list group [{_g}] adds 0 unique "
                        f"packages — all {len(_seeds)} seed(s) already "
                        "pulled in by an earlier group or required/"
                        "important.  Tasksel task remains valid (Key "
                        "entries resolve from elsewhere).",
                        tui.COLOR_INFO,
                    )
                    logger.info(
                        f"iso build installer: group [{_g}] redundant "
                        f"with earlier groups (all {len(_seeds)} seed(s) "
                        "already selected)"
                    )
                else:
                    _detail = (', '.join(_unresolved)
                               if _unresolved else '(empty seed list)')
                    console.print(
                        f"WARNING: pkg.list group [{_g}] resolved to ZERO "
                        "canonical packages — "
                        f"{len(_unresolved)}/{max(1, len(_seeds))} seed(s) "
                        f"not in cache: {_detail}.  Check seed names "
                        "against your cache.",
                        tui.COLOR_WARNING,
                    )
                    logger.warning(
                        f"iso build installer: group [{_g}] has empty "
                        f"closure; unresolved seeds: {_detail}"
                    )
            _non_base_groups = [
                _g for _g in _group_pkgs.keys() if _g != 'base'
            ]
            if _non_base_groups and not _group_extras:
                console.print(
                    f"WARNING: {len(_non_base_groups)} non-[base] group(s) "
                    "declared but pkg_group_extras_pkg_names is empty — every "
                    "package got credited to an earlier group (probably "
                    "[base]).  The non-base group(s) will be empty in tasksel.",
                    tui.COLOR_WARNING,
                )
                logger.warning(
                    "iso build installer: non-base groups exist but all "
                    "packages credited to earlier groups"
                )
            # ── pre-flight: surface config sanity ──────────
            # [Live]/[Disk] Groups must name real pkg.list groups; every
            # installer-defaults root must be in the selection (it ships
            # in the ISO pool for a d-i hook to install).
            import surfaces
            _valid_groups = set(_raw_pkg_groups.keys())
            for _surf, _gset in (('Live', self.config.live_groups),
                                 ('Disk', self.config.disk_groups)):
                _bad = _gset - _valid_groups
                if _bad:
                    console.print(
                        f"ERROR: [{_surf}] Groups references unknown pkg.list "
                        f"group(s): {', '.join(sorted(_bad))} — valid: "
                        f"{', '.join(sorted(_valid_groups))}",
                        tui.COLOR_ERROR)
                    return
            _di_roots = surfaces.read_flat_roots(
                self.config.installer_defaults_path)
            _di_missing = [_r for _r in _di_roots
                           if _r not in self.dep_tree.selected_pkgs]
            if _di_missing:
                console.print(
                    "ERROR: installer-defaults.list entries not in the "
                    f"selection: {', '.join(_di_missing)} — add them to "
                    "pool.list (build/publish roots) or remove from "
                    "installer-defaults.list.", tui.COLOR_ERROR)
                return

            _canonical = {
                _name for _name in self.dep_tree.selected_pkgs
                if _name == self.dep_tree.selected_pkgs[_name]['Package']
            }
            _base_include = sorted(
                _canonical - _extras - _live_excl - _pool_extras - _group_extras
            )
            # ── manifest-driven pool: /cdrom/pool ships ONLY
            # what something on the ISO can install —
            #   closure( [base] ∪ every non-base task group (the tasksel
            #            Keys) ∪ installer-defaults roots (d-i hooks)
            #            ∪ required/important, WITH Recommends extras )
            # Pool.list packages stay selected/built/published; entries
            # reachable by nothing on the ISO (asgard metas, the standard
            # residue) simply don't stage — install them post-boot via the
            # network mirror.  udebs are unaffected (_select_pool_files
            # keeps every .udeb for the installer ramdisk).
            _manifest_seeds = set(_raw_pkg_groups.get('base', []))
            for _g, _seeds_list in _raw_pkg_groups.items():
                if _g != 'base':
                    _manifest_seeds.update(_seeds_list)
            _manifest_seeds.update(_di_roots)
            # installer.list deb-arm roots (e.g. efibootmgr) — debs d-i
            # itself needs on /target; udeb names in the file are simply
            # not in the deb tree and fall out of the closure.
            _manifest_seeds.update(
                surfaces.read_flat_roots(self.config.installerlist_path))
            assert self.cache is not None
            _manifest_seeds |= set(self.cache.required)
            _manifest_seeds |= set(self.cache.important)
            _pool_whitelist = surfaces.surface_closure(
                self.dep_tree, _manifest_seeds,
                include_recommends_extras=True)
            _legacy_pool = _canonical - _live_excl
            _dropped = sorted(_legacy_pool - _pool_whitelist)
            console.print(
                f"ISO pool manifest: {len(_pool_whitelist)} package(s) "
                f"(legacy formula {len(_legacy_pool)}; "
                f"{len(_dropped)} not reachable by tasksel/d-i — "
                "mirror-only)", tui.COLOR_INFO)
            if _dropped:
                logger.info(
                    "iso pool manifest dropped (mirror-only): "
                    + ', '.join(_dropped))

            # Snapshot-aware kernel pick: tell _find_kernel which
            # linux-image-<ABI>-amd64 the cache expects.  Without
            # this, _find_kernel falls back to highest sorted on
            # disk — which can be a stale higher-ABI .deb left over
            # from a pre-rollback snapshot, breaking the installer
            # because the ramdisk's modules won't match
            # symptom from 2026-05-19).
            import re as _re
            _kernel_pat = _re.compile(
                r'^linux-image-\d+\.\d+\.\d+-\d+-amd64$'
            )
            _kernel_candidates = [
                _n for _n in self.cache.package_hashtable.keys()
                if _kernel_pat.match(_n)
            ]
            # version-aware — the prediction that feeds
            # expected_kernel_pkg must pick the true highest ABI (47 > 9),
            # not the lexicographic last, or the picker steers _find_kernel
            # to the wrong kernel.
            _expected_kernel = utils.latest_kernel_name(_kernel_candidates)
            if _expected_kernel:
                console.print(
                    f"Cache predicts kernel binary: {_expected_kernel}",
                    tui.COLOR_INFO,
                )

            _ok = iso_installer.build_installer_iso(
                dir_chroot_installer=self.config.dir_chroot_installer,
                dir_repo=self.config.dir_repo_main,
                dir_repo_main_udeb=self.config.dir_repo_main_udeb,
                dir_image=self.config.dir_image,
                installer_dir=os.path.join(self.config.working_dir, 'installer'),
                password=_password,
                iso_basename=_iso_basename,
                container=self.container,
                suite=_suite,
                codename=_codename,
                version=_version,
                snapshot=_snap,
                base_include_pkgs=_base_include,
                deb_whitelist=_pool_whitelist,
                signing_homedir=signing.signing_home(self.config),
                signing_pubkey_path=signing.signing_pubkey_path(self.config),
                pkg_groups=self.dep_tree.pkg_group_pkg_names,
                group_meta=self.dep_tree.pkg_group_meta,
                expected_kernel_pkg=_expected_kernel,
                # Drop upstream binaries a shipped fork supersedes (e.g.
                # apt-setup-udeb vs athena-setup-udeb) from the pool — anna
                # ignores Conflicts, so leaving them ships + runs both.
                exclude_names=self._superseded_binary_names(),
                # Non-main component dirs (tunneled firmware/microcode and
                # any future contrib/non-free binaries) so they reach the
                # cdrom pool — finish-install.d/08hw-detect apt-installs
                # microcode from cdrom on offline/no-mirror installs.
                dir_repo_extras=[
                    self.config.dir_repo_non_free_firmware,
                    self.config.dir_repo_non_free,
                    self.config.dir_repo_contrib,
                ],
                # [Audit] IdentityScan — gates the S3 staged-ISO scan.
                audit_identity_scan=self.config.audit_identity_scan,
                # the generated tasksel menu (from the signed
                # lockfile's groups) staged at /.disk/athena-tasks.desc.
                tasks_desc_text=self._generate_tasks_desc(),
            )
            if not _ok:
                console.print(
                    "ERROR: installer ISO build failed — check log for details"
                )
                logger.error("build_installer_iso returned False")
                return
            self.flags.iso_installer_ready = True
        finally:
            _password = '*' * len(_password)  # noqa: F841


    def cmd_build_iso_disk(self, *args):
        """Build a pre-installed bootable qcow2 disk image
        from the DISK surface chroot (buildroot/disk, the
        minimal [Disk] Groups closure — decoupled from the live/GNOME
        chroot).

        Usage: iso build disk [size_gb] [force]

          size_gb — disk image size in GB (default from
                    `[Build] DiskImageSizeGB`, fallback 5).  Sparse
                    qcow2 — actual on-disk footprint depends on the
                    chroot's payload, not this number.
          force   — bypass the chroot_disk_ready gate.

        Output: image/<distribution>-<version>-<arch>.qcow2

        Boots directly into the running OS (no installer step).
        Suitable for VM / cloud deployment.

        Prerequisites:
          - Disk chroot built (`chroot build disk`, chroot_disk_ready).
          - Host packages: rsync, dosfstools (mkfs.fat), qemu-utils
            (qemu-img).  Plus losetup/sfdisk/mkfs.ext4/grub-install/
            blkid from util-linux + grub-* (all in default Debian
            install).  Helper checks at entry and surfaces the first
            missing tool with an actionable message.

        grub-install runs via `chroot` into the disk's own root, so the
        installed boot binaries reflect the image's GRUB version, not the
        build host's.
        """
        if self._refuse_in_build_mode("iso build disk"):
            return
        import disk_image

        _force = 'force' in args
        # First non-flag arg is the size; ignore unknown flags.
        _size_gb = self.config.disk_image_size_gb
        for _a in args:
            if _a == 'force':
                continue
            try:
                _size_gb = int(_a)
                break
            except ValueError:
                console.print(
                    f"Ignoring unknown arg: {_a!r} (expected size_gb "
                    f"integer or `force`)"
                )

        if not _force and not self.flags.chroot_disk_ready:
            console.print("Run `chroot build disk` first (the disk image "
                          "packages buildroot/disk, not the live chroot)")
            return

        self.flags.iso_disk_ready = False  # reset before work; set True only on success

        # Cache sudo password — single copy in _collect_validated_sudo_password.
        _password = self._collect_validated_sudo_password('cmd_build_iso_disk')
        if _password is None:
            return

        try:
            # Force mode re-verifies the on-disk chroot before building —
            # same contract as `iso build live force`.  Without this, force
            # would master an UNVERIFIED chroot into a bootable image (the
            # gate above is bypassed but nothing re-checks the 8 invariants).
            if _force:
                console.print("Force mode: re-verifying chroot before disk image...")
                # the disk image masters dir_chroot_disk —
                # verify THAT chroot (pre-decoupling this pointed at the
                # live dir_chroot), and live-boot doesn't apply here.
                _passed, _failed = self._verify_chroot(
                    _password, self.config.dir_chroot_disk,
                    require_live_boot=False)
                if _failed > 0:
                    console.print(
                        f"ERROR: chroot verification failed "
                        f"({_failed} of {_passed + _failed} checks) — "
                        f"refusing to build disk image"
                    )
                    logger.error(
                        f"build_iso_disk force: verify failed "
                        f"{_failed}/{_passed + _failed}"
                    )
                    return
                # NOTE: deliberately does NOT touch chroot_verified —
                # that flag belongs to the LIVE surface; this verify ran
                # against dir_chroot_disk (decoupling).

            _version  = self.config.build_version.strip('"').strip("'")
            _distro   = self.config.build_distribution.strip('"').strip("'")
            _arch     = self.config.arch
            _out_name = f'{_distro.lower()}-{_version}-{_arch}.qcow2'
            _out_path = os.path.join(self.config.dir_image, _out_name)

            console.print(
                f"Building {_size_gb} GB pre-installed disk image: "
                f"{_out_path}"
            )
            _ok = disk_image.build_disk_image(
                dir_chroot=self.config.dir_chroot_disk,
                output_qcow2=_out_path,
                size_gb=_size_gb,
                password=_password,
                container=self.container,
            )
            if not _ok:
                console.print(
                    "ERROR: disk image build failed — see logs for details"
                )
                logger.error("cmd_build_iso_disk: build_disk_image returned False")
                return
            self.flags.iso_disk_ready = True
        finally:
            _password = '*' * len(_password)  # noqa: F841

    # ---------------------------------------------------------------------------
    # Command: verify_chroot
    # ---------------------------------------------------------------------------

    def _verify_chroot(self, password: str, chroot: str,
                       require_live_boot: bool = True) -> tuple:
        """Run the 8-check chroot verification suite. Returns (passed, failed).

        Caller is responsible for prerequisite checks, password validation, and
        setting any progress flags. Prints per-check PASS/FAIL lines and a summary.

        require_live_boot: the live-boot check (7) applies to the LIVE
        surface only — the disk-image chroot ships without live-boot by
        design, so disk call sites pass False and the
        check is reported as SKIP without counting either way.
        """
        # Checks performed:
        #   1. dpkg --audit          — no packages in a broken state
        #   2. dpkg --get-selections — all packages fully installed (none half-configured)
        #   3. Kernel present        — at least one vmlinuz-* in /boot/
        #   4. Initramfs present     — at least one initrd.img-* in /boot/
        #   5. bash --version        — shell is executable inside the chroot
        #   6. systemctl --version   — systemd is present and executable
        #   7. live-boot installed   — required for live ISO boot (live surface only)
        #   8. /etc/os-release       — OS identity file written by generate_system_configs
        console.print(f"Verifying chroot at {chroot}...")

        _passed = 0
        _failed = 0

        def _check(label: str, ok: bool, detail: str = ''):
            nonlocal _passed, _failed
            _status = '[PASS]' if ok else '[FAIL]'
            _color  = tui.COLOR_HIGHLIGHT if ok else tui.COLOR_ERROR
            _suffix = f' — {detail}' if detail else ''
            console.print(f'  {label:<45} {_status}{_suffix}', _color)
            if ok:
                _passed += 1
            else:
                _failed += 1

        def _chroot_run(*cmd):
            return subprocess.run(
                ['sudo', '-S', 'chroot', chroot] + list(cmd),
                input=password + '\n', capture_output=True, text=True
            )

        # ── Check 1: dpkg --audit ────────────────────────────────────────────────
        _r = _chroot_run('dpkg', '--audit')
        _audit_out = _r.stdout.strip()
        _check('dpkg audit — no broken packages',
               _r.returncode == 0 and not _audit_out,
               'clean' if not _audit_out else _audit_out.splitlines()[0][:60])

        # ── Check 2: all packages fully configured ───────────────────────────────
        _r = _chroot_run('dpkg', '--get-selections')
        _lines      = _r.stdout.splitlines()
        _total      = len(_lines)
        _incomplete = [l.split()[0] for l in _lines if l and not l.endswith('\tinstall')]
        # Fold the returncode in: if `dpkg --get-selections` itself failed
        # (broken dpkg db, missing root, sudo refusal) its stdout is empty, so
        # `_incomplete` is [] and the check would falsely PASS.  Surface the
        # failure distinctly.
        if _r.returncode != 0:
            _check('All packages fully installed', False,
                   f'dpkg --get-selections failed (rc={_r.returncode}): '
                   f'{_r.stderr.strip().splitlines()[0][:60] if _r.stderr.strip() else "no output"}')
        else:
            _check('All packages fully installed',
                   not _incomplete,
                   f'{_total} packages installed' if not _incomplete
                   else f'{len(_incomplete)} incomplete: {", ".join(_incomplete[:4])}')

        # ── Check 3: kernel ──────────────────────────────────────────────────────
        # version-aware display (matches what the ISO/disk builders
        # actually pick).
        _kernels = glob.glob(os.path.join(chroot, 'boot', 'vmlinuz-*'))
        _kname = utils.latest_kernel_name(_kernels)
        _check('Kernel present in /boot/',
               bool(_kernels),
               os.path.basename(_kname) if _kname else 'no vmlinuz-* found')

        # ── Check 4: initramfs ───────────────────────────────────────────────────
        _initrds = glob.glob(os.path.join(chroot, 'boot', 'initrd.img-*'))
        _iname = utils.latest_kernel_name(_initrds)
        _check('Initramfs present in /boot/',
               bool(_initrds),
               os.path.basename(_iname) if _iname else 'no initrd.img-* found')

        # ── Check 5: bash ────────────────────────────────────────────────────────
        _r = _chroot_run('bash', '--version')
        _ver = _r.stdout.splitlines()[0] if _r.stdout else ''
        _check('Bash executable inside chroot',
               _r.returncode == 0,
               _ver[:60] if _ver else _r.stderr.strip()[:60])

        # ── Check 6: systemd ─────────────────────────────────────────────────────
        _r = _chroot_run('systemctl', '--version')
        _ver = _r.stdout.splitlines()[0] if _r.stdout else ''
        _check('systemd present and executable',
               _r.returncode == 0,
               _ver[:60] if _ver else _r.stderr.strip()[:60])

        # ── Check 7: live-boot (live surface only) ───────────────────────────────
        if require_live_boot:
            _r = _chroot_run('dpkg', '-l', 'live-boot')
            _live_ok = _r.returncode == 0 and any(l.startswith('ii') for l in _r.stdout.splitlines())
            _check('live-boot installed',
                   _live_ok,
                   'installed' if _live_ok else 'not installed or unconfigured')
        else:
            console.print(
                f'  {"live-boot installed":<45} [SKIP] — '
                'not required for the disk surface')

        # ── Check 8: /etc/os-release ─────────────────────────────────────────────
        _os_release = os.path.join(chroot, 'etc', 'os-release')
        _os_ok = os.path.exists(_os_release)
        _os_detail = ''
        if _os_ok:
            try:
                with open(_os_release) as _osf:
                    _os_lines = _osf.read().splitlines()
                _os_detail = next(
                    (l.split('=', 1)[1].strip('"') for l in _os_lines
                     if l.startswith('PRETTY_NAME=')), 'present')
            except OSError:
                _os_detail = 'present'
        _check('/etc/os-release written',
               _os_ok,
               _os_detail if _os_ok else "missing — run 'chroot build' again")

        # ── phase 3: signing keyring present? (informational) ──────────
        # Not a check — non-gating because the chroot is still a valid live
        # ISO without our keyring; the keyring matters for trusting future
        # apt sources pointing at the Athena repo.  Surfaced here so the
        # operator sees presence/absence at verify time without having to
        # poke at the chroot tree manually.
        _keyring = os.path.join(
            chroot, 'usr/share/keyrings/athena-archive-keyring.gpg')
        if os.path.exists(_keyring):
            console.print(
                '  Athena signing keyring                        present',
                tui.COLOR_INFO,
            )
        else:
            console.print(
                '  Athena signing keyring                        absent  '
                '(run `key generate` then re-run `chroot build`)'
            )

        # ── Summary ──────────────────────────────────────────────────────────────
        _total_checks = _passed + _failed
        console.print('')
        if _failed == 0:
            console.print(
                f'Verification complete: {_passed}/{_total_checks} passed'
                f' — chroot is ready for ISO build',
                tui.COLOR_HIGHLIGHT
            )
        else:
            console.print(
                f'Verification complete: {_passed}/{_total_checks} passed,'
                f' {_failed} failed — build_iso blocked until verify passes',
                tui.COLOR_ERROR
            )

        return _passed, _failed

    def cmd_verify_chroot(self):
        """Re-run the chroot verification suite against an existing chroot.

        Useful after a manual edit of the chroot to re-establish the
        chroot_verified flag without rebuilding from scratch.

        Prerequisites: chroot must already be built (chroot_ready flag).
        """
        if not self.flags.chroot_ready:
            console.print("Run 'chroot build' first")
            return

        _password = self._collect_validated_sudo_password('verify_chroot')
        if _password is None:
            return
        try:
            _passed, _failed = self._verify_chroot(_password, self.config.dir_chroot)
            self.flags.chroot_verified = (_failed == 0)
        finally:
            # Drop the local reference as soon as we are done — same caveat
            # as BuildSystem.scrub_password (Python strings are immutable).
            _password = ''
