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

logger = logging.getLogger('athena.cache')


def _dep_target_names(_obj) -> 'set[str]':
    """Every dependency-target NAME across a Package's hard + alternative
    dep fields (depends / pre_depends / alt_depends / alt_pre_depends).

    Names only — version constraints are intentionally ignored, so an
    NMU-strip that rewrites `(= 1.47.0-2)` → `(>= …)` is NOT seen as a
    removal (STA-24 diffs on dep PRESENCE, not on constraints)."""
    _names: 'set[str]' = set()
    for _f in ('depends', 'pre_depends'):
        for _t in getattr(_obj, _f, None) or ():
            if _t and _t[0]:
                _names.add(_t[0])
    for _f in ('alt_depends', 'alt_pre_depends'):
        for _grp in getattr(_obj, _f, None) or ():
            for _alt in _grp:
                if _alt and _alt[0]:
                    _names.add(_alt[0])
    return _names


def _provided_names(_obj) -> 'set[str]':
    """Every package NAME the Package satisfies via its `Provides:` field.

    apt treats `Depends: X` as satisfied by the real package X OR by any
    installed package that `Provides: X`.  STA-24 uses this to tell a real
    dropped edge from a benign modernisation: when shlibdeps emits a renamed
    library (libgcc-s1, which `Provides: libgcc1`) in place of a virtual /
    transitional name (libgcc1), the dependency is still satisfied — no
    package names are special-cased, it falls straight out of Provides."""
    _names: 'set[str]' = set()
    for _grp in getattr(_obj, 'provides', None) or ():
        for _p in _grp:
            if _p and _p[0]:
                _names.add(_p[0])
    return _names


class _DepDriftMixin:
    # Instance attributes set by `BuildSystem.__init__` (the composer
    # that mixes this in).  Type-only stubs for mypy; no runtime
    # assignment here.
    _dependencytree: 'dependencytree.DependencyTree'
    _dir_repo: str
    _dir_repo_main: str   # D
    _config: 'utils.BuildConfig'
    strip_build_version: Callable[[str], str]
    normalize_repo_filename: Callable[[str], str]

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
        alt_pre_depends AND Version on every canonical Package object with
        what dpkg-deb reports.  Other fields (Arch, Filename, …) are left
        untouched.  Drift is logged at info level (log tab only) in the form
        "Dep drift seen for package <pkg> from <cache_ver> to <disk_ver>".

        Fix-up (2026-05-22): the Version sync was added
        because dep constraints emerging from on-disk .debs are
        NMU-stripped (e.g. `(= 0.8-10)`), but the cache-derived Version
        on the SAME Package object was still un-stripped (`0.8-10+deb12u1`).
        _verify_dep_resolution then saw consumer-stripped vs provider-
        unstripped and reported 144 spurious mismatches on a clean repo.
        Syncing Version too keeps both sides of the dep constraint
        consistent.  Pre-Stage-D this didn't bite because dep_drift's
        path resolution missed the .debs entirely (empty old repo/main)
        and sync was a no-op — accidentally consistent.

        Pass 2 — verify.  After every package is synced, walk every (now-
        synced) dep field and check that each named dep is in selected_pkgs
        and that any version constraint is satisfied via apt_pkg.check_dep.
        OR-groups (alt_*) pass if at least one alternative resolves.  Any
        unresolved or version-mismatched dep is collected and the build
        aborts via RuntimeError — proceeding would just fail at install time
        with a less actionable message.
        """
        logger.info(
            f"dep_drift: scanning {len(self._dependencytree.canonical_pkgs)} "
            f"canonical pkg(s) for cache↔disk skew"
        )
        import package as _pkg_module
        # accumulate built packages that DROPPED a dependency edge
        # on a selected package vs the upstream record (the dangerous
        # dep-loss case — see the summary below).
        _dep_loss: 'list[tuple[str, list[str]]]' = []
        for _pkg_name, _pkg_obj in self._dependencytree.canonical_pkgs.items():
            _filename = os.path.basename(_pkg_obj.get('Filename', ''))
            if not _filename:
                continue
            _filename  = self.normalize_repo_filename(_filename)
            # _dir_repo_main is now dists/<codename>/
            # main/binary-<arch>/, set by buildsystem.py composer.  The on-disk
            # .deb may carry a +asg<R>u<N> stamp; find_matching_artifact accepts
            # the pristine name OR its stamped variant so drift detection isn't
            # silently skipped for stamped packages.
            _deb_path  = utils.find_matching_artifact(self._dir_repo_main, _filename)
            if not _deb_path:
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

            # a dep present on the upstream cache record but ABSENT
            # on the built .deb is a LOST dependency.  If its target is a
            # package we SELECT, the minimal SURFACES-01 closure won't pull
            # it in and it silently won't be installed — the e2fsprogs case
            # (built binaries lost libext2fs2/libcom-err2/libss2 when the
            # patch dropped `-L shlibs.local`), where the disk image's root
            # fsck then exec-failed (127) → boot loop.  Checked BEFORE the
            # sync below overwrites _pkg_obj's deps with the (broken) disk
            # truth, where the lost edge would already be invisible.
            # A dropped edge isn't a real loss when apt still satisfies it
            # through the NEW dep set: some package the built .deb now
            # depends on PROVIDES the dropped name.  Plain Debian dependency
            # semantics (a Depends is met by the real package OR a provider)
            # — this generically covers transitional renames (shlibdeps
            # emits libgcc-s1, which `Provides: libgcc1`, instead of the
            # virtual libgcc1) with NO package names special-cased.  A drop
            # with no provider in the new deps (xorg → libunwind8, nothing
            # Provides libunwind8) stays a real loss.
            _disk_names = _dep_target_names(_deb_pkg)
            _provided_by_new: 'set[str]' = set()
            for _dn in _disk_names:
                _dobj = (self._dependencytree.selected_pkgs.get(_dn)
                         or self._dependencytree.canonical_pkgs.get(_dn))
                if _dobj is not None:
                    _provided_by_new |= _provided_names(_dobj)
            _lost = (_dep_target_names(_pkg_obj) - _disk_names
                     - _provided_by_new)
            _lost_selected = sorted(
                _n for _n in _lost
                if _n in self._dependencytree.selected_pkgs)
            if _lost_selected:
                _dep_loss.append((_pkg_name, _lost_selected))

            _pkg_obj.depends         = _deb_pkg.depends
            _pkg_obj.alt_depends     = _deb_pkg.alt_depends
            _pkg_obj.pre_depends     = _deb_pkg.pre_depends
            _pkg_obj.alt_pre_depends = _deb_pkg.alt_pre_depends
            # fix-up: sync Version too — see method
            # docstring for the asymmetric-sync bug rationale.  Both
            # .version attr and the underlying ['Version'] field need
            # the update (different consumers read different surfaces).
            _pkg_obj.version = _deb_pkg.version
            try:
                _pkg_obj['Version'] = _deb_pkg.get(
                    'Version', _pkg_obj.get('Version', ''),
                )
            except (TypeError, KeyError):
                # Some Package subclasses don't expose __setitem__;
                # the .version attr update above is the load-bearing
                # one for _verify_dep_resolution.
                pass

        # surface dep-loss prominently — it never reaches
        # _verify_dep_resolution (that pass runs on the post-sync deps,
        # where the lost edge is already gone) and the legacy ship-
        # everything pool used to mask it; minimal closures make a lost
        # edge load-bearing.  Loud WARNING (both surfaces), not a hard gate
        # — a fork may intentionally drop a dep — but never silent.
        if _dep_loss:
            _total = sum(len(_v) for _, _v in _dep_loss)
            logger.warning(
                f"DEP-LOSS drift: {len(_dep_loss)} built package(s) dropped "
                f"{_total} dependency edge(s) on SELECTED packages vs the "
                f"upstream record — the minimal closure may NOT install "
                f"those targets (boot/runtime failure far from the cause; "
                f"cf. e2fsprogs→libext2fs2 2026-06-11).  Suspect a lost "
                f"`-L shlibs.local` mapping or a dropped Depends; verify "
                f"the build before shipping.")
            tui.console.print(
                f"WARNING: dep-loss drift — {len(_dep_loss)} built "
                f"package(s) lost dependency edges on selected packages:",
                tui.COLOR_WARNING)
            for _pkg_name, _lost_selected in _dep_loss:
                logger.warning(
                    f"  {_pkg_name} lost: {', '.join(_lost_selected)}")
                tui.console.print(
                    f"  {_pkg_name} → lost {', '.join(_lost_selected)}",
                    tui.COLOR_WARNING)

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
            # (extension): if `_name` is a virtual alias (the
            # provider's Package field differs from `_name`), use the
            # Provides clause's version not the provider's own Version.
            # Without this, `gnome-control-center → desktop-base (>= 10.0.0)`
            # resolves via `athena-branding` (which Provides:
            # desktop-base (= 12.0.6)) but apt_pkg.check_dep gets
            # athena-branding's own version `1.2.0` → spurious mismatch.
            # _version_for_constraint_target handles both cases (real
            # pkg → own version, alias → Provides clause version, or
            # None for unversioned Provides which can't satisfy a
            # versioned constraint per Policy §7.5).
            _have = self._dependencytree._version_for_constraint_target(
                _name, _name,
            )
            if _have is None:
                return (
                    False,
                    f'unversioned Provides cannot satisfy {_op} {_ver}',
                )
            try:
                if apt_pkg.check_dep(_have, _op, str(_ver)):
                    return True, None
                return False, f'version mismatch (have {_have}, need {_op} {_ver})'
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
