"""Single source of truth for the Athena-Build TOOLCHAIN version.

This versions the build SYSTEM itself — distinct from the DISTRIBUTION version
(`[Build] VERSION` -> `config.build_version`, the Asgard/thor release stamped
into os-release and the ISO).  Athena-Build vX.Y.Z builds Asgard
<build_version>; the two move independently.

`get_version()` resolves in order — a pure function of the deployment state, so
two different trees can never claim the same exact version:

  1. _buildstamp.py     frozen at release/package time (works without .git)
  2. git describe        working tree -> 0.1.0-14-g58c3661[-dirty]
                         (only v[0-9]* tags — ignores the descriptive savepoint
                         tags like working-branding-* that also live in the repo)
  3. importlib.metadata  pip-installed dist "athena-build"
  4. <base>+unknown      last resort

`base_version()` returns just the SemVer base (no git suffix) for long-lived
identifiers like the HTTP User-Agent that must NOT churn per-commit.  It mirrors
pyproject.toml [project].version; `scripts/bump_version.py` keeps the two in
lockstep and `test_version_base_matches_pyproject` guards against drift.
"""

import os
import subprocess
import sys

# Mirrors pyproject.toml [project].version.  Bumped ONLY by
# scripts/bump_version.py (which rewrites pyproject in the same commit);
# test_version_base_matches_pyproject fails the suite if the two drift.
_BASE_VERSION = "0.1.1"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def base_version() -> str:
    """The plain SemVer base (e.g. '0.1.0'), stable across commits.  Use for
    long-lived identifiers (User-Agent) that should not change per-commit."""
    return _BASE_VERSION


def _git(*args: str) -> 'str | None':
    """Run a git command at the repo root; return stripped stdout or None.

    Returns None when there is no `.git` (a packaged/exported tree), git is
    missing, or the command fails — every caller treats None as "git can't
    answer" and falls through to the next source.
    """
    if not os.path.isdir(os.path.join(_REPO_ROOT, '.git')):
        return None
    try:
        _r = subprocess.run(
            ['git', '-C', _REPO_ROOT, *args],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    # Defensive type checks: a test that broadly mocks subprocess.run returns a
    # MagicMock whose .returncode/.stdout are also MagicMocks.  Without these
    # guards a MagicMock would flow into get_version() (and thence a build
    # record / Release file, breaking JSON serialisation).  Only a real
    # success with real text output is trusted; anything else falls through.
    if not isinstance(_r.returncode, int) or _r.returncode != 0:
        return None
    if not isinstance(_r.stdout, str):
        return None
    return _r.stdout.strip() or None


def _from_buildstamp() -> 'str | None':
    try:
        import _buildstamp            # generated at release; absent in a checkout
        return getattr(_buildstamp, '__version__', None)
    except Exception:                 # noqa: BLE001 — any import failure = absent
        return None


def _from_git() -> 'str | None':
    # Only consider real version tags (v1.2.3).  The repo also carries
    # descriptive savepoint tags (working-branding-*, pre-audit-split-*) that a
    # bare `git describe` would otherwise latch onto and emit as the "version".
    _desc = _git('describe', '--tags', '--dirty', '--match', 'v[0-9]*')
    if _desc:
        return _desc[1:] if _desc.startswith('v') else _desc
    # No version tag yet (the current state): compose <base>+g<short-sha>[-dirty]
    # so the commit still pins the build exactly.
    _sha = _git('rev-parse', '--short', 'HEAD')
    if not _sha:
        return None
    _dirty = '-dirty' if _git('status', '--porcelain') else ''
    return f"{_BASE_VERSION}+g{_sha}{_dirty}"


def _from_metadata() -> 'str | None':
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version('athena-build')
        except PackageNotFoundError:
            return None
    except Exception:                 # noqa: BLE001
        return None


_CACHED_VERSION: 'str | None' = None


def get_version() -> str:
    """Full provenance-exact version string (carries the git suffix in a tree).

    Cached after the first resolution: the version is a property of the running
    CODE, which doesn't change mid-process, so provenance stampers (build
    records, ISO, repo) can call this freely without repeated git subprocesses.
    The dirty flag is sampled once at first call — that's intentional, the code
    didn't change just because the build wrote output files.
    """
    global _CACHED_VERSION
    if _CACHED_VERSION is not None:
        return _CACHED_VERSION
    for _fn in (_from_buildstamp, _from_git, _from_metadata):
        _v = _fn()
        if _v:
            _CACHED_VERSION = _v
            return _v
    _CACHED_VERSION = f"{_BASE_VERSION}+unknown"
    return _CACHED_VERSION


def get_commit() -> 'str | None':
    """Short commit SHA from the frozen stamp or git; None if neither answers."""
    try:
        import _buildstamp
        _c = getattr(_buildstamp, '__commit__', None)
        if _c:
            return _c
    except Exception:                 # noqa: BLE001
        pass
    return _git('rev-parse', '--short', 'HEAD')


def version_line(verbose: bool = False) -> str:
    """Human-facing version string — one line, or multi-line when verbose
    (adds python, commit, and the frozen build date when present)."""
    _line = f"athena-build {get_version()}"
    if not verbose:
        return _line
    _py = (f"{sys.version_info.major}.{sys.version_info.minor}"
           f".{sys.version_info.micro}")
    _parts = [_line, f"  python   {_py}"]
    _commit = get_commit()
    if _commit:
        _parts.append(f"  commit   {_commit}")
    try:
        import _buildstamp
        _date = getattr(_buildstamp, '__date__', None)
        if _date:
            _parts.append(f"  built    {_date}")
    except Exception:                 # noqa: BLE001
        pass
    return "\n".join(_parts)
