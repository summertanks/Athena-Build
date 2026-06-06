"""OBS-04 — exhaustive per-package build/tunnel log.

Writes a verbose, human-readable narrative of EVERYTHING that happens to
one source package during a build or a tunnel: resolved build-depends, the
container, files emitted by ``dpkg-buildpackage``, files expected per the
``Package-List``, files relocated into repo subdirs, files purged, NMU
strips, ``+asg<R>u<N>`` stamps, per-file hash + size, and timing.

This is SEPARATE from the two existing artifacts:
  * ``log/build/<pkg>``            — raw container stdout/stderr stream
  * ``log/build/<pkg>.build.json`` — structured machine record (OBS-01)
The new file is ``log/build/<pkg>.buildlog`` — the narrative an operator
reads to understand the whole workflow end to end.

DESIGN INVARIANT — observability must NEVER break a build.  A full repo
rebuild runs 24-36h; a logging bug that aborts a build would waste it.  So
every public method is best-effort: it catches and swallows all exceptions
(logging them via the module logger) and never propagates.  Accumulation
only appends strings to an in-memory list; ``write()`` does the only IO,
atomically (temp + rename), and is itself fully guarded.  A caller may
invoke any method, in any order, at any time, without a try/except.
"""

import logging
import os

logger = logging.getLogger(__name__)

_COL = 18  # key column width for kv()


def human_size(n: 'int | float') -> str:
    """Human-readable byte count.  Best-effort: returns '?' on bad input
    rather than raising (this is logging-path code)."""
    try:
        _n = float(n)
    except (TypeError, ValueError):
        return '?'
    for _unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(_n) < 1024.0:
            return f"{_n:.1f} {_unit}" if _unit != 'B' else f"{int(_n)} B"
        _n /= 1024.0
    return f"{_n:.1f} PB"


def safe_size(path: str) -> int:
    """os.path.getsize that never raises (returns -1 on any error)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


class BuildLog:
    """Accumulate a verbose per-package build/tunnel narrative and write it
    to ``<log_dir>/<package>.buildlog``.

    All methods are best-effort and MUST NOT raise into the build path.
    Typical use::

        blog = BuildLog(buildlog_dir, src.package, kind='build')
        blog.header(intended_version=..., arch=..., profiles=...)
        ... blog.section('EMITTED'); blog.file('foo.deb', size=123) ...
        blog.footer(status='PASS')
        blog.write()
    """

    def __init__(self, log_dir: str, package: str, kind: str = 'build',
                 suffix: str = '.buildlog'):
        self._dir = log_dir
        self._package = str(package)
        self._kind = str(kind)
        self._lines: 'list[str]' = []
        self._path = ''
        try:
            self._path = os.path.join(log_dir, f"{self._package}{suffix}")
        except Exception as _e:  # pragma: no cover — pathological inputs
            logger.warning(f"BuildLog init for {package!r}: {_e}")

    # ----- accumulation (append-only, cannot fail the build) -------------

    def line(self, text: str = '') -> None:
        self._append(str(text))

    def section(self, title: str) -> None:
        self._append('')
        self._append(f"--- {title} ---")

    def kv(self, key: str, value: object) -> None:
        self._append(f"{str(key):<{_COL}}{value}")

    def bullet(self, text: str) -> None:
        self._append(f"  {text}")

    def file(self, name: str, *, size: 'int | None' = None,
             sha256: str = '', detail: str = '') -> None:
        """One file line, optionally annotated with size / sha256 / detail."""
        _parts = [f"  {name}"]
        if size is not None and size >= 0:
            _parts.append(f"size={human_size(size)}")
        if sha256:
            _parts.append(f"sha256={sha256[:16]}…")
        if detail:
            _parts.append(detail)
        self._append('  '.join(_parts))

    def relocation(self, name: str, dest_dir: str) -> None:
        self._append(f"  {name}  →  {dest_dir}")

    def empty(self, label: str = '(none)') -> None:
        """Marker for an empty section so the reader sees it was checked."""
        self._append(f"  {label}")

    # ----- structured headers / footers ----------------------------------

    def header(self, **fields: object) -> None:
        self._append(f"=== {self._kind.upper()} LOG: {self._package} ===")
        for _k, _v in fields.items():
            self.kv(_k, _v)

    def footer(self, **fields: object) -> None:
        _summary = '  '.join(f"{_k}={_v}" for _k, _v in fields.items())
        self._append('')
        self._append(f"=== END {self._package}  {_summary} ===")

    # ----- IO (the only place that touches disk) -------------------------

    def write(self) -> None:
        """Atomically (temp + rename) write the accumulated narrative.
        Idempotent — may be called repeatedly (e.g. after the container
        exits and again at terminal) to flush partial progress; each call
        rewrites the whole file from the in-memory buffer.  Fully guarded:
        an IO failure is logged, never raised."""
        if not self._path:
            return
        try:
            _tmp = f"{self._path}.tmp"
            with open(_tmp, 'w') as _fh:
                _fh.write('\n'.join(self._lines))
                _fh.write('\n')
            os.replace(_tmp, self._path)
        except Exception as _e:
            logger.warning(f"BuildLog write for {self._package!r}: {_e}")

    # ----- internal ------------------------------------------------------

    def _append(self, text: str) -> None:
        try:
            self._lines.append(text)
        except Exception:  # pragma: no cover — list.append on MemoryError
            pass
