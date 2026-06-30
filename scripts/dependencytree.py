# Internal modules
from collections import defaultdict
from typing import Dict, List, Optional

import package
from package import Version
from cache import Cache
import utils

# External Modules
import apt_pkg

import logging
import tui
from tui import Prompt, PROMPT_OPTIONS

logger = logging.getLogger('athena.cache')


def _auto_pick_candidate(candidates, prefer_name=None):
    """Collapse same-name version dupes; auto-pick if one Package name remains.

    Returns (picked_or_None, collapsed_list).

    Collapse step: per Package name, keep only the highest version — within a
    coherent mirror set this is always correct and matches apt's "highest
    version satisfying constraints" rule.  Result is sorted alphabetically by
    Package name for deterministic prompt ordering.

    Auto-pick rules (in order):
      1. `prefer_name` matches a Package: name in the collapsed set — auto-
         pick that one.  Mirrors apt's rule that a real package wins over
         virtual `Provides:` when names match.  Example: seed `anacron` →
         candidates {anacron, systemd-cron (Provides anacron)} → pick
         anacron, never systemd-cron.  Without this rule the resolver
         falls through to highest-version-across-names (or a prompt) and
         can pick a provider whose Conflicts then blow up the lookahead
         (caught 2026-05-18 with anacron vs systemd-cron in [laptop]).
      2. Only one Package name in the collapsed set — auto-pick it.
      3. Multiple names remain — return (None, collapsed) so caller prompts
         or applies a per-tree fallback (e.g. udeb-tree's highest-version).
    """
    if not candidates:
        return None, []
    _by_name: Dict[str, list] = defaultdict(list)
    for _c in candidates:
        _by_name[_c['Package']].append(_c)
    _collapsed = [max(_versions, key=lambda p: p.version)
                  for _versions in _by_name.values()]
    _collapsed.sort(key=lambda p: p['Package'])
    if prefer_name is not None:
        for _c in _collapsed:
            if _c['Package'] == prefer_name:
                return _c, _collapsed
    if len(_collapsed) == 1:
        return _collapsed[0], _collapsed
    return None, _collapsed


class DependencyTree:

    # Operators accepted by apt_pkg.check_dep for version constraint checks.
    # Sourced from utils.VALID_CONSTRAINTS so the cache + dep-tree modules
    # stay in lockstep (HK-01b consolidation).  Bound at class scope so
    # `self._VALID_CONSTRAINTS` lookups resolve top-down without depending
    # on Python's runtime-name-resolution.
    _VALID_CONSTRAINTS = utils.VALID_CONSTRAINTS

    def __init__(self, cache: Cache, select_recommended: bool, arch: str,
                 build_profiles: frozenset = frozenset(), lookahead=None,
                 auto_pick_highest_when_ambiguous: bool = False,
                 pins=None):

        self.__recommended = select_recommended
        self.__cache = cache
        # pinned alternative picks fed from selection.state so a
        # genuine multi-provider prompt (mawk vs gawk for `awk`, telnet vs
        # inetutils-telnet for `telnet-client`) resolves deterministically and
        # NEVER re-prompts — otherwise a different pick would false-trigger the
        # closure guard.  `_pins`: {dep_name: chosen_Package_name} (authority).
        # `_pinned_chosen`: every genuinely-ambiguous pick this run made (pin
        # hit OR fresh prompt), harvested by the parse to auto-seed the lockfile
        # on first run.  Plain dicts → pickle-safe for `resume`.
        self._pins: Dict[str, str] = dict(pins) if pins else {}
        self._pinned_chosen: Dict[str, str] = {}
        # When True, multi-Package-name candidates that
        # _auto_pick_candidate refuses to auto-pick (different names) get
        # an additional fallback — pick the candidate with the highest
        # version across names.  Used by the udeb tree where kernel-ABI
        # variants like ext4-modules-6.1.0-{39..47}-amd64-di present as
        # multiple "providers" but really just want the latest.  Off by
        # default for the deb tree because there genuine alternatives
        # (mawk vs gawk for `awk`) need operator input.
        self._auto_pick_highest_when_ambiguous = auto_pick_highest_when_ambiguous
        # Dict[name_or_virtual, Dict[Version, Package]]
        # Mirrors package_hashtable structure — multiple versions per name co-exist without overwrite
        self.__lookahead: Dict[str, Dict[Version, package.Package]] = defaultdict(dict)

        self.selected_pkgs: Dict[str, package.Package] = {}
        self.selected_srcs: Dict[str, package.Source]  = {}
        # Subset of selected_pkgs whose entries were pulled in via
        # depth-1 Recommends-of-selected (rather than the required/important/
        # manual closure).  Empty when [Build] IncludeRecommends is off.
        # build_chroot filters these out of the install batches; source_build
        # default skips them; `source_build recommended` builds only them.
        self.extras_pkg_names: set = set()
        # Subset of selected_srcs.keys() whose every produced binary is in
        # extras_pkg_names — i.e. sources that are here ONLY for the recommends
        # (would not be in selected_srcs without IncludeRecommends).
        # Mixed sources (some selected binaries, some extras binaries) are NOT
        # in this set — source_build builds them normally and the recommended
        # binaries fall out as side artefacts of dpkg-buildpackage.  Derived
        # by derive_extras_src_names() AFTER parse_sources has populated
        # selected_srcs.
        self.extras_src_names: set = set()

        # live.list / installer.list split.  After Pass III (pkg.list)
        # resolves, snapshot pkg_closure; Pass IV/V resolve_packages
        # against live.list / installer.list and the deltas land here.
        # "Exclusive" = needed by live (or installer) but NOT in pkg.list's
        # closure — anything pkg.list already pulls in is the source of truth
        # for "must install".  Mirror of extras_pkg_names / extras_src_names
        # but indexed on a different axis.
        # KNOWN LIMITATION: live runs before installer; if a package were
        # needed by BOTH live and installer (and not pkg), it would only
        # show up in live_exclusive.
        self.live_exclusive_pkg_names: set = set()
        self.installer_exclusive_pkg_names: set = set()
        self.live_exclusive_src_names: set = set()
        self.installer_exclusive_src_names: set = set()

        # phase D follow-up: pool.list — packages that ship in
        # the apt pool on the installer ISO but are NEVER installed in
        # any chroot (live, installer ramdisk, or target).  They go
        # through the resolver normally so their Depends are pulled in
        # transitively (everything ends up in selected_pkgs and ships
        # in /cdrom/pool), but `validate_selection` skips Conflicts
        # AND Breaks involving pool extras — apt on the target enforces
        # those at install time.  This lets mutually-conflicting
        # bootloader metas (`grub-pc` + `grub-efi-amd64`) coexist in
        # the pool while only one ever gets installed on a given
        # firmware mode.  Populated by Pass VII in build.py:
        # pool_extras_pkg_names = (selected_pkgs after VII) − (selected_pkgs before VII).
        self.pool_extras_pkg_names: set = set()
        self.pool_extras_src_names: set = set()

        # Build-closure (IncludeBuildClosure): the Build-Depends of every
        # selected source, resolved transitively, so the toolchain is built
        # from + served by our own mirror.  build_closure_pkg_names is the
        # full added set; build_closure_tiers is its toolchain/language/leaf
        # segregation (see build_closure.py).  Empty unless the flag is on.
        self.build_closure_pkg_names: set = set()
        self.build_closure_tiers: dict = {}

        # pkg.list groups — operator-defined install-time package
        # groups.  `pkg_group_pkg_names[<group>]` is the set of
        # canonical package names whose membership in selected_pkgs is
        # owed to that group (declaration-order respected — a package
        # also reachable from an earlier group's closure is credited
        # there, not here).  The `[base]` group is always installed
        # (live image + target debootstrap); non-base groups ship in
        # `/cdrom/pool` only and the installer apt-installs the
        # operator-selected subset onto /target at install time.
        # `pkg_group_extras_pkg_names` is the union of non-base
        # groups' canonical names — same exclusion semantics as
        # `pool_extras_pkg_names` (subtract from base_include, filter
        # from live install batches) but conflicts ARE enforced
        # because group-level packages are not mutually exclusive
        # within a single install.
        self.pkg_group_pkg_names: 'Dict[str, set]' = {}
        # Per-group metadata (description) parsed from
        # `## Description: ...` lines after `[group]` headers in
        # pkg.list.  Empty dict per group if operator declared no
        # metadata; consumers (tasksel `.desc` generation) fall back
        # to a default title.
        self.pkg_group_meta: 'Dict[str, Dict[str, str]]' = {}
        self.pkg_group_extras_pkg_names: set = set()
        self.pkg_group_extras_src_names: set = set()

        # Per-tree predicted-binary-filename map.  Source-name → list of
        # binary filenames (post utils.normalize_repo_filename) this tree
        # expects to land in repo/ from a successful dpkg-buildpackage of
        # the source.
        #
        # Stored per-tree (not on Source) because Source objects are shared
        # across DependencyTree instances via cache.source_hashtable.  The
        # old design mutated Source.pkgs in-place; the udeb tree's
        # parse_sources reset the shared list, overwriting the deb tree's
        # entries.  Source audit then saw only udeb binaries (libc6-udeb
        # present → "ok") and missed missing deb binaries like libc6-dev.
        # Bug found 2026-05-20; pre-audit-split-2026-05-20 tag preserves
        # the pre-fix state.
        #
        # Readers that need the union across both trees (e.g.
        # BuildContainer.check_build deciding skip-rebuild) take both
        # trees' maps explicitly and union them in the caller — no
        # implicit cross-tree state.
        self.src_pkg_files: 'Dict[str, List[str]]' = {}

        self.arch = arch
        self.build_profiles = build_profiles

        if lookahead is not None:
            self.add_lookahead(lookahead)

    def _apply_pin(self, name: str, collapsed: list):
        """If selection.state pins `name` to a candidate present in
        `collapsed`, return that Package (and record it as this run's chosen
        pick).  Returns None when there is no pin or the pinned package is no
        longer among the candidates (vanished upstream → caller re-prompts and
        the resulting closure delta flows through the guard / re-baseline)."""
        _want = self._pins.get(name)
        if _want:
            for _c in collapsed:
                if _c['Package'] == _want:
                    self._pinned_chosen[name] = _want
                    return _c
        return None

    def _record_prompt_pick(self, name: str, selected):
        """Record a genuinely-ambiguous pick (a real operator prompt) so the
        parse can auto-seed it into selection.state on first run."""
        self._pinned_chosen[name] = selected['Package']

    def add_lookahead(self, lookahead: List[str], check_conflicts: bool = True):
        """When `check_conflicts=False`, step 3 (lookahead-time conflict
        check) is skipped — the caller is asserting that conflicts among
        these packages don't matter at selection time (e.g. pool.list
        entries that ship in the apt pool but never get installed
        together; apt enforces at install time on the target).  Used
        by Pass VII to let `grub-pc` and `grub-efi-amd64` coexist in
        selected_pkgs.
        """
        for _pkg_name in lookahead:
            if not _pkg_name or _pkg_name.isspace():
                continue
            _pkg_name = _pkg_name.strip()

            # 1. Verify package exists in cache
            _candidates = self.__cache.get_packages(_pkg_name)
            if not _candidates:
                tui.console.print(f"WARNING: add_lookahead: '{_pkg_name}' not found in cache, skipping")
                logger.warning(f"add_lookahead: '{_pkg_name}' not found in cache, skipping")
                continue

            # 2. Select version — auto-pick when only versions differ
            # (mechanically correct within a coherent mirror set), prompt
            # only on genuine alternative providers (different Package
            # names like mawk vs gawk for `awk`).  Pass prefer_name=
            # _pkg_name so a real package whose name matches the seed
            # wins over virtual Provides (e.g. anacron real > systemd-
            # cron Provides anacron) — apt's same rule.
            _auto, _collapsed = _auto_pick_candidate(_candidates, prefer_name=_pkg_name)
            if _auto is None and self._auto_pick_highest_when_ambiguous and _collapsed:
                # Udeb-tree fallback: multiple providers with different
                # Package names — pick the highest version across names
                # (typical for kernel-ABI udeb variants).
                _auto = max(_collapsed, key=lambda p: p.version)
                logger.info(
                    f"add_lookahead: highest-version fallback picked "
                    f"{_auto.package} {_auto.version} from "
                    f"{len(_collapsed)} multi-name providers of '{_pkg_name}'"
                )
            if _auto is not None:
                _selected = _auto
                if len(_candidates) > 1:
                    logger.info(
                        f"add_lookahead: auto-pick {_selected.package} "
                        f"{_selected.version} from {len(_candidates)} candidates "
                        f"(collapsed {len(_candidates)}→{len(_collapsed)}) of '{_pkg_name}'"
                    )
            elif (_pinned := self._apply_pin(_pkg_name, _collapsed)) is not None:
                _selected = _pinned
                logger.info(
                    f"add_lookahead: pinned-pick {_selected.package} "
                    f"{_selected.version} for '{_pkg_name}' (selection.state)"
                )
            else:
                _mark = tui.console.mark()
                tui.console.print(f"Multiple providers for '{_pkg_name}':")
                for _i, _c in enumerate(_collapsed, 1):
                    tui.console.print(f"  {_i}.  {_c.package}  ({_c.version})")
                if len(_candidates) != len(_collapsed):
                    logger.info(
                        f"add_lookahead: prompt for '{_pkg_name}' "
                        f"collapsed {len(_candidates)}→{len(_collapsed)} candidates"
                    )
                _options = [str(_i) for _i in range(1, len(_collapsed) + 1)]
                _choice  = Prompt(PROMPT_OPTIONS, f"Select [1-{len(_collapsed)}]", _options).get_response()
                _selected = _collapsed[int(_choice) - 1]
                self._record_prompt_pick(_pkg_name, _selected)
                tui.console.trim_to(_mark)
                tui.console.print(f"Multiple providers for '{_pkg_name}': Selected {_selected.package} ({_selected.version})")

            # 3. Hard conflict check against entries already in lookahead
            #    (skipped when check_conflicts=False — pool.list path)
            _conflict_found = False
            if not check_conflicts:
                self.__lookahead[_pkg_name][_selected.version] = _selected
                for _provided_name, _provided_ver in _selected.get_provides():
                    if _provided_name != _pkg_name:
                        self.__lookahead[_provided_name][_provided_ver] = _selected
                continue
            for _conflict_group in _selected.conflicts:
                _conflict_name    = _conflict_group[0][0]
                _conflict_ver_str = _conflict_group[0][1]
                _conflict_op      = _conflict_group[0][2]
                if _conflict_name in self.__lookahead:
                    # Only block on real-package conflicts. Provides-aliases (e.g. apt registering
                    # 'debconf-tiny' as a virtual name) should not trigger here — those are deferred
                    # to validate_selection() where version-aware checks run properly.
                    _is_real = any(pkg['Package'] == _conflict_name
                                   for pkg in self.__lookahead[_conflict_name].values())
                    if not _is_real:
                        continue

                    # Check version constraint: e.g. Conflicts: apt (<< 0.5.4) must not fire
                    # against apt 2.x. Only block if the lookahead package's version actually
                    # satisfies the conflict operator. No version string = unconditional conflict.
                    if _conflict_ver_str and _conflict_op in self._VALID_CONSTRAINTS:
                        _triggered     = False
                        _triggered_ver = None
                        for _lver in self.__lookahead[_conflict_name]:
                            try:
                                if apt_pkg.check_dep(str(_lver), _conflict_op, _conflict_ver_str):
                                    _triggered     = True
                                    _triggered_ver = _lver
                                    break
                            except Exception:
                                _triggered     = True  # conservative: assume conflict on parse error
                                _triggered_ver = _lver
                                break
                    else:
                        _triggered     = True   # no version constraint = unconditional conflict
                        _triggered_ver = next(iter(self.__lookahead[_conflict_name]), None)

                    if _triggered:
                        _ver_info = f" ({_triggered_ver})" if _triggered_ver else ""
                        tui.console.print(f"ERROR: Cannot add '{_pkg_name}' — conflicts with '{_conflict_name}{_ver_info}' already in lookahead")
                        logger.error(f"CRITICAL: add_lookahead — '{_pkg_name}' conflicts with '{_conflict_name}{_ver_info}'")
                        _conflict_found = True
                        break
            if _conflict_found:
                continue

            # 4. Add real name under its version; add provided virtual names under their provided version
            self.__lookahead[_pkg_name][_selected.version] = _selected
            for _provided_name, _provided_ver in _selected.get_provides():
                if _provided_name != _pkg_name:
                    self.__lookahead[_provided_name][_provided_ver] = _selected

    @property
    def selected_count(self) -> int:
        return sum(1 for _k in self.selected_pkgs if _k == self.selected_pkgs[_k]['Package'])

    @property
    def canonical_pkgs(self) -> Dict[str, 'package.Package']:
        """selected_pkgs filtered to canonical entries only (key == pkg['Package']).

        selected_pkgs also stores virtual-alias entries where the key is a
        provided name (e.g. 'libcomerr2') mapping to the real Package object
        (package='libcom-err2').  Iterating canonical_pkgs skips those aliases
        so callers get exactly one entry per real package.
        """
        return {k: v for k, v in self.selected_pkgs.items() if k == v['Package']}

    def resolve_packages(self, packages: list[str],
                         check_conflicts: bool = True) -> list[str]:
        """When `check_conflicts=False`, propagated to add_lookahead so
        the lookahead-time conflict check is skipped.  validate_selection
        still runs on the full closure but skips conflicts/breaks
        involving any package in `pool_extras_pkg_names` — see
        validate_selection() for the membership-based bypass.
        """
        self.add_lookahead(packages, check_conflicts=check_conflicts)
        unresolved = [pkg for pkg in packages if self.parse_dependency(pkg) is None]
        for pkg in unresolved:
            tui.console.print(f"WARNING: cannot resolve '{pkg}'")
            logger.error(f"parse_dependency({pkg}) returned None")
        return unresolved


    def parse_dependency(self, package_name: str,
                         version: Optional[Version] = None,
                         constraint: str = '') -> Optional[package.Package]:

        _selected_pkg: package.Package
        # Normalise constraint — fall back to '>=' for anything unrecognised
        _constraint = constraint if constraint in self._VALID_CONSTRAINTS else '>='

        def _satisfies(pkg_ver: Version) -> bool:
            """True if pkg_ver meets the requested version/constraint, or no version was requested."""
            if version is None:
                return True
            try:
                return apt_pkg.check_dep(str(pkg_ver), _constraint, str(version))
            except Exception:
                return True   # can't evaluate — assume satisfied, validate_selection will catch

        def _satisfies_via_provides(pkg: package.Package) -> bool:
            """True if any of pkg's versioned Provides entries (for the
            requested package_name) satisfies the constraint.  Per Debian
            Policy §7.5, Provides versions are load-bearing for version-
            constrained deps — `Depends: foo (>= X)` is satisfied by
            `bar Provides: foo (= Y)` when Y >= X, even if bar's own
            version is different.  Mirror fix landed in package.py's
            constraints_satisfied (2026-05-17 commit 77a3143).
            """
            if version is None:
                return True
            try:
                for _name, _prov_ver in pkg.get_provides():
                    if _name != package_name:
                        continue
                    if _prov_ver and _satisfies(_prov_ver):
                        return True
            except (ValueError, KeyError, AttributeError, SystemError):
                pass
            return False

        if not package_name:
            tui.console.print("Dependency Check: Dependency asked for empty package name")
            return None

        # Early return if already selected by name
        if package_name in self.selected_pkgs:
            _existing = self.selected_pkgs[package_name]
            if _satisfies(_existing.version) or _satisfies_via_provides(_existing):
                return _existing
            tui.console.print(f"WARNING: '{package_name}' already selected at {_existing.version}, cannot satisfy {_constraint} {version}")
            logger.warning(f"'{package_name}({_existing.version})' in selected not matching required {_constraint} {version}")
            return None


        # which package provide given package_name (pre-filtered by version if provided)
        _pkg_candidates = self.__cache.get_packages(package_name, version, constraint)

        # Early return if a candidate's real name is already in selected_pkgs
        for _pkg in _pkg_candidates:
            if _pkg['Package'] in self.selected_pkgs:
                _existing = self.selected_pkgs[_pkg['Package']]
                # Same Provides-version honoring fix as the by-name early
                # return above (Debian Policy §7.5).  Without this, virtuals
                # like libgcc1 (Provided by libgcc-s1 with version 1:N) get
                # spurious "cannot satisfy" warnings even though the
                # Provides version meets the constraint.
                if (not _satisfies(_existing.version)
                        and not _satisfies_via_provides(_existing)):
                    logger.warning(f"'{package_name}({_existing.version})' in selected not matching required {_constraint} {version}")
                return _existing

        # At this point, if lookahead is available use that to select packages.
        _selected_pkg_lookahead = [pkg for pkg in _pkg_candidates if pkg['Package'] in self.__lookahead]
        # Collapse the lookahead-matched candidates to one entry per Package
        # name (highest version wins, same rule the prompt path uses).  This
        # lets Case I fire when the cache returns multiple versions of the
        # same Package — common when the cache layers bookworm +
        # bookworm-security and both index the same Package (e.g. sudo).
        # Without this collapse the old Case I (`len == 1`) missed those
        # cases and fell into the multi-candidate prompt even though
        # add_lookahead had already disambiguated.  Caught 2026-05-12 —
        # `sudo` prompted twice within the same Pass III resolve_packages
        # because package_hashtable['sudo'] returned [sudo v1, sudo v2,
        # sudo-ldap v1, sudo-ldap v2]; add_lookahead's choice put 'sudo'
        # in __lookahead, and the filter matched BOTH sudo v1 and sudo v2.
        if _selected_pkg_lookahead:
            _la_by_name: Dict[str, list] = defaultdict(list)
            for _pkg in _selected_pkg_lookahead:
                _la_by_name[_pkg['Package']].append(_pkg)
            _selected_pkg_lookahead = [
                max(_versions, key=lambda p: p.version)
                for _versions in _la_by_name.values()
            ]

        # Case - I  : Incase one candidate and already in lookahead, simplified
        if len(_selected_pkg_lookahead) == 1:
            _selected_pkg = _selected_pkg_lookahead[0]

        # Case - II : No match for Package or Provides - Raise Value Error
        elif len(_pkg_candidates) == 0:
            tui.console.print(f"Dependency Check: Cant find anything that provides: {package_name}")
            return None

        # Case - III: One package found
        elif len(_pkg_candidates) == 1:
            _selected_pkg = _pkg_candidates[0]

        # Case - IV : Multiple candidates.  If they're all the same Package
        # name (only versions differ), auto-pick the highest — within a
        # coherent mirror set this is always correct and matches apt.
        # Prompt only when names differ (genuine alternative providers).
        elif len(_pkg_candidates) > 1:
            # Same prefer_name= logic as add_lookahead: real package whose
            # name matches the dep wins over Provides-only candidates.
            _auto, _collapsed = _auto_pick_candidate(_pkg_candidates, prefer_name=package_name)
            if _auto is None and self._auto_pick_highest_when_ambiguous and _collapsed:
                # Udeb-tree fallback: highest-version across multi-name
                # providers (typical for kernel-ABI udeb variants — see
                # __init__ docstring).
                _auto = max(_collapsed, key=lambda p: p.version)
                logger.info(
                    f"highest-version fallback picked {_auto.package} "
                    f"{_auto.version} from {len(_collapsed)} multi-name "
                    f"providers of '{package_name}'"
                )
            if _auto is not None:
                _selected_pkg = _auto
                logger.info(
                    f"auto-pick {_selected_pkg.package} {_selected_pkg.version} "
                    f"from {len(_pkg_candidates)} candidates "
                    f"(collapsed {len(_pkg_candidates)}→{len(_collapsed)}) of '{package_name}'"
                )
            elif (_pinned := self._apply_pin(package_name, _collapsed)) is not None:
                _selected_pkg = _pinned
                logger.info(
                    f"pinned-pick {_selected_pkg.package} {_selected_pkg.version} "
                    f"for '{package_name}' (selection.state)"
                )
            else:
                _mark = tui.console.mark()
                tui.console.print(f"Multiple packages satisfy '{package_name}':")
                for _i, _pkg in enumerate(_collapsed, 1):
                    tui.console.print(f"  {_i}.  {_pkg.package}  ({_pkg.version})")
                if len(_pkg_candidates) != len(_collapsed):
                    logger.info(
                        f"parse_dependency: prompt for '{package_name}' "
                        f"collapsed {len(_pkg_candidates)}→{len(_collapsed)} candidates"
                    )
                _options = [str(_i) for _i in range(1, len(_collapsed) + 1)]
                _choice  = Prompt(PROMPT_OPTIONS, f"Select [1-{len(_collapsed)}]", _options).get_response()
                _selected_pkg = _collapsed[int(_choice) - 1]
                self._record_prompt_pick(package_name, _selected_pkg)
                tui.console.trim_to(_mark)
                tui.console.print(f"Multiple packages satisfy '{package_name}': Selected {_selected_pkg.package} ({_selected_pkg.version})")

        else:  # Do not know how we got here
            logger.error(f"Unknown Error in Parsing dependencies: {package_name}")
            return None

        # Insert BEFORE recursing: cycle protection. If A depends on B and B depends on A,
        # the early-return at the top of this function (checking selected_pkgs) only works
        # if A is already recorded here before parse_dependency(B) runs.
        self.selected_pkgs[_selected_pkg['Package']] = _selected_pkg

        # Also register every virtual name this package provides so that the L84 early-return
        # catches virtual-name lookups (e.g. 'awk' → gawk_pkg) without needing the L93 loop.
        #
        # a virtual Provides must NEVER clobber a REAL selected package of the same
        # name — apt's "real package beats virtual Provides" rule.  The old unconditional
        # write let a later-resolved provider (B Provides: foo) overwrite an already-selected
        # real `foo`; consumers that filter `key == selected_pkgs[key]['Package']` then drop
        # the now-mismatched key, so `foo` vanished from the closure — silently and
        # order-dependently (the athena-cdrom-setup / 50mirror loss class).
        #
        # EXEMPTION (follow-up): a REPLACEMENT package declares the standard Debian
        # idiom `Provides: X` + `Replaces: X` (+ usually `Conflicts: X`) — it deliberately
        # supersedes X (our same-name forks: athena-tasksel→tasksel, athena-branding→
        # desktop-base).  Such a package MUST take the name so the real X drops out of the
        # canonical set and the `Provides X + Conflicts X = "I am X"` self-conflict skip in
        # the Breaks/Conflicts pass holds.  Only block the clobber for a GENUINE virtual
        # provider (Provides but does NOT Replace the name).
        for _provided_name, _ in _selected_pkg.get_provides():
            _prior = self.selected_pkgs.get(_provided_name)
            _replaces_it = any(
                _g and _g[0][0] == _provided_name for _g in _selected_pkg.replaces)
            if (_prior is not None
                    and _prior['Package'] == _provided_name
                    and _prior is not _selected_pkg
                    and not _replaces_it):
                # genuine virtual provider colliding with a real package — real wins, warn.
                logger.warning(
                    f"{_selected_pkg['Package']} Provides '{_provided_name}', which is "
                    f"already a real selected package — keeping the real package, not "
                    f"registering the virtual alias (apt real-beats-virtual)")
                continue
            self.selected_pkgs[_provided_name] = _selected_pkg

        # list packages to get dependencies for (copy — the loop below mutates _depends,
        # aliasing _selected_pkg.depends would corrupt the Package's stored list)
        _depends = list(_selected_pkg.depends)

        # Pre-Depends must be satisfied before the package can unpack — treat same as Depends
        _depends += list(_selected_pkg.pre_depends)

        # Slightly more tricky how to handle alt_depends
        # Virtual names are registered in selected_pkgs at L134-135, so the name check
        # below covers both real packages and provides without a separate provides lookup.
        # alt_pre_depends (OR-grouped Pre-Depends, e.g. `Pre-Depends: A | B`) are
        # resolved through the SAME alternative-selection loop: Pre-Depends are
        # treated like Depends (see pre_depends above), so an OR pre-dep must
        # pull a provider too.  Without this an OR pre-dep whose providers are
        # not otherwise selected silently never enters selected_pkgs.
        _alt_depends = list(_selected_pkg.alt_depends) + \
            list(_selected_pkg.alt_pre_depends)

        for _alt in _alt_depends:
            # Find alts already selected whose version constraint is also satisfied.
            # `_alt_dep` here is a dep-tuple (name, ver, op) — distinct from
            # `_pkg` elsewhere in this method which is a Package object.
            _selected_alt_pkg = []
            for _alt_dep in _alt:
                _alt_name = _alt_dep[0]
                if _alt_name not in self.selected_pkgs:
                    continue
                _alt_ver_str = _alt_dep[1]
                if not _alt_ver_str:
                    _selected_alt_pkg.append(_alt_dep)   # no version constraint — name match sufficient
                    continue
                _alt_op = _alt_dep[2] if _alt_dep[2] in self._VALID_CONSTRAINTS else '>='
                _pkg_ver = self._version_for_constraint_target(_alt_name, _alt_name)
                if _pkg_ver is None:
                    # Provider declares `Provides: <name>` unversioned —
                    # per Policy §7.5 cannot satisfy a versioned alt-dep
                    # constraint.  Skip this alt; another may satisfy.
                    continue
                try:
                    if apt_pkg.check_dep(_pkg_ver, _alt_op, _alt_ver_str):
                        _selected_alt_pkg.append(_alt_dep)
                except Exception:
                    _selected_alt_pkg.append(_alt_dep)   # can't evaluate — assume satisfied

            if _selected_alt_pkg:
                # one or more already selected and satisfying — pick first, arbitrary decision
                _depends.append(_selected_alt_pkg[0])
                continue

            # No alt already selected and satisfying — default to first alternative (Debian convention)
            _depends.append(_alt[0])

        # check if we should include recommended packages
        if self.__recommended:
            _depends += _selected_pkg.recommends

        # recursively.  `_dep` is a dep-tuple (name, ver, op) — same
        # tuple shape as the alt-deps loop above; distinct from `_pkg`
        # (Package) used in the candidate-selection blocks earlier.
        for _dep in _depends:
            # Extract version info from dep tuple and build Version object safely
            _dep_ver: Optional[Version] = None
            if _dep[1]:
                try:
                    _dep_ver = Version(_dep[1])
                except (ValueError, TypeError):
                    logger.warning(f"Malformed version '{_dep[1]}' in dep on '{_dep[0]}', ignoring")

            _parsed_pkg = self.parse_dependency(_dep[0], _dep_ver, _dep[2])
            if _parsed_pkg is None:
                tui.console.print(f"WARNING: unresolved dependency '{_dep[0]}' for {_selected_pkg.package}")
                logger.warning(f"parse_dependency({_dep[0]}) from {_selected_pkg.package} returned None")
                continue

            # add forward dependency
            if _parsed_pkg.package not in _selected_pkg.depends_on:
                _selected_pkg.depends_on.append(_parsed_pkg.package)
            # add reverse dependency
            if _selected_pkg.package not in _parsed_pkg.depended_by:
                _parsed_pkg.depended_by.append(_selected_pkg.package)

            # Record version constraint in the selected package for validate_selection
            if _dep_ver is not None:
                try:
                    self.selected_pkgs[_parsed_pkg['Package']].add_constraint(_dep_ver, _dep[2])
                except (ValueError, TypeError) as e:
                    logger.warning(f"Skipping invalid version constraint '{_dep[1]}' "
                                        f"on {_parsed_pkg.package} (from {_selected_pkg.package}): {e}")

        return _selected_pkg

    def _version_for_constraint_target(self, entry_key: str,
                                         target_name: str) -> 'Optional[str]':
        """Return the version string to compare against a constraint
        on `target_name`, given that `entry_key` is the selected_pkgs
        entry that purports to satisfy it.

        Two cases:
          - entry IS the real package named `target_name` (Package field
            matches): returns str(entry.version).
          - entry provides `target_name` via Provides (Package field
            differs, e.g. libgcc-s1 provides libgcc1): returns the
            Provides clause version when one is declared, else None.

        Returns None when the only Provides clause covering `target_name`
        is unversioned.  Per Debian Policy §7.5, an unversioned Provides
        cannot satisfy a versioned constraint — callers must treat None
        as "name match only" (satisfies unversioned constraint only).

        Fixes previously `selected_pkgs[alias].version` was read
        directly, returning the provider's own Version field even when
        the Provides clause carried a different version (e.g. an epoch
        the provider lacks — libgcc-s1's Version is 12.2.0-14+deb12u1
        epoch 0, but its Provides: libgcc1 (= 1:12.2.0-14+deb12u1) is
        epoch 1; a consumer Depends: libgcc1 (>= 1:4.0) then failed the
        version compare and emitted spurious WARNINGs).
        """
        _obj = self.selected_pkgs[entry_key]
        if _obj['Package'] == target_name:
            return str(_obj.version)
        _provided_ver = _obj.explicit_provides_version(target_name)
        return str(_provided_ver) if _provided_ver is not None else None

    def validate_selection(self) -> bool:

        # Checking breaks first
        # When one binary package declares that it breaks another, dpkg will refuse to allow the package which
        # declares Breaks to be unpacked unless the broken package is de-configured first, and it will refuse to
        # allow the broken package to be reconfigured.

        # Note: No comparator is absolute, just existence breaks, with Comparator checks if the comparator is satisfied

        _breaks = False
        for _pkg in self.selected_pkgs:
            if _pkg != self.selected_pkgs[_pkg]['Package']:
                continue
            # Breaks will still allow to install - Warning
            # Debian policy forbids alternatives in Breaks, so each group has exactly one entry at [0]
            for _break_group in self.selected_pkgs[_pkg].breaks:
                _breaks_name = _break_group[0][0]
                if _breaks_name in self.selected_pkgs:
                    # Standard Debian pattern: a package declares Provides: X and Breaks: X to
                    # satisfy X while blocking other providers. selected_pkgs registers virtual
                    # names pointing to the same Package object, so an identity check distinguishes
                    # "package breaks its own alias" (false positive) from a real break.
                    if self.selected_pkgs[_breaks_name] is self.selected_pkgs[_pkg]:
                        continue
                    # phase D follow-up: pool.list contract — when
                    # either side of the relationship is a pool extra,
                    # apt enforces at install time on the target.  We
                    # intentionally ship mutually-Breaking pool entries
                    # so the operator can apt-install one or the other.
                    _real_breaks_name = self.selected_pkgs[_breaks_name]['Package']
                    if (_pkg in self.pool_extras_pkg_names or
                        _real_breaks_name in self.pool_extras_pkg_names):
                        continue
                    _broken_obj = self.selected_pkgs[_breaks_name]
                    _break_version = _break_group[0][1]
                    _break_comparator = _break_group[0][2]
                    if _broken_obj['Package'] != _breaks_name:
                        # Virtual via Provides.  Per Debian Policy §7.5,
                        # an UNVERSIONED Provides cannot satisfy a
                        # versioned Breaks/Conflicts constraint — apt
                        # will install both pkgs and not flag a break.
                        # Real-world case: `fwupd Provides: fwupdate`
                        # (unversioned) + `linux-image Breaks:
                        # fwupdate (<< 12-7)` — fwupd's own version
                        # (1.8.12-2) IS << 12-7 by Debian comparison,
                        # but that comparison is irrelevant because
                        # fwupd's Provides makes no version claim about
                        # the fwupdate name.
                        # explicit_provides_version returns None for
                        # unversioned Provides (no substitution to
                        # self.version like get_provides does — that
                        # fallback is wrong for this scope).  Use it
                        # instead of get_provides to avoid the spurious
                        # break that blocked dep parse 2026-05-20 once
                        # Bug-2's parse_dependency started registering
                        # virtual aliases in selected_pkgs.
                        _provided_ver = _broken_obj.explicit_provides_version(
                            _breaks_name)
                        if _provided_ver is None:
                            if _break_comparator:
                                continue  # versioned break vs unversioned provides — no match
                            _pkg_ver = None   # unversioned break — existence triggers below
                        else:
                            _pkg_ver = str(_provided_ver)
                    else:
                        _pkg_ver = str(_broken_obj.version)

                    # Check if it breaks
                    try:
                        _triggered = (_break_comparator == '' or
                                      apt_pkg.check_dep(_pkg_ver or '', _break_comparator, _break_version))
                    except Exception as e:
                        logger.warning(f"check_dep raised for breaks {_breaks_name} "
                                            f"({_pkg_ver} {_break_comparator} {_break_version}): {e}")
                        _triggered = True  # conservative: assume break on parse error
                    if _triggered:
                        tui.console.print(f"ERROR: Package {_pkg} breaks {_breaks_name}")
                        logger.error(f"DEPENDENCY HELL: {_pkg} breaks {_breaks_name} "
                                          f"(ver {_pkg_ver} {_break_comparator} {_break_version})")
                        _breaks = True

            # Conflicts will break installation - Error
            # Debian policy forbids alternatives in Conflicts, so each group has exactly one entry at [0]
            for _conflict_group in self.selected_pkgs[_pkg].conflicts:
                _conflicts_name = _conflict_group[0][0]
                if _conflicts_name in self.selected_pkgs:
                    # Same Debian pattern as Breaks above: Provides: X + Conflicts: X means
                    # "I am X and nothing else can be X". Not a real conflict with another package.
                    if self.selected_pkgs[_conflicts_name] is self.selected_pkgs[_pkg]:
                        continue
                    # phase D follow-up: pool.list bypass — see
                    # the matching block in the Breaks loop above.
                    _real_conflicts_name = self.selected_pkgs[_conflicts_name]['Package']
                    if (_pkg in self.pool_extras_pkg_names or
                        _real_conflicts_name in self.pool_extras_pkg_names):
                        continue
                    _conflict_obj = self.selected_pkgs[_conflicts_name]
                    _conflict_version = _conflict_group[0][1]
                    _conflict_comparator = _conflict_group[0][2]
                    if _conflict_obj['Package'] != _conflicts_name:
                        # Same unversioned-Provides rule as the Breaks
                        # arm above — Debian Policy §7.5: an unversioned
                        # Provides cannot satisfy a versioned Conflicts
                        # constraint.  Use explicit_provides_version (no
                        # fallback to self.version) — get_provides
                        # substitutes the provider's own version which
                        # produces spurious conflict triggers for the
                        # unversioned-Provides case.
                        _provided_ver = _conflict_obj.explicit_provides_version(
                            _conflicts_name)
                        if _provided_ver is None:
                            if _conflict_comparator:
                                continue  # versioned conflict vs unversioned provides — no match
                            _pkg_ver = None   # unversioned conflict — existence triggers below
                        else:
                            _pkg_ver = str(_provided_ver)
                    else:
                        _pkg_ver = str(_conflict_obj.version)

                    # Check if conflicts
                    try:
                        _triggered = (_conflict_comparator == '' or
                                      apt_pkg.check_dep(_pkg_ver or '', _conflict_comparator, _conflict_version))
                    except Exception as e:
                        logger.warning(f"check_dep raised for conflicts {_conflicts_name} "
                                            f"({_pkg_ver} {_conflict_comparator} {_conflict_version}): {e}")
                        _triggered = True  # conservative: assume conflict on parse error
                    if _triggered:
                        tui.console.print(f"ERROR: Package {_pkg} conflicts with {_conflicts_name}")
                        logger.error(f"DEPENDENCY HELL: {_pkg} conflicts with {_conflicts_name} "
                                          f"(ver {_pkg_ver} {_conflict_comparator} {_conflict_version})")
                        _breaks = True

            # Check for package version constraints collected from upstream
            if not self.selected_pkgs[_pkg].constraints_satisfied:
                tui.console.print(f"ERROR: Package {_pkg} version constraints unsatisfied")
                logger.error(f"DEPENDENCY HELL: {_pkg} version constraints unsatisfied")
                _breaks = True

            # Check Alt Depends — and Alt Pre-Depends (same OR-group shape;
            # an unsatisfied OR pre-dep must be flagged, not silently passed).
            for _section in (list(self.selected_pkgs[_pkg].alt_depends)
                             + list(self.selected_pkgs[_pkg].alt_pre_depends)):
                _found = False

                for pkg in _section:
                    # if one has been satisfied, don't bother with others - May have to check logic holds
                    if _found:
                        break
                    pkg_name = pkg[0]
                    # Simpler is Package in Selected Package Name
                    if pkg_name in self.selected_pkgs:
                        pkg_version = pkg[1]
                        pkg_constraint = pkg[2]
                        # Empty comparator = no version constraint, name-match alone satisfies (matches Breaks/Conflicts pattern)
                        _pkg_ver = self._version_for_constraint_target(pkg_name, pkg_name)
                        try:
                            if pkg_constraint == '':
                                _satisfies = True
                            elif _pkg_ver is None:
                                # unversioned Provides vs versioned constraint
                                _satisfies = False
                            else:
                                _satisfies = apt_pkg.check_dep(_pkg_ver, pkg_constraint, pkg_version)
                        except Exception as e:
                            logger.warning(f"check_dep raised for {pkg_name} "
                                                f"({_pkg_ver} {pkg_constraint} {pkg_version}): {e}")
                            _satisfies = False
                        if _satisfies:
                            _found = True
                        else:
                            logger.warning(f"Alt-dep version constraint failed for {pkg_name} "
                                                f"({_pkg_ver} {pkg_constraint} {pkg_version})")
                    else:
                        # Lets try in Provides, little more complex
                        _provides_options = self.__cache.get_packages(pkg_name)
                        _pkg_names = [_pkg['Package'] for _pkg in _provides_options
                                      if _pkg['Package'] in self.selected_pkgs]
                        # Tricky - can be more than one package that don't conflict with each other.
                        # e.g. awk can be provided by both mawk & gawk without conflict.
                        if len(_pkg_names) > 0:
                            for _pkg_name in _pkg_names:
                                pkg_version = pkg[1]
                                pkg_constraint = pkg[2]
                                # Empty comparator = no version constraint, name-match alone satisfies
                                # _pkg_name is the real provider; pkg_name is the virtual target —
                                # use the Provides clause version of pkg_name as declared by _pkg_name.
                                _pkg_ver = self._version_for_constraint_target(_pkg_name, pkg_name)
                                try:
                                    if pkg_constraint == '':
                                        _satisfies = True
                                    elif _pkg_ver is None:
                                        # unversioned Provides vs versioned constraint
                                        _satisfies = False
                                    else:
                                        _satisfies = apt_pkg.check_dep(_pkg_ver, pkg_constraint, pkg_version)
                                except Exception as e:
                                    logger.warning(f"check_dep raised for {_pkg_name} "
                                                        f"({_pkg_ver} "
                                                        f"{pkg_constraint} {pkg_version}): {e}")
                                    _satisfies = False
                                if _satisfies:
                                    _found = True
                                else:
                                    logger.warning(f"Alt-dep (via provides) version constraint failed for "
                                                        f"{_pkg_name} ({_pkg_ver} "
                                                        f"{pkg_constraint} {pkg_version})")

                if not _found:
                    tui.console.print(f"ERROR: unresolved alt-dependency for package {_pkg}")
                    logger.error(f"DEPENDENCY HELL: {_pkg} unresolved alt-dep section: {_section}")
                    _breaks = True

        return not _breaks

    def pull_recommends_extras(self) -> int:
        """Walk the current selected_pkgs and pull depth-1 Recommends
        into selected_pkgs (and their transitive Depends closure) as
        'extras'.

        Extras land in selected_pkgs (so source_download fetches their
        upstream tarballs via the existing parse_sources path) and their
        names are tracked in self.extras_pkg_names so build_chroot can
        skip them and source_build can route between default / recommended
        modes.

        Behaviour:
          - Depth-1 for RECOMMENDS: a recommend's own Recommends are
            NOT followed (constructor's select_recommended=False ensures
            parse_dependency skips them too).
          - FULL DEPTH for DEPENDS of the recommend: parse_dependency
            recursively walks the recommend's hard Depends.  Without
            this the recommend lands as an orphan — its libs (libksba8,
            libnpth0, libmbim*, libqmi*, libtss2-*, etc.) never enter
            selected_pkgs, never get a source build, and chroot-install
            fails at audit time.  Both extras AND their transitive
            Depends end up in extras_pkg_names so the WHOLE recommends-
            only subtree ships in /cdrom/pool but skips the live/target
            install batches.
          - OR-grouped recommends ('foo | bar') are silently skipped —
            today's package.recommends only carries single-name groups
            (package.py:201 `if len(g) == 1`).  Documented gap; widening
            this is a separate ticket.
          - A recommend whose source is in cache.skip_src is skipped with
            a WARN — promising it in the repo would lie since neither
            source_build nor tunnel will produce a .deb for it.
          - A recommend already in selected_pkgs (covered by required/
            important/manual closure) is NOT re-walked.

        Caller is responsible for calling derive_extras_src_names()
        AFTER parse_sources runs to populate self.extras_src_names.

        Returns the number of direct recommends pulled in (does NOT
        count transitive Depends added on their behalf — the logger
        WARNING line reports both numbers separately).
        """
        # Snapshot — we'll mutate selected_pkgs while iterating.
        _seed_names = [
            name for name in self.selected_pkgs.keys()
            if name == self.selected_pkgs[name]['Package']  # canonical only
        ]
        _pre_keys = set(self.selected_pkgs.keys())
        _added = 0
        _skipped_skip_src = 0
        for _seed_name in _seed_names:
            _seed_pkg = self.selected_pkgs[_seed_name]
            for _recommend_tuple in _seed_pkg.recommends:
                _rec_name = _recommend_tuple[0]
                if _rec_name in self.selected_pkgs:
                    continue  # already in the install closure — nothing to do
                _candidates = self.__cache.package_hashtable.get(_rec_name)
                if not _candidates:
                    logger.warning(
                        f"pull_recommends_extras: '{_rec_name}' (recommended by "
                        f"'{_seed_name}') not in package cache — skipped"
                    )
                    continue
                # skip_src gate: inspect the latest-version candidate's
                # source name before calling parse_dependency so we don't
                # waste a walk on something we'll refuse to build.
                # package_hashtable is Dict[name, Dict[Version, List[Package]]]
                # — inner list is per-mirror (same name+version from main
                # AND security).  Any mirror's record works for the
                # source-name lookup; parse_sources picks the right mirror
                # later when mapping binary → source.
                _ver = max(_candidates.keys())
                _ver_bucket = _candidates[_ver]
                if not _ver_bucket:
                    logger.warning(
                        f"pull_recommends_extras: '{_rec_name}' has empty "
                        f"version bucket for {_ver} — skipped"
                    )
                    continue
                try:
                    _src_name = _ver_bucket[0].source
                except Exception as e:
                    logger.warning(
                        f"pull_recommends_extras: cannot read source for "
                        f"'{_rec_name}': {type(e).__name__}: {e} — skipped"
                    )
                    continue
                if _src_name in self.__cache.skip_src:
                    logger.warning(
                        f"pull_recommends_extras: '{_rec_name}' skipped — "
                        f"source '{_src_name}' is on cache.skip_src"
                    )
                    _skipped_skip_src += 1
                    continue
                # Walk via parse_dependency so the recommend's own hard
                # Depends graph (libksba8, libnpth0, etc) gets pulled in
                # transitively.  Constructor's select_recommended=False
                # means recommends-of-recommends are NOT followed.
                _resolved = self.parse_dependency(_rec_name)
                if _resolved is None:
                    logger.warning(
                        f"pull_recommends_extras: '{_rec_name}' (recommended "
                        f"by '{_seed_name}') failed to resolve — skipped"
                    )
                    continue
                _added += 1
        # Mark every NEWLY added canonical pkg as extras — the direct
        # recommends AND every transitive Depends pulled in for them.
        # Virtual-alias entries (same Package object under a Provides
        # name) are filtered by the canonical-only check.
        _delta_canonical = {
            n for n in (set(self.selected_pkgs.keys()) - _pre_keys)
            if n == self.selected_pkgs[n]['Package']
        }
        self.extras_pkg_names |= _delta_canonical
        _transitive = len(_delta_canonical) - _added
        logger.warning(
            f"pull_recommends_extras: added {_added} direct recommend(s), "
            f"+{_transitive} transitive dep(s) to selected_pkgs "
            f"(skip_src skipped {_skipped_skip_src})"
        )
        return _added

    def derive_extras_src_names(self) -> int:
        """After parse_sources has populated selected_srcs, identify
        which sources are extras-only (every produced binary is
        in extras_pkg_names) so source_build can route them to the
        `recommended` mode and away from the default build run.

        A source whose binaries are mixed (some selected, some extras) is
        intentionally NOT in extras_src_names — it gets built in the
        default source_build run and the recommended binaries fall out
        as side artefacts of dpkg-buildpackage.

        Returns the number of extras-only sources identified.
        """
        self.extras_src_names.clear()
        if not self.extras_pkg_names:
            return 0
        # Map binary filename → canonical package name.  src_pkg_files[src]
        # holds filenames (e.g. 'foo_1.0_amd64.deb'); we need package names
        # to check membership in extras_pkg_names.  Build a reverse index
        # from selected_pkgs once.
        _bin_filename_to_name = {}
        for _name in self.selected_pkgs:
            if _name != self.selected_pkgs[_name]['Package']:
                continue  # virtuals — same canonical pkg, skip duplicates
            _filename = (self.selected_pkgs[_name].get('Filename') or '')\
                .rsplit('/', 1)[-1]
            if _filename:
                _filename = utils.normalize_repo_filename(_filename)
                _bin_filename_to_name[_filename] = _name
        for _src_name in self.selected_srcs:
            _src_bins = self.src_pkg_files.get(_src_name) or []
            if not _src_bins:
                continue  # source has no binaries known yet — not extras-only
            _pkg_names = [
                _bin_filename_to_name.get(_fn) for _fn in _src_bins
            ]
            # All binaries known to us AND every one is in extras_pkg_names.
            if all(n is not None and n in self.extras_pkg_names
                   for n in _pkg_names):
                self.extras_src_names.add(_src_name)
        logger.info(
            f"derive_extras_src_names: {len(self.extras_src_names)} "
            f"extras-only source(s) identified"
        )
        return len(self.extras_src_names)

    def derive_subset_exclusive_src_names(self) -> tuple:
        """After parse_sources has populated selected_srcs, identify
        which sources are exclusive to live or installer (every
        produced binary lives in the corresponding *_exclusive_pkg_names).

        Mirrors derive_extras_src_names: a source whose binaries are mixed
        (some pkg-layer binaries, some live/installer-exclusive) is NOT
        marked exclusive — it gets built in the pkg-layer source build run
        and the live/installer binaries fall out as side artefacts of
        dpkg-buildpackage.

        Returns ``(live_count, installer_count)``.
        """
        self.live_exclusive_src_names.clear()
        self.installer_exclusive_src_names.clear()
        if not (self.live_exclusive_pkg_names or
                self.installer_exclusive_pkg_names):
            return 0, 0
        # Reuse the same binary-filename → canonical-pkg-name index
        # construction as derive_extras_src_names; factor later if a
        # third caller arrives.
        _bin_filename_to_name = {}
        for _name in self.selected_pkgs:
            if _name != self.selected_pkgs[_name]['Package']:
                continue
            _filename = (self.selected_pkgs[_name].get('Filename') or '')\
                .rsplit('/', 1)[-1]
            if _filename:
                _filename = utils.normalize_repo_filename(_filename)
                _bin_filename_to_name[_filename] = _name
        for _src_name in self.selected_srcs:
            _src_bins = self.src_pkg_files.get(_src_name) or []
            if not _src_bins:
                continue
            _pkg_names = [_bin_filename_to_name.get(_fn) for _fn in _src_bins]
            if all(n is not None and n in self.live_exclusive_pkg_names
                   for n in _pkg_names):
                self.live_exclusive_src_names.add(_src_name)
            if all(n is not None and n in self.installer_exclusive_pkg_names
                   for n in _pkg_names):
                self.installer_exclusive_src_names.add(_src_name)
        logger.info(
            f"derive_subset_exclusive_src_names: "
            f"live={len(self.live_exclusive_src_names)}, "
            f"installer={len(self.installer_exclusive_src_names)}"
        )
        return (len(self.live_exclusive_src_names),
                len(self.installer_exclusive_src_names))

    def parse_sources(self) -> bool:
        """Populate self.selected_srcs (source-name → Source) and
        self.src_pkg_files (source-name → list of predicted binary
        filenames in THIS tree) for every canonical pkg in
        self.selected_pkgs.

        src_pkg_files is per-tree storage — see __init__ for why
        (Source.pkgs was shared across trees, causing the udeb tree's
        parse_sources to overwrite the deb tree's entries).  Readers
        that need the union across both trees take both maps and union
        explicitly at the call site.
        """
        logger.info(
            f"parse_sources: resolving sources for {len(self.selected_pkgs)} "
            f"selected pkg(s)"
        )
        _found = True
        # Reset src_pkg_files — caller may invoke parse_sources multiple
        # times (e.g. `dep parse force` after pkg.list edits).  Stale
        # filename entries would survive selected_pkgs changes.
        self.src_pkg_files.clear()
        # Build (source_name, source_version, Package) tuples. Skip packages whose
        # python-debian .source / .source_version properties raise on malformed data.
        _src_list = []
        for _pkg in self.selected_pkgs:
            if _pkg != self.selected_pkgs[_pkg]['Package']:
                continue
            try:
                _src_list.append((self.selected_pkgs[_pkg].source,
                                  self.selected_pkgs[_pkg].source_version,
                                  self.selected_pkgs[_pkg]))
            except Exception as e:
                tui.console.print(f"WARNING: cannot read source info for {_pkg}, skipping")
                logger.error(f"parse_sources source-access for {_pkg}: {type(e).__name__}: {e}")
                _found = False
        for _src in _src_list:
            _src_name = _src[0]
            _bin_pkg   = _src[2]
            if _src_name not in self.selected_srcs:
                _src_version = _src[1]

                if _src_name not in self.__cache.source_hashtable:
                    tui.console.print(f"ERROR: Source package '{_src_name}' not found in cache")
                    logger.error(f"CRITICAL: Source '{_src_name}' not found in cache — build cannot proceed")
                    _found = False
                    continue

                _src_candidates = self.__cache.source_hashtable[_src_name]
                if len(_src_candidates) == 1:
                    self.selected_srcs[_src_name] = _src_candidates[0]
                else:
                    # Multiple candidates: filter by exact source version.
                    # With multi-mirror ingest the same (name, version) can
                    # appear in BOTH main and security (a security upload
                    # also lands in main once unstable→testing→stable
                    # transitions catch up).  When that happens, prefer the
                    # source whose _mirror matches the binary's origin so
                    # all downloads for this build come from the same pool.
                    _matched = [s for s in _src_candidates if s.version == Version(_src_version)]
                    if not _matched:
                        tui.console.print(f"ERROR: Source '{_src_name}' version '{_src_version}' not found")
                        logger.error(f"Source '{_src_name}' version '{_src_version}' not found in cache")
                        _found = False
                        continue
                    if len(_matched) == 1:
                        self.selected_srcs[_src_name] = _matched[0]
                    else:
                        _bin_mirror = getattr(_bin_pkg, '_mirror', None)
                        _same_mirror = [s for s in _matched
                                        if _bin_mirror is not None and s._mirror is _bin_mirror]
                        _picked = _same_mirror[0] if _same_mirror else _matched[0]
                        self.selected_srcs[_src_name] = _picked
                        # _picked._mirror is set by Cache.__build_cache at
                        # parse time; non-None by the time we get here.
                        _picked_mirror_id = (
                            _picked._mirror.id if _picked._mirror else '?')
                        logger.info(
                            f"parse_sources: {_src_name} {_src_version} present in "
                            f"{len(_matched)} mirrors; picked {_picked_mirror_id}"
                        )

            _bin_filename = (_bin_pkg.get('Filename') or '').rsplit('/', 1)[-1]
            if _bin_filename:
                # Map the cache's upstream APT Filename onto what our
                # build pipeline emits.  utils.normalize_repo_filename
                # strips both +bN (Debian-buildd binNMU) and +debNuN /
                # ~bpoN+N / +rpiN / -Nb (upstream NMU layers, removed
                # by our post-`dpkg-buildpackage` strip).  Result: the
                # filename in src_pkg_files matches what lands in repo/main.
                _bin_filename = utils.normalize_repo_filename(_bin_filename)
                _bucket = self.src_pkg_files.setdefault(_src_name, [])
                if _bin_filename not in _bucket:
                    _bucket.append(_bin_filename)

        logger.warning(f"parse_sources: selected {len(self.selected_srcs)} source packages")
        return _found

    @property
    def download_size(self):
        _download_size = 0
        for _pkg in self.selected_srcs:
            _download_size += self.selected_srcs[_pkg].download_size
        return _download_size

    # ── pickle support (dormant) ──────────────────────────────────
    # Kept as correct serialization support though no caller pickles a
    # DependencyTree today (the session-persistence/`resume` layer that did
    # was removed — MAT-06).  Drops the back-ref to Cache (a re-loader would
    # rewire it after rebuilding Cache from disk) and flattens __lookahead's
    # defaultdict (lambdas can't pickle).  Both restored on __setstate__ —
    # __cache to None (caller rewires), __lookahead to a fresh defaultdict.
    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop('_DependencyTree__cache', None)
        _la = state.get('_DependencyTree__lookahead')
        if _la is not None:
            state['_DependencyTree__lookahead'] = {
                k: dict(v) for k, v in _la.items()
            }
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        _la_flat = self.__dict__.get('_DependencyTree__lookahead', {})
        _la: 'Dict[str, Dict[Version, package.Package]]' = defaultdict(dict)
        _la.update(_la_flat)
        self.__dict__['_DependencyTree__lookahead'] = _la
        # __cache left None — a re-loader rewires it after rebuilding Cache.
        self.__dict__['_DependencyTree__cache'] = None
