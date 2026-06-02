"""Handlers for the `print` TUI command.

`cmd_print()` in build.py is a thin dispatcher into this module — the actual
view code lives here.  Each handler receives the live ``BuildSession`` so it
can read ``self.config`` / ``self.cache`` / ``self.dep_tree`` and writes to
the console tab via ``tui.console.print``.

Adding a new view: write a ``_print_<category>(session, *extras)`` function
and register it in :data:`CATEGORIES` with a group + one-line description.
The help screen (``print help``) and the unknown-category error path pick
it up automatically; no changes to build.py or the TUI registration line
needed.

Group field controls where a category appears in the help screen.  Add a
new group by appending to :data:`_HELP_GROUP_ORDER` so the section shows up.
"""
import datetime
import os
from collections import defaultdict
from typing import Callable, NamedTuple, Optional, TYPE_CHECKING

import tui
import utils

if TYPE_CHECKING:
    from build import BuildSession


# ─── public types ────────────────────────────────────────────────────────────

class AutorunTiming(NamedTuple):
    """Wall-clock context the autorun summary needs from cmd_auto_run.

    Operator-invoked `print summary` passes timing=None and the helper
    renders a state snapshot without the timing rows.
    """
    started:    datetime.datetime
    finished:   datetime.datetime
    elapsed:    int
    aborted_at: Optional[str]


def format_duration(seconds: int) -> str:
    """Render `seconds` as `Hh MMm SSs` / `Mm SSs` / `Ss`.

    Public so cmd_auto_run can compose log lines with the same shape the
    summary uses, and so unit tests pin the format.
    """
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ─── helpers ────────────────────────────────────────────────────────────────

def _fmt_dep(dep_tuple) -> str:
    """Turn a parsed dep tuple (name, ver, op) into 'name' or 'name (op ver)'.

    apt_pkg.parse_depends returns each constraint as (name, version, op)
    where unconstrained deps have op == '' and version == ''.
    """
    name, ver, op = dep_tuple[0], dep_tuple[1] if len(dep_tuple) > 1 else '', \
                    dep_tuple[2] if len(dep_tuple) > 2 else ''
    if op and ver:
        return f"{name} ({op} {ver})"
    return name


def _fmt_dep_group(group) -> str:
    """An alt-dep group renders as 'a | b (>= 2) | c'."""
    return ' | '.join(_fmt_dep(t) for t in group)


def _require_dep_check(session) -> bool:
    """Print 'run parse_dependency first' and return False if not ready."""
    if not session.flags.dep_check_ready:
        tui.console.print("Run 'cache parse' first")
        return False
    return True


def _require_cache(session) -> bool:
    """Print 'run build_cache first' and return False if not ready."""
    if not session.flags.cache_ready or session.cache is None:
        tui.console.print("Run 'cache build' first")
        return False
    return True


# ─── Configuration views ────────────────────────────────────────────────────

def _print_config(session, *_extras) -> None:
    """Overall build configuration.  Calls out to mirrors / snapshot / paths
    sub-views for the nested sections to keep this digestible."""
    cfg = session.config
    tui.console.print("Build Configuration:")
    tui.console.print(f"    Arch                : {cfg.arch}")
    tui.console.print(f"    Release             : {cfg.release}")
    tui.console.print(f"    Base ID (default)   : {cfg.baseid}")
    tui.console.print(f"    Parent version      : {cfg.baseversion}")
    tui.console.print(f"    Distribution        : {cfg.build_distribution}")
    tui.console.print(f"    Build codename      : {cfg.build_codename}")
    tui.console.print(f"    Build version       : {cfg.build_version}")
    tui.console.print(
        f"    Recommends in repo  : "
        f"{getattr(cfg, 'include_recommends', False)}  "
        f"([Build] IncludeRecommends)"
    )
    if getattr(cfg, 'build_profiles', None):
        tui.console.print(f"    Build profiles      : {', '.join(sorted(cfg.build_profiles))}")
    if getattr(cfg, 'build_options', None):
        tui.console.print(f"    Build options       : {', '.join(sorted(cfg.build_options))}")
    tui.console.print("")
    tui.console.print(f"  Mirrors             : {len(cfg.mirrors)}  (use `print mirrors` for detail)")
    if cfg.snapshot_enabled:
        tui.console.print("  Snapshot            : enabled    (use `print snapshot` for detail)")
    else:
        tui.console.print("  Snapshot            : disabled (live mirrors)")
    tui.console.print(f"  Tunneled packages   : {len(getattr(cfg, 'tunnel_packages', []))}  (use `print tunneled`)")
    tui.console.print(f"  Paths               : working dir {cfg.working_dir}  (use `print paths`)")


def _print_mirrors(session, *_extras) -> None:
    """All configured mirrors with URL, suite, component."""
    cfg = session.config
    tui.console.print(f"Configured mirrors ({len(cfg.mirrors)}):")
    for _m in cfg.mirrors:
        tui.console.print(f"  [{_m.id:8}]")
        tui.console.print(f"    URL        : {_m.url}")
        tui.console.print(f"    Suite      : {_m.suite}")
        tui.console.print(f"    Component  : {_m.component}")
        if hasattr(_m, 'baseurl'):
            tui.console.print(f"    Base URL   : {_m.baseurl}")


def _print_snapshot(session, *_extras) -> None:
    """Snapshot pin status: configured value + resolved date + state file."""
    cfg = session.config
    if not cfg.snapshot_enabled:
        tui.console.print("Snapshot pinning: disabled (live mirrors)")
        tui.console.print("  (set [Snapshot] Enabled = true in config/build.conf to pin)")
        return
    tui.console.print("Snapshot pinning: enabled")
    tui.console.print(f"  Configured  : {cfg.snapshot_timestamp_config}")
    try:
        _resolved = utils.resolve_snapshot_timestamp(cfg)
    except (RuntimeError, ValueError) as e:
        tui.console.print(f"  Resolved    : <unresolved: {e}>")
        _resolved = None
    if _resolved:
        _human = utils.format_snapshot_timestamp(_resolved)
        tui.console.print(f"  Resolved    : {_resolved}  ({_human})")
    _state = os.path.join(cfg.dir_cache, 'snapshot.timestamp')
    _state_exists = os.path.exists(_state)
    tui.console.print(f"  State file  : {_state}  "
                      f"{'(present)' if _state_exists else '(absent — will resolve on next run)'}")


def _print_paths(session, *_extras) -> None:
    """Every directory the build uses, plus key file paths."""
    cfg = session.config
    tui.console.print("Build paths:")
    tui.console.print(f"  Working dir       : {cfg.working_dir}")
    _attrs = [
        ('dir_cache',            'Cache             '),
        ('dir_download',         'Download          '),
        ('dir_source',           'Source            '),
        ('dir_repo',             'Repo (.debs)      '),
        ('dir_chroot',           'Chroot            '),
        ('dir_image',            'Image (ISO)       '),
        ('dir_log',              'Log               '),
        ('dir_temp',             'Temp              '),
        ('dir_config',           'Config            '),
        ('dir_patch',            'Patch             '),
        ('dir_patch_source',     '  source patches  '),
        ('dir_patch_preinstall', '  pre-install     '),
        ('dir_patch_postinstall','  post-install    '),
        ('dir_gnupg',            'GPG home          '),
    ]
    for attr, label in _attrs:
        if hasattr(cfg, attr):
            tui.console.print(f"  {label}: {getattr(cfg, attr)}")
    tui.console.print("")
    tui.console.print(f"  Config file       : {cfg.config_path}")
    tui.console.print(f"  Package list      : {cfg.pkglist_path}")


# ─── Build state views ──────────────────────────────────────────────────────

def _print_state(session, *_extras) -> None:
    """Pipeline stage progress (BuildFlags) split by target.

    Three sections:
      Shared          — stages whose output feeds both ISO targets
                        (cache, dep tree, sources, build container,
                        source build, signing key).
      Live ISO target — live chroot build, verify, ISO build.
      Installer ISO target — installer chroot build (from udeb
                        closure), ISO build.

    The installer chroot has no `verify` step today (its layout is a
    rootskel-driven udeb closure, not a target system; the 8-check
    verifier in `chroot.py` doesn't apply).  If/when an installer-
    side verifier lands, add a row here.
    """
    flags = session.flags

    def _row(attr: str, label: str, desc: str) -> None:
        _ok = bool(getattr(flags, attr, False))
        _mark = '✓' if _ok else '·'
        tui.console.print(f"  [{_mark}] {label}  {desc}")

    tui.console.print("Pipeline state:")
    tui.console.print("")
    tui.console.print("  Shared:", tui.COLOR_INFO)
    _shared = [
        ('cache_ready',           'cache_build           ', 'fetch + index APT mirrors'),
        ('dep_check_ready',       'dep_parse             ',
            'resolve dep graph (pkg/live/installer/pool.list)'),
        ('download_ready',        'source_download       ', 'fetch upstream source archives'),
        ('build_container_ready', 'container_init        ', 'init Docker build sandbox'),
        ('source_build_ready',    'source_build          ', 'run dpkg-buildpackage per source'),
        ('signing_key_verified',  'signing_key_verified  ',
            'sign+verify roundtrip against project key (CONF-02)'),
    ]
    for attr, label, desc in _shared:
        _row(attr, label, desc)

    tui.console.print("")
    tui.console.print("  Live ISO target:", tui.COLOR_INFO)
    _live = [
        ('chroot_ready',     'chroot_build_live     ',
            'install built .debs into buildroot/live'),
        ('chroot_verified',  'chroot_verify         ',
            '8-check verifier (passes ⇒ live ISO ok)'),
        ('iso_live_ready',   'iso_build_live        ',
            'hybrid BIOS/EFI live ISO built'),
        ('iso_disk_ready',   'iso_build_disk        ',
            'pre-installed bootable qcow2 disk image'),
    ]
    for attr, label, desc in _live:
        _row(attr, label, desc)

    tui.console.print("")
    tui.console.print("  Installer ISO target:", tui.COLOR_INFO)
    _installer = [
        ('chroot_installer_ready', 'chroot_build_installer',
            'udeb closure unpacked into buildroot/installer'),
        ('iso_installer_ready',    'iso_build_installer   ',
            'hybrid BIOS/EFI installer ISO built'),
    ]
    for attr, label, desc in _installer:
        _row(attr, label, desc)
    tui.console.print("")
    tui.console.print(
        "  `iso build` runs separately — `iso build live` after "
        "chroot_verify, `iso build installer` after chroot_build_installer."
    )


def _print_stats(session, *_extras) -> None:
    """High-level counts across cache, dep tree, and source resolution."""
    tui.console.print("Build statistics:")
    cfg = session.config
    tui.console.print(f"  Mirrors configured       : {len(cfg.mirrors)}")
    tui.console.print("  Snapshot pinning         : "
                      f"{'enabled' if cfg.snapshot_enabled else 'disabled'}")
    if session.cache is not None:
        c = session.cache
        tui.console.print(f"  Cache: package names     : {len(c.package_hashtable)}")
        tui.console.print(f"  Cache: source names      : {len(c.source_hashtable)}")
        tui.console.print(f"  Cache: required priority : {len(c.required)}")
        tui.console.print(f"  Cache: important priority: {len(c.important)}")
    else:
        tui.console.print("  Cache                    : not built (run build_cache)")
    if session.dep_tree is not None and session.flags.dep_check_ready:
        dt = session.dep_tree
        canonical = sum(1 for k, v in dt.selected_pkgs.items()
                        if k == v['Package'])
        virtuals = len(dt.selected_pkgs) - canonical
        tui.console.print(f"  Dep tree: canonical pkgs : {canonical}")
        tui.console.print(f"  Dep tree: virtual aliases: {virtuals}")
        tui.console.print(f"  Dep tree: source pkgs    : {len(dt.selected_srcs)}")
        # Always surface the extras counts so the operator can
        # tell "toggle off" from "toggle on, 0 pulled" from "toggle on,
        # N pulled".  Suppressing the row when 0 made the failure mode
        # invisible and confused the operator on the first real run.
        _extras_n = len(getattr(dt, 'extras_pkg_names', set()))
        _extras_only_n = len(getattr(dt, 'extras_src_names', set()))
        _toggle = getattr(cfg, 'include_recommends', False)
        tui.console.print(
            f"  Dep tree: extras (recom) : {_extras_n} pkg(s) "
            f"from {_extras_only_n} extras-only source(s)  "
            f"(toggle={'on' if _toggle else 'off'})"
        )
        tui.console.print("  Dep tree: download size  : "
                          f"{getattr(dt, 'download_size', 0) // (2**20)} MB")
    else:
        tui.console.print("  Dep tree                 : not built (run parse_dependency)")


def summary(session, *, timing: Optional[AutorunTiming] = None) -> None:
    """Render the full pipeline summary used by cmd_auto_run and operator-
    invoked ``print summary``.

    When ``timing`` is provided (autorun-driven path), show SUCCESS / ABORTED
    header and the wall-clock rows.  When ``timing`` is None (operator-driven
    path), show only the per-stage state snapshot — useful as a diagnostic
    view at any point in the pipeline.

    Surfaces a ``ready for build_iso`` hint at the bottom when chroot is
    verified.  Build_iso is intentionally not auto-invoked (see README §
    Building Image / Intro for the rationale: the operator gets a chance to
    inspect / edit the chroot tree before it is sealed into a squashfs).
    """
    cfg = session.config
    tui.console.print("")
    if timing is None:
        tui.console.print("Pipeline summary (current state):", tui.COLOR_HIGHLIGHT)
    elif timing.aborted_at is None:
        tui.console.print("Autorun summary: SUCCESS", tui.COLOR_HIGHLIGHT)
    else:
        tui.console.print(
            f"Autorun summary: ABORTED at '{timing.aborted_at}'",
            tui.COLOR_ERROR,
        )

    if timing is not None:
        _ts_fmt = "%Y-%m-%d %H:%M:%S"
        tui.console.print(f"  Started        : {timing.started.strftime(_ts_fmt)}")
        tui.console.print(f"  Finished       : {timing.finished.strftime(_ts_fmt)}")
        tui.console.print(f"  Wall time      : {format_duration(timing.elapsed)}")
        tui.console.print("")

    # Cache
    if session.cache is not None and session.flags.cache_ready:
        _names = len(session.cache.package_hashtable)
        tui.console.print(f"  Cache          : {_names} package names indexed")
    else:
        tui.console.print("  Cache          : not built")

    # Dep tree
    if session.dep_tree is not None and session.flags.dep_check_ready:
        _canonical = sum(1 for k, v in session.dep_tree.selected_pkgs.items()
                         if k == v['Package'])
        _srcs = len(session.dep_tree.selected_srcs)
        tui.console.print(
            f"  Dep tree       : {_canonical} canonical packages, "
            f"{_srcs} source packages"
        )
        # Extras row only when the toggle has actually pulled
        # something — keeps the summary tight when off.
        _extras_n = len(getattr(session.dep_tree, 'extras_pkg_names', set()))
        if _extras_n:
            _extras_only_n = len(
                getattr(session.dep_tree, 'extras_src_names', set())
            )
            tui.console.print(
                f"  Extras (recom) : {_extras_n} pkg(s) in repo, "
                f"{_extras_only_n} extras-only source(s)"
            )
    else:
        tui.console.print("  Dep tree       : not built")

    # Source build
    sb = getattr(session, 'last_source_build_counts', None)
    if sb is not None:
        tui.console.print(
            f"  Source build   : {sb['built']} built, {sb['tunneled']} tunneled, "
            f"{sb['failed']} failed, {sb['skipped']} skipped"
        )
    else:
        tui.console.print("  Source build   : not run")

    # Chroot — live + installer rendered separately.  Each has its own
    # readiness flag; the installer side has no verify step today (see
    # _print_state docstring for why).
    if session.flags.chroot_verified:
        tui.console.print("  Chroot (live)  : built and verified (8/8 checks passed)")
    elif session.flags.chroot_ready:
        tui.console.print("  Chroot (live)  : built but verify failed — re-run `chroot verify`")
    else:
        tui.console.print("  Chroot (live)  : not built")
    if session.flags.chroot_installer_ready:
        tui.console.print("  Chroot (inst)  : udeb closure unpacked into buildroot/installer")
    else:
        tui.console.print("  Chroot (inst)  : not built")

    # Predicted ISO paths + next-step hints.  Build_iso intentionally
    # not auto-invoked — see docstring above.
    tui.console.print("")
    _version = cfg.build_version.strip('"').strip("'")
    _live_iso_name      = f"athena-{_version}-{cfg.arch}.iso"
    _installer_iso_name = f"athena-installer-{_version}-{cfg.arch}.iso"
    _live_iso_path      = os.path.join(cfg.dir_image, _live_iso_name)
    _installer_iso_path = os.path.join(cfg.dir_image, _installer_iso_name)

    if session.flags.iso_live_ready:
        tui.console.print(f"  ISO live       : {_live_iso_path}  (built)",
                          tui.COLOR_HIGHLIGHT)
    elif session.flags.chroot_verified:
        tui.console.print(f"  ISO live       : {_live_iso_path}", tui.COLOR_INFO)
        tui.console.print("                   Ready — run `iso build live` to produce it.",
                          tui.COLOR_HIGHLIGHT)
    else:
        tui.console.print(f"  ISO live       : {_live_iso_path}  (chroot must verify first)")

    if session.flags.iso_installer_ready:
        tui.console.print(f"  ISO installer  : {_installer_iso_path}  (built)",
                          tui.COLOR_HIGHLIGHT)
    elif session.flags.chroot_installer_ready:
        tui.console.print(f"  ISO installer  : {_installer_iso_path}", tui.COLOR_INFO)
        tui.console.print("                   Ready — run `iso build installer` to produce it.",
                          tui.COLOR_HIGHLIGHT)
    else:
        tui.console.print(f"  ISO installer  : {_installer_iso_path}  (installer chroot must build first)")

    # OBS-01 build times — slowest-N + aggregate, only when records exist.
    _summary_build_times_section(session)

    # Disk image reuses the verified live chroot (same gate as ISO live).
    _distro     = cfg.build_distribution.strip('"').strip("'")
    _disk_name  = f"{_distro.lower()}-{_version}-{cfg.arch}.qcow2"
    _disk_path  = os.path.join(cfg.dir_image, _disk_name)
    if session.flags.iso_disk_ready:
        tui.console.print(f"  Disk image     : {_disk_path}  (built)",
                          tui.COLOR_HIGHLIGHT)
    elif session.flags.chroot_verified:
        tui.console.print(f"  Disk image     : {_disk_path}", tui.COLOR_INFO)
        tui.console.print("                   Ready — run `iso build disk` to produce it.",
                          tui.COLOR_HIGHLIGHT)
    else:
        tui.console.print(f"  Disk image     : {_disk_path}  (chroot must verify first)")


def _print_summary(session, *_extras) -> None:
    """Dispatch handler for ``print summary``.  Operator-invoked, so no
    autorun timing is available — render the state-snapshot variant."""
    summary(session, timing=None)


# ─── OBS-01: build-time views ───────────────────────────────────────────────

def _iter_build_records(session) -> 'list[dict]':
    """Load every `<pkg>.build.json` under log/build/ and verify each
    via the signed-record path.  Skips records that fail HMAC or are
    malformed — the audit treats those as missing, same as readers do."""
    import utils
    _records: 'list[dict]' = []
    _log_dir = getattr(getattr(session, 'config', None), 'dir_log', None)
    if not isinstance(_log_dir, str):
        return _records
    _dir = os.path.join(_log_dir, 'build')
    try:
        _names = os.listdir(_dir)
    except OSError:
        return _records
    for _f in _names:
        if not _f.endswith(utils.BUILD_RECORD_SUFFIX):
            continue
        _pkg = _f[:-len(utils.BUILD_RECORD_SUFFIX)]
        _rec = utils.read_build_record(_dir, _pkg)
        if _rec is None:
            continue
        _records.append(_rec)
    return _records


def _print_build_times(session, *_extras) -> None:
    """All sources sorted by wall-clock elapsed.  Reads the OBS-01
    build records — covers built (phase=done) and failed (phase=failed)
    attempts.  Tunneled and interrupted records carry timing too and
    are surfaced with a status tag.

    The total at the bottom is the upper bound on snapshot-pivot
    rebuild time: changing the snapshot rebuilds every source with
    approximately the same per-source cost ± kernel/glibc variation.
    """
    _records = _iter_build_records(session)
    if not _records:
        tui.console.print(
            "No build records found "
            "(run `source build` to populate log/build/)"
        )
        return
    _rows: 'list[tuple[float, str, str, str]]' = []
    _total = 0.0
    for _r in _records:
        _elapsed = _r.get('elapsed_seconds')
        if not isinstance(_elapsed, (int, float)) or _elapsed < 0:
            _elapsed = 0.0
        _pkg = str(_r.get('package') or '?')
        _status = str(_r.get('status') or _r.get('phase') or '?')
        _built = str(_r.get('built_version') or _r.get('intended_version') or '')
        _rows.append((float(_elapsed), _pkg, _status, _built))
        _total += float(_elapsed)
    _rows.sort(reverse=True)
    tui.console.print(
        f"Build times ({len(_rows)} record(s), "
        f"total {format_duration(int(_total))}):"
    )
    for _e, _p, _s, _v in _rows:
        tui.console.print(
            f"  {format_duration(int(_e)):>10}  "
            f"{_s:<10}  {_p:<32}  {_v}"
        )


def _summary_build_times_section(session) -> None:
    """Slowest-N + aggregate wall time, slotted into `print summary`.
    No-op when no records exist (don't clutter the summary on fresh
    repos)."""
    _records = _iter_build_records(session)
    if not _records:
        return
    _timed = sorted(
        (
            (float(_r.get('elapsed_seconds') or 0.0), str(_r.get('package') or '?'))
            for _r in _records
            if isinstance(_r.get('elapsed_seconds'), (int, float))
        ),
        reverse=True,
    )
    if not _timed:
        return
    _total = sum(_e for _e, _ in _timed)
    tui.console.print("")
    tui.console.print(
        f"  Build times    : {len(_timed)} source(s), "
        f"total {format_duration(int(_total))} wall-clock"
    )
    for _e, _p in _timed[:5]:
        tui.console.print(f"                   {format_duration(int(_e)):>10}  {_p}")


# ─── Package list views ─────────────────────────────────────────────────────

def _print_required(session, *_extras) -> None:
    """All packages with Priority: required from the APT cache."""
    if not _require_cache(session):
        return
    pkgs = session.cache.required
    tui.console.print(f"Required packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_important(session, *_extras) -> None:
    """All packages with Priority: important from the APT cache."""
    if not _require_cache(session):
        return
    pkgs = session.cache.important
    tui.console.print(f"Important packages ({len(pkgs)}):")
    for pkg in sorted(pkgs):
        tui.console.print(f"  {pkg}")


def _print_selected(session, *_extras) -> None:
    """All packages resolved by parse_dependency (canonical names only).
    Marks Recommends-only extras with `(extra)`, live-exclusive with
    `(live)`, and installer-exclusive with `(installer)` so the operator
    can tell at a glance which subset each entry belongs to."""
    if not _require_dep_check(session):
        return
    pkgs = session.dep_tree.selected_pkgs
    real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
    _extras_set:    set = getattr(session.dep_tree, 'extras_pkg_names',           set())
    _live_set:      set = getattr(session.dep_tree, 'live_exclusive_pkg_names',   set())
    _installer_set: set = getattr(session.dep_tree, 'installer_exclusive_pkg_names', set())
    tui.console.print(f"Selected packages ({len(real_pkgs)}):")
    for name in sorted(real_pkgs.keys()):
        if name in _extras_set:
            _suffix = '  (extra)'
        elif name in _live_set:
            _suffix = '  (live)'
        elif name in _installer_set:
            _suffix = '  (installer)'
        else:
            _suffix = ''
        tui.console.print(f"  {name:<40} {real_pkgs[name].version}{_suffix}")


def _print_extras(session, *_extras) -> None:
    """Depth-1 Recommends pulled in by parse_dependency.  These are
    downloaded by source_download, NOT built by default `source_build`
    (use `source build recommended`), and NOT installed in the chroot."""
    if not _require_dep_check(session):
        return
    extras_pkg_names: set = getattr(session.dep_tree, 'extras_pkg_names', set())
    extras_src_names: set = getattr(session.dep_tree, 'extras_src_names', set())
    if not extras_pkg_names:
        tui.console.print(
            "No recommended extras pulled "
            "(check [Build] IncludeRecommends in build.conf)"
        )
        return
    # Map binary name → source name for display.  Walk selected_srcs and
    # build a reverse index from each source's pkgs filename list.
    _src_for_pkg = {}
    for _src_name, _src in session.dep_tree.selected_srcs.items():
        for _bin_filename in (getattr(_src, 'pkgs', []) or []):
            # Filenames are 'name_ver_arch.deb' — first underscore-split chunk
            # is the package name.
            _pkg_name = _bin_filename.split('_', 1)[0]
            _src_for_pkg[_pkg_name] = _src_name
    tui.console.print(
        f"Recommended extras ({len(extras_pkg_names)} pkg(s) from "
        f"{len(extras_src_names)} extras-only source(s)):"
    )
    for name in sorted(extras_pkg_names):
        _src = _src_for_pkg.get(name, '?')
        _src_kind = 'extras-only' if _src in extras_src_names else 'mixed source'
        tui.console.print(
            f"  {name:<40} from {_src:<24} ({_src_kind})"
        )


def _print_live_exclusive(session, *_extras) -> None:
    """Packages pulled in by live.list that are NOT already in
    pkg.list's closure.  These ride on top of pkg's install set to make
    a complete live system."""
    if not _require_dep_check(session):
        return
    live_pkgs: set = getattr(session.dep_tree, 'live_exclusive_pkg_names', set())
    live_srcs: set = getattr(session.dep_tree, 'live_exclusive_src_names', set())
    if not live_pkgs:
        tui.console.print(
            "No live-exclusive packages "
            "(live.list empty, or every entry already pulled by pkg.list)"
        )
        return
    _src_for_pkg = {}
    for _src_name, _src in session.dep_tree.selected_srcs.items():
        for _bin_filename in (getattr(_src, 'pkgs', []) or []):
            _pkg_name = _bin_filename.split('_', 1)[0]
            _src_for_pkg[_pkg_name] = _src_name
    tui.console.print(
        f"Live-exclusive ({len(live_pkgs)} pkg(s) from "
        f"{len(live_srcs)} live-only source(s)):"
    )
    for name in sorted(live_pkgs):
        _src = _src_for_pkg.get(name, '?')
        _src_kind = 'live-only src' if _src in live_srcs else 'mixed src'
        tui.console.print(
            f"  {name:<40} from {_src:<24} ({_src_kind})"
        )


def _print_udebs(session, *_extras) -> None:
    """Udeb closure (the installer ramdisk content).  Drawn from the
    parallel udeb_dep_tree resolved against the d-i Packages index.
    Empty until parse_dependency runs."""
    if not _require_dep_check(session):
        return
    udeb_tree = getattr(session, 'udeb_dep_tree', None)
    if udeb_tree is None:
        tui.console.print("Udeb tree not built — re-run 'cache parse'")
        return
    pkgs = udeb_tree.selected_pkgs
    real_pkgs = {k: v for k, v in pkgs.items() if k == v['Package']}
    srcs = getattr(udeb_tree, 'selected_srcs', {})
    if not real_pkgs:
        tui.console.print(
            "Udeb closure is empty (installer.list has no udeb entries; "
            "check installer.list and Cache.udeb_required/important)"
        )
        return
    tui.console.print(
        f"Udeb closure ({len(real_pkgs)} udeb(s) from {len(srcs)} source(s)):"
    )
    for name in sorted(real_pkgs.keys()):
        # Version is python-debian's Version class which raises on format
        # specifiers like {:<24}; coerce to str first.
        _ver = str(real_pkgs[name].version)
        try:
            _src = real_pkgs[name].source or '?'
        except Exception:
            _src = '?'
        tui.console.print(f"  {name:<40} {_ver:<24} ← {_src}")


def _print_installer_exclusive(session, *_extras) -> None:
    """Packages pulled in by installer.list that are NOT already in
    pkg.list's closure.  Ride on top of pkg's install set for the
    installer ISO target."""
    if not _require_dep_check(session):
        return
    inst_pkgs: set = getattr(session.dep_tree, 'installer_exclusive_pkg_names', set())
    inst_srcs: set = getattr(session.dep_tree, 'installer_exclusive_src_names', set())
    if not inst_pkgs:
        tui.console.print(
            "No installer-exclusive packages (installer.list empty)"
        )
        return
    _src_for_pkg = {}
    for _src_name, _src in session.dep_tree.selected_srcs.items():
        for _bin_filename in (getattr(_src, 'pkgs', []) or []):
            _pkg_name = _bin_filename.split('_', 1)[0]
            _src_for_pkg[_pkg_name] = _src_name
    tui.console.print(
        f"Installer-exclusive ({len(inst_pkgs)} pkg(s) from "
        f"{len(inst_srcs)} installer-only source(s)):"
    )
    for name in sorted(inst_pkgs):
        _src = _src_for_pkg.get(name, '?')
        _src_kind = 'installer-only src' if _src in inst_srcs else 'mixed src'
        tui.console.print(
            f"  {name:<40} from {_src:<24} ({_src_kind})"
        )


def _print_groups(session, *_extras) -> None:
    """pkg.list groups — packages resolved per `[group]` header in
    declaration order.  `[base]` is always installed (live image +
    target debootstrap); other groups ship in /cdrom/pool only and the
    installer (tasksel) apt-installs the operator-selected subset
    onto /target at install time.

    Per-group size column is best-effort: sums the on-disk size of
    every canonical-name `.deb` in `repo/` matching the group's
    members.  Shown in MB rounded to one decimal.  When `repo/` is
    empty or no .debs match yet (source build hasn't run), the size
    column reads `?` so the operator still sees the package count."""
    if not _require_dep_check(session):
        return
    _groups: 'dict[str, set]' = getattr(
        session.dep_tree, 'pkg_group_pkg_names', {})
    if not _groups:
        tui.console.print("No pkg.list groups resolved yet")
        return
    _extras_set: set = getattr(
        session.dep_tree, 'pkg_group_extras_pkg_names', set())

    # Best-effort .deb size map for the size column.  Reuses the same
    # filename parser that _select_pool_files uses so the canonical
    # name lookup matches what ends up in the ISO pool.
    _size_by_pkg: 'dict[str, int]' = {}
    _repo = getattr(session.config, 'dir_repo', None)
    if _repo and os.path.isdir(_repo):
        import iso_installer as _iso_inst
        try:
            for _fn in os.listdir(_repo):
                if not _fn.endswith('.deb'):
                    continue
                _pkg, _ = _iso_inst._parse_deb_filename(_fn)
                if not _pkg:
                    continue
                try:
                    _sz = os.path.getsize(os.path.join(_repo, _fn))
                except OSError:
                    continue
                # When repo/ holds multiple versions of the same
                # package, take the largest as the size estimate.
                # Approximate, fine for an operator-facing report.
                if _sz > _size_by_pkg.get(_pkg, 0):
                    _size_by_pkg[_pkg] = _sz
        except OSError:
            pass

    tui.console.print(
        f"pkg.list groups ({len(_groups)} group(s), "
        f"{len(_extras_set)} non-base canonical(s) in /cdrom/pool):"
    )
    for _group, _names in _groups.items():
        _label = 'base — installed everywhere' if _group == 'base' else \
                 'pool-only, installer apt-installs on selection'
        _total = sum(_size_by_pkg.get(_n, 0) for _n in _names)
        if _total > 0:
            _size_str = f"{_total / (1024 * 1024):.1f} MB"
        else:
            _size_str = '?'
        tui.console.print(
            f"  [{_group}]  {len(_names)} pkg(s)  {_size_str}  ({_label})"
        )
        for _name in sorted(_names):
            tui.console.print(f"    {_name}")


def _print_tunneled(session, *_extras) -> None:
    """Packages set to use prebuilt .debs from the base Debian repo."""
    cfg = session.config
    tunneled = getattr(cfg, 'tunnel_packages', [])
    if not tunneled:
        tui.console.print("No tunneled packages configured "
                          "(Tunneled list empty in config/build.conf)")
        return
    tui.console.print(f"Tunneled packages ({len(tunneled)}):")
    for name in sorted(tunneled):
        tui.console.print(f"  {name}")


def _print_pkg(session, *extras) -> None:
    """`print pkg <name>` — full record for one package from the dep tree."""
    if not extras:
        tui.console.print("Usage: print pkg <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    pkgs = session.dep_tree.selected_pkgs
    if name not in pkgs:
        tui.console.print(f"Package {name!r} is not in the current dep tree.")
        tui.console.print("  (`print selected` lists everything that is.)")
        return
    p = pkgs[name]
    _mirror = getattr(p, '_mirror', None)
    _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'

    tui.console.print(f"Package: {p['Package']}")
    if name != p['Package']:
        tui.console.print(f"  (entry under virtual name {name!r})")
    tui.console.print(f"  Version       : {p.version}")
    tui.console.print(f"  Architecture  : {p.arch}")
    tui.console.print(f"  Priority      : {p.priority}")
    tui.console.print(f"  Mirror        : {_mirror_id}")
    if p.get('Section'):
        tui.console.print(f"  Section       : {p['Section']}")
    if p.get('Maintainer'):
        tui.console.print(f"  Maintainer    : {p['Maintainer']}")
    if p.get('Filename'):
        tui.console.print(f"  Filename      : {p['Filename']}")

    _sections = [
        ('Pre-Depends', p.pre_depends, p.alt_pre_depends),
        ('Depends',     p.depends,     p.alt_depends),
    ]
    for label, plain, alts in _sections:
        if plain or alts:
            tui.console.print(f"  {label}:")
            for d in plain:
                tui.console.print(f"      {_fmt_dep(d)}")
            for grp in alts:
                tui.console.print(f"      {_fmt_dep_group(grp)}")

    _multi_sections = [
        ('Recommends',  [[d] for d in p.recommends]),
        ('Suggests',    [[d] for d in p.suggests]),
        ('Conflicts',   p.conflicts),
        ('Breaks',      p.breaks),
        ('Provides',    p.provides),
        ('Replaces',    p.replaces),
    ]
    for label, groups in _multi_sections:
        if groups:
            tui.console.print(f"  {label}:")
            for grp in groups:
                tui.console.print(f"      {_fmt_dep_group(grp)}")


def _print_deps(session, *extras) -> None:
    """`print deps <name>` — flat depends + pre-depends list (no other fields)."""
    if not extras:
        tui.console.print("Usage: print deps <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    pkgs = session.dep_tree.selected_pkgs
    if name not in pkgs:
        tui.console.print(f"Package {name!r} is not in the current dep tree.")
        return
    p = pkgs[name]
    tui.console.print(f"{name} dependencies:")
    if p.pre_depends or p.alt_pre_depends:
        tui.console.print("  Pre-Depends:")
        for d in p.pre_depends:
            tui.console.print(f"    {_fmt_dep(d)}")
        for grp in p.alt_pre_depends:
            tui.console.print(f"    {_fmt_dep_group(grp)}")
    if p.depends or p.alt_depends:
        tui.console.print("  Depends:")
        for d in p.depends:
            tui.console.print(f"    {_fmt_dep(d)}")
        for grp in p.alt_depends:
            tui.console.print(f"    {_fmt_dep_group(grp)}")
    if not (p.depends or p.alt_depends or p.pre_depends or p.alt_pre_depends):
        tui.console.print("  (no dependencies)")


# ─── Source views ───────────────────────────────────────────────────────────

def _print_sources(session, *_extras) -> None:
    """All selected source packages with mirror + version."""
    if not _require_dep_check(session):
        return
    srcs = session.dep_tree.selected_srcs
    tui.console.print(f"Selected source packages ({len(srcs)}):")
    for name in sorted(srcs):
        s = srcs[name]
        _mirror = getattr(s, '_mirror', None)
        _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'
        tui.console.print(f"  {name:<40} {str(s.version):<24} [{_mirror_id}]")


def _print_src(session, *extras) -> None:
    """`print src <name>` — full record for one source package."""
    if not extras:
        tui.console.print("Usage: print src <name>")
        return
    if not _require_dep_check(session):
        return
    name = extras[0]
    srcs = session.dep_tree.selected_srcs
    if name not in srcs:
        tui.console.print(f"Source {name!r} is not in the current dep tree.")
        tui.console.print("  (`print sources` lists everything that is.)")
        return
    s = srcs[name]
    _mirror = getattr(s, '_mirror', None)
    _mirror_id = getattr(_mirror, 'id', '?') if _mirror else '?'

    tui.console.print(f"Source: {s.package}")
    tui.console.print(f"  Version       : {s.version}")
    tui.console.print(f"  Mirror        : {_mirror_id}")
    if getattr(s, 'directory', ''):
        tui.console.print(f"  Directory     : {s.directory}")
    if getattr(s, 'binary', None):
        tui.console.print(f"  Binary pkgs   : {len(s.binary)}")
        for b in sorted(s.binary):
            tui.console.print(f"      {b}")
    if getattr(s, 'files', None):
        tui.console.print("  Files (.dsc / .orig.tar.* / .debian.tar.*):")
        for fname, meta in sorted(s.files.items()):
            _size = meta.get('size', '?')
            tui.console.print(f"      {fname}  ({_size} B)")
    # Predicted binary filenames — split per-tree so the operator can
    # see which cohort drives each binary.  Source objects no longer
    # carry a .pkgs attribute; the per-tree maps live on
    # DependencyTree.src_pkg_files.
    _deb_files = sorted(session.dep_tree.src_pkg_files.get(name) or [])
    _udeb_files = []
    if getattr(session, 'udeb_dep_tree', None) is not None:
        _udeb_files = sorted(
            session.udeb_dep_tree.src_pkg_files.get(name) or [])
    if _deb_files:
        tui.console.print(f"  Produced .debs ({len(_deb_files)}):")
        for d in _deb_files:
            tui.console.print(f"      {d}")
    if _udeb_files:
        tui.console.print(f"  Produced .udebs ({len(_udeb_files)}):")
        for d in _udeb_files:
            tui.console.print(f"      {d}")


# ─── Relations / debugging views ────────────────────────────────────────────

def _print_provides(session, *_extras) -> None:
    """Virtual-package map — only entries with multiple providers, since the
    full Provides graph is enormous and most entries have a single provider."""
    if not _require_dep_check(session):
        return
    virtuals = defaultdict(list)
    for name, pkg in session.dep_tree.selected_pkgs.items():
        canonical = pkg['Package']
        if name != canonical:
            virtuals[name].append((canonical, pkg.version))
    multi = {v: ps for v, ps in virtuals.items() if len(ps) > 1}
    tui.console.print(f"Virtual packages with multiple providers ({len(multi)}):")
    if not multi:
        tui.console.print("  (every virtual name has a single provider — no contention)")
        return
    for v in sorted(multi):
        tui.console.print(f"  {v}")
        for cn, ver in sorted(multi[v]):
            tui.console.print(f"      → {cn} ({ver})")


# ─── Repo / signing views ──────────────────────────────────────────────────

def _print_signing(session, *_extras) -> None:
    """Show the project signing key's identity + on-disk state.

    No active sign+verify roundtrip — that's what the
    `verify_signing_key` command does (it actually exercises gpg).
    This view just inspects what's there: configured UID, gnupg
    homedir, and (if a key exists) its fingerprint, primary uid,
    creation/expiration timestamps, and the path of the exported
    public keyring.
    """
    import signing
    cfg = session.config
    tui.console.print("Signing key:")
    tui.console.print(f"  UID configured : {cfg.signing_key_uid}")
    tui.console.print(f"  Home dir       : {signing.signing_home(cfg)}")
    info = signing.get_key_info(cfg)
    if info is None:
        tui.console.print(
            "  Status         : NOT generated  "
            "(run `generate_signing_key` to create)"
        )
        return
    tui.console.print(f"  Fingerprint    : {info['fingerprint']}")
    tui.console.print(f"  UID present    : {info['uid']}")
    tui.console.print(f"  Created        : {signing.format_gpg_time(info['created'])}")
    tui.console.print(
        f"  Expires        : "
        f"{signing.format_gpg_time(info['expires'], '(never — manual rotation)')}"
    )
    tui.console.print(f"  Public key     : {signing.signing_pubkey_path(cfg)}")


# ─── Help screen ────────────────────────────────────────────────────────────

def _print_help(_session=None, *_extras) -> None:
    """Show the available `print` categories grouped by intent.

    Iterates the CATEGORIES dict so any newly-registered handler appears
    automatically with no help-text edit needed.
    """
    by_group = defaultdict(list)
    for name, (_handler, group, desc) in CATEGORIES.items():
        by_group[group].append((name, desc))

    tui.console.print("print — display build state")
    tui.console.print("Usage: print <category> [args]")
    tui.console.print("")
    _max = max(len(name) for name in CATEGORIES) + 8  # room for ' <name>' suffix
    for group in _HELP_GROUP_ORDER:
        if group not in by_group:
            continue
        tui.console.print(f"{group}:")
        for name, desc in sorted(by_group[group]):
            # Categories whose description starts with 'usage:' carry an arg
            # in their displayed name so the user sees the call shape.
            tui.console.print(f"  {name:<{_max}}  {desc}")
        tui.console.print("")


# ─── Dispatch table ─────────────────────────────────────────────────────────
# Adding a category: write _print_<name>(session, *extras), append a row.
# (handler, group label for help screen, one-line description)

CATEGORIES: 'dict[str, tuple[Callable[..., None], str, str]]' = {
    # Configuration
    'config':    (_print_config,    'Configuration', 'overall build configuration values'),
    'mirrors':   (_print_mirrors,   'Configuration', 'configured mirrors with URL, suite, component'),
    'snapshot':  (_print_snapshot,  'Configuration', 'snapshot pin status (configured + resolved date)'),
    'paths':     (_print_paths,     'Configuration', 'every directory the build uses'),

    # Build state
    'state':     (_print_state,     'Build state',   'pipeline stage progress (which stages are done)'),
    'stats':     (_print_stats,     'Build state',   'high-level counts across cache, dep tree, sources'),
    'summary':   (_print_summary,   'Build state',   'full pipeline summary (counts + chroot + ISO target)'),
    'build-times': (_print_build_times, 'Build state', 'per-source wall-clock from build records (OBS-01)'),

    # Packages
    'required':  (_print_required,  'Packages',      "packages with 'required' priority from APT cache"),
    'important': (_print_important, 'Packages',      "packages with 'important' priority from APT cache"),
    'selected':  (_print_selected,  'Packages',      'packages resolved by parse_dependency'),
    'tunneled':  (_print_tunneled,  'Packages',      'packages set to use prebuilt .debs (Tunneled list)'),
    'extras':    (_print_extras,    'Packages',      'depth-1 Recommends pulled into the repo (not chroot-installed)'),
    'live':      (_print_live_exclusive,      'Packages', 'packages live needs over and above pkg.list'),
    'installer': (_print_installer_exclusive, 'Packages', 'deb-arm of installer.list (efibootmgr/grub-pc-bin etc.)'),
    'udebs':     (_print_udebs,                'Packages', 'udeb closure for installer ramdisk'),
    'groups':    (_print_groups,               'Packages', 'pkg.list groups + per-group canonical packages'),
    'pkg':       (_print_pkg,       'Packages',      'full package detail — usage: print pkg <name>'),
    'deps':      (_print_deps,      'Packages',      'flat dep list of a package — usage: print deps <name>'),

    # Sources
    'sources':   (_print_sources,   'Sources',       'all selected source packages with mirror + version'),
    'src':       (_print_src,       'Sources',       'full source detail — usage: print src <name>'),

    # Relations
    'provides':  (_print_provides,  'Relations',     'virtual packages with multiple providers'),

    # Repo
    'signing':   (_print_signing,   'Repo',          'project signing key — identity + on-disk state (CONF-02)'),

    # Meta
    'help':      (_print_help,      'Meta',          'this help'),
}

# Order in which sections appear in `print help`.
_HELP_GROUP_ORDER = [
    'Configuration',
    'Build state',
    'Packages',
    'Sources',
    'Relations',
    'Repo',
    'Meta',
]


def dispatch(session: 'BuildSession', category: str, *extras) -> None:
    """Route to a category handler.  Empty / unknown categories show help.

    Extras (additional args after the category) are forwarded to the handler
    for parametrized views like `print pkg <name>`.
    """
    if not category:
        _print_help(session)
        return
    entry = CATEGORIES.get(category)
    if entry is None:
        tui.console.print(f"Unknown print category: {category!r}")
        tui.console.print("Try: print help")
        return
    handler, _group, _desc = entry
    handler(session, *extras)
