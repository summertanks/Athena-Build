"""Chroot install logic for BuildSystem.

Mixin: `_ChrootMixin` adds `build_chroot` and all of its supporting
helpers — virtual-fs mount/umount, dpkg-in-chroot probe, install-batch
computation, named-package configure, dpkg database initialisation,
package-list extraction helpers, FHS directory layout, pre/post-install
overlay handling, and per-system config file generation.

All methods access `self._dependencytree`, `self._dir_chroot`,
`self._dir_image`, `self._dir_log`, `self._dir_repo`,
`self._dir_preinstall_patch`, `self._dir_postinstall_patch`,
`self._password`, `self._config`, and `self.strip_build_version`
— provided by `BuildSystem`.
"""

import os
import pathlib
import re
import shlex
import subprocess
import tempfile

import tui
import utils


class _ChrootMixin:

    def build_chroot(self, debug: bool = False) -> bool:
        """Install all selected packages into the chroot in dependency order.

        Computes a topo-sorted batch sequence over Pre-Depends ∪ Depends
        once (_compute_install_batches), then for each batch runs one
        `dpkg --unpack` followed by one `dpkg --configure pkg1 pkg2 ...`.
        A final `dpkg --configure -a` retries any package whose postinst
        was deferred by an undeclared helper-tool dependency (e.g. udev
        calling update-rc.d from init-system-helpers); the dpkg
        --get-selections summary at the end is the authoritative gate.

        Args:
            debug: When True, generate_system_configs() also writes a
                journald drop-in to forward all entries to /dev/console.
                Off by default — opt in for serial-debug builds only.

        Returns:
            True on completion, False on a fatal error during install
            (mount failure, unbreakable dep cycle, etc.).  Individual
            postinst failures are tolerated and surfaced via the final
            get-selections check.
        """
        selected = self._dependencytree.selected_pkgs
        # All canonical packages — filter out virtual-package alias entries
        # (entries where the key differs from Package['Package']).
        all_pkgs = [p for p in selected if p == selected[p]['Package']]

        # gcc-NN-base bootstrap: libc6 → libgcc-s1 → gcc-NN-base forms a
        # Pre-Depends cycle Kahn cannot break on its own.  Pre-install these
        # as the first batch so the rest of the graph has all libc deps
        # already satisfied — same pattern as the legacy unpack-forest seed.
        _gcc_base = next(
            (p for p in all_pkgs if re.fullmatch(r'gcc-\d+-base', p)), None
        )
        if _gcc_base is None:
            tui.console.print(
                "WARNING: no gcc-NN-base found in selected set — "
                "circular Pre-Depends bootstrap may fail"
            )
            tui.console.warning("build_chroot: gcc-NN-base not found in selected_pkgs")
            libc_seed = ['libc6', 'libgcc-s1', 'libcrypt1']
        else:
            libc_seed = [_gcc_base, 'libc6', 'libgcc-s1', 'libcrypt1']
        libc_seed_set = set(libc_seed)

        # Compute the batch sequence up front.  Failure here means an
        # unbreakable cycle remains after libc-seed handling — surface it
        # before touching the chroot so the operator sees the dep problem
        # without first sitting through unrelated dpkg activity.
        try:
            batches = self._compute_install_batches(libc_seed_set)
        except RuntimeError as e:
            tui.console.print(f"ERROR: cannot order packages — {e}")
            tui.console.error(f"_compute_install_batches: {e}")
            return False

        # batches is List[Tuple[List[str], bool]]; len(b) on the tuple
        # is always 2 — use b[0] for the actual package list.
        _total_to_install = len(libc_seed) + sum(len(b[0]) for b in batches)
        _forced = sum(1 for b in batches if b[1])
        tui.console.print(
            f"Computed install plan: {len(batches) + 1} batches, "
            f"{_total_to_install} packages "
            f"(seed: {len(libc_seed)}; batches 1..{len(batches)}: "
            f"{_forced} forced, {len(batches) - _forced} acyclic)"
        )

        # --- Install ---
        self._setup_chroot_env()
        self._init_dpkg_database()
        _log_path = os.path.join(self._dir_log, 'chroot-install.log')

        configured: set = set()
        _result = True
        try:
            # /proc, /sys, /dev, /dev/pts are needed by many postinstall
            # scripts (systemctl detection, uname, pty operations).
            # Mounted inside the try block so a partial-mount failure
            # (raised by _mount_chroot_fs) still flows through the finally
            # below and umount-cleans whatever did get mounted.
            self._mount_chroot_fs()

            with open(_log_path, 'w') as fh:

                # Batch 0: libc circular-dep bootstrap.
                tui.console.print(f"Batch 0 (bootstrap): {libc_seed}")
                fh.write(f'--- Batch 0 (bootstrap) ---\n')
                self._unpack_packages(libc_seed, fh)
                configured |= self._configure_packages(libc_seed, fh)

                # Batches 1..N — Kahn-ordered, dep-independent within each.
                # The terminal batch may be a "cycle batch" (force_deps=True)
                # that contains a real SCC; in that case dpkg gets
                # --force-depends scoped to that batch only.
                for _i, (_batch, _force) in enumerate(batches, start=1):
                    _label = f"Batch {_i}/{len(batches)} — {len(_batch)} package(s)"
                    if _force:
                        _label += " [cycle, --force-depends]"
                    tui.console.print(_label)
                    fh.write(f'\n--- Batch {_i} ({len(_batch)} pkgs'
                             f'{", forced" if _force else ""}) ---\n')
                    self._unpack_packages(_batch, fh)
                    configured |= self._configure_packages(
                        _batch, fh, force_deps=_force
                    )

                # Final defensive sweep.  Many Debian postinst scripts
                # invoke helper tools from packages they do not formally
                # Depend on (udev calls update-rc.d from
                # init-system-helpers; apt calls deb-systemd-helper).
                # When those helpers are scheduled later in the topo
                # order, the early postinst returns non-zero and dpkg
                # marks the package half-configured.  `dpkg --configure
                # -a` retries every half-configured package — by this
                # point all batches are unpacked + their declared deps
                # configured, so the implicit helpers are present and
                # the postinst succeeds.  The dpkg --get-selections
                # check below is the authoritative pass/fail gate.
                tui.console.print(
                    "Final configure sweep — "
                    "retrying any postinst-deferred packages..."
                )
                fh.write('\n--- Final configure sweep ---\n')
                _final_configured = self._configure_chroot(fh, is_final=True)
                configured |= _final_configured
        except RuntimeError as e:
            tui.console.print(f"ERROR: chroot install aborted — {e}")
            tui.console.error(f"build_chroot install loop: {e}")
            _result = False
        finally:
            self._umount_chroot_fs()

        if not _result:
            return False

        # Defence-in-depth: even with a clean batch sequence, query dpkg
        # for the authoritative list of packages NOT in 'install ok
        # installed' state.  Should be empty after a successful run; if
        # not, surface the names so the operator can investigate without
        # parsing 80k lines of chroot-install.log.
        _status_cmd = ['sudo', '-S', 'chroot', self._dir_chroot,
                       'dpkg', '--get-selections']
        _status = subprocess.run(_status_cmd, input=self._password + '\n',
                                 capture_output=True, text=True)
        _not_installed = [
            l.split()[0] for l in _status.stdout.splitlines()
            if l.endswith('\thalf-configured') or l.endswith('\tunpacked')
        ] if _status.returncode == 0 else []

        if _not_installed:
            tui.console.print(
                f"Chroot build done — {len(configured)} packages configured, "
                f"{len(_not_installed)} incomplete: {', '.join(_not_installed[:8])}"
                f"{'…' if len(_not_installed) > 8 else ''}"
            )
            tui.console.warning(
                f"build_chroot: {len(_not_installed)} packages not fully configured: "
                f"{_not_installed}"
            )
        else:
            tui.console.print(
                f"Chroot build complete — {len(configured)} packages installed "
                f"in {len(batches) + 1} batches — all packages fully configured"
            )

        # Ensure every installed kernel has an initramfs.  Defence-in-depth
        # against update-initramfs edge cases that can leave no initrd.img-*
        # even when dpkg reports the kernel as fully configured.
        self._ensure_initramfs()

        # Apply post-install patches and overlay files now that all packages
        # are configured.  This is the right moment to override config files
        # or apply distro-specific fixups that would be clobbered by dpkg.
        self.post_install()

        # Write base system configuration files that dpkg does not create:
        # OS identity, hostname, hosts, fstab, machine-id, apt sources.
        self.generate_system_configs(debug=debug)

        return True

    # ── chroot configure helpers ─────────────────────────────────────────────

    def _mount_chroot_fs(self):
        """Bind-mount /proc, /sys, /dev, /dev/pts into the chroot.

        Required before running `dpkg --configure -a` inside the chroot.
        Many postinstall scripts check /proc/1/environ, call systemctl,
        or open ptys — they fail silently or loudly without these mounts.
        Always paired with _umount_chroot_fs() in a try/finally block.

        Two-step verification on each entry:
          1. `mount` returncode must be zero, and
          2. os.path.ismount(target) must report True after the call.
        Step 2 catches the rare case where mount silently no-ops without
        raising (we have not seen this in practice on Linux, but rc=0
        without an actual mount would let dpkg's postinstall scripts
        produce a chroot whose only failure signal is "many packages
        not fully configured" much later in the run — far from the
        actionable cause).

        On any failure the method raises RuntimeError so build_chroot()'s
        try/finally cleans up partial state via _umount_chroot_fs().
        """
        _chroot = self._dir_chroot
        _mounts = [
            (['-t', 'proc',   'proc',       f'{_chroot}/proc']),
            (['-t', 'sysfs',  'sysfs',      f'{_chroot}/sys']),
            (['--bind',       '/dev',        f'{_chroot}/dev']),
            (['--bind',       '/dev/pts',    f'{_chroot}/dev/pts']),
        ]
        for _args in _mounts:
            _dst = _args[-1]
            subprocess.run(['sudo', '-S', 'mkdir', '-p', _dst],
                           input=self._password + '\n', capture_output=True, text=True)
            _proc = subprocess.run(['sudo', '-S', 'mount'] + _args,
                                   input=self._password + '\n', capture_output=True, text=True)
            if _proc.returncode != 0:
                tui.console.error(
                    f'_mount_chroot_fs: mount {_dst} failed (rc={_proc.returncode}): '
                    f'{_proc.stderr.strip()}'
                )
                raise RuntimeError(
                    f'mount {_dst} failed: {_proc.stderr.strip() or "no stderr"}'
                )

            # ismount uses st_dev comparison against the parent — valid for
            # the four targets here (procfs / sysfs / devtmpfs / devpts all
            # have different st_dev from a typical chroot's host filesystem).
            if not os.path.ismount(_dst):
                tui.console.error(
                    f'_mount_chroot_fs: mount {_dst} returned 0 but kernel '
                    f'mountinfo does not show it as a mount point'
                )
                raise RuntimeError(
                    f'mount {_dst} reported success but is not actually mounted'
                )

            tui.console.info(f'_mount_chroot_fs: mounted {_dst}')

    def _umount_chroot_fs(self):
        """Unmount virtual filesystems from the chroot (reverse of _mount_chroot_fs).

        Uses -lf (lazy force) so a partial mount state from a previous failed
        run does not prevent cleanup.  Safe to call even if nothing was mounted
        (umount on a non-mountpoint returns non-zero but is harmless here).
        Logs each successful unmount at info level for symmetry with the
        mount path; failures (target was never a mount point) are silent
        because that's the normal case after a partial-mount cleanup.
        """
        _chroot = self._dir_chroot
        for _dst in [
            f'{_chroot}/dev/pts',
            f'{_chroot}/dev',
            f'{_chroot}/sys',
            f'{_chroot}/proc',
        ]:
            _was_mounted = os.path.ismount(_dst)
            subprocess.run(['sudo', '-S', 'umount', '-lf', _dst],
                           input=self._password + '\n', capture_output=True, text=True)
            if _was_mounted:
                tui.console.info(f'_umount_chroot_fs: unmounted {_dst}')

    def _chroot_dpkg_available(self) -> bool:
        """True when the chroot has both dpkg and sh, so chroot-mode dpkg
        can invoke maintainer scripts.  Until that's true the caller must
        stay in chrootless mode (host sh + DPKG_ROOT).  sh may live at
        /bin/sh or /usr/bin/sh depending on update-alternatives layout.
        """
        _chroot = self._dir_chroot
        return (
            os.path.exists(os.path.join(_chroot, 'usr/bin/dpkg')) and
            (os.path.lexists(os.path.join(_chroot, 'bin/sh')) or
             os.path.lexists(os.path.join(_chroot, 'usr/bin/sh')))
        )

    def _configure_chroot(self, fh, is_final: bool = False) -> set:
        """Run `dpkg --configure -a` and return the set of successfully configured package names.

        Automatically selects between two modes based on whether dpkg is present
        inside the chroot yet:

          Chroot mode (dpkg present in chroot):
            sudo env VAR=val chroot <dir> dpkg --configure -a
            `env` runs on the HOST (always available) and sets the environment
            before entering the chroot.  Scripts run inside the real chroot so
            tools like addgroup, shadowconfig, etc. operate on the chroot's files
            directly with no DPKG_ROOT confusion.

          Chrootless fallback (dpkg not yet in chroot — early bootstrap rounds):
            sudo env VAR=val dpkg --root=<dir> --force-script-chrootless --configure -a
            Used for the first few rounds before coreutils/dpkg are unpacked.
            DPKG_ROOT workarounds apply here, but the packages that configure in
            these early rounds (libc6, libssl3, …) do not trigger them.

        Args:
            is_final: True only for the post-loop cleanup pass.  Intermediate
                      round failures are expected (packages waiting on deps that
                      arrive in a later round) and are silently logged to file.
                      Only the final pass surfaces errors on the console so the
                      user gets a single, definitive signal.

        Returns a set of base package names (arch suffix stripped) that dpkg
        reported as "Setting up X (version)" — i.e., successfully configured.
        Failed packages are NOT in the returned set.
        """
        _chroot = self._dir_chroot
        _dpkg_in_chroot = self._chroot_dpkg_available()

        if _dpkg_in_chroot:
            # env runs on the host so we don't need /usr/bin/env inside the
            # chroot.  Snapshot pinning + the dep-drift verifier (run before
            # this method) ensure unresolved deps are caught upstream, so we
            # surface dpkg's real error rather than masking it with --force-depends.
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'chroot {_chroot} '
                f'dpkg --configure -a --force-confdef --force-confnew'
            )
        else:
            # Chrootless fallback before /usr/bin/dpkg + sh exist in the chroot.
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'dpkg --root={_chroot} --instdir={_chroot} '
                f'--admindir={_chroot}/var/lib/dpkg '
                f'--force-script-chrootless --force-confdef --force-confnew '
                f'--configure -a'
            )

        _proc = subprocess.run(_cmd, input=self._password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)
        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            _mode = 'chroot' if _dpkg_in_chroot else 'chrootless'
            if is_final:
                # Final pass failures are real — surface them immediately.
                _failed = [
                    l.split()[-1] for l in _proc.stdout.splitlines()
                    if l.startswith('dpkg: error processing package')
                ]
                tui.console.print(
                    f'Final configure had errors — {len(_failed)} package(s) failed: '
                    f'{", ".join(_failed[:5])}{"…" if len(_failed) > 5 else ""}'
                )
                tui.console.error(
                    f'_configure_chroot final ({_mode}): '
                    f'{len(_failed)} failed — see log for details'
                )
            else:
                # Intermediate round: expected for packages waiting on deps from
                # a later round.  Log to file only; do not clutter the console.
                tui.console.warning(
                    f'Round configure ({_mode}): some packages deferred — '
                    f'will retry in next round'
                )

        _configured: set = set()
        for _line in _proc.stdout.splitlines():
            _m = re.match(r'^Setting up (\S+?)(?::\S+)? \(', _line)
            if _m:
                _configured.add(_m.group(1))
        return _configured

    def _ensure_initramfs(self):
        """Generate initramfs for any kernel that is missing one.

        Defence-in-depth after configure.  update-initramfs occasionally
        skips generation silently (kernel-package quirks, races between
        linux-image's postinst and an initramfs hook that isn't ready
        yet).  Scan /boot/ for vmlinuz-* and, for each one missing its
        matching initrd.img-*, run update-initramfs -c inside the chroot.
        """
        _chroot = self._dir_chroot
        _boot = os.path.join(_chroot, 'boot')

        _kernels = sorted(
            f for f in os.listdir(_boot) if f.startswith('vmlinuz-')
        ) if os.path.isdir(_boot) else []

        for _vmlinuz in _kernels:
            _kver  = _vmlinuz.replace('vmlinuz-', '')
            _initrd = os.path.join(_boot, f'initrd.img-{_kver}')
            if os.path.exists(_initrd):
                continue

            tui.console.print(f"Generating initramfs for {_kver} (was missing)...")
            _proc = subprocess.run(
                ['sudo', '-S', 'chroot', _chroot,
                 'update-initramfs', '-c', '-k', _kver],
                input=self._password + '\n',
                capture_output=True, text=True
            )
            if _proc.returncode != 0:
                tui.console.print(f"WARNING: update-initramfs failed for {_kver} — see log")
                tui.console.warning(
                    f"_ensure_initramfs {_kver}: {_proc.stderr.strip()[:300]}"
                )
            else:
                tui.console.print(f"Initramfs generated: initrd.img-{_kver}")

    # ── Dependency sync from .deb files ──────────────────────────────────────


    def _resolve_pre_depends(self, pkg_name: str) -> list:
        """Return Pre-Depends canonical package names for pkg_name present in selected_pkgs.

        Handles both single-alternative and OR-alternative Pre-Depends.  For OR
        groups (e.g. 'systemd-sysv | sysvinit-core'), the first alternative that
        is present in selected_pkgs is used as the ordering constraint.  Names not
        in selected_pkgs are omitted — satisfied by something already on the host.

        Virtual-package names (e.g. 'awk') are resolved to the real Package name
        (e.g. 'mawk') so that the unpack-forest node deletion in build_chroot()
        works correctly: dpkg reports "Setting up mawk", not "Setting up awk".
        Without this resolution the virtual-name child node would never be deleted
        and the parent would stay non-childless forever.
        """
        pkg = self._dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        selected = self._dependencytree.selected_pkgs
        result: list = []
        seen: set = set()
        for dep in pkg.pre_depends:
            dep_name = dep[0]
            if dep_name in selected:
                canonical = selected[dep_name]['Package']
                if canonical not in seen:
                    seen.add(canonical)
                    result.append(canonical)
        for group in pkg.alt_pre_depends:
            for alt in group:
                dep_name = alt[0]
                if dep_name in selected:
                    canonical = selected[dep_name]['Package']
                    if canonical not in seen:
                        seen.add(canonical)
                        result.append(canonical)
                    break
        return result

    def _resolve_depends(self, pkg_name: str) -> list:
        """Return Depends canonical package names for pkg_name present in selected_pkgs.

        Handles both single-alternative and OR-alternative Depends.  For OR
        groups, the first alternative present in selected_pkgs is used so that
        configure ordering tracks the actual package we installed.  Virtual
        names (e.g. 'awk' → 'mawk') are resolved to the real Package name so
        callers comparing names against actual package identifiers match.
        """
        pkg = self._dependencytree.selected_pkgs.get(pkg_name)
        if pkg is None:
            return []
        selected = self._dependencytree.selected_pkgs
        result: list = []
        seen: set = set()
        for dep in pkg.depends:
            dep_name = dep[0]
            if dep_name in selected:
                canonical = selected[dep_name]['Package']
                if canonical not in seen:
                    seen.add(canonical)
                    result.append(canonical)
        for group in pkg.alt_depends:
            for alt in group:
                if alt[0] in selected:
                    canonical = selected[alt[0]]['Package']
                    if canonical not in seen:
                        seen.add(canonical)
                        result.append(canonical)
                    break
        return result

    def _compute_install_batches(self, libc_seed_set: set) -> list:
        """Topologically sort selected packages over Pre-Depends ∪ Depends.

        Returns a list of (batch, needs_force) tuples.  Each batch is a
        list of canonical package names; needs_force is True only for a
        terminal "cycle batch" emitted when Kahn cannot make further
        progress on a real dep cycle (libdevmapper1.02.1 ↔ dmsetup +
        reachable closure on bookworm).

        Acyclic batches (needs_force=False) share two properties:

          1. None of their members depends on any other in the same batch.
             So one `dpkg --unpack` followed by one `dpkg --configure`
             always satisfies dpkg's ordering constraints.
          2. All Pre-Depends and Depends are in *earlier* batches (or in
             libc_seed_set, treated as already-resolved).

        The cycle batch (needs_force=True) does NOT have property 1 —
        by construction its members mutually depend.  The caller scopes
        --force-depends to this batch alone (see _configure_packages),
        which lets dpkg break the SCC without weakening dep checking
        on the rest of the graph.

        libc_seed_set is the hard-coded base bootstrap set
        (gcc-NN-base, libc6, libgcc-s1, libcrypt1) installed first by
        the caller to break the libc circular Pre-Depends.  Edges into
        the seed are dropped so other packages see those deps as
        already satisfied.

        Raises:
            RuntimeError only if Kahn produces no acyclic batches AND the
            graph has more than one disjoint SCC large enough to suggest
            a genuinely pathological state (every package in one cycle).
            Practical Debian dep graphs do not hit this.
        """
        selected = self._dependencytree.selected_pkgs

        # Restrict the graph to canonical (non-virtual) names that are NOT
        # part of the libc seed.  Aliases would inflate the graph with
        # redundant nodes and Kahn would never be able to retire them.
        all_pkgs = [
            p for p in selected
            if p == selected[p]['Package'] and p not in libc_seed_set
        ]
        in_scope = set(all_pkgs)

        # deps[pkg] = set of canonical names that pkg depends on AND are
        # also in scope.  Edges to libc_seed_set or to packages outside
        # selected_pkgs (presumed satisfied by chroot base state) are
        # silently dropped — Kahn's invariant only needs to see edges
        # that block ordering decisions.
        deps: dict = {}
        for pkg in all_pkgs:
            d: set = set()
            for n in self._resolve_pre_depends(pkg) + self._resolve_depends(pkg):
                if n == pkg:
                    continue                # self-edge (via Provides loops)
                if n in in_scope:
                    d.add(n)
            deps[pkg] = d

        # Kahn: each round, pull every node with zero remaining in-scope
        # deps into a batch; remove them from `remaining`; repeat.
        # Sorted output for deterministic batches across runs.
        batches: list = []
        remaining = set(all_pkgs)
        while remaining:
            ready = sorted(p for p in remaining if not (deps[p] & remaining))
            if not ready:
                # Cycle remains in the Pre-Depends ∪ Depends graph.
                # Split the SCC by Pre-Depends order alone (Depends
                # cycles are tolerated by --force-depends, but
                # Pre-Depends are strict — dpkg refuses to unpack X
                # until X's Pre-Depends are configured).  Each
                # sub-batch is emitted as its own forced batch so
                # build_chroot does unpack→configure cycles within
                # the SCC in the right order.
                _stuck = sorted(remaining)
                _subs = self._pre_depends_subbatches(_stuck)
                tui.console.warning(
                    f"_compute_install_batches: dep cycle in "
                    f"{len(_stuck)} package(s); split into "
                    f"{len(_subs)} forced sub-batch(es) by Pre-Depends: "
                    f"{_stuck[:6]}{'…' if len(_stuck) > 6 else ''}"
                )
                for _sub in _subs:
                    batches.append((_sub, True))
                return batches
            batches.append((ready, False))
            remaining -= set(ready)
        return batches

    def _pre_depends_subbatches(self, pkgs: list) -> list:
        """Kahn over Pre-Depends edges only, restricted to `pkgs`.

        Used by _compute_install_batches to split a Depends-cycle batch
        into Pre-Depends-respecting sub-batches.  Edges outside `pkgs`
        are dropped — those Pre-Depends are already satisfied by
        earlier batches (or by the libc seed).

        If a Pre-Depends cycle remains within `pkgs` (rare; Debian
        Pre-Depends cycles are essentially limited to libc), the
        residue is emitted as one final sub-batch and dpkg's
        --force-depends has to break it.
        """
        in_scope = set(pkgs)
        pre_deps: dict = {}
        for p in pkgs:
            d: set = set()
            for n in self._resolve_pre_depends(p):
                if n != p and n in in_scope:
                    d.add(n)
            pre_deps[p] = d

        sub_batches: list = []
        remaining = set(pkgs)
        while remaining:
            ready = sorted(p for p in remaining if not (pre_deps[p] & remaining))
            if not ready:
                sub_batches.append(sorted(remaining))
                break
            sub_batches.append(ready)
            remaining -= set(ready)
        return sub_batches

    def _configure_packages(self, pkg_list: list, fh,
                            force_deps: bool = False) -> set:
        """Run `dpkg --configure pkg1 pkg2 ...` for the named packages.

        Counterpart to _unpack_packages and the named-list alternative
        to _configure_chroot's `--configure -a` blanket sweep.  Used by
        the single-pass batched install in build_chroot.

        Auto-selects chroot vs chrootless mode using the same probe as
        _configure_chroot: dpkg-in-chroot once /usr/bin/dpkg AND sh are
        in the chroot, chrootless --root with --force-script-chrootless
        before that.

        Args:
            pkg_list:   canonical package names to configure.
            fh:         open file handle for the chroot install log.
            force_deps: True only for the cycle batch emitted by
                        _compute_install_batches.  Adds --force-depends
                        scoped to this single dpkg invocation so dpkg
                        can break the SCC; acyclic batches do not force,
                        but their postinst-failure tolerance is the
                        same (see Returns).

        Returns the set of base package names dpkg reported as
        "Setting up X (version)".

        Does NOT raise on rc != 0.  In Debian, many postinst scripts
        invoke helper tools from packages not declared as Depends —
        e.g. udev.postinst calls update-rc.d from init-system-helpers,
        apt.postinst calls deb-systemd-helper.  When those helper
        packages are scheduled later in the topo order, the early
        batch's postinst returns non-zero and dpkg marks the package
        half-configured but keeps going.  build_chroot follows the
        per-batch loop with a single defensive `dpkg --configure -a`
        which retries every half-configured package now that its
        implicit deps are present; the final `dpkg --get-selections`
        sweep is the authoritative pass/fail gate.

        rc != 0 is logged at warn level so the operator can correlate
        a final-sweep failure back to the originating batch when
        triaging.
        """
        if not pkg_list:
            return set()

        _chroot = self._dir_chroot
        _dpkg_in_chroot = self._chroot_dpkg_available()
        _force = '--force-depends ' if force_deps else ''

        if _dpkg_in_chroot:
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'chroot {_chroot} '
                f'dpkg --configure --force-confdef --force-confnew {_force}'
            ) + pkg_list
        else:
            _cmd = shlex.split(
                f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
                f'dpkg --root={_chroot} --instdir={_chroot} '
                f'--admindir={_chroot}/var/lib/dpkg '
                f'--force-script-chrootless --force-confdef --force-confnew {_force}'
                f'--configure'
            ) + pkg_list

        fh.write(f'Configure: {" ".join(pkg_list)}\n')
        _proc = subprocess.run(_cmd, input=self._password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)

        _configured: set = set()
        for _line in _proc.stdout.splitlines():
            _m = re.match(r'^Setting up (\S+?)(?::\S+)? \(', _line)
            if _m:
                _configured.add(_m.group(1))

        # Anything that was asked to be configured but didn't show up in
        # "Setting up" stdout is a failure.  dpkg may print errors in any
        # mix of stdout and stderr; capture both for the diagnostic so the
        # operator can see the underlying cause without scanning the log
        # file separately.
        # Tolerate rc != 0 — log and let the build_chroot defensive
        # `dpkg --configure -a` retry any half-configured packages once
        # their implicit deps are in place (see method docstring).  The
        # warning here gives the operator a breadcrumb back to the
        # originating batch if the final sweep cannot recover.
        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            _missing = [p for p in pkg_list if p not in _configured]
            tui.console.warning(
                f'_configure_packages: rc={_proc.returncode}, '
                f'{len(_configured)}/{len(pkg_list)} configured this pass; '
                f'deferred to final sweep: '
                f'{", ".join(_missing[:5])}'
                f'{"…" if len(_missing) > 5 else ""}'
            )

        return _configured

    # ── dpkg execution helpers ────────────────────────────────────────────────

    def _init_dpkg_database(self):
        """Create the dpkg database structure inside the chroot.

        dpkg refuses to run if /var/lib/dpkg and its subdirectories do not
        exist, and requires /var/lib/dpkg/status and /var/lib/dpkg/available
        to be present (even if empty) before the first package is installed.

        All operations run via sudo -S because after a failed or partial
        previous run the chroot tree may be owned by root, making os.makedirs
        silently pass exist_ok but leave files in an inconsistent state.
        sudo touch is idempotent — safe to call on every startup.
        """
        _dpkg_dirs = [
            'var/lib/dpkg',
            'var/lib/dpkg/info',
            'var/lib/dpkg/updates',
            'var/lib/dpkg/triggers',
            'var/cache/apt/archives',
        ]
        for _d in _dpkg_dirs:
            _path = os.path.join(self._dir_chroot, _d)
            subprocess.run(
                ['sudo', '-S', 'mkdir', '-p', _path],
                input=self._password + '\n',
                capture_output=True, text=True
            )

        # status and available must exist before dpkg's first invocation.
        for _f in ['var/lib/dpkg/status', 'var/lib/dpkg/available']:
            _path = os.path.join(self._dir_chroot, _f)
            subprocess.run(
                ['sudo', '-S', 'touch', _path],
                input=self._password + '\n',
                capture_output=True, text=True
            )

        # --force-script-chrootless runs pre/post-install scripts on the HOST
        # but exports DPKG_ROOT=<chroot> to them.  Debconf's Config.pm reads
        # $DPKG_ROOT/etc/debconf.conf — if it doesn't exist, every package
        # whose maintainer script uses debconf (libc6, bash, apt, …) fails
        # its pre-install script with "No config file found".
        # Writing a minimal debconf.conf into the chroot's /etc/ before the
        # first dpkg call makes debconf find its config and proceed silently
        # in noninteractive mode.
        _debconf_conf_content = (
            'Config: configdb\n'
            'Templates: templatedb\n'
            '\n'
            'Name: config\n'
            'Driver: File\n'
            'Mode: 644\n'
            'Filename: /var/cache/debconf/config.dat\n'
            '\n'
            'Name: passwords\n'
            'Driver: File\n'
            'Mode: 600\n'
            'Backup: false\n'
            'Required: false\n'
            'Filename: /var/cache/debconf/passwords.dat\n'
            '\n'
            'Name: templates\n'
            'Driver: File\n'
            'Mode: 644\n'
            'Filename: /var/cache/debconf/templates.dat\n'
            '\n'
            'Name: configdb\n'
            'Driver: Stack\n'
            'Stack: config, passwords\n'
            '\n'
            'Name: templatedb\n'
            'Driver: Stack\n'
            'Stack: templates\n'
        )
        self._write_chroot_file('/etc/debconf.conf', _debconf_conf_content)

        # Ensure the debconf data directory and seed files exist so debconf
        # does not fail trying to create them during installation.
        _debconf_dir = os.path.join(self._dir_chroot, 'var/cache/debconf')
        subprocess.run(
            ['sudo', '-S', 'mkdir', '-p', _debconf_dir],
            input=self._password + '\n', capture_output=True, text=True
        )
        for _f in ['config.dat', 'passwords.dat', 'templates.dat']:
            subprocess.run(
                ['sudo', '-S', 'touch', os.path.join(_debconf_dir, _f)],
                input=self._password + '\n', capture_output=True, text=True
            )

    def _setup_chroot_env(self):
        """Set environment variables required for non-interactive dpkg in a chroot."""
        os.environ['PATH'] = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
        os.environ['DPKG_ROOT'] = self._dir_chroot
        os.environ['DEBIAN_FRONTEND'] = 'noninteractive'
        os.environ['DEBCONF_NONINTERACTIVE_SEEN'] = 'true'

    def _get_deb_files(self, pkg_list: list) -> list:
        """Resolve package names to absolute .deb paths in the repo directory.

        Uses the Filename field from the binary Packages index and strips any
        binNMU suffix (+bN) via strip_build_version() so the path matches what
        dpkg-buildpackage actually produced on disk.
        """
        file_list = []
        for pkg in pkg_list:
            _filename = os.path.basename(
                self._dependencytree.selected_pkgs[pkg]['Filename']
            )
            _filename = self.strip_build_version(_filename)
            _filepath = os.path.join(self._dir_repo, _filename)
            if not os.path.exists(_filepath):
                tui.console.print(f"WARNING: .deb not found, skipping: {_filename}")
                tui.console.warning(f"_get_deb_files: missing {_filepath}")
                continue
            file_list.append(_filepath)
        return file_list

    def _unpack_packages(self, pkg_list: list, fh) -> set:
        """Run dpkg --unpack for pkg_list inside the chroot.

        --force-script-chrootless lets maintainer scripts run without a
        chroot bind-mount; --no-triggers defers trigger processing to
        configure time.

        Returns the set of base package names dpkg reported as
        "Unpacking NAME:arch (ver)".  Packages dpkg rejected (pre-dep
        failures etc.) are not in the returned set; the caller's
        _configure_packages surfaces the actual failure when it tries
        to configure a missing package.

        Failure routing:
          - Total failure (rc != 0, nothing unpacked) — console error
            with the LAST line of stderr (dpkg's real message).
          - Partial failure (some unpacked) — log-tab warning only;
            the configure step will produce the actionable error.
        """
        if not pkg_list:
            return set()
        _chroot = self._dir_chroot
        # No -D1: dpkg debug-level-1 prints a D000001 line per internal
        # action to stderr.  That noise (a) provides nothing actionable
        # in production and (b) drowns the real failure message when we
        # later try to surface stderr to the user.
        _cmd = shlex.split(
            f'sudo -S env DEBIAN_FRONTEND=noninteractive DEBCONF_NONINTERACTIVE_SEEN=true '
            f'dpkg --root={_chroot} --instdir={_chroot} '
            f'--admindir={_chroot}/var/lib/dpkg '
            f'--force-script-chrootless --no-triggers --unpack'
        ) + self._get_deb_files(pkg_list)

        fh.write(f'Unpack: {" ".join(pkg_list)}\n')
        _proc = subprocess.run(_cmd, input=self._password + '\n',
                               capture_output=True, text=True, env=os.environ)
        fh.write(_proc.stdout)

        # Parse successfully unpacked package names from dpkg output first
        # so the failure classifier below can distinguish partial from total.
        _unpacked: set = set()
        for _line in _proc.stdout.splitlines():
            _m = re.match(r'^Unpacking (\S+)', _line)
            if _m:
                _unpacked.add(_m.group(1).split(':')[0])

        if _proc.returncode != 0:
            fh.write(_proc.stderr)
            _failed = [p for p in pkg_list if p not in _unpacked]
            if _unpacked:
                tui.console.warning(
                    f'_unpack_packages: partial — '
                    f'{len(_unpacked)}/{len(pkg_list)} succeeded; '
                    f'unmet pre-deps for: '
                    f'{", ".join(_failed[:5])}'
                    f'{"…" if len(_failed) > 5 else ""}'
                )
            else:
                _stderr_lines = [
                    l for l in _proc.stderr.strip().splitlines() if l.strip()
                ]
                _stderr_tail = _stderr_lines[-1] if _stderr_lines else 'no stderr'
                tui.console.print(
                    f'Error unpacking {pkg_list[:3]}'
                    f'{"…" if len(pkg_list) > 3 else ""}: '
                    f'{_stderr_tail[:200]}'
                )

        return _unpacked

    def get_install_sequence(self, selected_pkgs: [], installed_pkgs: []) -> []:
        """Produce a topologically sorted installation order for selected_pkgs.

        Algorithm — iterative leaf-removal (Kahn's algorithm on a forest):
          1. Build one dependency tree per package: root = package, children =
             its direct dependencies (Package.depends_on).
          2. Strip any dependency node that is already in installed_pkgs — those
             constraints are already satisfied and don't affect ordering.
          3. Repeatedly harvest all trees whose root has no remaining children
             (i.e. all dependencies satisfied).  Each harvest is one installation
             batch; packages within a batch are mutually independent and can be
             installed together.  After harvesting, remove those package names
             from every other tree's dependency lists so their dependents may
             become eligible in the next round.
          4. Terminate when no trees remain (success) or when a round produces
             nothing despite non-empty trees still existing (failure).

        The failure case arises in two situations:
          - True circular dependency (A→B→A): neither tree ever becomes childless.
          - Dependency on a package outside selected_pkgs that is also not in
            installed_pkgs: the child node is never removed, so the parent tree
            never clears.

        Args:
            selected_pkgs:  Package names to install, in any order.
            installed_pkgs: Package names already present in the chroot; their
                            dependency obligations are treated as satisfied.

        Returns:
            List of batches, where each batch is a list of package names that
            can be installed together (all their deps satisfied by earlier batches
            or by installed_pkgs).

        Raises:
            ValueError: if a circular or unresolvable dependency is detected,
                        with a message naming each stuck package and its unmet deps.
        """
        sequence = []
        collection = []

        # Build one tree per package with its direct dependencies as children.
        for _pkg in selected_pkgs:
            tree = utils.Tree()
            tree.add_node(_pkg)
            collection.append(tree)

            for leaf in self._dependencytree.selected_pkgs[_pkg].depends_on:
                tree.add_node(leaf, tree.root.value)

        # Remove already-installed packages from all dependency lists so they
        # don't block packages that only depend on them.
        for _pkg in installed_pkgs:
            for _tree in collection:
                if _tree.find_node(_pkg):
                    _tree.delete_node(_pkg)

        while True:
            # Childless non-empty trees: root's dependencies are all satisfied.
            pkg_list = [_tree.root.value for _tree in collection
                        if _tree.is_childless and not _tree.is_empty]

            if not pkg_list:
                # No childless trees this round.  If any trees are still
                # non-empty their deps can never be cleared — circular or
                # unresolvable dependency.
                #
                # NOTE: the original code checked `is_childless and not is_empty`
                # here, which is the same predicate as pkg_list and is therefore
                # always False when pkg_list is empty.  The correct check is
                # simply `not is_empty`.
                stuck = [_tree for _tree in collection if not _tree.is_empty]
                if stuck:
                    _details = '; '.join(
                        f"{_tree.root.value} waiting on "
                        f"[{', '.join(c.value for c in _tree.root.children)}]"
                        for _tree in stuck
                    )
                    raise ValueError(
                        f"Circular or unresolvable dependencies detected: {_details}"
                    )
                # All trees are empty — every package was placed in a batch.
                break

            # Remove this batch's packages from every tree so their dependents
            # may become childless in the next round.
            for _pkg in pkg_list:
                for _tree in collection:
                    if _tree.find_node(_pkg):
                        _tree.delete_node(_pkg)

            sequence.append(pkg_list)

        return sequence

    def _build_chroot_directories(self):
        """Create the standard FHS directory structure inside the chroot.

        Sequence matters here — usrmerge symlinks must be created before any
        directory tree expansion, otherwise os.makedirs will create a real /lib
        (and /bin, /sbin) which conflicts with dpkg's expectation that those
        paths are symlinks into /usr.

        Step 1 — usrmerge symlinks
            Since Debian bookworm all of /lib, /lib32, /lib64, /bin, /sbin are
            symlinks into /usr (the 'usrmerge' change).  dpkg and the packages
            it installs hard-depend on this layout: if any of those names exist
            as a real directory, package installation into /usr/lib (for example)
            will not be visible through /lib, breaking ld.so and any binary that
            embeds the old path.

            The symlink targets are relative (e.g. 'usr/lib' not '/usr/lib') so
            they remain valid when the chroot is bind-mounted or moved.  The
            /usr/<name> target directories are created with os.makedirs before
            os.symlink so the link always points at an existing directory.

        Step 2 — remaining FHS tree
            Only directories that cannot exist as symlinks are listed here.
            /usr/bin, /usr/sbin, /usr/lib* are already created in step 1, so
            they appear in the expansion list but os.makedirs(exist_ok=True)
            will simply skip them.

        Ref: https://www.linuxfromscratch.org/lfs/view/development/chapter07/creatingdirs.html
        Note: man(1..8) directories are intentionally omitted for now.
        TODO: load dir/owner/permission list from a config file rather than hardcoding.
        """

        # Step 1: usrmerge — create /usr/<name> targets then symlink the legacy
        # top-level names to them.  Order: mkdir target → symlink, so the link
        # is never dangling even if a later step fails partway through.
        _usrmerge = ['bin', 'sbin', 'lib', 'lib32', 'lib64']
        for _name in _usrmerge:
            _target = os.path.join(self._dir_chroot, 'usr', _name)
            _link   = os.path.join(self._dir_chroot, _name)
            os.makedirs(_target, exist_ok=True)
            if not os.path.lexists(_link):
                # Relative target keeps the symlink valid after the chroot is
                # moved or bind-mounted; 'usr/lib' resolves relative to chroot/
                # (the parent of the symlink), giving chroot/usr/lib.
                os.symlink(os.path.join('usr', _name), _link)

        # Step 2: remaining FHS tree.  /usr/{bin,sbin,lib,...} are listed
        # explicitly so the expansion is self-documenting, even though step 1
        # already created them — makedirs with exist_ok=True is idempotent.
        # /lib/{firmware} is intentionally absent: firmware lives at
        # /usr/lib/firmware and is reachable via the /lib symlink.
        dir_structure = [
            '/{boot,home,mnt,opt,srv,sys,proc,dev}',
            '/etc/{opt,sysconfig}',
            '/media/{floppy,cdrom}',
            '/usr/{bin,sbin,lib,lib32,lib64,local,include,src,share}',
            '/usr/lib/{firmware}',
            '/usr/local/{bin,lib,sbin,include,src,share}',
            '/usr/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
            '/usr/local/share/{color,dict,doc,info,locale,man,misc,terminfo,zoneinfo}',
            '/var/{cache,local,log,mail,opt,spool}',
            '/var/lib/{color,misc,locate}',
        ]

        for _dir in dir_structure:
            utils.create_folders(self._dir_chroot + _dir)

    def pre_install(self):
        # Two parts here - copy files and then run commands
        # TODO: Let it load from file rather than hard coding it, risk of something malicious coming in though
        # Parse files to copy
        for root, dirs, files in os.walk(self._dir_preinstall_patch):

            if len(files) == 0:
                continue

            # reached the list of files, here on three steps will be taken
            # use relative path and create if not existing in chroot dir
            chroot_relative_dir = root.replace(self._dir_preinstall_patch, self._dir_chroot)
            pathlib.Path(chroot_relative_dir).mkdir(parents=True, exist_ok=True)

            for _file in files:
                _orig_file = os.path.join(root, _file)
                if os.path.splitext(_file)[1] != '.patch':
                    # Non-patch files are copied directly into the chroot directory.
                    # Note: cp does not preserve all permissions — packages that
                    # need specific ownership must be handled in the cmd_list below.
                    _proc = subprocess.run(['sudo', '-S', 'cp', _orig_file, chroot_relative_dir],
                                           input=self._password + '\n', capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed copying pre-install file — {_file}')
                        tui.console.error(f'pre_install cp {_file}: {_proc.stderr}')
                else:
                    # Patch files are applied relative to chroot_relative_dir.
                    # Use -i to pass the patch file directly; '<' is a shell
                    # redirect and is not valid as a subprocess argument.
                    _proc = subprocess.run(['patch', '-p1', '-i', _orig_file],
                                           cwd=chroot_relative_dir,
                                           capture_output=True, text=True, env=os.environ)
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed applying pre-install patch — {_file}')
                        tui.console.error(f'pre_install patch {_file}: {_proc.stderr}')

        # Parse commands to execute, since nothing else has been till now, it is usually used to set right permission
        # All commands use sudo -S so the password is read from stdin rather than
        # the terminal (which would corrupt TUI rendering).  The '\n' terminator
        # is required by sudo -S — without it sudo blocks waiting for more input.
        cmd_list = [f'sudo -S ln -sfv /run {self._dir_chroot}/var/run',
                    f'sudo -S ln -sfv /run/lock {self._dir_chroot}/var/lock',
                    f'sudo -S install -dv -m 0750 {self._dir_chroot}/root',
                    f'sudo -S install -dv -m 1777 {self._dir_chroot}/tmp {self._dir_chroot}/var/tmp',
                    f'sudo -S chgrp -v utmp {self._dir_chroot}/var/log/lastlog',
                    f'sudo -S chmod -v 664 {self._dir_chroot}/var/log/lastlog',
                    f'sudo -S chmod -v 600 {self._dir_chroot}/var/log/btmp',
                    f'sudo -S chmod -R 755 {self._dir_chroot}/etc/'
                    ]

        for _cmd in cmd_list:
            _proc = subprocess.run(shlex.split(_cmd), input=self._password + '\n',
                                   capture_output=True, text=True)
            if _proc.returncode != 0:
                tui.console.print(f"WARNING: pre-install command failed: {_cmd}")
                tui.console.warning(f"pre_install cmd: {_cmd}\n{_proc.stdout.strip()}")

    def post_install(self):
        """Apply post-install overlay files and patches into the chroot.

        Runs after all packages are unpacked and configured.  Use this for:
          - Distro-specific config file overrides that dpkg would otherwise
            reset (e.g. /etc/os-release, /etc/hostname, /etc/locale.conf)
          - Files that must land after dpkg creates the target path
          - Patches against files installed by packages

        Directory layout mirrors pre_install: files under dir_patch_postinstall
        are placed at the same relative path inside the chroot.  Files with a
        .patch extension are applied with patch -p1 -i; all other files are
        copied verbatim.

        Unlike pre_install there is no hardcoded cmd_list — post-install
        permission fixups belong in the overlay files themselves or in
        generate_system_configs().
        """
        for root, dirs, files in os.walk(self._dir_postinstall_patch):
            if not files:
                continue

            # Mirror the source directory structure into the chroot.
            chroot_relative_dir = root.replace(self._dir_postinstall_patch, self._dir_chroot)
            pathlib.Path(chroot_relative_dir).mkdir(parents=True, exist_ok=True)

            for _file in files:
                _orig_file = os.path.join(root, _file)
                if os.path.splitext(_file)[1] != '.patch':
                    _proc = subprocess.run(
                        ['sudo', '-S', 'cp', _orig_file, chroot_relative_dir],
                        input=self._password + '\n', capture_output=True, text=True, env=os.environ
                    )
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed copying post-install file — {_file}')
                        tui.console.error(f'post_install cp {_file}: {_proc.stderr}')
                else:
                    _proc = subprocess.run(
                        ['patch', '-p1', '-i', _orig_file],
                        cwd=chroot_relative_dir,
                        capture_output=True, text=True, env=os.environ
                    )
                    if _proc.returncode != 0:
                        tui.console.print(f'Error: Failed applying post-install patch — {_file}')
                        tui.console.error(f'post_install patch {_file}: {_proc.stderr}')

    def generate_system_configs(self, debug: bool = False):
        """Write base system configuration files into the chroot.

        Args:
            debug: When True, also write
                /etc/systemd/journald.conf.d/50-console.conf to forward all
                journal entries to /dev/console (= ttyS0 with serial boot).
                Off by default to keep production images quiet.

        Covers files that dpkg does not create but the OS needs to be
        functional and identifiable.  All files are written via
        _write_chroot_file() which uses sudo -S tee so ownership is correct
        even after dpkg has made the chroot root-owned.

        Files written:
          /etc/os-release      — OS identity (name, version, codename)
          /etc/hostname        — default hostname
          /etc/hosts           — localhost resolution
          /etc/fstab           — virtual filesystem mount points
          /etc/machine-id      — empty; systemd generates on first boot
          /etc/apt/sources.list — APT repository for the installed system
        """
        cfg = self._config

        # /etc/os-release — standard file read by systemd, lsb_release, etc.
        self._write_chroot_file('/etc/os-release', (
            f'# Athena Linux — (c) 2025-26 Harkirat S Virk\n'
            f'NAME="{cfg.build_codename}"\n'
            f'VERSION="{cfg.build_version}"\n'
            f'ID=athena\n'
            f'ID_LIKE=debian\n'
            f'VERSION_CODENAME={cfg.build_codename.lower()}\n'
            f'PRETTY_NAME="{cfg.build_codename} {cfg.build_version}"\n'
            f'HOME_URL="https://athenalinux.org"\n'
            f'VENDOR_NAME="Harkirat S Virk"\n'
        ))

        # /etc/issue — pre-login banner on TTY consoles. Uses agetty escapes:
        #   \S = PRETTY_NAME from os-release, \r = kernel, \l = tty
        self._write_chroot_file('/etc/issue', (
            f'{cfg.build_codename} {cfg.build_version} \\n \\l\n'
            f'\n'
            f'(c) 2025-26 Harkirat S Virk\n'
            f'\n'
        ))

        # /etc/hostname — a sensible default; can be changed post-install.
        self._write_chroot_file('/etc/hostname', 'athena\n')

        # /etc/hosts — minimal localhost entries required for basic name
        # resolution before a real DNS resolver is configured.
        self._write_chroot_file('/etc/hosts', (
            '127.0.0.1   localhost\n'
            '127.0.1.1   athena\n'
            '::1         localhost ip6-localhost ip6-loopback\n'
            'ff02::1     ip6-allnodes\n'
            'ff02::2     ip6-allrouters\n'
        ))

        # /etc/fstab — virtual filesystems that systemd or init needs at boot.
        # Real block devices (root, swap, efi) are left out — they are image-
        # or hardware-specific and should be added by the image-build stage.
        # No /tmp entry here: systemd ships its own tmp.mount unit and
        # listing /tmp in fstab causes systemd-fstab-generator to write a
        # duplicate /run/systemd/generator/tmp.mount and fail at boot.
        self._write_chroot_file('/etc/fstab', (
            '# <file system>  <mount point>  <type>     <options>  <dump>  <pass>\n'
            'proc             /proc          proc       defaults   0       0\n'
            'sysfs            /sys           sysfs      defaults   0       0\n'
            'devtmpfs         /dev           devtmpfs   defaults   0       0\n'
        ))

        # /etc/machine-id — empty file; systemd-machine-id-setup will populate
        # it on first boot.  Must exist (even empty) for systemd to start.
        self._write_chroot_file('/etc/machine-id', '')

        # /etc/apt/sources.list — lets the installed system update itself
        # from the same mirrors used to build it.  Composed from the
        # configured mirrors so a config change rebases the live system too.
        # Extra components (contrib/non-free/non-free-firmware) added to each
        # line so the running system can install firmware/non-free pkgs even
        # though our build only consumes 'main'.
        _extra_comp = 'contrib non-free non-free-firmware'
        _lines = [
            f'deb {_m.url} {_m.suite} {_m.component} {_extra_comp}\n'
            for _m in cfg.mirrors
        ]
        self._write_chroot_file('/etc/apt/sources.list', ''.join(_lines))

        # Forward all journal entries to /dev/console (= ttyS0 via console=ttyS0
        # kernel cmdline) so auth/PAM failures are visible on serial output
        # even when autologin is broken and no interactive login is possible.
        # Opt-in via 'build_chroot with_debug' — production builds skip this.
        if debug:
            self._write_chroot_file(
                '/etc/systemd/journald.conf.d/50-console.conf',
                '[Journal]\n'
                'ForwardToConsole=yes\n'
                'MaxLevelConsole=info\n'
            )
            tui.console.print("journald console forwarding configured (ttyS0) — debug mode")

        _proc = subprocess.run(
            ['sudo', '-S', 'systemd-firstboot',
             f'--root={self._dir_chroot}',
             '--root-password=root',
             '--hostname=athena',
             '--timezone=UTC',
             '--locale=C.UTF-8',
             '--setup-machine-id',
             '--force'],
            input=self._password + '\n',
            capture_output=True, text=True
        )
        if _proc.returncode != 0:
            tui.console.print(f"WARNING: systemd-firstboot failed: {_proc.stderr.strip()}")
            tui.console.warning(f"systemd-firstboot stderr: {_proc.stderr.strip()}")
        else:
            tui.console.print("systemd-firstboot: root password / hostname / machine-id configured")

        tui.console.print("System configuration files written")

    def _write_chroot_file(self, rel_path: str, content: str):
        """Write content to rel_path inside the chroot as root.

        Uses a NamedTemporaryFile + sudo cp rather than sudo tee with stdin,
        because when sudo's credential is already cached it does NOT consume the
        password line from stdin, so tee would receive and write the raw password
        as the first line of the file.

        Args:
            rel_path: Path relative to the chroot root (e.g. '/etc/hostname').
            content:  Text content to write; may be empty.
        """
        _dest = os.path.join(self._dir_chroot, rel_path.lstrip('/'))

        _parent = os.path.dirname(_dest)
        subprocess.run(
            ['sudo', '-S', 'mkdir', '-p', _parent],
            input=self._password + '\n', capture_output=True, text=True
        )

        with tempfile.NamedTemporaryFile(mode='w', suffix='.chroot-write', delete=False) as _tf:
            _tf.write(content)
            _tmp_path = _tf.name

        try:
            _proc = subprocess.run(
                ['sudo', '-S', 'cp', _tmp_path, _dest],
                input=self._password + '\n', capture_output=True, text=True
            )
            if _proc.returncode != 0:
                tui.console.print(f"ERROR: Failed to write {rel_path} into chroot")
                tui.console.error(f"_write_chroot_file {rel_path}: {_proc.stderr}")
        finally:
            os.unlink(_tmp_path)

    # ── ISO build ─────────────────────────────────────────────────────────────

