"""Supply-chain reporting — the `sbom` and `cve` commands.

cmd_sbom emits a CycloneDX SBOM over the built repo; cmd_cve runs the
SBOM through the vulnerability matcher.  Extracted verbatim from build.py's
BuildSession; see commands/base.py for how the mixin shares session state.
"""
import logging
import os

import tui
import utils
from tui import console

from commands.base import SessionState

logger = logging.getLogger('athena.build')


class SupplyChainCommandsMixin(SessionState):
    def cmd_sbom(self, *args):
        """Emit a CycloneDX 1.5 JSON Software Bill of Materials.

        Walks dep_tree.selected_srcs (∪ udeb_dep_tree.selected_srcs)
        and writes one component per source — name + version + DSC
        sha256 + patch-set hash + PURL (`pkg:deb/<base-id>/<name>@
        <version>`).  Top-level metadata records distribution +
        version + codename + arch + snapshot timestamp.

        Usage:
          sbom               — write to <dir_image>/<distro-version-
                                snapshot-arch>.cdx.json (next to ISOs)
          sbom <path>        — write to the given path

        Requires a parsed dep tree; run `cache parse` first.
        """
        if not self.flags.dep_check_ready:
            console.print(
                "sbom: dep tree not built — run `cache parse` first"
            )
            return
        if self.dep_tree is None:
            console.print(
                "sbom: dep_tree is None even though dep_check_ready is set "
                "— this should not happen; rerun `cache parse`"
            )
            return

        import sbom as _sbom_mod
        if args:
            _out = args[0]
        else:
            _snap = utils.snapshot_iso_tag(self.config)
            _distro = str(self.config.build_distribution).strip(
                '"').strip("'").lower()
            _version = str(self.config.build_version).strip(
                '"').strip("'")
            _arch = self.config.arch
            _basename = (
                f"{_distro}-{_version}-{_snap}-{_arch}"
                if _snap
                else f"{_distro}-{_version}-{_arch}"
            )
            _out = os.path.join(
                self.config.dir_image, f"{_basename}.cdx.json",
            )

        _path = _sbom_mod.generate_cdx(
            self.config,
            self.dep_tree,
            udeb_dep_tree=self.udeb_dep_tree,
            out_path=_out,
            container=self.container,
        )
        if not _path:
            console.print("ERROR: sbom generation failed — see log")
            return

        try:
            _size_kb = os.path.getsize(_path) // 1024
        except OSError:
            _size_kb = 0
        _n = len(self.dep_tree.selected_srcs)
        if self.udeb_dep_tree is not None:
            # Same dedup the generator applies.
            _udeb_only = set(self.udeb_dep_tree.selected_srcs.keys()) - set(
                self.dep_tree.selected_srcs.keys())
            _n += len(_udeb_only)
        console.print(
            f"sbom: {_path} ({_size_kb} KB, {_n} component(s))",
            tui.COLOR_HIGHLIGHT,
        )

    @staticmethod
    def _cve_report_path(sbom_path: str) -> str:
        """Derive the `.cve.json` report path from an SBOM path, stripping a
        `.cdx.json` double-suffix (`foo.cdx.json` → `foo.cve.json`).

        The old `sbom_path.replace('.cdx.json', '.cve.json')` was a
        no-op on any name not ending `.cdx.json` (e.g. `sbom.json`), so the
        report path equalled the input and the write clobbered the operator's
        SBOM.  splitext-based derivation never collides with the input."""
        _root, _ = os.path.splitext(sbom_path)
        if _root.endswith('.cdx'):
            _root = _root[:-len('.cdx')]
        return _root + '.cve.json'

    def cmd_cve(self, *args):
        """Scan the latest SBOM against Grype's vulnerability
        databases (NVD + GHSA + Debian Security Tracker).

        Reads a CycloneDX 1.5 JSON SBOM (produced by `sbom`) and
        delegates to grype for the actual lookup.  Renders a severity
        summary on the console + writes the full JSON report next to
        the SBOM.

        Usage:
          cve               — scan the most recent .cdx.json in dir_image
          cve <path>        — scan the given SBOM path

        Grype is an OPTIONAL prerequisite (warned at startup).  When
        absent this command prints install instructions + returns
        without scanning.

        Note: scans the SBOM, NOT the live dpkg DB.  Live-system
        scanning produces false positives for our NMU-stripped
        binaries (see docs/cve-tracking.md).  The
        X-Athena-Upstream-Version field on each .deb preserves the
        upstream version for future custom matchers; until then,
        SBOM-side scanning is the source of truth.
        """
        import shlex as _shlex
        import shutil as _shutil
        import subprocess as _subprocess
        from collections import Counter as _Counter

        _grype = _shutil.which('grype')
        if not _grype:
            console.print(
                "cve: `grype` not on PATH — install via Anchore's "
                "static binary release: "
                "curl -sSfL https://raw.githubusercontent.com/anchore/grype/"
                "main/install.sh | sudo sh -s -- -b /usr/local/bin",
                tui.COLOR_WARNING,
            )
            console.print(
                "Once installed, re-run `cve [path]` to scan the SBOM.",
                tui.COLOR_INFO,
            )
            return

        # Resolve SBOM path.
        if args:
            _sbom_path = args[0]
        else:
            try:
                _candidates = sorted(
                    (_f for _f in os.listdir(self.config.dir_image)
                     if _f.endswith('.cdx.json')),
                    key=lambda _f: os.path.getmtime(
                        os.path.join(self.config.dir_image, _f)),
                    reverse=True,
                )
            except OSError as _e:
                console.print(f"cve: cannot list {self.config.dir_image}: {_e}")
                return
            if not _candidates:
                console.print(
                    f"cve: no .cdx.json found in {self.config.dir_image} — "
                    f"run `sbom` first"
                )
                return
            _sbom_path = os.path.join(self.config.dir_image, _candidates[0])
            console.print(f"cve: using {_candidates[0]} (most recent SBOM)")

        if not os.path.isfile(_sbom_path):
            console.print(f"cve: SBOM not found: {_sbom_path}")
            return

        _report_path = self._cve_report_path(_sbom_path)
        # Defence in depth: never write the report over the input SBOM
        # (refuse BEFORE the multi-second grype scan).
        if os.path.abspath(_report_path) == os.path.abspath(_sbom_path):
            console.print(
                f"cve: refusing to overwrite the input SBOM ({_sbom_path}) "
                "with the report — rename it or move it aside",
                tui.COLOR_ERROR)
            return
        _cmd = [_grype, f'sbom:{_sbom_path}', '-o', 'json']
        logger.info(f"cve: {' '.join(_shlex.quote(_p) for _p in _cmd)}")

        # grype's first run downloads the vuln DB (~30s); spinner so
        # the operator sees progress.
        _spin = tui.Spinner(
            f"grype scan {os.path.basename(_sbom_path)}"
        )
        try:
            _r = _subprocess.run(_cmd, capture_output=True, text=True)
        finally:
            _spin.done()
        if _r.returncode != 0:
            console.print(
                f"cve: grype exited {_r.returncode}: "
                f"{_r.stderr.strip()[:200]}",
                tui.COLOR_ERROR,
            )
            logger.error(
                f"cve: grype stderr_tail={_r.stderr.strip().splitlines()[-5:]}"
            )
            return

        try:
            import json as _json
            _doc = _json.loads(_r.stdout)
        except (ValueError, TypeError) as _e:
            console.print(f"cve: grype output not JSON-parseable: {_e}")
            return

        # Persist full report.
        try:
            with open(_report_path, 'w', encoding='utf-8') as _fh:
                _fh.write(_r.stdout)
        except OSError as _e:
            logger.warning(f"cve: could not write report sidecar {_report_path}: {_e}")

        # Render severity summary on console.
        _matches = _doc.get('matches', []) or []
        if not _matches:
            console.print(
                "cve: clean — no vulnerabilities reported",
                tui.COLOR_HIGHLIGHT,
            )
            console.print(f"cve: report → {_report_path}")
            return

        _by_sev: _Counter = _Counter()
        for _m in _matches:
            _sev = (_m.get('vulnerability', {}) or {}).get('severity', 'Unknown')
            _by_sev[_sev] += 1

        console.print(
            f"cve: {len(_matches)} finding(s) across "
            f"{len({(_m.get('artifact', {}) or {}).get('name', '') for _m in _matches})}"
            f" component(s)",
            tui.COLOR_WARNING,
        )
        for _sev in ('Critical', 'High', 'Medium', 'Low', 'Negligible', 'Unknown'):
            if _by_sev.get(_sev, 0):
                console.print(f"  {_sev:11s} {_by_sev[_sev]}")

        # Show the top critical/high findings so the operator has
        # actionable context without leaving the TUI.
        _top = [
            _m for _m in _matches
            if (_m.get('vulnerability', {}) or {}).get('severity')
            in ('Critical', 'High')
        ]
        if _top:
            console.print(f"\nTop {min(10, len(_top))} Critical/High:")
            for _m in _top[:10]:
                _vuln = _m.get('vulnerability', {}) or {}
                _art = _m.get('artifact', {}) or {}
                _fix = (_vuln.get('fix') or {}).get('versions') or []
                _fix_str = (', '.join(_fix) if _fix else '—')
                console.print(
                    f"  [{_vuln.get('severity', '?'):8s}] "
                    f"{_vuln.get('id', '?'):16s} "
                    f"{_art.get('name', '?')}@{_art.get('version', '?')}  "
                    f"fix: {_fix_str}"
                )
        console.print(f"\ncve: report → {_report_path}")
