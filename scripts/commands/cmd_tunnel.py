"""Source sync + package tunnelling — the `source sync` / `source tunnel`
handlers.

cmd_source_sync / _sync_named_sources fetch upstream source packages into
dir_source; _do_tunnel / cmd_tunnel_package download upstream's prebuilt
.debs and normalise them to pristine / +asg on disk (republished, no
owner).  Extracted verbatim from BuildSession; see commands/base.py for
how the mixin shares session state.
"""
import logging
import os
import shutil
from typing import Optional

import tui
import utils
from buildlog import BuildLog, human_size, safe_size
from tui import console, Prompt, PROMPT_YESNO, ProgressBar

from commands.base import SessionState

logger = logging.getLogger('athena.build')


def _shorten_origin(url: str, max_len: int = 70) -> str:
    """Compact a long pool URL: keep the host and the last 5 path
    components, drop the middle.  No-op when under max_len."""
    if len(url) <= max_len:
        return url
    if '://' not in url:
        return url
    _scheme, _rest = url.split('://', 1)
    _host, _, _path = _rest.partition('/')
    _parts = [_p for _p in _path.split('/') if _p]
    if len(_parts) <= 5:
        return url
    return f"{_host}/.../{'/'.join(_parts[-5:])}"


class TunnelCommandsMixin(SessionState):
    # -----------------------------------Command: source_download--------------------

    def cmd_source_sync(self, *args):
        """Download upstream source archives.

        Bulk mode (no args) — fetch .dsc, .orig.tar.*, .debian.tar.* for
        every source in dep_tree.selected_srcs + udeb_dep_tree.selected_srcs.
        Skips files whose SHA256 already matches; sets `download_ready`.

        Per-pkg mode (`source sync <pkg> [<pkg>…] [force]`) — fetch
        just the named source(s).  `force` deletes existing files first,
        bypassing the SHA256-skip short-circuit (useful when a file is
        corrupt but its size+sha somehow still match what's expected).
        Doesn't touch `download_ready` — partial pulls aren't the
        full-corpus gate the flag tracks.

        Downloads from BOTH the deb tree AND the udeb tree in bulk
        mode.  Without the udeb pass, sources that exist only in the
        udeb closure (base-installer, debian-installer-utils,
        debootstrap, …) never land in dir_source, and a later
        `source build installer` fails with "cp: cannot stat
        /source/<pkg>*: No such file or directory" inside the build
        container.  Sources shared between trees are skipped in the
        second pass via the on-disk sha check.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return

        _force = 'force' in args
        _named = [a for a in args if a != 'force']

        if _named:
            return self._sync_named_sources(_named, _force)

        # Bulk path.
        self.flags.download_ready = False  # reset before starting

        assert self.dep_tree is not None
        _deb_size  = self.dep_tree.download_size
        _udeb_size = (self.udeb_dep_tree.download_size
                      if self.udeb_dep_tree is not None else 0)
        _src_download_size = _deb_size + _udeb_size
        console.print(
            f"Total download is about {_src_download_size // (2**20)} MB "
            f"(deb: {_deb_size // (2**20)} MB, udeb: {_udeb_size // (2**20)} MB)"
        )

        _total, _used, _free = shutil.disk_usage(self.config.dir_source)
        console.print(f"Disk space — Total: {_total // (2**30)} GiB, "
                      f"Used: {_used // (2**30)} GiB, Free: {_free // (2**30)} GiB")

        console.print("Starting downloads (deb tree)...")
        _downloaded_size = utils.download_source(self.dep_tree, self.config.dir_source)

        if self.udeb_dep_tree is not None and self.udeb_dep_tree.selected_srcs:
            console.print("Starting downloads (udeb tree)...")
            _downloaded_size += utils.download_source(
                self.udeb_dep_tree, self.config.dir_source)

        # A size mismatch usually means a network interruption or a package whose
        # expected size in the index differs from what the mirror actually served.
        if _src_download_size != _downloaded_size:
            _resp = Prompt(
                PROMPT_YESNO,
                "Download size mismatch, continue?",
                informational=True,
            ).get_response()
            if _resp.lower() not in ('y', 'yes'):
                return

        self.flags.download_ready = True

    def _sync_named_sources(self, named: 'list[str]', force: bool) -> None:
        """Per-source download path for `source sync <pkg> [force]`.

        Looks up each name in either dep_tree.selected_srcs or
        udeb_dep_tree.selected_srcs; constructs a synthetic minimal
        tree-shaped wrapper that exposes the two attributes
        utils.download_source reads (`selected_srcs` dict, integer
        `download_size`); runs the download.  Unknown names are
        reported and skipped — partial success is the design choice.
        """
        class _SingleSrcTree:
            def __init__(self, _name, _src):
                self.selected_srcs = {_name: _src}
                self.download_size = sum(
                    int(_f.get('size', 0)) for _f in _src.files.values()
                )

        for _name in named:
            _src = None
            for _tree in (self.dep_tree, self.udeb_dep_tree):
                if _tree is not None and _name in _tree.selected_srcs:
                    _src = _tree.selected_srcs[_name]
                    break
            if _src is None:
                console.print(
                    f"source sync {_name}: not in dep_tree.selected_srcs "
                    f"(run `cache parse` if you expect it to be there)",
                    tui.COLOR_WARNING,
                )
                continue
            if force:
                for _f in _src.files:
                    try:
                        os.unlink(os.path.join(self.config.dir_source, _f))
                    except OSError:
                        pass
            console.print(f"source sync {_name}: fetching "
                          f"{len(_src.files)} file(s)…")
            utils.download_source(
                _SingleSrcTree(_name, _src),  # type: ignore[arg-type]
                self.config.dir_source,
            )

    # --------------------------Internal helper: tunnel download------------------

    def _do_tunnel(self, src_pkg) -> bool:
        """Download upstream's prebuilt .deb files for src_pkg, then run
        the same post-build normalisation a from-source build would: strip
        NMU layers to pristine version + filename, and (when this is a
        delta and an asg ledger is loaded) stamp `+asg<R>u<N>`.

        Net effect: a tunneled binary is, on disk and in repo audit, a
        legitimately-built binary — pristine-named or +asg-stamped, byte-
        for-byte rebuilt by us.  The ONLY artefact preserved from the
        upstream form is `republished_from = {url, upstream_sha256}` on
        the build record (and the federation claim), which the mirror
        sidecar uses to mark the claim as "no owner" — the federation's
        ownership projection sees tunneled packages as un-owned without
        affecting any apt / repo-audit / source-audit interpretation.

        Returns True iff every output landed and normalised successfully.
        """
        # Upstream Filename: required for the snapshot.debian.org URL —
        # the pristine prediction names don't exist on the server.  We
        # rename after download.
        _upstream_files = self._tunnel_filenames_for_source(src_pkg.package)
        if not _upstream_files:
            logger.error(f"tunnel {src_pkg.package}: no binary packages known (run 'cache parse' first)")
            return False

        if src_pkg._mirror is None:
            logger.error(f"tunnel {src_pkg.package}: source has no _mirror — cache ingest bug")
            return False
        _base = src_pkg._mirror.url
        # Route tunneled binaries to their apt component (from the origin
        # mirror): non-free/contrib/non-free-firmware land in the matching
        # repo/dists/<codename>/<comp>/ dir, main stays main.  Empty/flat
        # component → main.
        _comp = src_pkg._mirror.component or 'main'
        _success = True

        import time as _time
        _buildlog_path = os.path.join(self.config.dir_log, 'build')
        _t_tunnel_start = _time.monotonic()
        # observability accumulators (tunnel path) — best-effort,
        # consumed by the verbose .buildlog written at the terminal.
        _purged_stale: 'list[str]' = []
        _stamp_events: 'list[tuple[str, str, str]]' = []
        try:
            # the tunnel entry record RECREATES the file —
            # carry the lifecycle layer (+ prior-build stash) through.
            utils.write_build_record(
                _buildlog_path,
                utils.preserve_lifecycle(
                    utils.read_build_record(_buildlog_path, src_pkg.package),
                    utils.new_build_record(
                        package=src_pkg.package,
                        intended_version=str(src_pkg.version),
                        patch_set_hash='',
                        component=_comp,
                    ),
                ),
            )
        except OSError as _e:
            logger.warning(f"tunnel {src_pkg.package}: build-record entry: {_e}")

        # Download phase: pull every upstream-named .deb to its routed
        # pool dir.  Hash each freshly-downloaded file BEFORE strip so
        # we can record the upstream SHA-256 (federation provenance).
        _upstream_paths: 'dict[str, str]' = {}
        _upstream_urls: 'dict[str, str]' = {}
        _upstream_sha256s: 'dict[str, str]' = {}
        # Cache each component dir's listing so the per-binary stale-file wipe
        # below doesn't re-listdir the dir once per binary (was quadratic for
        # large firmware sources that route many files into one pool dir).
        _dir_listings: 'dict[str, list]' = {}
        for _filename in _upstream_files:
            _dst_dir = self.config.deb_dest_for_filename(_filename, _comp)
            _dest = os.path.join(_dst_dir, _filename)

            # Stale-file wipe: same binary basename but a DIFFERENT pristine
            # version (e.g. a prior tunnel of an older upstream).  Files
            # whose pristine base matches the target's pristine base
            # (post-strip target, or +asg variant of it) are KEPT — they
            # are the legitimate skip-gate target downstream.
            _bin_name = _filename.split('_', 1)[0]
            _target_ver = _filename.split('_')[1]
            _target_pristine = utils.strip_nmu_suffix(_target_ver)
            _listing = _dir_listings.get(_dst_dir)
            if _listing is None:
                try:
                    _listing = os.listdir(_dst_dir)
                except OSError:
                    _listing = []
                _dir_listings[_dst_dir] = _listing
            for _existing in list(_listing):
                if not _existing.endswith(('.deb', '.udeb')):
                    continue
                if _existing.split('_', 1)[0] != _bin_name:
                    continue
                if _existing == _filename:
                    continue
                _ex_ver = _existing.split('_')[1]
                if utils.pristine_base(_ex_ver) == _target_pristine:
                    continue
                _stale = os.path.join(_dst_dir, _existing)
                logger.info(
                    f"tunnel {src_pkg.package}: removing stale "
                    f"non-matching {_existing} (target pristine "
                    f"{_target_pristine})")
                try:
                    os.remove(_stale)
                    _purged_stale.append(_existing)
                    _listing.remove(_existing)   # keep the cached listing current
                except OSError as _e:
                    logger.warning(
                        f"tunnel {src_pkg.package}: rm {_stale}: {_e}")

            if os.path.isfile(_dest):
                logger.info(f"tunnel {src_pkg.package}: {_filename} already present, skipping download")
                _upstream_paths[_filename] = _dest
                _upstream_urls[_filename] = (
                    f"{_base}/{src_pkg.directory}/{_filename}")
                _h = utils.get_sha256(_dest, use_cache=False)
                if _h:
                    _upstream_sha256s[_filename] = _h
                continue

            _url = f"{_base}/{src_pkg.directory}/{_filename}"
            _upstream_urls[_filename] = _url
            logger.info(f"tunnel {src_pkg.package}: downloading {_url}")
            _bytes, _detail = utils.download_file(_url, _dest)
            if _bytes < 0:
                logger.error(f"tunnel {src_pkg.package}: failed to download {_filename}: {_detail or 'unknown'}")
                _success = False
                continue
            _upstream_paths[_filename] = _dest
            _h = utils.get_sha256(_dest, use_cache=False)
            if _h:
                _upstream_sha256s[_filename] = _h

        # Normalisation phase: TRANSPOSE each tunnelled .deb in place, mirroring
        # BuildContainer._normalize_built_artifacts on the tunnel path.  A
        # trailing +debNuK on the control Version + deps becomes +asg<R>uK; any
        # trailing +bN is KEPT (tunnelled binaries aren't rebuilt, so their
        # frozen inter-binary `=` pins reference that +bN), and the signed
        # data.tar is never touched (control-only repack).  Tunnelled packages
        # are never patched or force-built, so P=0 / force_bn=None — no ledger.
        # final_paths is keyed by the FINAL on-disk filename; final_to_upstream
        # remembers which upstream filename each post-normalize file came
        # from so we can attach `republished_from` provenance.
        # [Build] VERSION is the transpose release ordinal R.  A non-integer
        # DISABLES the transpose — we then ship AND record the upstream-named
        # debs as-is (no +debNuK → +asg<R>uK rewrite, no strip-to-pristine).
        # Parsed here (not inside the success block) so it stays in scope for
        # the build-record + narrative below.
        try:
            _release: 'Optional[int]' = int(
                str(self.config.build_version).strip('"').strip("'"))
        except (TypeError, ValueError):
            if _success and _upstream_paths:
                logger.warning(
                    "tunnel transpose: [Build] VERSION not an integer "
                    f"({self.config.build_version!r}) — shipping upstream "
                    f"version for {src_pkg.package}")
            _release = None

        # Bounds targeting a tunnelled binary must KEEP their +bN/backport
        # layer (the target ships it verbatim): full tunnelled set from the
        # cache, unioned with THIS source's own binary names so the frozen
        # sibling `=` pins are covered even pre-`cache parse`.
        _keep_bn = set(utils.tunneled_binary_names(
            self.config, getattr(self, 'cache', None)))
        for _ups_fn in (_upstream_paths or {}):
            _pf = utils.parse_deb_filename(_ups_fn)
            if _pf is not None:
                _keep_bn.add(_pf[0])

        _final_paths: 'dict[str, str]' = {}
        _final_to_upstream: 'dict[str, str]' = {}
        _stamps_count = 0
        if _success and _upstream_paths:
            for _ups_fn, _ups_path in _upstream_paths.items():
                _final_path = _ups_path
                if _release is not None:
                    _b = os.path.basename(_ups_path)
                    try:
                        _r = utils.transpose_deb(
                            _ups_path, 'asg', _release,
                            keep_binnmu_names=frozenset(_keep_bn))
                        _new_path = _r.get('new_path', _ups_path)
                        if (_r.get('status') == 'rewritten'
                                and _new_path != _ups_path):
                            _stamps_count += 1
                            logger.info(
                                f"tunnel transpose: {_b} → "
                                f"{os.path.basename(_new_path)}")
                            _stamp_events.append((
                                _b, os.path.basename(_new_path),
                                str(_r.get('version', ''))))
                        _final_path = _new_path
                    except Exception as _e:
                        logger.warning(f"tunnel transpose: {_b} failed: {_e}")
                _final_fn = os.path.basename(_final_path)
                _final_paths[_final_fn] = _final_path
                _final_to_upstream[_final_fn] = _ups_fn

        # Build-record terminal: outputs/output_hashes are FINAL
        # post-normalize names + SHA-256 of the rewritten on-disk file.
        # republished_from provenance keys by FINAL name → upstream URL +
        # upstream SHA-256 (pre-strip — the actual hash at the remote URL).
        _output_hashes: 'dict[str, str]' = {}
        if _success:
            for _fn, _dst in _final_paths.items():
                _h = utils.get_sha256(_dst, use_cache=False)
                if _h:
                    _output_hashes[_fn] = _h
        _republished_from: 'dict[str, dict]' = {}
        if _success:
            for _final_fn, _ups_fn in _final_to_upstream.items():
                _ups_url = _upstream_urls.get(_ups_fn)
                _ups_sha = _upstream_sha256s.get(_ups_fn)
                if not _ups_url or not _ups_sha:
                    continue
                _republished_from[_final_fn] = {
                    'url':             _ups_url,
                    'upstream_sha256': _ups_sha,
                }

        _outputs_sorted = sorted(_final_paths.keys()) if _final_paths \
            else sorted(_upstream_files)
        try:
            # resolve the prior-build stash — a re-tunnel at a
            # new version rolls the old episode into history as 'obsolete'.
            # When transpose is active we record the PRISTINE base (the +asg
            # stamp is a generation layer on disk); when it's DISABLED
            # (_release is None) the deb ships upstream-named, so the record
            # must carry that same un-stripped upstream version.
            _tunnel_built_ver = (
                utils.strip_nmu_suffix(str(src_pkg.version))
                if _release is not None else str(src_pkg.version))
            if _success:
                utils.roll_prior_build_history(
                    _buildlog_path, src_pkg.package, _tunnel_built_ver)
            utils.update_build_record(
                _buildlog_path, src_pkg.package,
                phase=('tunneled' if _success else 'failed'),
                built_version=(_tunnel_built_ver if _success else None),
                finished=utils._utc_now_iso(),
                elapsed_seconds=round(_time.monotonic() - _t_tunnel_start, 3),
                output_count=len(_outputs_sorted),
                outputs=_outputs_sorted,
                output_hashes=_output_hashes,
                republished_from=_republished_from,
            )
        except (OSError, FileNotFoundError) as _e:
            logger.warning(f"tunnel {src_pkg.package}: build-record terminal: {_e}")

        # verbose tunnel narrative (log/build/<pkg>.buildlog).
        # Fully guarded — never reaches the tunnel control flow.
        try:
            _elapsed_t = round(_time.monotonic() - _t_tunnel_start, 3)
            _tblog = BuildLog(_buildlog_path, src_pkg.package, kind='tunnel')
            _tblog.header(
                status=('TUNNELED' if _success else 'FAIL'),
                intended_version=str(src_pkg.version),
                arch=self.config.arch,
                component=_comp,
                base_url=_base,
            )
            _tblog.section(
                f"EXPECTED (upstream binaries: {len(_upstream_files)})")
            for _uf in sorted(_upstream_files):
                _tblog.bullet(_uf)

            _tblog.section(
                f"DOWNLOADED ({len(_upstream_paths)})")
            if _upstream_paths:
                for _uf in sorted(_upstream_paths):
                    _tblog.file(
                        _uf, size=safe_size(_upstream_paths[_uf]),
                        sha256=_upstream_sha256s.get(_uf, ''))
            else:
                _tblog.empty()

            _tblog.section(f"PURGED stale ({len(_purged_stale)})")
            if _purged_stale:
                for _ps in sorted(_purged_stale):
                    _tblog.bullet(_ps)
            else:
                _tblog.empty()

            _tblog.section(f"TRANSPOSE ({len(_stamp_events)})")
            if _stamp_events:
                for _old, _new, _tag in sorted(_stamp_events):
                    _tblog.bullet(f"{_old}  →  {_new}  ({_tag})")
            else:
                _tblog.empty()

            _tblog.section(
                f"FINAL ARTIFACTS (post-normalize: {len(_final_paths)})")
            _tot = 0
            if _final_paths:
                for _fn in sorted(_final_paths):
                    _sz = safe_size(_final_paths[_fn])
                    if _sz >= 0:
                        _tot += _sz
                    _prov = ' republished' if _fn in _republished_from else ''
                    _tblog.file(
                        _fn, size=_sz, sha256=_output_hashes.get(_fn, ''),
                        detail=_prov.strip())
            else:
                _tblog.empty()

            _tblog.footer(
                status=('TUNNELED' if _success else 'FAIL'),
                files=len(_final_paths),
                size=human_size(_tot),
                elapsed=f"{_elapsed_t}s")
            _tblog.write()
        except Exception as _e:
            logger.warning(f"tunnel buildlog {src_pkg.package}: {_e}")

        if _success:
            _upstream_ver = str(src_pkg.version)
            _pristine_ver = utils.strip_nmu_suffix(_upstream_ver)
            _total_bytes = sum(
                max(safe_size(_dst), 0) for _dst in _final_paths.values())
            if _release is None:
                # transpose disabled (non-integer VERSION): we ship the
                # upstream-named deb as-is — no '→ pristine' arrow, since it
                # was NOT stripped/transposed.
                _ver_line = f"{_upstream_ver} (upstream; transpose disabled)"
            elif _pristine_ver == _upstream_ver and _stamps_count == 0:
                _ver_line = f"{_pristine_ver} (pristine)"
            else:
                _ver_line = f"{_upstream_ver} → {_pristine_ver}"
            console.print(
                f"  {src_pkg.package}  TUNNELED  {_ver_line}")
            console.print(
                f"    files     {len(_outputs_sorted)}  "
                f"({human_size(_total_bytes)}, "
                f"{_stamps_count} transposed)")
            _pool_dir: 'Optional[str]' = None
            for _fn in _outputs_sorted:
                _dst = _final_paths.get(_fn, '')
                _rel = os.path.relpath(_dst, self.config.dir_repo) \
                    if _dst else _fn
                _dir_part, _base = os.path.split(_rel)
                if _pool_dir is None:
                    _pool_dir = _dir_part
                    console.print(f"    pool      {_pool_dir}/")
                console.print(f"            + {_base}")
            if _upstream_urls:
                _origin = sorted(_upstream_urls.values())[0].rsplit('/', 1)[0]
                console.print(f"    origin    {_shorten_origin(_origin)}/")

        return _success


    # --------------------------Command: tunnel_package--------------------------

    def cmd_tunnel_package(self, *args):
        """Download prebuilt binary .debs from the base Debian repo for named packages.

        Usage: tunnel_package [pkg ...]

        If no package names are given, uses the 'Tunneled' list from build.conf.
        Packages must already be present in the dependency tree (run `cache
        parse` first).  Every named package is re-tunnelled: the download is
        SHA256-skipped when the on-disk .deb already matches, but the package
        is always re-stamped (pristine/+asg) and its result re-recorded.
        """
        if not self.flags.dep_check_ready:
            console.print("Run 'cache parse' first")
            return

        # Fall back to the config list if no names were given on the command line.
        _names = list(args) if args else self.config.tunnel_packages
        if not _names:
            console.print("No packages specified and Tunneled list in build.conf is empty")
            return

        # Validate all names up front before starting any downloads.
        packages = []
        for name in _names:
            assert self.dep_tree is not None
            src = self.dep_tree.selected_srcs.get(name)
            if src is None:
                console.print(f"Unknown package: {name}")
                return
            packages.append(src)

        _success = _failed = 0
        _failed_names: 'list[str]' = []
        progress_bar = ProgressBar(label='Tunnel', maxvalue=len(packages), show_rate=False)

        for _src_pkg in packages:
            _result = self._do_tunnel(_src_pkg)
            if _result:
                logger.warning(f"Tunnel {_src_pkg.package} [TUNNELED]")
                _success += 1
            else:
                logger.error(f"Tunnel {_src_pkg.package} [FAIL]")
                _failed += 1
                _failed_names.append(_src_pkg.package)
            progress_bar.step(1)
        progress_bar.close()

        console.print(
            f"Tunnel complete: {_success} tunneled, {_failed} failed "
            f"(of {len(packages)} requested)")
        if _failed_names:
            console.print(
                f"  failed: {', '.join(_failed_names)}  "
                f"(see log/build/<pkg> for details)")
