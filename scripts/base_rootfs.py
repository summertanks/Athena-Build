"""Base rootfs for the build-container image — bootstrapped FROM THE PINNED
SNAPSHOT, not pulled from Docker Hub.

`FROM debian:<release>-slim` made the image's base layer the one part of the
build environment outside our snapshot pin: whatever Docker Hub served on
pull day, assembled with doc/locale stripping and docker-specific dpkg
config baked in at THEIR build time.  That cost us a class of buildd-parity
failures (dpkg exact-stderr tests, libpsl's publicsuffix doc fixture,
init-system-helpers' dpkg-database-vs-disk walk) and left a third-party,
unpinned artifact under a toolchain whose identity is "every byte from the
pinned snapshot".

This module bootstraps the base instead: ``mmdebstrap --variant=buildd``
against the same three snapshot pockets the per-build sources use, emitting
a tar the Dockerfile consumes via ``FROM scratch`` + ``ADD``.  The buildd
variant is what sbuild chroots are made of — buildd parity by construction
(no stripping, no docker baggage, build-essential present).  The tar is
cached per (release, arch, snapshot) next to the other snapshot-keyed
caches and reused until the snapshot advances.

mmdebstrap is a host preflight requirement (build-system.sh enforces the
floor); it runs unprivileged (unshare mode) unlike debootstrap.
"""
import logging
import os
import subprocess
from typing import Any, List, Optional

logger = logging.getLogger('athena.build')

# mmdebstrap output is tar when the target ends .tar — keep in step with
# rootfs_path() and the Dockerfile's `ADD base-rootfs.tar /`.
ROOTFS_BASENAME = 'base-rootfs.tar'


def rootfs_path(config: Any, snapshot_ts: str) -> str:
    """Cache path of the bootstrapped base tar for THIS (release, arch,
    snapshot) triple — snapshot-keyed like the image tag, so a snapshot
    advance misses the cache and bootstraps fresh."""
    return os.path.join(
        config.dir_cache,
        f'base-rootfs-{config.container_release}-{config.arch}'
        f'-{snapshot_ts}.tar')


def snapshot_sources(config: Any, snapshot_ts: str) -> 'List[str]':
    """The three snapshot pockets, same shape as the Dockerfile's
    sources.list and _write_snapshot_sources_cmd: main + -updates +
    -security at ONE timestamp.  Packages absent from the release suite at
    a given TS (moved to a pocket) fail resolution without all three."""
    _base = config.snapshot_baseurl
    _rel = config.container_release
    return [
        f'deb [check-valid-until=no] {_base}/debian/{snapshot_ts} '
        f'{_rel} main',
        f'deb [check-valid-until=no] {_base}/debian/{snapshot_ts} '
        f'{_rel}-updates main',
        f'deb [check-valid-until=no] {_base}/debian-security/{snapshot_ts} '
        f'{_rel}-security main',
    ]


def _clear_partial(partial: str) -> None:
    """Remove a stale .partial — file or directory (unshare-mode dir
    bootstraps create root-owned-looking trees that need rmtree)."""
    import shutil
    if os.path.isdir(partial):
        shutil.rmtree(partial, ignore_errors=True)
    elif os.path.exists(partial):
        try:
            os.remove(partial)
        except OSError:
            pass


def ensure_base_rootfs(config: Any, snapshot_ts: 'Optional[str]') -> str:
    """Return the cached base tar for this snapshot, bootstrapping it with
    mmdebstrap when absent.  Writes to a .partial then renames — a killed
    bootstrap never leaves a truncated tar behind for ADD to consume.
    Raises RuntimeError on bootstrap failure (stderr tail included)."""
    if not snapshot_ts:
        raise RuntimeError('base rootfs: snapshot timestamp is empty')
    _out = rootfs_path(config, snapshot_ts)
    if os.path.isfile(_out) and os.path.getsize(_out) > 0:
        return _out
    os.makedirs(os.path.dirname(_out), exist_ok=True)
    _partial = _out + '.partial'
    # clear any stale partial from a killed/failed prior run — it may be
    # a DIRECTORY (a pre-`--format=tar` run bootstrapped in directory
    # mode), which the tar write cannot open.
    _clear_partial(_partial)
    _cmd = [
        'mmdebstrap',
        '--variant=buildd',
        # explicit: mmdebstrap otherwise infers the format from the target
        # EXTENSION, and the atomic .partial suffix hides the .tar — it
        # fell into directory mode and died on mkdirs (rc=25, 2026-07-08).
        '--format=tar',
        f'--architectures={config.arch}',
        # ca-certificates: the buildd variant has NO trust store, so the
        # image's https apt fetches from snapshot.debian.org fail — and
        # apt-get update only WARNS (exit 0), leaving empty lists and
        # "Unable to locate" for every install.  This was also the
        # never-root-caused CONF-15 anomaly on the old slim base (slim
        # lacks ca-certificates too; its live-mirror step was plain http).
        '--include=ca-certificates',
        # snapshot Release files are long-expired by design
        '--aptopt=Acquire::Check-Valid-Until "false"',
        '--aptopt=Acquire::Retries "5"',
        config.container_release,
        _partial,
        *snapshot_sources(config, snapshot_ts),
    ]
    logger.info(f"base rootfs: bootstrapping {os.path.basename(_out)}")
    try:
        _res = subprocess.run(
            _cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as _e:   # mmdebstrap missing despite preflight
        raise RuntimeError(
            f'base rootfs: mmdebstrap not runnable: {_e}') from _e
    if _res.returncode != 0:
        _tail = _res.stderr.decode('utf-8', errors='replace')[-800:]
        _clear_partial(_partial)
        raise RuntimeError(
            f'base rootfs: mmdebstrap failed (rc={_res.returncode}):\n{_tail}')
    os.replace(_partial, _out)
    logger.info(
        f"base rootfs: {os.path.basename(_out)} "
        f"({os.path.getsize(_out)} bytes)")
    return _out
