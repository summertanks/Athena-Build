"""apt-repo metadata generators — Packages, Sources, Release, InRelease.

CONF-01 Stage A (2026-05-22): lifted verbatim from iso_installer.py.
See docs/plans/conf-01-repo-layout-migration.md for the full migration
plan.

Stage A scope:
  - pure code motion (no behavioural change)
  - iso_installer.py imports from here instead of having locals
  - exposed surface (called from iso_installer.py):
        generate_apt_repo
        sign_release_files
        export_pubkey_to_staging
  - module-private helpers (underscore prefix):
        _scan_packages_to, _scan_sources_to, _compress_index,
        _count_records, _write_subdir_release, _generate_top_release,
        _sudo

Later stages:
  - Stage B: add generate_repo_indexes(repo_root, suites_spec, …)
    multi-suite orchestrator that supersedes the single-suite
    generate_apt_repo (which stays as the iso-staging helper).
  - Stage E: helpers that PARSE indexes (e.g. iter_packages_records())
    so audits can read the Packages file instead of walking the fs.
"""

import logging
import os
import shutil
import subprocess
from typing import Optional

import tui

logger = logging.getLogger('athena')


def _sudo(cmd_args, password: str) -> subprocess.CompletedProcess:
    """sudo -S <cmd> with cached password.  Local copy (vs importing
    from iso_installer) to avoid a cross-module import cycle once
    iso_installer starts pulling helpers FROM here."""
    return subprocess.run(
        ['sudo', '-S'] + cmd_args,
        input=password + '\n',
        capture_output=True, text=True,
    )


def deb_excluded_from_minimal(filename: str) -> bool:
    """True if a .deb should be left OUT of the `minimal` (runtime) repo.

    Minimal = what a booted system apt-installs.  Exclude packages a
    running system never needs: debug symbols (`*-dbg`, `*-dbgsym`) and
    source-shipping debs (`*-source`, e.g. linux-source, gcc-12-source).
    These are also where every >100 MB file lives, so dropping them keeps
    the published tree under GitHub's 100 MB-per-file hard push limit.

    Decided by the package name (the `_`-delimited head of the filename),
    so `linux-image-6.1.0-47-amd64-dbg_..._amd64.deb` is excluded but a
    hypothetical `libfoo-dev_..._amd64.deb` is not.

    Source-shipping debs come in two name shapes: `<pkg>-source`
    (gcc-12-source, bluez-source) and the kernel's `linux-source-<ver>`
    (the version is baked into the package name, so the `-source` suffix
    test misses it).  We handle the kernel form by exact prefix rather
    than an `-source-` substring, which would wrongly catch legitimate
    runtime packages like `fonts-source-code-pro`.
    """
    _name = filename.split('_', 1)[0]
    if _name.endswith(('-dbg', '-dbgsym', '-source')):
        return True
    if _name.startswith('linux-source-'):
        return True
    return False


def generate_apt_repo(
    staging: str, suite: str, codename: str, version: str, password: str,
) -> bool:
    """Generate apt-repo metadata under staging/dists/<suite>/.

    Layout produced (Debian apt-repo convention):
      dists/<suite>/Release                                        (top-level)
      dists/<suite>/main/binary-amd64/{Release,Packages,Packages.gz,Packages.xz}
      dists/<suite>/main/debian-installer/binary-amd64/{Release,Packages,Packages.gz,Packages.xz}
      dists/<suite>/main/source/{Release,Sources,Sources.gz,Sources.xz}

    Pool layout stays FLAT — apt reads Filename: from each Packages
    record, which points into pool/ relative to the apt root.  No need
    to restructure into pool/<comp>/<initial>/<src>/.

    The Release file is unsigned at the end of this function — signing
    happens in sign_release_files, invoked from build_installer_iso
    after the apt-repo is laid out.  Splitting the steps keeps each
    helper focused on one job and makes the failure modes distinct
    (apt-ftparchive failure vs gpg failure).

    Tools used:
      dpkg-scanpackages  — scans pool/ for .debs / .udebs, emits Packages
      dpkg-scansources   — scans pool/ for .dsc, emits Sources
      apt-ftparchive release — generates the top-level Release with
                               SHA256 hashes of every Packages/Sources

    All three are standard Debian utilities in dpkg-dev + apt-utils.
    """
    _COMPONENT = 'main'
    _ARCH = 'amd64'

    _suite_base   = os.path.join(staging, 'dists', suite)
    _comp_base    = os.path.join(_suite_base, _COMPONENT)
    _binary_dir   = os.path.join(_comp_base, f'binary-{_ARCH}')
    _udeb_dir     = os.path.join(_comp_base, 'debian-installer', f'binary-{_ARCH}')
    _source_dir   = os.path.join(_comp_base, 'source')

    tui.console.print(f"Generating apt-repo metadata in dists/{suite}/...")

    # Step 1: directory hierarchy.  Made user-owned (no sudo) so the
    # subsequent sudo-driven dpkg-scanpackages output can write into them
    # — the parent staging tree is user-owned too.  Files inside will
    # become root-owned via the sudo invocation, which is fine.
    for _d in (_binary_dir, _udeb_dir, _source_dir):
        try:
            os.makedirs(_d, exist_ok=True)
        except OSError as e:
            tui.console.print(f"ERROR: mkdir {_d}: {e}")
            logger.error(f"generate_apt_repo mkdir {_d}: {e}")
            return False

    # Step 2: regular .deb Packages index.  -m tolerates multiple
    # versions of the same package without warning (we have multiple
    # kernel ABIs etc. in repo/).  Run from staging/ so Filename:
    # entries are relative paths like `pool/foo.deb`.
    if not _scan_packages_to(
            staging, 'pool', os.path.join(_binary_dir, 'Packages'),
            password, udeb=False):
        return False

    # Step 3: udeb Packages index (Section: debian-installer records).
    # dpkg-scanpackages -t udeb filters to .udeb files only.  allow_empty:
    # a debs-only pool (the `repo index minimal` runtime subset) has no
    # udebs, and an empty debian-installer component is valid — only the
    # main deb index (Step 2) must be non-empty.
    if not _scan_packages_to(
            staging, 'pool', os.path.join(_udeb_dir, 'Packages'),
            password, udeb=True, allow_empty=True):
        return False

    # Step 4: source Sources index.  dpkg-scansources walks pool/ for
    # .dsc files (which our source-build pipeline lands alongside .debs).
    if not _scan_sources_to(
            staging, 'pool', os.path.join(_source_dir, 'Sources'),
            password):
        return False

    # Step 5: per-component Release files.  These pin Suite/Codename/
    # Component/Architecture on each binary-arch / source dir so apt can
    # verify they match what the top-level Release advertises.
    for _dir, _arch_label in (
        (_binary_dir, _ARCH),
        (_udeb_dir,   _ARCH),
        (_source_dir, 'source'),
    ):
        if not _write_subdir_release(
                _dir, suite, codename, _COMPONENT, _arch_label, password):
            return False

    # Step 6: top-level dists/<suite>/Release.  apt-ftparchive release
    # walks the sub-tree, hashes every Packages/Sources/Packages.gz/...,
    # and emits the SHA256: block apt verifies.  -o flags carry the
    # distro identity fields; without them apt-ftparchive emits empty
    # Suite/Codename and apt refuses the repo.
    if not _generate_top_release(
            staging, suite, codename, version,
            os.path.join(_suite_base, 'Release'), password):
        return False

    tui.console.print(
        f"apt-repo: dists/{suite}/ ready (unsigned — sign_release_files "
        "will sign next)",
        tui.COLOR_HIGHLIGHT,
    )
    return True


def generate_repo_indexes(
    repo_root: str,
    suites_spec: 'dict[str, list[str]]',
    codename_for_suite: 'dict[str, str]',
    version: str,
    arch: str,
    password: str,
    signing_homedir: Optional[str] = None,
    signing_pubkey_path: Optional[str] = None,
    description_for_suite: Optional['dict[str, str]'] = None,
) -> bool:
    """Multi-suite apt-repo index generator — operates IN-PLACE on
    repo_root/dists/, assuming the unified layout from CONF-01.

    Distinct from generate_apt_repo (which is the single-suite ISO-
    staging helper).  This one is what cmd_index_repo invokes against
    repo/.

    Args:
      repo_root:           The apt root.  Indexes land under
                           <repo_root>/dists/<suite>/<comp>/...
                           Filename: records in produced Packages point
                           at paths relative to repo_root (e.g.
                           "dists/thor/main/binary-amd64/foo.deb").
      suites_spec:         {suite_name: [component_name, ...]}.  Each
                           (suite, component) pair must already have a
                           directory at <repo_root>/dists/<suite>/<comp>/
                           binary-<arch>/ — Stage C of CONF-01 creates
                           these as part of the layout migration.  This
                           helper FAILS clean if the directory is
                           missing (signals "Stage C hasn't run yet").
      codename_for_suite:  {suite_name: codename}.  The codename to
                           write into per-subdir Release files.  Usually
                           the codename IS the suite name (thor/thor),
                           but Debian's convention for debug suites is
                           Suite=bookworm-debug Codename=bookworm-debug,
                           so we accept different values per suite.
      version:             apt version string for the top-level Release.
      arch:                'amd64' (or future cross-arch values).
      password:            Cached sudo password — needed for dpkg-
                           scanpackages writes to root-owned dirs.
      signing_homedir:     Optional GPG homedir.  When provided,
                           Release.gpg + InRelease are generated per
                           suite via sign_release_files.
      signing_pubkey_path: Unused here (the pubkey isn't shipped to
                           repo_root the way the ISO ships it to
                           .disk/archive-key.gpg — operators consume
                           via their own keyring management).  Accepted
                           for symmetry with generate_apt_repo.

    Per (suite, component) pair, generates:
      <comp>/binary-<arch>/{Packages, Packages.gz, Packages.xz, Release}
    If <comp>/debian-installer/binary-<arch>/ exists (udeb subdir):
      <comp>/debian-installer/binary-<arch>/{Packages, .gz, .xz, Release}
    If <comp>/source/ exists (source subdir):
      <comp>/source/{Sources, Sources.gz, Sources.xz, Release}

    Per suite:
      dists/<suite>/Release      via apt-ftparchive release
      dists/<suite>/Release.gpg  if signing_homedir given (detached sig)
      dists/<suite>/InRelease    if signing_homedir given (clearsigned)

    Returns True iff every suite's indexes generated cleanly.
    """
    del signing_pubkey_path   # accepted for symmetry; unused here
    if description_for_suite is None:
        description_for_suite = {}

    for _suite, _components in suites_spec.items():
        _codename = codename_for_suite.get(_suite, _suite)
        _desc = description_for_suite.get(_suite, f'Athena repo — {_suite}')

        tui.console.print(
            f"Indexing suite {_suite} (codename={_codename}, "
            f"components={_components})..."
        )

        # Track which components actually had content — only these
        # get listed in the suite's top-level Release Components: field
        # (apt rejects the repo if Components: lists a component with
        # no corresponding indexes).  Components with missing or empty
        # binary-<arch>/ dirs are SKIPPED with INFO — not an error.
        # Legitimate cases: -debug suite when no dbgsyms were built
        # (nodoc/nostrip profiles often skip dbgsyms entirely); doc
        # component when no -doc packages exist yet; etc.
        _populated_components: 'list[str]' = []

        for _comp in _components:
            _binary_rel = f'dists/{_suite}/{_comp}/binary-{arch}'
            _binary_abs = os.path.join(repo_root, _binary_rel)

            # Missing directory → no content for this (suite, component)
            # pair — skip cleanly with INFO.  Don't fail the whole
            # operation just because one component is empty.
            if not os.path.isdir(_binary_abs):
                tui.console.print(
                    f"  → {_suite}/{_comp}: no binary-{arch}/ dir, "
                    f"skipping (no content)"
                )
                continue

            # Empty directory → same handling.  An empty Packages
            # index is technically valid but it's clearer to just
            # skip the component than to ship an empty one.
            _deb_count = sum(
                1 for _f in os.listdir(_binary_abs)
                if _f.endswith('.deb')
            )
            if _deb_count == 0:
                tui.console.print(
                    f"  → {_suite}/{_comp}: no .debs in {_binary_rel}, "
                    f"skipping (empty)"
                )
                continue

            # Regular .deb Packages index.  cwd=repo_root so Filename:
            # records carry the full relative path (apt resolves them
            # from BASE-URL = repo_root).
            if not _scan_packages_to(
                    repo_root, _binary_rel,
                    os.path.join(_binary_abs, 'Packages'),
                    password, udeb=False):
                return False
            if not _write_subdir_release(
                    _binary_abs, _suite, _codename, _comp, arch, password):
                return False
            _populated_components.append(_comp)

            # Optional udeb subdir.  Only relevant for main of the
            # primary suite (thor); -debug suites and doc/tests
            # components don't ship udebs.
            _udeb_rel = f'dists/{_suite}/{_comp}/debian-installer/binary-{arch}'
            _udeb_abs = os.path.join(repo_root, _udeb_rel)
            if os.path.isdir(_udeb_abs):
                if not _scan_packages_to(
                        repo_root, _udeb_rel,
                        os.path.join(_udeb_abs, 'Packages'),
                        password, udeb=True):
                    return False
                if not _write_subdir_release(
                        _udeb_abs, _suite, _codename, _comp, arch, password):
                    return False

            # Optional source subdir.  Same scope as udebs — main of
            # the primary suite, not debug or side-component dirs.
            _source_rel = f'dists/{_suite}/{_comp}/source'
            _source_abs = os.path.join(repo_root, _source_rel)
            if os.path.isdir(_source_abs):
                if not _scan_sources_to(
                        repo_root, _source_rel,
                        os.path.join(_source_abs, 'Sources'),
                        password):
                    return False
                if not _write_subdir_release(
                        _source_abs, _suite, _codename, _comp, 'source',
                        password):
                    return False

        # Suite has zero populated components → skip the suite entirely.
        # Don't generate a top-level Release for an empty suite; apt
        # would refuse it for having Components: with no targets.
        if not _populated_components:
            tui.console.print(
                f"  → dists/{_suite}/: no populated components, "
                f"skipping suite entirely",
            )
            continue

        # Top-level dists/<suite>/Release.  apt-ftparchive walks the
        # sub-tree and hashes every Packages/Sources file.  Components:
        # field lists ONLY the components we actually populated above
        # (apt rejects the repo if Components: names a missing dir).
        _top_release = os.path.join(repo_root, 'dists', _suite, 'Release')
        if not _generate_top_release(
                repo_root, _suite, _codename, version,
                _top_release, password,
                components=_populated_components,
                description=_desc):
            return False

        # Sign the top-level Release per suite if a homedir was given.
        if signing_homedir is not None:
            if not sign_release_files(
                    repo_root, _suite, signing_homedir, password):
                return False

        tui.console.print(
            f"  → dists/{_suite}/ indexed (components: "
            f"{', '.join(_populated_components)})",
            tui.COLOR_HIGHLIGHT,
        )

    return True


def merge_packages_indexes(remote_text: str, local_text: str) -> str:
    """Union two Packages indexes into ONE multi-version index, deduped by
    (Package, Version).

    Local stanzas (this snapshot's freshly built pool) take precedence on a
    (Package, Version) collision; every remote-only version is PRESERVED
    (append-only — clients keep their rollback targets).  This is the UPD-01
    §5 step-8 core: the remote stays multi-version even though local repo/
    holds one version per package and references .debs the remote also has.
    Stanzas are re-emitted via apt_pkg, which is sufficient for a valid index
    (apt re-parses it; the signed Release hashes whatever bytes we write)."""
    import apt_pkg
    import tempfile as _tf
    _seen: 'set[tuple[str, str]]' = set()
    _stanzas: 'list[str]' = []
    for _text in (local_text, remote_text):       # local first → local wins
        if not _text or not _text.strip():
            continue
        with _tf.NamedTemporaryFile(
                'w', suffix='.Packages', delete=False) as _fh:
            _fh.write(_text if _text.endswith('\n') else _text + '\n')
            _path = _fh.name
        try:
            with open(_path) as _rf:
                for _sec in apt_pkg.TagFile(_rf):
                    _pkg = (_sec.get('Package') or '').strip()
                    _ver = (_sec.get('Version') or '').strip()
                    if not _pkg or not _ver or (_pkg, _ver) in _seen:
                        continue
                    _seen.add((_pkg, _ver))
                    _stanzas.append(str(_sec).rstrip('\n'))
        finally:
            os.remove(_path)
    return ('\n\n'.join(_stanzas) + '\n') if _stanzas else ''


def _read_text(path: str) -> str:
    try:
        with open(path) as _fh:
            return _fh.read()
    except OSError:
        return ''


def _write_text_sudo(path: str, text: str, password: str) -> bool:
    """Write `text` to `path` (which may be sudo-owned under repo/) via a
    user-owned temp file + `sudo cp`."""
    import tempfile as _tf
    with _tf.NamedTemporaryFile('w', delete=False) as _fh:
        _fh.write(text)
        _tmp = _fh.name
    try:
        _r = _sudo(['cp', _tmp, path], password)
    finally:
        os.remove(_tmp)
    if _r.returncode != 0:
        tui.console.print(f"ERROR: write {path}: {_r.stderr.strip()[:200]}")
        logger.error(f"_write_text_sudo {path}: rc={_r.returncode}")
        return False
    return True


def merge_remote_index(
    repo_root: str, suite: str, codename: str, arch: str, version: str,
    remote_packages_path: str, password: str,
    signing_homedir: Optional[str] = None,
) -> bool:
    """Build the suite index as the UNION of the local (single-snapshot) pool
    and the remote published Packages (the append-only archive of record),
    signed LOCALLY (the key never leaves this host).  UPD-01 §5 step 8.

    `remote_packages_path` is the Packages file fetched from the remote
    (repo_audit.fetch_remote_ledger caches it); '' / missing → local-only
    (first publish to an empty remote).  Only `main` is merged here — the
    binary pool clients `apt upgrade` from; the installer udeb pool is scanned
    local-only (it ships on the ISO, not via client apt)."""
    _comp = 'main'
    _binary_rel = f'dists/{suite}/{_comp}/binary-{arch}'
    _binary_abs = os.path.join(repo_root, _binary_rel)
    if not os.path.isdir(_binary_abs):
        tui.console.print(f"merge_remote_index: {_binary_rel} missing")
        return False
    _pkgs = os.path.join(_binary_abs, 'Packages')

    # 1. scan the local single-snapshot pool
    if not _scan_packages_to(repo_root, _binary_rel, _pkgs, password,
                             udeb=False):
        return False

    # 2. merge remote-only versions in (append-only multi-version)
    _remote_text = (_read_text(remote_packages_path)
                    if remote_packages_path and os.path.exists(
                        remote_packages_path) else '')
    if _remote_text.strip():
        _merged = merge_packages_indexes(_remote_text, _read_text(_pkgs))
        if not _write_text_sudo(_pkgs, _merged, password):
            return False
        if not _compress_index(_pkgs, password):
            return False
        tui.console.print(
            f"  → merged remote+local index: {_count_records(_pkgs)} records")

    # 3. per-subdir Release for main binary
    if not _write_subdir_release(
            _binary_abs, suite, codename, _comp, arch, password):
        return False

    # installer udeb pool — local scan only (not a client apt-upgrade surface)
    _udeb_rel = f'dists/{suite}/{_comp}/debian-installer/binary-{arch}'
    _udeb_abs = os.path.join(repo_root, _udeb_rel)
    if os.path.isdir(_udeb_abs):
        if not _scan_packages_to(
                repo_root, _udeb_rel, os.path.join(_udeb_abs, 'Packages'),
                password, udeb=True, allow_empty=True):
            return False
        if not _write_subdir_release(
                _udeb_abs, suite, codename, _comp, arch, password):
            return False

    # 4. top-level Release + sign locally
    _top = os.path.join(repo_root, 'dists', suite, 'Release')
    if not _generate_top_release(
            repo_root, suite, codename, version, _top, password,
            components=[_comp], description=f'Athena repo — {suite}'):
        return False
    if signing_homedir is not None:
        if not sign_release_files(repo_root, suite, signing_homedir, password):
            return False
    tui.console.print(
        f"  → dists/{suite}/ merged-indexed + signed (multi-version main)",
        tui.COLOR_HIGHLIGHT)
    return True


def _remote_has_debs(ssh_cmd: 'list[str]', userhost: str,
                     remote_root: str, rel: str) -> bool:
    """True if the remote subdir `<remote_root>/<rel>` holds any .deb/.udeb."""
    _shell = (f"cd {remote_root} && ls {rel}/*.deb {rel}/*.udeb "
              f"2>/dev/null | head -1")
    _r = subprocess.run(ssh_cmd + [userhost, _shell],
                        capture_output=True, text=True)
    return _r.returncode == 0 and bool(_r.stdout.strip())


def _remote_scan_packages(ssh_cmd: 'list[str]', userhost: str,
                          remote_root: str, rel: str,
                          udeb: bool) -> 'Optional[str]':
    """Run dpkg-scanpackages ON THE REMOTE over `<remote_root>/<rel>` and
    return the Packages text (Filenames relative to remote_root).  Needs
    dpkg-dev on the VM.  None on failure."""
    _scan = 'dpkg-scanpackages -m' + (' -t udeb' if udeb else '')
    _shell = f"cd {remote_root} && {_scan} {rel} /dev/null 2>/dev/null"
    _r = subprocess.run(ssh_cmd + [userhost, _shell],
                        capture_output=True, text=True)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: remote dpkg-scanpackages {rel}: {_r.stderr.strip()[:200]} "
            f"(is dpkg-dev installed on the VM?)")
        logger.error(f"_remote_scan_packages {rel}: rc={_r.returncode}")
        return None
    return _r.stdout


def remote_reindex_and_sign(
    staging: str, ssh_cmd: 'list[str]', userhost: str, remote_root: str,
    suites_spec: 'dict[str, list[str]]', codename_for_suite: dict,
    primary_suite: str, arch: str, version: str, password: str,
    signing_homedir: 'Optional[str]' = None,
    description_for_suite: 'Optional[dict]' = None,
) -> 'Optional[str]':
    """Regenerate the apt index from what's ACTUALLY on the remote (UPD-02):
    `dpkg-scanpackages` runs ON THE VM (via ssh) over each remote pool subdir;
    the metadata is assembled in a LOCAL `staging` mirror and signed LOCALLY
    (key never leaves the box).  The caller then rsyncs `staging/dists` up.

    Only the SCAN is remote (needs dpkg-dev on the VM); compress / per-subdir
    Release / top Release (apt-ftparchive over the local staging tree, which
    hashes the Packages files, not the .debs) / signing all stay local.

    Returns the PRIMARY suite's main/binary-<arch> Packages text (for the local
    manifest), or None on failure.  Subdirs with no .debs on the remote are
    skipped (e.g. doc/tests/debug/udeb after a minimal publish)."""
    if description_for_suite is None:
        description_for_suite = {}
    _main_packages = ''
    for _suite, _components in suites_spec.items():
        _codename = codename_for_suite.get(_suite, _suite)
        _desc = description_for_suite.get(_suite, f'Athena repo — {_suite}')
        _populated: 'list[str]' = []
        for _comp in _components:
            for _udeb, _sub in (
                    (False, f'{_comp}/binary-{arch}'),
                    (True,  f'{_comp}/debian-installer/binary-{arch}')):
                _rel = f'dists/{_suite}/{_sub}'
                if not _remote_has_debs(ssh_cmd, userhost, remote_root, _rel):
                    continue
                _text = _remote_scan_packages(
                    ssh_cmd, userhost, remote_root, _rel, udeb=_udeb)
                if _text is None:
                    return None
                _abs = os.path.join(staging, _rel)
                os.makedirs(_abs, exist_ok=True)
                _out = os.path.join(_abs, 'Packages')
                with open(_out, 'w') as _fh:
                    _fh.write(_text)
                if not _compress_index(_out, password):
                    return None
                if not _write_subdir_release(
                        _abs, _suite, _codename, _comp, arch, password):
                    return None
                if not _udeb and _comp not in _populated:
                    _populated.append(_comp)
                if (not _udeb and _comp == 'main'
                        and _suite == primary_suite):
                    _main_packages = _text
        if not _populated:
            continue
        _top = os.path.join(staging, 'dists', _suite, 'Release')
        os.makedirs(os.path.dirname(_top), exist_ok=True)
        if not _generate_top_release(
                staging, _suite, _codename, version, _top, password,
                components=_populated, description=_desc):
            return None
        if signing_homedir is not None:
            if not sign_release_files(staging, _suite, signing_homedir,
                                      password):
                return None
        tui.console.print(
            f"  → dists/{_suite}/ re-indexed from remote + signed "
            f"(components: {', '.join(_populated)})", tui.COLOR_HIGHLIGHT)
    return _main_packages



def _scan_packages_to(
    staging: str, pool_subdir: str, output_path: str,
    password: str, udeb: bool, allow_empty: bool = False,
) -> bool:
    """sudo dpkg-scanpackages -m [-t udeb] <pool_subdir> > <output> + compress.

    Run with cwd=staging so Packages records carry relative Filename
    entries (matching the layout apt walks via /cdrom/pool/...).
    Spinner-wrapped (no per-file ProgressBar): streaming Perl stdout
    through sudo's pipe didn't surface lines mid-scan reliably even
    with `stdbuf -oL` (operator-observed 2026-05-22) — a Spinner is
    the honest signal here.
    """
    _label = 'udebs' if udeb else 'debs'
    _argv = ['dpkg-scanpackages', '-m']
    if udeb:
        _argv += ['-t', 'udeb']
    _argv += [pool_subdir]
    _spin = tui.Spinner(f"dpkg-scanpackages {pool_subdir} ({_label})")
    try:
        _shell = (
            f'cd {staging} && '
            f'{" ".join(_argv)} 2>/dev/null > {output_path}'
        )
        _r = _sudo(['bash', '-c', _shell], password)
    finally:
        _spin.done()
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: dpkg-scanpackages ({_label}) failed: "
            f"{_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_scan_packages_to {_label}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    if not os.path.exists(output_path):
        tui.console.print(
            f"ERROR: Packages output {output_path} is missing"
        )
        logger.error(f"_scan_packages_to {_label}: missing output at {output_path}")
        return False
    # An empty index is valid for an optional component (e.g. a debs-only
    # minimal pool has no udebs) — allow_empty lets the caller accept it.
    # The main binary index keeps allow_empty=False, so an empty deb scan
    # still fails loud.
    if os.path.getsize(output_path) == 0 and not allow_empty:
        tui.console.print(
            f"ERROR: Packages output {output_path} is empty"
        )
        logger.error(f"_scan_packages_to {_label}: empty output at {output_path}")
        return False
    _n = _count_records(output_path)
    tui.console.print(f"  → {_n} {_label} indexed")
    return _compress_index(output_path, password)


def _scan_sources_to(
    staging: str, pool_subdir: str, output_path: str, password: str,
) -> bool:
    """sudo dpkg-scansources <pool_subdir> > <output> + compress.

    Same Spinner-wrapped approach as _scan_packages_to — see that
    docstring for the streaming-vs-spinner rationale.
    """
    _spin = tui.Spinner(f"dpkg-scansources {pool_subdir}")
    try:
        _shell = (
            f'cd {staging} && '
            f'dpkg-scansources {pool_subdir} 2>/dev/null > {output_path}'
        )
        _r = _sudo(['bash', '-c', _shell], password)
    finally:
        _spin.done()
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: dpkg-scansources failed: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_scan_sources_to: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    _n = _count_records(output_path) if os.path.exists(output_path) else 0
    tui.console.print(f"  → {_n} sources indexed")
    return _compress_index(output_path, password)


def _compress_index(path: str, password: str) -> bool:
    """gzip -9k + xz -9k to produce Packages.gz / Packages.xz (or
    Sources.gz / Sources.xz).  -k keeps the original uncompressed file
    — apt accepts any of the three forms but the uncompressed one is
    what's hashed in the per-subdir Release."""
    for _tool in (['gzip', '-9', '-k', '-f', path],
                  ['xz',   '-9', '-k', '-f', path]):
        _r = _sudo(_tool, password)
        if _r.returncode != 0:
            tui.console.print(
                f"ERROR: {_tool[0]} compress {path}: "
                f"{_r.stderr.strip()[:200]}"
            )
            logger.error(
                f"_compress_index {_tool[0]} {path}: rc={_r.returncode}, "
                f"stderr={_r.stderr.strip()}"
            )
            return False
    return True


def _count_records(path: str) -> int:
    """Count Packages/Sources records.  Each record is a Package: (or
    a Source: stanza in Sources files) at column 0; records are
    separated by a blank line.  Sources records use 'Package: <name>'
    too (the field is named Package even in Sources)."""
    try:
        with open(path, 'r', errors='replace') as fh:
            _content = fh.read()
    except OSError:
        return 0
    return _content.count('\nPackage: ') + (
        1 if _content.startswith('Package: ') else 0
    )


def _write_subdir_release(
    target_dir: str, suite: str, codename: str, component: str,
    arch_label: str, password: str,
) -> bool:
    """Write a minimal per-subdir Release file pinning Suite/Codename/
    Component/Architecture.  apt cross-checks these against the top-level
    Release; mismatch → apt refuses the repo with a useful error.
    """
    _content = (
        f"Origin: Athena\n"
        f"Label: Athena\n"
        f"Archive: {suite}\n"
        f"Suite: {suite}\n"
        f"Codename: {codename}\n"
        f"Component: {component}\n"
        f"Architecture: {arch_label}\n"
        f"Description: Athena installer media — {component}/{arch_label}\n"
    )
    _path = os.path.join(target_dir, 'Release')
    # Write via sudo tee so the file lands root-owned next to the other
    # root-owned index files in this subdir.
    _shell = f"cat > {_path}"
    _r = subprocess.run(
        ['sudo', '-S', 'bash', '-c', _shell],
        input=password + '\n' + _content,
        capture_output=True, text=True,
    )
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: write {_path}: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"_write_subdir_release {_path}: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    return True


def _generate_top_release(
    staging: str, suite: str, codename: str, version: str,
    output_path: str, password: str,
    components: 'Optional[list[str]]' = None,    # Stage B (2026-05-22)
    description: str = 'Athena installer disc',
) -> bool:
    """apt-ftparchive release dists/<suite>/ > dists/<suite>/Release.

    The -o flags pin distro identity — apt-ftparchive emits empty
    Suite/Codename/etc. otherwise and apt then refuses the repo with
    "Repository ... does not have a Release file."

    `components` controls the Components: field listed in the produced
    Release.  Single-suite ISO path uses the default ('main',).
    Multi-component repo (Stage B+) passes ['main', 'doc', 'tests']
    for the thor suite, ['main'] for thor-debug.

    NOT using `bash -c` for this call: some -o values can contain
    spaces (e.g. Description="Athena installer disc"), and shell
    word-splitting would chop them into separate tokens — apt-ftparchive
    then chokes with "Invalid operation installer".  Caught in
    production 2026-05-11.  Pass argv directly to subprocess.run with
    cwd= and stdout=file_handle for redirection.
    """
    if components is None:
        components = ['main']
    _opts = [
        '-o', 'APT::FTPArchive::Release::Origin=Athena',
        '-o', 'APT::FTPArchive::Release::Label=Athena',
        '-o', f'APT::FTPArchive::Release::Suite={suite}',
        '-o', f'APT::FTPArchive::Release::Codename={codename}',
        '-o', f'APT::FTPArchive::Release::Version={version}',
        '-o', 'APT::FTPArchive::Release::Architectures=amd64',
        '-o', f'APT::FTPArchive::Release::Components={" ".join(components)}',
        '-o', f'APT::FTPArchive::Release::Description={description}',
    ]
    _argv = (
        ['sudo', '-S', 'apt-ftparchive'] + _opts +
        ['release', f'dists/{suite}']
    )
    try:
        with open(output_path, 'wb') as fh:
            _r = subprocess.run(
                _argv,
                input=(password + '\n').encode('utf-8'),
                stdout=fh,
                stderr=subprocess.PIPE,
                cwd=staging,
            )
    except OSError as e:
        tui.console.print(f"ERROR: open {output_path} for write: {e}")
        logger.error(f"_generate_top_release open: {e}")
        return False
    _stderr = (_r.stderr or b'').decode('utf-8', errors='replace').strip()
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: apt-ftparchive release failed (rc={_r.returncode}): "
            f"{_stderr[:200]}"
        )
        logger.error(
            f"_generate_top_release: rc={_r.returncode}, stderr={_stderr}"
        )
        return False
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        tui.console.print(
            f"ERROR: top-level Release at {output_path} is missing or empty"
        )
        logger.error(f"_generate_top_release: empty output at {output_path}")
        return False
    return True


def sign_release_files(
    staging: str, suite: str, signing_homedir: str, password: str,
) -> bool:
    """Sign dists/<suite>/Release with our project key.

    Produces two artifacts apt verifies on the target:
      Release.gpg  — detached signature, classic v1 format
      InRelease    — clearsigned (signature inline), modern preferred format

    apt fetches InRelease first; if absent, falls back to
    Release + Release.gpg.  Shipping both maximises compatibility with
    older apt versions that don't speak InRelease.  Both files MUST be
    signed by a key the target trusts — we ship the matching pubkey at
    .disk/archive-key.gpg so the install-time hook can install it into
    /target/etc/apt/trusted.gpg.d/ before its apt-cdrom add call.

    Caught 2026-05-13: without these, apt-get update on the target said
    "does not have a Release file" — apt-setup's verify step then
    discarded 40cdrom's output and the installed target's sources.list
    never got a working cdrom entry, so any post-install apt-get
    install failed with "Unable to locate package".  See Phase C
    diagnosis in docs/known-issues.md.
    """
    del password   # unused (gpg signing doesn't need sudo; signing_homedir
                   # is user-owned).  Accepted for symmetry with the other
                   # iso_installer helpers.
    _release = os.path.join(staging, 'dists', suite, 'Release')
    if not os.path.isfile(_release):
        tui.console.print(
            f"ERROR: {_release} missing — apt-repo generation must run first"
        )
        logger.error(f"sign_release_files: {_release} absent")
        return False
    if not os.path.isdir(signing_homedir):
        tui.console.print(
            f"ERROR: signing homedir {signing_homedir} absent — run "
            "'signing keygen' (or whatever sets up the project key) first"
        )
        logger.error(f"sign_release_files: {signing_homedir} absent")
        return False
    _release_gpg = _release + '.gpg'
    _inrelease   = os.path.join(os.path.dirname(_release), 'InRelease')
    # gpg refuses to overwrite by default; remove any stale signatures
    # from a previous build run first.
    for _f in (_release_gpg, _inrelease):
        if os.path.exists(_f):
            try:
                os.unlink(_f)
            except OSError as e:
                tui.console.print(f"ERROR: rm {_f}: {e}")
                logger.error(f"sign_release_files rm {_f}: {e}")
                return False
    # Detached, ASCII-armored signature → Release.gpg.  --batch + --yes
    # so gpg never prompts (we always overwrite the previously-removed file).
    _argv = [
        'gpg', '--homedir', signing_homedir,
        '--batch', '--yes',
        '--output', _release_gpg,
        '--detach-sign', '--armor',
        _release,
    ]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: gpg --detach-sign Release: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"sign_release_files detach-sign: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    # Clearsigned (signature wraps the original content) → InRelease.
    _argv = [
        'gpg', '--homedir', signing_homedir,
        '--batch', '--yes',
        '--output', _inrelease,
        '--clearsign',
        _release,
    ]
    _r = subprocess.run(_argv, capture_output=True, text=True)
    if _r.returncode != 0:
        tui.console.print(
            f"ERROR: gpg --clearsign Release: {_r.stderr.strip()[:200]}"
        )
        logger.error(
            f"sign_release_files clearsign: rc={_r.returncode}, "
            f"stderr={_r.stderr.strip()}"
        )
        return False
    tui.console.print(
        f"Release signed: dists/{suite}/Release.gpg + InRelease"
    )
    return True


def export_pubkey_to_staging(
    staging: str, signing_pubkey_path: str, password: str,
) -> bool:
    """Copy the project pubkey to staging/.disk/archive-key.gpg.

    The install-time hook (base-installer quilt patch, see
    patch/source/base-installer/) reads this and copies it to
    /target/etc/apt/trusted.gpg.d/athena-archive-keyring.gpg BEFORE
    running its apt-cdrom add call.  Without the keyring installed,
    apt rejects our signed Release with "NO_PUBKEY" and the chain
    falls apart the same way an unsigned Release would.

    The pubkey was already exported by signing.generate_key at project
    setup time (CONF-02 phase 1); we just copy it onto the disc.  The
    .disk/ directory is created earlier by _stage_disk_info, so we
    just need a write into an existing user-owned dir — no sudo.
    """
    del password   # unused (writing into user-owned .disk/)
    if not os.path.isfile(signing_pubkey_path):
        tui.console.print(
            f"ERROR: pubkey {signing_pubkey_path} missing — run "
            "'signing keygen' (or whatever sets up the project key) first"
        )
        logger.error(
            f"export_pubkey_to_staging: {signing_pubkey_path} absent"
        )
        return False
    _dst_dir = os.path.join(staging, '.disk')
    if not os.path.isdir(_dst_dir):
        tui.console.print(
            f"ERROR: {_dst_dir} missing — _stage_disk_info must run first"
        )
        logger.error(f"export_pubkey_to_staging: {_dst_dir} absent")
        return False
    _dst = os.path.join(_dst_dir, 'archive-key.gpg')
    try:
        shutil.copyfile(signing_pubkey_path, _dst)
        os.chmod(_dst, 0o644)
    except OSError as e:
        tui.console.print(f"ERROR: copy pubkey to {_dst}: {e}")
        logger.error(f"export_pubkey_to_staging copy: {e}")
        return False
    tui.console.print(
        "Pubkey exported: .disk/archive-key.gpg "
        f"({os.path.getsize(_dst)} bytes)"
    )
    return True
