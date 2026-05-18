"""Dependency-drift detection for BuildSystem.

Mixin: `_DepDriftMixin` adds two methods to `BuildSystem`:

  * `_check_dep_drift` patches each canonical Package's dep fields from
    the on-disk .deb (the cache and built .debs can disagree when a
    binNMU rebuild has shifted runtime deps under the same source
    version), then chains into…
  * `_verify_dep_resolution` which walks every synced dep edge and
    raises if anything is unresolved or version-mismatched in
    selected_pkgs.

Both methods access `self._dependencytree`, `self._dir_repo`, and
`self.strip_build_version` — provided by `BuildSystem`.
"""

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Callable

import tui
import utils

if TYPE_CHECKING:
    import dependencytree

logger = logging.getLogger('athena')


class _DepDriftMixin:
    # Instance attributes set by `BuildSystem.__init__` (the composer
    # that mixes this in).  Type-only stubs for mypy; no runtime
    # assignment here.
    _dependencytree: 'dependencytree.DependencyTree'
    _dir_repo: str
    _config: 'utils.BuildConfig'
    strip_build_version: Callable[[str], str]

    def _check_dep_drift(self):
        """Patch Package dep fields from the on-disk .debs and verify resolution.

        The Packages cache and the downloaded .debs may be out of sync when a
        binNMU rebuild has shifted runtime deps under the same source version.
        Two stanzas can describe the "same" package with different Depends:

          Cache index entry (fetched from deb.debian.org)
              Package: libcom-err2
              Version: 1.47.0-2+b2
              Depends: libc6 (>= 2.17)

          Locally-built .deb in repo/
              Package: libcom-err2
              Version: 1.47.0-2
              Depends: libnsl2, libc6 (>= 2.17)

        The buildd's binNMU (+b2) was rebuilt after upstream silently dropped
        the libnsl2 link; we built from the unsuffixed source and still link
        against libnsl2.  If the configure-ordering forest is built from the
        cache, libcom-err2 looks like it only needs libc6 and is scheduled
        right after it — but dpkg installs the on-disk .deb, which actually
        Pre-Depends on libnsl2 being configured first, and refuses with:
            libcom-err2 depends on libnsl2; however:
            Package libnsl2 is not configured yet.

        Pass 1 — sync.  Overwrites depends / alt_depends / pre_depends /
        alt_pre_depends on every canonical Package object with what dpkg-deb
        reports.  All other fields (Version, Arch, Filename, …) are left
        untouched.  Drift is logged at info level (log tab only) in the form
        "Dep drift seen for package <pkg> from <cache_ver> to <disk_ver>".

        Pass 2 — verify.  After every package is synced, walk every (now-
        synced) dep field and check that each named dep is in selected_pkgs
        and that any version constraint is satisfied via apt_pkg.check_dep.
        OR-groups (alt_*) pass if at least one alternative resolves.  Any
        unresolved or version-mismatched dep is collected and the build
        aborts via RuntimeError — proceeding would just fail at install time
        with a less actionable message.
        """
        import package as _pkg_module
        for _pkg_name, _pkg_obj in self._dependencytree.canonical_pkgs.items():
            _filename = os.path.basename(_pkg_obj.get('Filename', ''))
            if not _filename:
                continue
            _filename  = self.strip_build_version(_filename)
            _filename  = utils.apply_distro_suffix(
                _filename, self._config.distro_suffix,
            )
            _deb_path  = os.path.join(self._dir_repo, _filename)
            if not os.path.exists(_deb_path):
                continue
            _proc = subprocess.run(
                ['dpkg-deb', '-f', _deb_path],
                capture_output=True, text=True
            )
            if _proc.returncode != 0 or not _proc.stdout.strip():
                continue
            _deb_pkg = _pkg_module.Package(_proc.stdout)
            if not _deb_pkg.isvalid:
                continue

            _drift = []
            for _field in ('pre_depends', 'depends', 'alt_pre_depends', 'alt_depends'):
                _cache_val = getattr(_pkg_obj, _field)
                _disk_val  = getattr(_deb_pkg, _field)
                if _cache_val != _disk_val:
                    _drift.append((_field, _cache_val, _disk_val))
            if _drift:
                _cache_ver = _pkg_obj.get('Version', '?')
                _disk_ver  = _deb_pkg.get('Version', '?')
                logger.info(
                    f"Dep drift seen for package {_pkg_name} "
                    f"from {_cache_ver} to {_disk_ver}"
                )
                for _field, _cache_val, _disk_val in _drift:
                    logger.info(f"  {_field}: from {_cache_val} to {_disk_val}")

            _pkg_obj.depends         = _deb_pkg.depends
            _pkg_obj.alt_depends     = _deb_pkg.alt_depends
            _pkg_obj.pre_depends     = _deb_pkg.pre_depends
            _pkg_obj.alt_pre_depends = _deb_pkg.alt_pre_depends

        self._verify_dep_resolution()

    def _verify_dep_resolution(self):
        """Second pass: confirm every synced dep resolves in selected_pkgs.

        For each canonical package, walk pre_depends + depends as hard
        requirements and alt_pre_depends + alt_depends as OR-groups.  A hard
        requirement passes when its name is in selected_pkgs (real or virtual
        alias) and the version constraint, if any, is satisfied by the
        provider's own version.  An OR-group passes when at least one
        alternative passes.  Aggregate all violations and raise at the end so
        a single run reports every problem rather than failing on the first.
        """
        import apt_pkg
        _selected = self._dependencytree.selected_pkgs

        def _resolves(dep_tuple):
            _name, _ver, _op = dep_tuple[0], dep_tuple[1], dep_tuple[2]
            _provider = _selected.get(_name)
            if _provider is None:
                return False, 'unresolved'
            if not _op:
                return True, None
            try:
                if apt_pkg.check_dep(str(_provider.version), _op, str(_ver)):
                    return True, None
                return False, f'version mismatch (have {_provider.version}, need {_op} {_ver})'
            except SystemError as e:
                return False, f'check_dep error: {e}'

        # Skip extras when verifying.  Extras are pulled into
        # selected_pkgs so source_download fetches them and source_build
        # (in `recommended` mode) can produce their .debs, but they are
        # NOT installed in the chroot — `_compute_install_batches`
        # filters them out via `extras_pkg_names`.  Their transitive
        # depends therefore don't need to resolve in OUR install set;
        # apt at install-time on the booted system resolves them via
        # upstream Debian sources.  Verifying them here would block the
        # chroot build on deps we never promised to satisfy (real
        # example: ca-certificates → openssl, xauth → libx11-6).
        _extras = getattr(self._dependencytree, 'extras_pkg_names', set())
        _violations = []
        for _pkg_name, _pkg_obj in self._dependencytree.canonical_pkgs.items():
            if _pkg_name in _extras:
                continue
            for _field in ('pre_depends', 'depends'):
                for _dep in getattr(_pkg_obj, _field):
                    _ok, _why = _resolves(_dep)
                    if not _ok:
                        _violations.append(
                            f"{_pkg_name} {_field}: {_dep[0]} "
                            f"({_dep[2]} {_dep[1]}) — {_why}"
                        )
            for _field in ('alt_pre_depends', 'alt_depends'):
                for _group in getattr(_pkg_obj, _field):
                    if not any(_resolves(_alt)[0] for _alt in _group):
                        _alts = ' | '.join(
                            f"{_alt[0]} ({_alt[2]} {_alt[1]})" if _alt[2] else _alt[0]
                            for _alt in _group
                        )
                        _violations.append(
                            f"{_pkg_name} {_field}: [{_alts}] — no alternative resolves"
                        )

        if _violations:
            logger.error(
                f"_verify_dep_resolution: {len(_violations)} unresolved dep(s) after sync"
            )
            for _v in _violations:
                logger.error(f"  {_v}")
            raise RuntimeError(
                f"_verify_dep_resolution: {len(_violations)} unresolved dep(s); "
                "see log for details"
            )
