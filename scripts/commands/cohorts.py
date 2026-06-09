"""Selection-cohort and tunnel-filename resolvers — shared resolution
infrastructure.

Pure read-only helpers that map the resolved dependency trees onto the
package/binary cohorts the rest of the pipeline reasons about: live /
deb / udeb / installer cohorts, the install corpus, canonical-name
projection, and the predicted/tunnel filename sets for a source.  Used
across the audit, source, build and tunnel command mixins; consolidated
here so they have one home rather than living mid-BuildSession.  See
commands/base.py for how the mixin shares session state.
"""
import logging
from typing import Optional

import apt_pkg
import dependencytree

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class CohortResolverMixin(SessionState):
    @staticmethod
    def _canonical_names(tree) -> 'set[str]':
        """Return only canonical-name keys from selected_pkgs.

        DependencyTree.selected_pkgs registers BOTH canonical pkg names
        AND every virtual name a selected pkg provides (parse_dependency
        L494's Provides walk).  Raw .keys() therefore includes virtual
        aliases.

        For cohort/corpus scopes used by the audit, virtual aliases are
        misleading: a name like 'fuse' that's in selected_pkgs only as
        an alias of fuse3 (via `Provides: fuse`) doesn't represent a
        pkg dpkg would install under that name.  Including it as a
        cohort member causes audit_conflict_cohort to false-positive on
        the canonical Debian fork-replaces-upstream idiom:
          fuse3 Provides: fuse + fuse3 Breaks: fuse
                  → 'fuse' is in cohort (as virtual)
                  → audit flags fuse3 → fuse conflict
                  → but at install only fuse3 is installed; no real
                    'fuse' package present → no actual conflict

        Filter to canonical names (where the key == the underlying
        Package's own 'Package' field).  Matches what dpkg actually
        sees at install time.
        """
        return {
            n for n, p in tree.selected_pkgs.items()
            if n == p['Package']
        }

    def _resolve_live_cohort(self) -> Optional[frozenset]:
        """The set of pkgs that get dpkg-installed in the live chroot
        simultaneously — the scope within which Conflicts/Breaks are
        hard violations.

        = dep_tree.selected_pkgs (canonical names only)
          − pool_extras_pkg_names   (pool-only; not auto-installed)
          − installer_exclusive_pkg_names  (installer-support debs not
            in the live closure)

        Returns None when dep_tree isn't populated (operator hasn't
        run `cache parse`).  Caller should fall back to a coarser check
        or print a hint.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        _selected = self._canonical_names(self.dep_tree)
        _selected -= getattr(self.dep_tree, 'pool_extras_pkg_names', set())
        _selected -= getattr(self.dep_tree, 'installer_exclusive_pkg_names',
                              set())
        return frozenset(_selected)

    def _predicted_files_for_source(self, src_name: str) -> 'list[str]':
        """Union of deb-tree + udeb-tree predicted binary filenames for
        this source.  Order: deb entries first, udeb appended.

        Used by every reader that needs "what binaries will this source
        produce in the repo after a successful build?" — check_build,
        cmd_source_audit, cmd_source_repair, cmd_audit's content-
        integrity section, _do_tunnel.  Pulled here because the two
        trees store per-tree maps (src_pkg_files); Source objects no
        longer carry a .pkgs attribute (it leaked across trees — see
        dependencytree.py:src_pkg_files docstring for full history).
        """
        _files: 'list[str]' = []
        if self.dep_tree is not None:
            _files.extend(self.dep_tree.src_pkg_files.get(src_name) or [])
        if self.udeb_dep_tree is not None:
            for _f in (self.udeb_dep_tree.src_pkg_files.get(src_name) or []):
                if _f not in _files:
                    _files.append(_f)
        return _files

    def _tunnel_filenames_for_source(self, src_name: str) -> 'list[str]':
        """Same shape as _predicted_files_for_source but returns the ACTUAL
        upstream binary filenames (Filename: from the cached Packages record)
        instead of strip_nmu pristine names.

        Tunneled packages are pristine Debian passthrough — the on-disk file
        keeps its upstream NMU/security suffix (e.g. ~deb12u1) and the .deb's
        internal control Version matches that filename.  Snapshot.debian.org
        serves the file at that suffixed path; the pristine-stripped URL would
        404 (or worse, coincidentally hit a same-named unstable binary — wrong
        version, silent corruption, caught 2026-05-28 when amd64-microcode
        downloaded the unsuffixed unstable build instead of the bookworm-
        security ~deb12u1 build).

        OPTION A (2026-06-08): tunnel the source's FULL declared binary
        set, filtered by arch + active build PROFILES — the SAME gates
        virtual_build uses to predict — NOT just the dep-closure subset.
        A built source emits (and we keep) its whole binary set; a
        tunneled source must contribute the same complete set, so e.g.
        every firmware-nonfree blob lands in /cdrom/pool (not only the
        few the installed system happens to need) and the `.vbuildlog`
        prediction matches the on-disk reality.  Binaries outside the
        closure resolve against the FULL cache universe, not just
        selected_pkgs.  Falls back to the closure subset when the cache
        isn't loaded yet (tunnel still works pre-`cache parse`).
        """
        import virtual_build as _vb
        _cache = getattr(self, 'cache', None)
        _src = None
        if _cache is not None:
            _cands = _cache.source_hashtable.get(src_name, [])
            _src = _cands[0] if _cands else None
        if _src is None:
            return self._tunnel_filenames_subset(src_name)
        _pl_idx = _vb._package_list_index(
            _src, fork_dsc_dir=getattr(
                self.config, 'dir_fork_source_repo', None))
        _profiles = frozenset(
            getattr(self.config, 'build_profiles', frozenset()))
        _arch = self.config.arch
        _actual: 'list[str]' = []
        _seen: 'set[str]' = set()
        for _bin in (getattr(_src, 'binary', []) or []):
            _entry = _pl_idx.get(_bin, _bin)
            if not _vb._binary_active_for_arch(_entry, _arch):
                continue
            if not _vb._binary_active_under_profiles(_entry, _profiles):
                continue
            _fn = self._resolve_tunnel_filename(_bin, _entry)
            if not _fn:
                logger.warning(
                    f"tunnel: binary {_bin!r} of source {src_name!r} "
                    f"unresolvable in cache — skipped")
                continue
            if _fn not in _seen:
                _seen.add(_fn)
                _actual.append(_fn)
        return _actual

    def _tunnel_filenames_subset(self, src_name: str) -> 'list[str]':
        """Pre-Option-A behaviour: upstream filenames for the dep-closure
        binaries only.  Fallback path when the cache isn't loaded."""
        _actual: 'list[str]' = []
        for _f in self._predicted_files_for_source(src_name):
            _bin_name = _f.split('_', 1)[0]
            _pkg = None
            if self.dep_tree is not None:
                _pkg = self.dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None and self.udeb_dep_tree is not None:
                _pkg = self.udeb_dep_tree.selected_pkgs.get(_bin_name)
            if _pkg is None:
                _actual.append(_f)
                continue
            _fn = (_pkg.get('Filename') or '').rsplit('/', 1)[-1]
            _actual.append(_fn or _f)
        return _actual

    def _resolve_tunnel_filename(self, bin_name: str,
                                pl_entry: str) -> str:
        """Upstream Filename basename for one binary.  selected_pkgs
        (closure — already version-resolved) first; otherwise the full
        cache universe (the extra non-closure binaries Option A adds),
        picking the highest-version record.  '' when unresolvable.

        The Package-List type token (`deb`/`udeb`) routes the cache
        lookup to the right table so a udeb resolves against the udeb
        universe, not the deb one."""
        _pkg = None
        if self.dep_tree is not None:
            _pkg = self.dep_tree.selected_pkgs.get(bin_name)
        if _pkg is None and self.udeb_dep_tree is not None:
            _pkg = self.udeb_dep_tree.selected_pkgs.get(bin_name)
        _cache = getattr(self, 'cache', None)
        if _pkg is None and _cache is not None:
            _tokens = pl_entry.split()
            _is_udeb = len(_tokens) >= 2 and _tokens[1] == 'udeb'
            _view = _cache.udeb_view() if _is_udeb else _cache
            _best = None
            for _rec in _view.get_packages(bin_name):
                if (_best is None or apt_pkg.version_compare(
                        str(_rec.get('Version') or '0'),
                        str(_best.get('Version') or '0')) > 0):
                    _best = _rec
            _pkg = _best
        if _pkg is None:
            return ''
        return (_pkg.get('Filename') or '').rsplit('/', 1)[-1]

    def _resolve_deb_cohort(self) -> Optional[frozenset]:
        """Consumers audited as the .deb-cohort by package_audit's
        DEP-GATE.  = dep_tree.selected_pkgs canonical names (everything
        we install via debootstrap, tasksel/apt at install time, or
        live-chroot batch).

        Excludes the udeb tree.  Audited separately so each cohort's
        unresolved surface is visible — the old combined audit hid
        per-cohort breakdowns and made it hard to tell which gap came
        from which install path.

        Canonical-name filter: see _canonical_names docstring for why
        virtual-alias keys must be excluded from scope sets.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        return frozenset(self._canonical_names(self.dep_tree))

    def _resolve_udeb_cohort(self) -> Optional[frozenset]:
        """Consumers audited as the .udeb-cohort by package_audit's
        DEP-GATE.  = udeb_dep_tree.selected_pkgs canonical names
        (everything dpkg-unpacked into the d-i installer ramdisk).

        Resolution still spans the whole repo per Option B — udebs
        with deb deps (~9 known upstream metadata cases like
        at-spi2-core-udeb → libsystemd0, libgtk-4-1-udeb → libtiff6)
        get resolved against deb providers, matching d-i's runtime
        behaviour where the deb gets debootstrapped onto /target.
        """
        if (not self.udeb_dep_tree
                or not self.udeb_dep_tree.selected_pkgs):
            return None
        return frozenset(self._canonical_names(self.udeb_dep_tree))

    def _resolve_install_corpus(self) -> Optional[frozenset]:
        """[pkg + installer + live + pool] — your hard-dep gate scope.

        = dep_tree.selected_pkgs ∪ udeb_dep_tree.selected_pkgs
          (canonical names from each tree)

        Every pkg in this union ends up dpkg-installed somewhere — in
        the live chroot, the d-i ramdisk, or the target via tasksel +
        apt at install time.  Their Depends are install-time hard
        constraints.

        Pkgs OUTSIDE this union are side artifacts of dpkg-buildpackage
        (libfoo-dev / -doc / -tests / -dbgsym from sources we built
        but didn't select).  Their Depends never resolve at runtime
        because they never install.

        Returns None when dep_tree isn't populated (operator hasn't
        run `cache parse`); caller falls back to whole-repo audit with
        a hint.
        """
        if not self.dep_tree or not self.dep_tree.selected_pkgs:
            return None
        _all = self._canonical_names(self.dep_tree)
        if self.udeb_dep_tree and self.udeb_dep_tree.selected_pkgs:
            _all |= self._canonical_names(self.udeb_dep_tree)
        return frozenset(_all)

    def _resolve_installer_cohort(self) -> Optional[frozenset]:
        """The set of pkgs that get dpkg-unpacked into the d-i installer
        ramdisk — the scope for installer conflict checks.

        = udeb_dep_tree.selected_pkgs (canonical names)

        Pool / live / pkg debs are NOT in this scope (the ramdisk is
        udeb-only; debs are pulled by the installer onto the target
        system, which is a separate install scenario).
        """
        if (not self.udeb_dep_tree
                or not self.udeb_dep_tree.selected_pkgs):
            return None
        return frozenset(self._canonical_names(self.udeb_dep_tree))
