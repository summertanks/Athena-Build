"""Static release-index generation for the published mirror.

`mirror publish` publishes, alongside the apt repo, two files at the pool
root (served at `<public_url>/`):

  * ``releases.json`` — the MACHINE-readable manifest (this is what the
    installer-smoke CI job reads to find + sha-verify the ISO to test).
  * ``index.html`` — a human landing page: distro / version / snapshot,
    the apt-setup line, and the ISO downloads with size + sha256.

Both are produced by the pure functions here from a single manifest dict,
so the HTML and the JSON can never disagree.  No I/O, no clock access —
the caller passes `generated_at` — which keeps this unit-testable and
deterministic.
"""

import html
import json
from typing import List, Optional

# Bump when the releases.json shape changes in a way consumers must notice.
RELEASES_SCHEMA_VERSION = 1


def build_release_manifest(
    *,
    distribution: str,
    version: str,
    snapshot: str,
    codename: str,
    component: str,
    arch: str,
    public_url: str,
    signed_by_keyring: str,
    isos: 'List[dict]',
    generated_at: str,
) -> dict:
    """Compose the ``releases.json`` manifest.

    `isos` is a list of ``{kind, file, size, sha256, built_at}`` dicts
    (kind ∈ live / installer / disk).  Each gains a resolved ``url`` under
    ``<public_url>/iso/``.  `public_url` is the HTTP base apt clients and
    the smoke job hit; `signed_by_keyring` is the on-target keyring path
    the apt line pins.
    """
    _base = public_url.rstrip('/')
    _components = [component]
    _deb_line = (
        f"deb [signed-by={signed_by_keyring}] {_base} {codename} "
        + ' '.join(_components)
    )
    _isos: 'List[dict]' = []
    for _i in isos:
        _file = str(_i.get('file') or '')
        if not _file:
            continue
        _isos.append({
            'kind':     str(_i.get('kind') or ''),
            'file':     _file,
            'url':      f"{_base}/iso/{_file}",
            'size':     int(_i.get('size') or 0),
            'sha256':   str(_i.get('sha256') or ''),
            'built_at': str(_i.get('built_at') or ''),
        })
    return {
        'schema':        RELEASES_SCHEMA_VERSION,
        'distribution':  distribution,
        'version':       version,
        'snapshot':      snapshot,
        'codename':      codename,
        'component':     component,
        'arch':          arch,
        'generated_at':  generated_at,
        'apt': {
            'url':        _base,
            'suite':      codename,
            'components': _components,
            'deb_line':   _deb_line,
        },
        'isos': _isos,
    }


def render_releases_json(manifest: dict) -> str:
    """Serialise the manifest deterministically (sorted keys, trailing
    newline) so consecutive publishes diff cleanly."""
    return json.dumps(manifest, indent=2, sort_keys=True) + '\n'


def _human_size(n: int) -> str:
    _f = float(n)
    for _u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if _f < 1024.0 or _u == 'TB':
            return f"{_f:.0f} {_u}" if _u == 'B' else f"{_f:.1f} {_u}"
        _f /= 1024.0
    return f"{n} B"


def render_index_html(manifest: dict) -> str:
    """Render the human landing page from the same manifest.  Minimal
    self-contained HTML (inline CSS, no external assets) — every dynamic
    value is HTML-escaped."""
    def _e(_v: object) -> str:
        return html.escape(str(_v))

    _distro = _e(manifest.get('distribution'))
    _ver = _e(manifest.get('version'))
    _snap = _e(manifest.get('snapshot') or '(no snapshot pin)')
    _apt = manifest.get('apt') or {}
    _deb_line = _e(_apt.get('deb_line'))

    _iso_rows = []
    for _i in manifest.get('isos') or []:
        _iso_rows.append(
            "      <tr>"
            f"<td>{_e(_i.get('kind'))}</td>"
            f"<td><a href=\"{_e(_i.get('url'))}\">{_e(_i.get('file'))}</a></td>"
            f"<td>{_e(_human_size(int(_i.get('size') or 0)))}</td>"
            f"<td class=\"sha\">{_e(_i.get('sha256'))}</td>"
            "</tr>"
        )
    _iso_table = (
        "    <table>\n"
        "      <tr><th>Image</th><th>File</th><th>Size</th><th>SHA-256</th></tr>\n"
        + '\n'.join(_iso_rows) + '\n'
        "    </table>"
    ) if _iso_rows else (
        "    <p class=\"muted\">No ISO images published for this "
        "snapshot yet.</p>"
    )

    return (
        "<!DOCTYPE html>\n"
        # Stable detection token: lets tooling (prep-mirror.sh's serving
        # probe, monitors) tell a served release page from an autoindex
        # directory listing without parsing the body.
        "<!-- asgard-release-index -->\n"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        f"<title>{_distro} {_ver}</title>\n"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1\">\n"
        "<style>\n"
        " body{font-family:system-ui,sans-serif;max-width:48rem;margin:2rem "
        "auto;padding:0 1rem;line-height:1.5;color:#1a1a1a}\n"
        " h1{margin-bottom:.2rem} .muted{color:#666}\n"
        " code,pre{background:#f4f4f4;border-radius:4px}\n"
        " pre{padding:.75rem;overflow-x:auto} code{padding:.1rem .3rem}\n"
        " table{border-collapse:collapse;width:100%;margin:1rem 0}\n"
        " th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px "
        "solid #e2e2e2;font-size:.95rem}\n"
        " td.sha{font-family:monospace;font-size:.75rem;word-break:break-all}\n"
        "</style></head><body>\n"
        f"<h1>{_distro} {_ver}</h1>\n"
        f"<p class=\"muted\">Snapshot {_snap}</p>\n"
        "<h2>Install media</h2>\n"
        f"{_iso_table}\n"
        "<h2>APT repository</h2>\n"
        "<p>Add this line to <code>/etc/apt/sources.list.d/</code> "
        "(the keyring ships in the installed system):</p>\n"
        f"<pre>{_deb_line}</pre>\n"
        "<p class=\"muted\">Machine-readable manifest: "
        "<a href=\"releases.json\">releases.json</a></p>\n"
        "</body></html>\n"
    )


def render_release_files(manifest: dict) -> 'tuple[str, str]':
    """Convenience: ``(index_html, releases_json)`` from one manifest."""
    return render_index_html(manifest), render_releases_json(manifest)
