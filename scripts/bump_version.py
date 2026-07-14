#!/usr/bin/env python3
"""Deliberate SemVer release bump for the Athena-Build TOOLCHAIN.

Per-push dev versions are automatic (git describe appends `-N-g<sha>` after the
latest tag — see scripts/_version.py); you do NOT bump for every commit.  At a
RELEASE you run this once to advance the semantic version:

    python3 scripts/bump_version.py {major|minor|patch}
    python3 scripts/bump_version.py 0.2.0          # or an explicit version

It rewrites the version in lockstep across its two homes — pyproject.toml
[project].version and scripts/_version.py `_BASE_VERSION` — rolls
CHANGELOG.md's [Unreleased] section into a dated [X.Y.Z] section (leaving a
fresh empty [Unreleased] on top; skipped with a warning when there is nothing
to roll — the v0.1.3 release shipped without a changelog section because this
step used to be manual), commits it all as a `release: vX.Y.Z` commit, and
creates a SIGNED annotated tag `vX.Y.Z`.  The tag
is the immutable anchor: never move or delete a release tag (same rule as a
published .deb filename).

Flags:
    --dry-run       show what would change; touch nothing
    --no-commit     rewrite the two files but don't commit (implies --no-tag)
    --no-tag        commit but don't create the tag
    --no-sign       annotated but UNsigned tag (default is `git tag -s`)
    --allow-dirty   permit a dirty tree (default: refuse, so the release commit
                    is exactly the version bump)
    --no-changelog  skip the CHANGELOG.md [Unreleased] -> [X.Y.Z] roll
    --yes           skip the interactive confirmation (CI / scripted use)
    --freeze-stamp  also (re)write scripts/_buildstamp.py from the new state —
                    the frozen fallback for exported/packaged trees with no .git

SemVer pre-1.0 reminder: in 0.x ANYTHING may change; bump MINOR for features,
PATCH for fixes, and cut 1.0.0 only when the config/command/record formats are
a stable promise.
"""

import argparse
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_ROOT, 'pyproject.toml')
_VERSION_PY = os.path.join(_ROOT, 'scripts', '_version.py')
_BUILDSTAMP_PY = os.path.join(_ROOT, 'scripts', '_buildstamp.py')
_CHANGELOG = os.path.join(_ROOT, 'CHANGELOG.md')

_SEMVER = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')


def _git(*args: str) -> str:
    _r = subprocess.run(['git', '-C', _ROOT, *args],
                        capture_output=True, text=True)
    if _r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {_r.stderr.strip()}")
    return _r.stdout.strip()


def _git_ok(*args: str) -> bool:
    return subprocess.run(['git', '-C', _ROOT, *args],
                          capture_output=True, text=True).returncode == 0


def _current_version() -> str:
    """Current base: the latest v[0-9]* tag if any, else pyproject's value."""
    _desc = subprocess.run(
        ['git', '-C', _ROOT, 'describe', '--tags', '--abbrev=0',
         '--match', 'v[0-9]*'],
        capture_output=True, text=True)
    if _desc.returncode == 0 and _desc.stdout.strip():
        _t = _desc.stdout.strip()
        return _t[1:] if _t.startswith('v') else _t
    return _read_pyproject_version()


def _read_pyproject_version() -> str:
    with open(_PYPROJECT) as _fh:
        _m = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', _fh.read())
    if not _m:
        raise SystemExit("could not find [project].version in pyproject.toml")
    return _m.group(1)


def _next_version(current: str, bump: str) -> str:
    _m = _SEMVER.match(current)
    if not _m:
        raise SystemExit(
            f"current version {current!r} is not plain SemVer X.Y.Z — "
            "fix it before bumping")
    _major, _minor, _patch = (int(_g) for _g in _m.groups())
    if bump == 'major':
        return f"{_major + 1}.0.0"
    if bump == 'minor':
        return f"{_major}.{_minor + 1}.0"
    if bump == 'patch':
        return f"{_major}.{_minor}.{_patch + 1}"
    # explicit version
    if not _SEMVER.match(bump):
        raise SystemExit(
            f"bump arg {bump!r} must be major|minor|patch or an X.Y.Z version")
    return bump


def _rewrite(path: str, pattern: str, replacement: str, dry_run: bool) -> None:
    with open(path) as _fh:
        _old = _fh.read()
    _new, _n = re.subn(pattern, replacement, _old, count=1)
    if _n != 1:
        raise SystemExit(f"expected exactly one match for {pattern!r} in {path}")
    print(f"  {'(dry-run) ' if dry_run else ''}update {os.path.relpath(path, _ROOT)}")
    if not dry_run:
        with open(path, 'w') as _fh:
            _fh.write(_new)


_UNRELEASED_RE = re.compile(r'(?ms)^## \[Unreleased\]\s*?\n(.*?)(?=^## \[|\Z)')


def _unreleased_body(text: str) -> str:
    """The [Unreleased] section's entry text ('' when absent/empty) —
    what the next release ships; shown at confirmation and rolled at
    release."""
    _m = _UNRELEASED_RE.search(text)
    return _m.group(1).strip() if _m else ''


def _rolled_changelog_text(text: str, version: str,
                           date: str) -> 'str | None':
    """Pure: CHANGELOG text with the [Unreleased] section rolled into a
    dated [version] section, a fresh empty [Unreleased] left on top.
    None when there is nothing to roll (no section, or no entries)."""
    _m = _UNRELEASED_RE.search(text)
    if not _m or not _m.group(1).strip():
        return None
    return (text[:_m.start()]
            + '## [Unreleased]\n\n'
            + f'## [{version}] — {date}\n\n'
            + _m.group(1).lstrip('\n')
            + text[_m.end():])


def _roll_changelog(version: str, dry_run: bool) -> bool:
    """Roll CHANGELOG.md's [Unreleased] into [version]; True when rolled
    (the file joins the release commit set)."""
    import datetime
    try:
        with open(_CHANGELOG) as _fh:
            _text = _fh.read()
    except OSError:
        print("  CHANGELOG.md unreadable — roll skipped")
        return False
    _new = _rolled_changelog_text(
        _text, version, datetime.date.today().isoformat())
    if _new is None:
        print("  CHANGELOG.md: [Unreleased] empty/absent — nothing to roll "
              "(capability entries missing for this release?)")
        return False
    print(f"  {'(dry-run) ' if dry_run else ''}roll CHANGELOG.md "
          f"[Unreleased] -> [{version}]")
    if not dry_run:
        with open(_CHANGELOG, 'w') as _fh:
            _fh.write(_new)
    return True


def _write_buildstamp(version: str, dry_run: bool) -> None:
    _commit = _git('rev-parse', '--short', 'HEAD')
    _date = _git('show', '-s', '--format=%cs', 'HEAD')   # YYYY-MM-DD, no clock
    _body = (
        '"""Generated by scripts/bump_version.py --freeze-stamp.  DO NOT EDIT.\n'
        'Frozen toolchain version for exported/packaged trees with no .git;\n'
        '_version.py reads this first.  gitignored — never commit a stale stamp."""\n'
        f'__version__ = "{version}"\n'
        f'__commit__ = "{_commit}"\n'
        f'__date__ = "{_date}"\n'
    )
    print(f"  {'(dry-run) ' if dry_run else ''}write "
          f"{os.path.relpath(_BUILDSTAMP_PY, _ROOT)}")
    if not dry_run:
        with open(_BUILDSTAMP_PY, 'w') as _fh:
            _fh.write(_body)


def main(argv: 'list[str] | None' = None) -> int:
    _p = argparse.ArgumentParser(
        prog='bump_version.py',
        description='Deliberate SemVer release bump for Athena-Build.')
    _p.add_argument('bump', help='major | minor | patch | <X.Y.Z>')
    _p.add_argument('--dry-run', action='store_true')
    _p.add_argument('--no-commit', action='store_true')
    _p.add_argument('--no-tag', action='store_true')
    _p.add_argument('--no-sign', action='store_true')
    _p.add_argument('--allow-dirty', action='store_true')
    _p.add_argument('--no-changelog', action='store_true')
    _p.add_argument('--yes', action='store_true')
    _p.add_argument('--freeze-stamp', action='store_true')
    _a = _p.parse_args(argv)

    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        raise SystemExit("not a git checkout — bump_version needs git")

    _current = _current_version()
    _next = _next_version(_current, _a.bump)
    _tag = f"v{_next}"
    print(f"bump: {_current} -> {_next}  (tag {_tag})")

    if _next == _current:
        raise SystemExit("next version equals current — nothing to do")
    if _git_ok('rev-parse', '-q', '--verify', f'refs/tags/{_tag}'):
        raise SystemExit(
            f"tag {_tag} already exists — release tags are immutable, "
            "never re-mint one")
    _dirty = bool(_git('status', '--porcelain'))
    if _dirty and not _a.allow_dirty and not _a.dry_run:
        raise SystemExit(
            "working tree is dirty — commit/stash first so the release commit "
            "is exactly the version bump (or pass --allow-dirty)")

    # UX: show what this release SHIPS (commits since the last tag + the
    # CHANGELOG [Unreleased] entries) and require explicit confirmation
    # BEFORE anything is touched — an aborted bump changes nothing.
    if _git_ok('rev-parse', '-q', '--verify', f'refs/tags/v{_current}'):
        _n_commits = _git('rev-list', '--count', f'v{_current}..HEAD')
        print(f"\nsince v{_current}: {_n_commits} commit(s)")
    try:
        with open(_CHANGELOG) as _fh:
            _entries = _unreleased_body(_fh.read())
    except OSError:
        _entries = ''
    if _entries:
        print("shipping (CHANGELOG [Unreleased]):")
        for _line in _entries.splitlines():
            print(f"  {_line}")
    else:
        print("WARNING: CHANGELOG [Unreleased] is empty — this release will "
              "carry no changelog section")
    if not _a.dry_run and not _a.yes:
        try:
            _resp = input(f"\nProceed with release {_tag}? [y/N] ")
        except EOFError:
            raise SystemExit(
                "no interactive input available — pass --yes for scripted "
                "use; aborted, nothing changed") from None
        if _resp.strip().lower() not in ('y', 'yes'):
            raise SystemExit("aborted — nothing changed")

    # 1. rewrite the two lockstep homes
    _rewrite(_PYPROJECT,
             r'(?m)^(version\s*=\s*")[^"]+(")',
             rf'\g<1>{_next}\g<2>', _a.dry_run)
    _rewrite(_VERSION_PY,
             r'(_BASE_VERSION\s*=\s*")[^"]+(")',
             rf'\g<1>{_next}\g<2>', _a.dry_run)
    if _a.freeze_stamp:
        _write_buildstamp(_next, _a.dry_run)
    _rolled = False if _a.no_changelog else _roll_changelog(_next, _a.dry_run)

    if _a.no_commit:
        print("--no-commit: files rewritten, not committed (no tag).")
        return 0
    if _a.dry_run:
        print("(dry-run) would: git commit + "
              f"{'no tag' if _a.no_tag else 'git tag ' + _tag}")
        return 0

    # 2. commit the bump.  _buildstamp.py is intentionally gitignored
    #    (.gitignore — never commit a stale stamp) and _write_buildstamp
    #    already wrote it to disk above, so it must NOT be in the commit set:
    #    `git add` on a gitignored path exits 1, which under --freeze-stamp
    #    aborted the release mid-way (pyproject + _version.py rewritten, stamp
    #    written, but NO commit and NO tag).
    _files = [_PYPROJECT, _VERSION_PY]
    if _rolled:
        _files.append(_CHANGELOG)
    _git('add', *_files)
    _git('commit', '-m', f'release: {_tag}')
    print(f"committed release: {_tag}")

    # 3. immutable signed annotated tag
    if not _a.no_tag:
        _tag_args = ['tag', '-a', _tag, '-m', f'Athena-Build {_tag}']
        if not _a.no_sign:
            _tag_args.insert(1, '-s')
        _git(*_tag_args)
        print(f"created {'signed ' if not _a.no_sign else ''}tag {_tag}")
        print(f"\nnext: git push && git push origin {_tag}")
    else:
        print("--no-tag: committed without tagging.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
