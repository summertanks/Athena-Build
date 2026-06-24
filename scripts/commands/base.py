"""Shared typed surface for the BuildSession command mixins.

``BuildSession`` (build.py) is split into functional ``*CommandsMixin``
classes, one per command cluster.  Every mixin inherits ``SessionState``,
which declares — *for the type checker only* — the instance attributes
that ``BuildSession.__init__`` sets.  Without this, mypy would flag
``self.config`` / ``self.cache`` / … inside a mixin's typed method bodies
as unknown attributes (the ``__init__`` that defines them lives on the
final class, not the mixin).

Runtime: ``SessionState`` has NO behaviour and sets nothing — the
declarations live under ``TYPE_CHECKING`` so they never shadow the real
values ``BuildSession.__init__`` assigns.  The mixins contribute methods;
MRO assembles them onto the one ``BuildSession`` instance, so a typed
method in one cluster calling ``self._helper()`` defined in another
resolves normally at runtime.  Cross-cluster helper signatures that the
checker needs are declared below as they arise.
"""
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import buildcontainer
    import dependencytree
    from cache import Cache
    from utils import BuildConfig
    from build import BuildFlags


class SessionState:
    """Type-only declaration of BuildSession's shared instance state.

    Inherited by every command mixin so their typed method bodies see the
    attributes ``BuildSession.__init__`` populates.  Carries no runtime
    behaviour.
    """

    if TYPE_CHECKING:
        config: 'BuildConfig'
        tui: Any                       # Tui | Cli — duck-typed, intentionally Any
        cache: 'Optional[Cache]'
        dep_tree: 'Optional[dependencytree.DependencyTree]'
        udeb_dep_tree: 'Optional[dependencytree.DependencyTree]'
        container: 'Optional[buildcontainer.BuildContainer]'
        flags: 'BuildFlags'
        last_source_build_counts: 'Optional[dict]'
        _in_update_build: bool
        selected_srcs: dict
        download_size: int
        # set/get registries — late-bound on BuildSession at module scope
        # in build.py (BuildSession._SETTABLE = {...}); declared here so the
        # cmd_set / cmd_get mixin methods type-check.
        _SETTABLE: dict
        _GETTABLE: dict

        # Cross-cluster helpers — defined in another mixin (or BuildSession
        # itself) but called from this mixin's typed method bodies, so the
        # checker needs their signatures here.  Listed as they arise; the
        # real implementations override these declarations via MRO.
        def _group_help(self, group: str, table: dict,
                        unknown: str = ...) -> None: ...
        def _update_build_pending(self) -> bool: ...
        def _resolve_live_cohort(self) -> 'Optional[frozenset]': ...
        def _resolve_install_corpus(self) -> 'Optional[frozenset]': ...
        def _resolve_installer_cohort(self) -> 'Optional[frozenset]': ...
        @staticmethod
        def _canonical_names(tree: Any) -> 'set[str]': ...
        def _scan_stale_files(self) -> 'tuple[list, list, list, list, int]': ...
        def _scan_orphaned_sidecars(self) -> 'list[tuple[str, str]]': ...
        def _source_state(self, pkg: str, src: Any) -> str: ...
        @staticmethod
        def _print_wrapped_names(label: str, names: 'list[str]',
                                 wrap: int = ...) -> None: ...
        def _mirror_floor(self) -> str: ...
        def _snapshot_current(self) -> Any: ...
        def _predicted_files_for_source(self, src_name: str) -> 'list[str]': ...
        def _do_tunnel(self, src_pkg: Any) -> bool: ...
        def cmd_audit(self, *args: Any) -> Any: ...
        def _tunnel_filenames_for_source(self, src_name: str) -> 'list[str]': ...
