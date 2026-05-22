"""scripts/grub_assembly.py — produce hybrid BIOS+EFI bootable ISOs
using OUR GRUB binaries from repo/main/, not the build host's.

COMP-14 fix.  Pre-fix, both iso.py:build_iso (live ISO) and
iso_installer.py:_run_grub_mkrescue (installer ISO) invoked the host's
/usr/bin/grub-mkrescue.  That wrapper uses the host's grub-mkimage to
build core.img and pulls modules from the host's /usr/lib/grub/.  Net
result: every ISO embedded whatever GRUB version the build host
happened to have — when the build host is Debian 13/trixie (GRUB
2.12), the ISO's bootloader self-reported 2.12 even though
repo/main/grub-*_2.06-13_*.deb said 2.06 in its dpkg metadata.

Fix: extract grub-common + grub-pc-bin + grub-efi-amd64-bin from
repo/main/ to a temporary dir, then invoke OUR extracted
grub-mkrescue with --directory= pointing at our modules and PATH
prepended so any grub-mkimage callouts resolve to our binary.  The
core.img embedded in the produced ISO is then built by our toolchain
from our modules.

Note on residue: our extracted grub-mkrescue --version still
self-reports "2.06-13+deb12u1" because PACKAGE_VERSION is compiled
into the binary at dpkg-buildpackage time on Debian's buildd (the
+deb12u1 NMU suffix that strip_nmu_from_deb rewrites in the dpkg
control field but can't rewrite inside the compiled binary).  That's
the compiled-in residue catalogued in docs/branding-methodology.md
§ 7.  The fix here is about the SOURCE of the GRUB code embedded in
the ISO (our repo's 2.06-13 vs the host's 2.12-9), not about
scrubbing the compiled-in version string inside the binary.
"""

import logging
import os
import subprocess
import tempfile
from glob import glob
from typing import Tuple

logger = logging.getLogger('athena')


# .debs we extract for the runtime GRUB toolchain.  Order is irrelevant
# (all extract into the same staging tree); listed here as the unit of
# work and for the failure-mode message when one is missing from the
# repo.
GRUB_PKG_NAMES = ('grub-common', 'grub-pc-bin', 'grub-efi-amd64-bin')


def _find_grub_deb(repo_path: str, pkg_name: str) -> str:
    """Locate the .deb for pkg_name in repo/main/.

    Raises FileNotFoundError if missing — signals that cache build
    hasn't populated grub and the ISO build is premature (caller
    should surface a clear error so the operator runs `cache build`
    first).
    """
    _glob = os.path.join(repo_path, 'main', f'{pkg_name}_*.deb')
    _matches = glob(_glob)
    if not _matches:
        raise FileNotFoundError(
            f'{pkg_name} not in repo/main/ — looked for {_glob}.  '
            f'Run cache build to populate repo/main/.'
        )
    if len(_matches) > 1:
        # After NMU strip there should be exactly one .deb per
        # binary package; multiple is unexpected.  Pick the first
        # deterministically (sorted) so the build is reproducible
        # even in this edge case, and warn so the operator notices.
        _matches.sort()
        logger.warning(
            f'multiple {pkg_name} .debs found in repo/main/; picking '
            f'{os.path.basename(_matches[0])} '
            f'(others: {[os.path.basename(_m) for _m in _matches[1:]]})'
        )
    return _matches[0]


def _extract_grub_toolchain(repo_path: str, dst_dir: str) -> None:
    """dpkg-deb -x our grub debs into dst_dir.  After this returns,
    <dst_dir>/usr/bin/grub-mkrescue + grub-mkimage exist and
    <dst_dir>/usr/lib/grub/{i386-pc,x86_64-efi}/ are populated.

    Raises FileNotFoundError if any of GRUB_PKG_NAMES is missing from
    the repo, RuntimeError if dpkg-deb exits non-zero (filesystem
    permission error, corrupt .deb, etc.).
    """
    for _name in GRUB_PKG_NAMES:
        _deb = _find_grub_deb(repo_path, _name)
        logger.debug(f'extracting {_deb} → {dst_dir}')
        _r = subprocess.run(
            ['dpkg-deb', '-x', _deb, dst_dir],
            capture_output=True, text=True,
        )
        if _r.returncode != 0:
            raise RuntimeError(
                f'dpkg-deb -x {_deb} failed (rc={_r.returncode}): '
                f'{_r.stderr.strip()}'
            )


def run_grub_mkrescue_from_repo(
    repo_path: str,
    stage_dir: str,
    iso_path: str,
) -> Tuple[bool, str, str]:
    """Run grub-mkrescue using OUR grub binaries from repo_path/main/
    instead of the host's.

    Args:
        repo_path: Path to the repo/ directory containing main/ with
                   our grub-common/grub-pc-bin/grub-efi-amd64-bin .debs.
        stage_dir: ISO source tree (boot/grub/grub.cfg + assets +
                   /pool + .disk/ etc.).
        iso_path:  Output ISO file.

    Returns (ok, stdout, stderr) for the caller to log and surface.
    On extraction failure (missing .deb, dpkg-deb error) returns
    (False, '', <reason>) without ever invoking grub-mkrescue.
    """
    try:
        # tempfile.TemporaryDirectory wipes the temp dir on context
        # exit.  Even if grub-mkrescue dies mid-run, cleanup is
        # automatic; the temp dir doesn't leak into successive builds.
        with tempfile.TemporaryDirectory(prefix='athena-grub-') as _gd:
            _extract_grub_toolchain(repo_path, _gd)
            _mkrescue = os.path.join(_gd, 'usr', 'bin', 'grub-mkrescue')
            _mod_dir  = os.path.join(_gd, 'usr', 'lib', 'grub')
            if not os.path.isfile(_mkrescue):
                return (False, '',
                        f'extracted grub-mkrescue missing at {_mkrescue}')
            if not os.path.isdir(_mod_dir):
                return (False, '',
                        f'extracted module dir missing at {_mod_dir}')
            # Prepend our extracted /usr/bin to PATH so grub-mkrescue's
            # internal callouts to grub-mkimage resolve to OUR binary
            # (PACKAGE_VERSION 2.06) rather than the host's (2.12).
            # grub-mkrescue uses bare `grub-mkimage` from PATH per its
            # strings table — no --grub-mkimage= flag exists.
            _env = os.environ.copy()
            _env['PATH'] = f'{os.path.join(_gd, "usr", "bin")}:{_env.get("PATH", "")}'
            _cmd = [
                _mkrescue,
                '--directory=' + _mod_dir,
                '-o', iso_path,
                stage_dir,
            ]
            logger.info(
                f'grub-mkrescue: directory={_mod_dir} '
                f'(PATH-prefixed with {os.path.join(_gd, "usr", "bin")})'
            )
            _r = subprocess.run(
                _cmd, capture_output=True, text=True, env=_env,
            )
            return (_r.returncode == 0, _r.stdout, _r.stderr)
    except FileNotFoundError as e:
        return (False, '', str(e))
    except RuntimeError as e:
        return (False, '', str(e))
