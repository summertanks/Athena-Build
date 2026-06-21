"""Mirror federation — the `mirror` command cluster.

Manage federated publish-target mirrors, signed claims, ownership and the
build host's view of the federation: add/remove/list/summary, publish,
pull, audit, query, reconcile, status, init, builders, conflict.  Coord
helpers (_coord_builder_id / _coord_self_keys) live here too — they serve
the mirror surface.  Extracted verbatim from build.py's BuildSession; see
commands/base.py for how the mixin shares session state.
"""
import logging
import os
import re
from typing import Optional

import tui
import utils
from tui import console, Prompt, PROMPT_YESNO, ProgressBar

from commands.base import SessionState

logger = logging.getLogger('athena.build')


def _print_audit_finding(_sev: str, _kind: str, _msg: str, _color) -> None:
    """Render one `mirror audit` finding.  A newline in *_msg* starts a
    structured multi-line body whose continuation lines align under the kind
    (column 12), so a verbose finding reads as `field  value` rows instead of
    one unreadable wrapped line."""
    _lines = str(_msg).split('\n')
    console.print(f"  {_sev:8s}  {_kind}: {_lines[0]}", _color)
    for _cont in _lines[1:]:
        console.print(f"            {_cont}", _color)


class MirrorCommandsMixin(SessionState):
    # ─────────────────────────────────────────────────────────────────────
    # Mirror umbrella — remote-endpoint federation
    # ─────────────────────────────────────────────────────────────────────

    def cmd_mirror(self, action: str = '', *args):
        """Manage federated publish-target mirrors and the build host's
        signed-claim identity.

        Per-mirror durable state lives at config/mirror.<name>.state.
        Per-builder Ed25519 identity (signed by tier-1 GPG via the
        federation coord-head) lives at coord/identity/.
        """
        if action == 'init':
            return self.cmd_mirror_init(*args)
        if action == 'add':
            return self.cmd_mirror_add(*args)
        if action == 'remove' or action == 'delete':
            return self.cmd_mirror_remove(*args)
        if action == 'list':
            return self.cmd_mirror_list(*args)
        if action == 'summary':
            return self.cmd_mirror_summary(*args)
        if action == 'status':
            return self.cmd_mirror_status(*args)
        if action == 'reconcile-neighbours':
            return self.cmd_mirror_reconcile_neighbours(*args)
        if action == 'publish':
            return self.cmd_mirror_publish(*args)
        if action == 'pull':
            return self.cmd_mirror_pull(*args)
        if action == 'reclaim':
            return self.cmd_mirror_reclaim(*args)
        if action == 'withdraw-foreign':
            return self.cmd_mirror_withdraw_foreign(*args)
        if action == 'audit':
            return self.cmd_mirror_audit(*args)
        if action == 'query':
            return self.cmd_mirror_query(*args)
        if action == 'builders':
            return self.cmd_mirror_builders(*args)
        if action == 'conflict':
            return self.cmd_mirror_conflict(*args)
        _table = {
            'init <id>':                   'generate Ed25519 builder identity + '
                                           'persist BUILDER_ID',
            'add <name> <url>':            'register a mirror; seeds base+current',
            'remove <name|url>':           'unregister a mirror',
            'list':                        'one-line-per-mirror inventory',
            'summary [<name>]':            'full per-mirror state',
            'status [<name>]':             'health overview + builder identity + '
                                           'halt sentinel',
            'reconcile-neighbours [<n>]':  'fan-out: align every peer\'s '
                                           'coord-head.neighbours with local '
                                           'config; re-sign + push',
            'publish [<name>] [--no-iso]':  'per-file .deb push + sign + push '
                                           'claims + re-sign coord-head + '
                                           'publish release index/ISOs '
                                           '(federation-gated; requires '
                                           'current-snapshot ISOs unless '
                                           '--no-iso)',
            'pull [<name>]':               'fetch + verify peer sidecar, then '
                                           'download claim .debs missing locally '
                                           '(skip-own; SHA-256 verified)',
            'reclaim [<src>|<file>] [<name>] [force]':
                                           'supersede OUR published claim for the '
                                           'SAME filename with the local rebuild '
                                           '(sanctioned filename-immutability '
                                           'exception; no target = list candidates)',
            'withdraw-foreign [<name>] [--execute]':
                                           'retract + prune OUR published '
                                           'foreign-target cross-toolchain '
                                           'binaries (dry-run without --execute)',
            'audit [<name>]':              'federation consistency, claim sigs, '
                                           'hash conflicts, cross-mirror pool drift',
            'query <pkg> [<name>]':        'show claims matching <pkg> from '
                                           'the last fetched view of each mirror',
            'builders <list|register>':    'federation builder registry: list, '
                                           'or register this builder on a '
                                           'mirror (upload pubkey + adopt the '
                                           'canonical lists)',
            'conflict resolve <pkg>':      'retract our claim for <pkg>; clear '
                                           'PUBLISH_HALT',
        }
        return self._group_help('mirror', _table, action)

    _MIRROR_ADD_USAGE = (
        "Usage: mirror add <ip|fqdn|local> <ssh-or-file-url> "
        "[--ssh-key PATH] [--proto http|https] [--name NAME] "
        "[--no-probe] [--yes]"
    )

    def _parse_mirror_add_args(self, args):
        """Parse the new `mirror add` argv into a flat dict.  Returns
        (parsed, error_or_empty).  Keeps the orchestrator readable."""
        if len(args) < 2:
            return None, self._MIRROR_ADD_USAGE
        _host_type = args[0]
        _url = args[1]
        _parsed = {
            'host_type':  _host_type,
            'url':        _url,
            'ssh_key':    None,
            'proto':      None,
            'name':       None,
            'no_probe':   False,
            'yes':        False,
        }
        _i = 2
        while _i < len(args):
            _a = args[_i]
            if _a in ('--ssh-key',) and _i + 1 < len(args):
                _parsed['ssh_key'] = args[_i + 1]
                _i += 2
            elif _a in ('--proto', '-proto') and _i + 1 < len(args):
                _parsed['proto'] = args[_i + 1]
                _i += 2
            elif _a in ('--name',) and _i + 1 < len(args):
                _parsed['name'] = args[_i + 1]
                _i += 2
            elif _a == '--no-probe':
                _parsed['no_probe'] = True
                _i += 1
            elif _a == '--yes':
                _parsed['yes'] = True
                _i += 1
            else:
                return None, f"unknown argument {_a!r}\n{self._MIRROR_ADD_USAGE}"
        return _parsed, ''

    def cmd_mirror_add(self, *args):
        """mirror add <ip|fqdn|local> <ssh-or-file-url> [--ssh-key PATH]
                       [--proto http|https] [--name NAME]
                       [--no-probe] [--yes]

        Register a new publish-target mirror with reachability + sidecar
        probing, federation discovery, and an operator-visible
        sources.list preview.

        Required gate before any probing: the local tier-1 signing key
        must be present + verifiable (`key generate` / `key verify`).

        The `ip` / `fqdn` keyword constrains what shape the URL's host
        portion may take.  `local` is for `file://` publish mirrors and
        bypasses every network probe.

        The mirror name auto-derives from the URL host (dots/colons → '-');
        operator override with `--name`.

        --proto: REQUIRED for ssh:// URLs.  The chroot writes
        `<proto>://<host>/<dist-id>` into `sources.list.d/athena-<n>.list`.

        --no-probe skips DNS/TCP/SSH/HTTP probes (dev / offline testing);
        sidecar pull + federation discovery still run if reachable.

        --yes accepts the federation-join summary without prompting.
        Refused state files (key-verify failure, unknown peer-key) are
        NEVER bypassed by --yes; only the final accept-or-not prompt is.

        Default for an unreachable discovered neighbour is
        "take-control-and-drop" — the new mirror joins the federation
        without that peer, and `mirror reconcile-neighbours` propagates
        the shrunk membership to the remaining reachable peers.
        """
        import mirror as _mirror
        import signing
        _parsed, _err = self._parse_mirror_add_args(args)
        if _parsed is None:
            console.print(_err, tui.COLOR_ERROR)
            return False

        # ── Gate 1: signing key must be verifiable ─────────────────
        _key_ok, _key_msg = signing.verify_key(self.config)
        if not _key_ok:
            console.print(
                "mirror add: REFUSED — tier-1 signing key not verifiable "
                f"({_key_msg}).  Run `key generate` then `key verify` "
                "before registering a mirror.",
                tui.COLOR_ERROR)
            return False
        console.print(
            f"signing key ok: {_key_msg}", tui.COLOR_INFO)

        # ── Step 1: host-type keyword × URL shape sanity ───────────
        _host_type = _parsed['host_type']
        _url_raw = _parsed['url']
        _normalised = _mirror._normalize_url(_url_raw)
        if _normalised is None:
            console.print(
                f"mirror add: invalid URL {_url_raw!r} "
                "(publish URLs must be ssh:// or file:///abs/path).",
                tui.COLOR_ERROR)
            return False
        _ok, _det = _mirror.validate_host_for_type(_normalised, _host_type)
        if not _ok:
            console.print(f"mirror add: {_det}", tui.COLOR_ERROR)
            return False

        # ── Step 2: --proto required for ssh, forbidden for local ──
        _proto = _parsed['proto']
        _is_ssh = _normalised.startswith('ssh://')
        if _is_ssh and _proto not in _mirror.VALID_PROTOS:
            console.print(
                "mirror add: ssh:// publish mirrors require --proto "
                f"http|https (got {_proto!r}).  The chroot needs an "
                "apt-readable URL to write into sources.list.d.",
                tui.COLOR_ERROR)
            return False
        if not _is_ssh and _proto is not None:
            console.print(
                "mirror add: --proto is only meaningful for ssh:// "
                "mirrors (file:// publish targets aren't dereferenced "
                "by apt over the network).",
                tui.COLOR_WARNING)
            _proto = None

        # ── Step 3: derive name + public_url + host ────────────────
        _name = _parsed['name'] or _mirror.derive_name_from_url(
            _normalised, _host_type)
        if not _name:
            console.print(
                "mirror add: could not auto-derive a mirror name from "
                f"{_normalised!r}; pass --name <name> explicitly.",
                tui.COLOR_ERROR)
            return False
        _host = _mirror._extract_host_from_ssh_url(_normalised) or ''
        _public_url = ''
        if _is_ssh and _proto:
            _public_url = _mirror.derive_public_url(
                _normalised, self.config.build_base_id, _proto) or ''
            if not _public_url:
                console.print(
                    "mirror add: could not derive public URL — check "
                    f"[Build] DISTRIBUTION ({self.config.build_base_id!r}) "
                    "and the --proto value.",
                    tui.COLOR_ERROR)
                return False

        console.print(
            f"derived: \tname={_name}  \n\t\turl={_normalised}  "
            f"\n\t\thost={_host or '(n/a)'}  \n\t\tpublic_url={_public_url or '(n/a)'}",
            tui.COLOR_HIGHLIGHT)

        # ── Step 4: refuse early on duplicates ─────────────────────
        if _mirror.read_mirror_state(self.config, _name) is not None:
            console.print(
                f"mirror add: a mirror named {_name!r} is already "
                "registered locally.", tui.COLOR_ERROR)
            return False
        for _other in _mirror.list_mirrors(self.config):
            _ost = _mirror.read_mirror_state(self.config, _other)
            if _ost and _ost.get('url') == _normalised:
                console.print(
                    f"mirror add: URL {_normalised!r} is already "
                    f"registered as {_other!r}.", tui.COLOR_ERROR)
                return False

        # ── Step 5: reachability + ssh + permissions + http probes ──
        _signing_homedir = signing.signing_home(self.config)
        if _is_ssh and not _parsed['no_probe']:
            _ssh_user = _mirror._extract_user_from_ssh_url(_normalised) or ''
            _pool_path = _mirror._extract_path_from_ssh_url(_normalised) or ''
            _ok = self._mirror_add_run_ssh_probes(
                host=_host, user=_ssh_user, ssh_key=_parsed['ssh_key'],
                pool_path=_pool_path,
                public_url=_public_url, proto=_proto or 'http',
            )
            if not _ok:
                console.print(
                    "mirror add: probes failed.  Pass `--no-probe` to "
                    "skip them (dev / offline).", tui.COLOR_ERROR)
                return False
        elif _parsed['no_probe']:
            console.print(
                "  probes skipped (--no-probe)", tui.COLOR_WARNING)

        # ── Step 5b: prepared-mirror gate ──────────────────────────
        # A publish target must be PREPARED (prep-mirror.sh: web server +
        # dir layout + the mirror-info.json marker) before we register it —
        # registering an unprepared host invites every "remote dir missing
        # / not serving" boundary case at publish time.  Probe the HTTP
        # marker (ssh mirrors with a public_url only; skippable --no-probe).
        if _is_ssh and _public_url and not _parsed['no_probe']:
            _prep_ok, _prep_detail = self._mirror_is_prepared(_public_url)
            if not _prep_ok:
                console.print(
                    f"mirror add: {_host} is NOT prepared as an Asgard "
                    f"mirror ({_prep_detail}).  Prepare it first:\n"
                    f"    ./prep-mirror.sh {_normalised} <ssh-key>\n"
                    "(or pass `--no-probe` to skip this check).",
                    tui.COLOR_ERROR)
                return False
            console.print(
                f"  mirror prepared (since {_prep_detail or '?'})",
                tui.COLOR_HIGHLIGHT)

        # ── Step 6: sidecar discovery (ssh mirrors only) ───────────
        _peer_head: 'Optional[dict]' = None
        _discovered: 'list[dict]' = []
        if _is_ssh:
            _coord_url = _mirror.coord_root_for(_normalised)
            _stage_root = os.path.join(
                self.config.dir_cache, 'mirror', _name, 'probe')
            _ok, _det, _peer_head = _mirror.probe_sidecar_head(
                _coord_url, signing_homedir=_signing_homedir,
                stage_dir=os.path.join(_stage_root, 'self'),
                ssh_key=_parsed['ssh_key'])
            console.print(
                f"sidecar: {_det}",
                tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
            if not _ok:
                # The signing-key-mismatch gate fires here.  No way past
                # this — operator must import the federation's key.
                return False
            if _peer_head is not None:
                self._mirror_add_print_head_summary(_peer_head)
                _discovered = _mirror.discover_federation_peers(
                    _peer_head, signing_homedir=_signing_homedir,
                    stage_root=_stage_root, ssh_key=_parsed['ssh_key'])

        # ── Step 7: dedup against local config + classify peers ────
        _local_urls = set(_mirror.all_mirror_urls(self.config))
        _net_new_peers: 'list[dict]' = []
        _dropped: 'list[dict]' = []
        for _p in _discovered:
            if _p['url'] in _local_urls:
                continue
            if _p['url'] == _normalised:
                continue  # the peer we're registering itself
            if not _p['reachable']:
                _dropped.append(_p)
                continue
            _net_new_peers.append(_p)

        self._mirror_add_print_join_summary(
            primary_name=_name, primary_url=_normalised,
            primary_public_url=_public_url,
            net_new=_net_new_peers, dropped=_dropped,
            proto=_proto)

        # ── Step 8: operator confirm ──────────────────────────────
        if not _parsed['yes']:
            _resp = Prompt(
                PROMPT_YESNO,
                f"Register {_name!r}"
                + (f" + {len(_net_new_peers)} discovered peer(s)"
                   if _net_new_peers else "")
                + (f" (dropping {len(_dropped)} unreachable)"
                   if _dropped else "")
                + "?",
            ).get_response()
            # a YESNO get_response() returns 'y'/'n'/'yes'/'no' —
            # ALL truthy, so a bare truthiness check only caught a missing
            # TUI backend (empty string); answering "n" fell straight
            # through to registration.  Decline on anything that isn't an
            # explicit yes (matches every other YESNO site).
            if _resp.lower() not in ('y', 'yes'):
                console.print(
                    "mirror add: aborted by operator.", tui.COLOR_WARNING)
                return False

        # ── Step 9: write state files ──────────────────────────────
        _seed = self._snapshot_current() or ''
        _ok, _detail = _mirror.add_mirror(
            self.config, name=_name, url=_normalised,
            type=('ssh' if _is_ssh else 'local'),
            ssh_key=_parsed['ssh_key'], seed_pin=_seed,
            host=_host, host_type=_host_type,
            public_proto=_proto or '', public_url=_public_url,
        )
        console.print(
            f"mirror add: {_detail}",
            tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
        if not _ok:
            return False

        # Net-new peers discovered via the federation join.  Each one
        # inherits the operator's SSH key (Phase 7 keeps keys
        # homogeneous).  Per-peer apt URL preference:
        #   1. UPSTREAM's v3 record (peer's own public_url / public_proto
        #      signed into the federation head) — federation
        #      source-of-truth.
        #   2. Local derivation from operator's --proto.
        # v2-shaped upstream peers surface empty meta → fall back to (2).
        for _peer in _net_new_peers:
            _peer_url = _peer['url']
            if not _peer_url.startswith('ssh://'):
                console.print(
                    f"  skip non-ssh peer {_peer_url} from federation "
                    "discovery (manual `mirror add` required for these)",
                    tui.COLOR_WARNING)
                continue
            _peer_name = _mirror.derive_name_from_url(_peer_url, 'fqdn') or \
                _mirror.derive_name_from_url(_peer_url, 'ip')
            if not _peer_name:
                console.print(
                    f"  skip peer {_peer_url} — could not derive name",
                    tui.COLOR_WARNING)
                continue
            if _mirror.read_mirror_state(self.config, _peer_name):
                continue
            _peer_host = _mirror._extract_host_from_ssh_url(_peer_url) or ''
            _peer_host_type = ('ip' if _mirror._is_valid_ip(_peer_host)
                               else 'fqdn')
            # Prefer upstream's signed per-peer meta; fall back to
            # operator's --proto for v2-shaped peers (empty fields).
            _peer_proto = (_peer.get('public_proto')
                           or _proto or 'http')
            _peer_public_url = (
                _peer.get('public_url')
                or _mirror.derive_public_url(
                    _peer_url, self.config.build_base_id, _peer_proto)
                or '')
            _pok, _pdet = _mirror.add_mirror(
                self.config, name=_peer_name, url=_peer_url, type='ssh',
                ssh_key=_parsed['ssh_key'], seed_pin=_seed,
                host=_peer_host, host_type=_peer_host_type,
                public_proto=_peer_proto, public_url=_peer_public_url,
            )
            console.print(
                f"  peer {_peer_name}: {_pdet}",
                tui.COLOR_INFO if _pok else tui.COLOR_WARNING)

        # ── Step 10: propagate via reconcile-neighbours ───────────
        if _is_ssh:
            console.print(
                "propagating federation membership "
                "(`mirror reconcile-neighbours`)…", tui.COLOR_INFO)
            self.cmd_mirror_reconcile_neighbours()
        return True

    def _mirror_add_run_ssh_probes(
        self, *, host: str, user: str, ssh_key: 'Optional[str]',
        pool_path: str, public_url: str, proto: str,
    ) -> bool:
        """Per-step probe runner (DNS/TCP → SSH auth → write perms →
        HTTP InRelease).  Returns True iff every step ok; prints a
        progress line per step.  Extracted from cmd_mirror_add for
        readability.

        `user` and `pool_path` are extracted from the operator-supplied
        ssh URL upstream (`_extract_user_from_ssh_url` /
        `_extract_path_from_ssh_url`).  Without a user we'd default to
        the LOCAL invocation's username, which is almost never the
        publish-target's account."""
        import mirror as _mirror

        # 5a — DNS + TCP probe on ssh port
        _ok, _det = _mirror.probe_dns_and_tcp(host, 22)
        console.print(
            f"  ssh reachability ({host}:22): {_det}",
            tui.COLOR_INFO if _ok else tui.COLOR_ERROR)
        if not _ok:
            return False

        # 5b — SSH auth probe (BatchMode=yes, echo round-trip)
        _ok, _det = _mirror.probe_ssh_auth(host, user, ssh_key)
        console.print(
            f"  ssh auth ({user or '(local-user)'}@{host}): {_det}",
            tui.COLOR_INFO if _ok else tui.COLOR_ERROR)
        if not _ok:
            return False

        # 5c — Remote write probe: ensure pool + coord dirs exist and
        # are writable by the ssh user.  No-op when pool_path couldn't
        # be derived (operator gave a userless ssh URL or similar);
        # the next ssh-probe failure will surface the same issue.
        if pool_path:
            _ok, _det = _mirror.probe_remote_writable(
                host, user, ssh_key, pool_path)
            console.print(
                f"  remote write ({pool_path} + -coord): {_det}",
                tui.COLOR_INFO if _ok else tui.COLOR_ERROR)
            if not _ok:
                return False

        # 5d — HTTP probe on public_url
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _ok, _det = _mirror.probe_http_inrelease(public_url, _codename)
        console.print(
            f"  apt URL ({proto}://{host}): {_det}",
            tui.COLOR_INFO if _ok else tui.COLOR_ERROR)
        if not _ok:
            return False
        return True

    def _mirror_add_print_head_summary(self, head: dict) -> None:
        """Render the peer's existing coord-head — operator sees what
        they're joining before deciding."""
        console.print(
            "\nexisting coord-head on this peer:", tui.COLOR_HIGHLIGHT)
        _ts = head.get('head_time') or '(unknown)'
        _ir = head.get('inrelease_sha256') or '(unknown)'
        _ls = head.get('last_seqs') or {}
        _nb = head.get('neighbours') or []
        console.print(f"  head_time:       {_ts}")
        console.print(f"  inrelease_sha:   {_ir[:16]}…")
        _seqs = ', '.join(f'{_b}={_s}' for _b, _s in _ls.items()) or '(none yet)'
        console.print(f"  builders/seqs:   {_seqs}")
        console.print(f"  neighbours ({len(_nb)}):")
        for _u in _nb:
            console.print(f"    - {_u}")

    def _mirror_add_print_join_summary(
        self, *, primary_name: str, primary_url: str,
        primary_public_url: str,
        net_new: 'list[dict]', dropped: 'list[dict]',
        proto: 'Optional[str]',
    ) -> None:
        """The "this is what would happen" preview the operator confirms.
        Lists every state file we'd write + every sources.list.d entry
        the next chroot build would emit."""
        import mirror as _mirror
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _keyring = '/usr/share/keyrings/athena-archive-keyring.gpg'
        console.print(
            "\nproposed sources.list (after add):", tui.COLOR_HIGHLIGHT)
        # Existing mirrors
        for _n in _mirror.list_mirrors(self.config):
            _st = _mirror.read_mirror_state(self.config, _n) or {}
            _apt = _st.get('public_url') or _st.get('url') or ''
            if _apt:
                console.print(
                    f"  athena-{_n}.list: deb [signed-by={_keyring}] "
                    f"{_apt} {_codename} main", tui.COLOR_NORMAL)
        # Primary being added
        _apt = primary_public_url or primary_url
        console.print(
            f"+ athena-{primary_name}.list: deb [signed-by={_keyring}] "
            f"{_apt} {_codename} main", tui.COLOR_INFO)
        # Net-new discovered peers — prefer upstream's signed per-peer
        # apt URL (v3 record) over re-deriving from the operator's --proto.
        for _peer in net_new:
            _u = _peer['url']
            _peer_proto = (_peer.get('public_proto')
                           or proto or 'http')
            _peer_public = (
                _peer.get('public_url')
                or _mirror.derive_public_url(
                    _u, self.config.build_base_id, _peer_proto)
                or _u)
            _peer_name = _mirror.derive_name_from_url(_u, 'fqdn') or \
                _mirror.derive_name_from_url(_u, 'ip') or '?'
            console.print(
                f"+ athena-{_peer_name}.list: deb [signed-by={_keyring}] "
                f"{_peer_public} {_codename} main", tui.COLOR_INFO)
        if dropped:
            console.print(
                f"\nDROPPING {len(dropped)} unreachable peer(s) from the "
                "federation (default policy — take-control-and-drop):",
                tui.COLOR_WARNING)
            for _peer in dropped:
                console.print(
                    f"  - {_peer['url']}: {_peer['detail']}",
                    tui.COLOR_WARNING)
            console.print(
                "  `mirror reconcile-neighbours` will propagate the "
                "shrunk membership to all remaining reachable peers.",
                tui.COLOR_WARNING)

    def cmd_mirror_remove(self, *args):
        """mirror remove <name|url> — unregister this mirror from local
        state.  Run `mirror reconcile-neighbours` after to drop the URL
        from every remaining peer's coord-head.neighbours."""
        import mirror as _mirror
        if not args:
            console.print(
                "Usage: mirror remove <name|url>", tui.COLOR_ERROR)
            return False
        _ok, _detail = _mirror.remove_mirror(self.config, url_or_name=args[0])
        console.print(
            f"mirror remove: {_detail}",
            tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
        if _ok:
            console.print(
                "  (Run `mirror reconcile-neighbours` to propagate the "
                "removal to remaining peers' coord-head.neighbours.)")
        return _ok

    def cmd_mirror_list(self, *args):
        """One-line-per-mirror inventory: name, type, federation-consistency
        tag, url.  The tag compares the peer's last-seen coord-head
        neighbours against the local config's mirror URL set —
        `in-sync` / `drift` / `unpublished` (first publish will
        bootstrap)."""
        del args
        import mirror as _mirror
        _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print("mirror list: no mirrors configured.")
            return True
        console.print(f"Mirrors ({len(_names)}):")
        _drift_seen = False
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n) or {}
            _url = _st.get('url') or '?'
            _type = _st.get('type') or '?'
            _tag, _missing, _extra = _mirror.neighbours_drift(self.config, _n)
            _color = (tui.COLOR_WARNING if _tag == 'drift'
                      else tui.COLOR_HIGHLIGHT if _tag == 'in-sync'
                      else tui.COLOR_NORMAL)
            console.print(
                f"  {_n:<24s}  [{_type:<6s}]  {_tag:<11s}  {_url}",
                _color)
            if _tag == 'drift':
                _drift_seen = True
                if _missing:
                    console.print(
                        f"    missing on peer: {', '.join(_missing)}")
                if _extra:
                    console.print(
                        f"    extra on peer:   {', '.join(_extra)}")
        if _drift_seen:
            console.print(
                "  (Run `mirror reconcile-neighbours` to align peers.)",
                tui.COLOR_WARNING)
        return True

    def cmd_mirror_summary(self, *args):
        """Full per-mirror state dump.  Phase 8 adds `we_own: N` — the
        count of non-retracted claims this builder owns on each mirror,
        sourced from the most-recently-fetched
        cache/mirror/<name>/fetched/claims/<our-builder-id>.jsonl.
        `(no claims jsonl fetched yet)` when no fetch has happened —
        run `mirror pull` or `mirror publish` first."""
        import mirror as _mirror
        _names = ([args[0]] if args
                  else _mirror.list_mirrors(self.config))
        if not _names:
            console.print("mirror summary: no mirrors configured.")
            return True
        _bid = self._coord_builder_id()
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            if _st is None:
                console.print(
                    f"mirror summary: {_n!r} not registered.",
                    tui.COLOR_ERROR)
                continue
            console.print(f"[{_n}]")
            console.print(f"  url:              {_st.get('url', '')}")
            console.print(f"  type:             {_st.get('type', '')}")
            console.print(f"  ssh_key:          {_st.get('ssh_key', '') or '(unset)'}")
            console.print(f"  base:             {_st.get('base', '') or '(unset)'}")
            console.print(f"  current:          {_st.get('current', '') or '(unset)'}")
            console.print(f"  last_publish_at:  {_st.get('last_publish_at', '') or '(never)'}")
            console.print(f"  we_own:           {self._mirror_summary_we_own_line(_n, _bid)}")
            _nb = _st.get('neighbours_known') or []
            if _nb:
                console.print(f"  neighbours_known: {len(_nb)} peer(s)")
                for _u in _nb:
                    console.print(f"    - {_u}")
            else:
                console.print("  neighbours_known: (none — first publish will populate)")
        return True

    def _mirror_summary_we_own_line(
        self, mirror_name: str, builder_id: 'Optional[str]',
    ) -> str:
        """Count of non-retracted claims THIS builder owns on the
        named mirror.  Reads from the local fetched cache (the same
        place mirror pull / publish writes to); returns a friendly
        sentinel string when there's nothing to count from."""
        if not builder_id:
            return "(builder id not initialised — run `mirror init <id>`)"
        _claims_path = os.path.join(
            self.config.dir_cache, 'mirror', mirror_name, 'fetched',
            'claims', f'{builder_id}.jsonl',
        )
        if not os.path.isfile(_claims_path):
            return "(no claims jsonl fetched yet — run `mirror pull`)"
        import json
        import coord.schema as _schema
        _claims: 'list[dict]' = []
        try:
            with open(_claims_path) as _fh:
                for _line in _fh:
                    _s = _line.strip()
                    if not _s:
                        continue
                    try:
                        _c = json.loads(_s)
                    except ValueError:
                        continue
                    if isinstance(_c, dict):
                        _claims.append(_c)
        except OSError as _e:
            return f"(unreadable: {_e})"
        # a published claim folded by a later reclaim/obsolescence
        # (its seq is a back-ref target) is NO LONGER live — the old code's
        # `else: _live += 1` double-counted the superseded original as live
        # (we_own showed "5 (1 obsolete)" when the truth was 4 live).
        _dead = _schema.superseded_seqs(_claims)
        _live = 0
        _retracted = 0
        _deprecated = 0
        _obsolete = 0
        _superseded = 0
        for _c in _claims:
            _cs = _c.get('claim_state')
            if _cs == _schema.CLAIM_STATE_RETRACTED:
                _retracted += 1
            elif _cs == _schema.CLAIM_STATE_DEPRECATED:
                _deprecated += 1
            elif _cs == _schema.CLAIM_STATE_OBSOLETE:
                _obsolete += 1
            elif isinstance(_c.get('seq'), int) and _c['seq'] in _dead:
                _superseded += 1
            else:
                _live += 1
        _extra = []
        if _obsolete:
            _extra.append(f"{_obsolete} obsolete")
        if _deprecated:
            _extra.append(f"{_deprecated} deprecated")
        if _retracted:
            _extra.append(f"{_retracted} retracted")
        if _superseded:
            _extra.append(f"{_superseded} superseded")
        _extra_str = f" ({', '.join(_extra)})" if _extra else ''
        return f"{_live} pkg(s){_extra_str}"

    def _mirror_audit_disk_vs_claims(
        self, mirror_name: str, mirror_state: dict,
        by_builder: 'dict',
    ) -> 'list[tuple[str, str, str]]':
        """Integrity sweep: cross-check the union of
        claim `filename` fields (sidecar truth) against the actual
        ``.deb``/``.udeb`` files present in the mirror's pool dir.

        Returns ``list[(severity, kind, message)]``:
          - ``CRITICAL  missing_on_disk``  claim references a file the
            remote pool doesn't carry — apt clients fetching it would
            404; consumers of `mirror pull` would skip that download
          - ``WARNING   orphan_on_disk``   on-disk file has no claim
            backing it (operator out-of-band rsync? leftover from a
            wiped builder?); not load-bearing today but operator
            should know

        ssh mirrors: single remote `find` over the pool tree.  file://
        mirrors: local `os.walk`.  Network/permission failures return
        an empty list (silent) — they're not the audit's signal.
        """
        _url = mirror_state.get('url', '')
        if not _url:
            return []

        # 1. Claim-side: every LIVE claim's filename.  Same two-step
        # fold as _mirror_recompute_base: drop state-marker claims
        # (retracted/deprecated/obsolete) AND the published claims they
        # supersede via *_seq back-refs.  Without the fold the ORIGINAL
        # published claim of an obsoleted/deprecated file keeps the
        # filename "expected on disk" and a legitimately-pruned file
        # false-CRITICALs as missing_on_disk (17 hits, 2026-06-11).
        # Filenames under marker claims land in _state_marked instead:
        # those bytes may be pruned (no finding) or retained
        # (prune-candidate, not an orphan).
        import coord.schema as _schema
        _claimed: set = set()
        _state_marked: set = set()
        for _bid, _claims in by_builder.items():
            _dead_seqs = _schema.superseded_seqs(_claims)  # STA-29
            for _c in _claims:
                _fn = _c.get('filename')
                if not (isinstance(_fn, str) and _fn):
                    continue
                if _schema.is_superseded_claim(_c, _dead_seqs):
                    _state_marked.add(_fn)
                    continue
                _claimed.add(_fn)
        # A filename can be both (old version marked, new version live)
        # — live wins for the missing check; marked only suppresses
        # orphan warnings.
        _state_marked -= _claimed

        # 2. Disk-side
        _on_disk: 'Optional[set]' = self._mirror_audit_pool_listing(
            _url, mirror_state.get('ssh_key') or None)
        if _on_disk is None:
            # Pool listing failed silently (network / perms / unsupported
            # scheme) — emit a single INFO-ish line so the operator knows
            # the cross-check was skipped, but don't gate.
            return [('INFO', 'pool_listing_unavailable',
                     f"could not enumerate pool for {mirror_name!r}; "
                     "claim-vs-disk cross-check skipped")]

        _missing = sorted(_claimed - _on_disk)
        _orphan = sorted(_on_disk - _claimed - _state_marked)
        _findings: 'list[tuple[str, str, str]]' = []
        for _fn in _missing[:20]:
            _findings.append((
                'CRITICAL', 'missing_on_disk',
                f"claim references {_fn!r} but no such file in pool"))
        if len(_missing) > 20:
            _findings.append((
                'CRITICAL', 'missing_on_disk',
                f"…and {len(_missing) - 20} more"))
        for _fn in _orphan[:20]:
            _findings.append((
                'WARNING', 'orphan_on_disk',
                f"pool carries {_fn!r} with no backing claim"))
        if len(_orphan) > 20:
            _findings.append((
                'WARNING', 'orphan_on_disk',
                f"…and {len(_orphan) - 20} more"))
        return _findings

    def _mirror_audit_pool_listing(
        self, url: str, ssh_key: 'Optional[str]',
    ) -> 'Optional[set]':
        """Enumerate ``.deb``/``.udeb`` files in the mirror's pool dir.
        Returns a set of basenames or None on unsupported scheme /
        I/O failure."""
        if url.startswith('file://'):
            _root = url[len('file://'):]
            if not os.path.isdir(_root):
                return None
            _out: set = set()
            for _dp, _dirs, _files in os.walk(_root):
                for _f in _files:
                    if _f.endswith(('.deb', '.udeb')):
                        _out.add(_f)
            return _out
        if not url.startswith('ssh://'):
            return None
        import shlex
        import subprocess
        import mirror as _mirror_mod
        _host = _mirror_mod._extract_host_from_ssh_url(url) or ''
        _user = _mirror_mod._extract_user_from_ssh_url(url) or ''
        _path = _mirror_mod._extract_path_from_ssh_url(url) or ''
        if not (_host and _path):
            return None
        _argv: 'list[str]' = [
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=accept-new',
        ]
        if ssh_key:
            _argv += ['-i', ssh_key]
        _target = f'{_user}@{_host}' if _user else _host
        _quoted = shlex.quote(_path)
        _argv += [
            _target,
            f'find {_quoted} -type f \\( -name "*.deb" -o '
            f'-name "*.udeb" \\) -printf "%f\\n" 2>/dev/null',
        ]
        try:
            _r = subprocess.run(
                _argv, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            return None
        if _r.returncode != 0:
            return None
        return {_l.strip() for _l in _r.stdout.splitlines() if _l.strip()}

    def _mirror_is_prepared(self, public_url: str) -> 'tuple[bool, str]':
        """Probe ``<public_url>/mirror-info.json`` for the marker that
        ``prep-mirror.sh`` writes once a host is set up to receive a
        publish (web server + dir layout).  Returns ``(prepared, detail)``
        — detail is the prepared-at stamp on success, else the reason."""
        import json as _json
        _url = public_url.rstrip('/') + '/mirror-info.json'
        try:
            _r = utils._http_session().get(_url, timeout=10)
        except Exception as _e:   # network layer — any failure = not reachable
            return False, f"marker unreachable at {_url} ({type(_e).__name__})"
        if _r.status_code != 200:
            return False, f"HTTP {_r.status_code} for {_url}"
        try:
            _m = _json.loads(_r.text)
        except ValueError:
            return False, "mirror-info.json is not valid JSON"
        if not (isinstance(_m, dict) and _m.get('marker') == 'asgard-mirror'):
            return False, "mirror-info.json is not an asgard-mirror marker"
        return True, str(_m.get('prepared_at') or '')

    def _release_iso_descriptors(self) -> 'tuple[list[dict], list[str]]':
        """Scan `image/` for the install media matching the CURRENT
        version + snapshot.  Returns ``(found, missing_required)`` where
        `found` is a list of ``{kind, file, path, size, sha256, built_at}``
        (kinds: live / installer / disk) and `missing_required` lists the
        REQUIRED kinds (live + installer) absent for this snapshot — the
        disk qcow2 is optional and not snapshot-tagged, so it's included
        when present but never gates."""
        import datetime
        _ver = str(self.config.build_version).strip('"').strip("'")
        _snap = utils.snapshot_iso_tag(self.config)
        _distro = str(self.config.build_distribution).strip('"').strip("'")
        _img = self.config.dir_image
        _suffix = f"-{_snap}-amd64.iso" if _snap else "-amd64.iso"
        _candidates = [
            ('live',      f"athena-{_ver}{_suffix}"),
            ('installer', f"athena-installer-{_ver}{_suffix}"),
            ('disk',      f"{_distro.lower()}-{_ver}-amd64.qcow2"),
        ]
        _found: 'list[dict]' = []
        for _kind, _fname in _candidates:
            _path = os.path.join(_img, _fname)
            if not os.path.isfile(_path):
                continue
            _stat = os.stat(_path)
            _found.append({
                'kind': _kind, 'file': _fname, 'path': _path,
                'size': _stat.st_size,
                'sha256': utils.get_sha256(_path),
                'built_at': datetime.datetime.fromtimestamp(
                    _stat.st_mtime,
                    datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            })
        _found_kinds = {_d['kind'] for _d in _found}
        _missing = [_k for _k in ('live', 'installer')
                    if _k not in _found_kinds]
        return _found, _missing

    def _push_release_assets(
        self, public_url: str, pool_spec: str,
        ssh_key: 'Optional[str]', isos: 'list[dict]',
    ) -> bool:
        """Generate index.html + releases.json and push them + the ISOs to
        the mirror's pool root.  ISOs push by name (--ignore-existing —
        immutable per filename); the index files OVERWRITE (they change
        every publish).  Returns True on full success."""
        import release_index
        import coord.transport as _transport
        import coord.publish as _publish
        _ver = str(self.config.build_version).strip('"').strip("'")
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _distro = str(self.config.build_distribution).strip('"').strip("'")
        _manifest = release_index.build_release_manifest(
            distribution=_distro, version=_ver,
            snapshot=utils.snapshot_iso_tag(self.config),
            codename=_codename, component='main', arch='amd64',
            public_url=public_url,
            signed_by_keyring=(
                '/usr/share/keyrings/athena-archive-keyring.gpg'),
            isos=[{_k: _d[_k] for _k in
                   ('kind', 'file', 'size', 'sha256', 'built_at')}
                  for _d in isos],
            generated_at=_publish._utc_now(),
        )
        _html, _json = release_index.render_release_files(_manifest)
        _root = pool_spec.rstrip('/')
        _ok = True
        # ISOs first (immutable by name — never overwrite a published one).
        # Drive a byte-valued ProgressBar off rsync's progress stream so a
        # multi-GB push shows MB transferred + rate (ProgressBar auto-scales
        # value/total/rate to K/M/G) instead of looking hung during a silent
        # transfer.  itr_label 'B/s' → the rate column reads e.g. "30.0MB/s".
        _total_bytes = sum(int(_d.get('size') or 0) for _d in isos)
        _bar = ProgressBar(
            label='push ISOs', itr_label='B/s',
            maxvalue=max(1, _total_bytes), show_rate=True, label_width=20)
        _sent = 0   # bytes from already-completed files (bar base offset)
        for _d in isos:
            def _on_bytes(_cum, *, _base=_sent, _bar=_bar):
                # _cum = cumulative bytes for THIS file; advance the bar to
                # base+cum (step takes a delta and caps at max).
                _target = _base + _cum
                if _target > _bar.value:
                    _bar.step(_target - _bar.value)
            _r, _detail = _transport.push_single_deb(
                local_path=_d['path'],
                remote_spec=f"{_root}/iso/{_d['file']}",
                ssh_key=ssh_key, overwrite=False, on_bytes=_on_bytes)
            # top up to the full file size: a skipped (already-present) file
            # emits no progress line, so advance the bar by its whole size.
            _sent += int(_d.get('size') or 0)
            if _bar.value < _sent:
                _bar.step(_sent - _bar.value)
            if not _r:
                logger.error(f"release push: ISO {_d['file']}: {_detail}")
                _ok = False
        _bar.close()
        # Then the index pair (overwrite — they reflect the current state).
        for _name, _content in (('index.html', _html),
                                ('releases.json', _json)):
            _local = os.path.join(self.config.dir_temp, _name)
            try:
                with open(_local, 'w') as _fh:
                    _fh.write(_content)
            except OSError as _e:
                logger.error(f"release push: write {_local}: {_e}")
                _ok = False
                continue
            _r, _detail = _transport.push_single_deb(
                local_path=_local, remote_spec=f"{_root}/{_name}",
                ssh_key=ssh_key, overwrite=True)
            if not _r:
                logger.error(f"release push: {_name}: {_detail}")
                _ok = False
        return _ok

    def cmd_mirror_publish(self, *args, reclaim_intents=None):
        """mirror publish [<name>] [--no-iso] — publish to one mirror, or all.

        A publish ships a COHERENT release: the apt repo + the install
        media (live + installer ISOs) + a static `index.html` /
        `releases.json` at the pool root.  It REFUSES unless the current
        version+snapshot ISOs exist in image/; `--no-iso` bypasses that for
        an incremental publish (reclaim / hotfix / stale-index re-push),
        and the index then carries whatever ISO set is present.

        Per mirror:
          1. Pull peer's coord-head, keyring, claims (under remote flock)
          2. Federation gate: peer's coord-head.neighbours must match local
             config (or peer has no head yet → first-publish bootstrap)
          3. Hash-conflict scan (CRITICAL → PUBLISH_HALT)
          4. Per-file .deb push to the pool (with progress bar)
          5. Sign + append claims for every new build.json output_hashes
          6. Push jsonl + re-sign coord-head pinning current InRelease sha
          7. Update local config/mirror.<name>.state (current,
             last_publish_at, neighbours_known)

        First-publish bootstrap: on a fresh empty mirror endpoint, this
        command uploads our pubkey to keyring/builders/ AND initialises
        the coord-head with neighbours = local config's mirror URL set.
        """
        import mirror as _mirror
        import signing
        import coord.publish as _publish
        # Resolve identity
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _priv, _pub = _keys
        # Resolve InRelease (the local copy we sign + push verbatim).
        # auto-index when InRelease is missing —
        # `repo index` is no longer an operator-visible command, so
        # mirror publish owns the side-effect.
        #
        # STALE-INDEX guard: missing is not the only bad
        # state.  Publish pushes dists/ verbatim AND coord-head PINS its
        # sha, so a stale local index publishes "cleanly" and every
        # downstream verification passes while apt clients keep
        # resolving superseded metadata — caught live when a Jun-9
        # index shipped alongside the fixed e2fsprogs +asg1u2 pool and
        # the mirror kept serving the broken +asg1u1 Pre-Depends.
        # Re-index whenever any pool artifact OR pool directory (covers
        # deletion-only changes) is newer than the local InRelease.
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _inrelease = os.path.join(
            self.config.dir_repo, 'dists', _codename, 'InRelease')
        _need_index = not os.path.isfile(_inrelease)
        _why = 'missing'
        if not _need_index:
            _inrel_mtime = os.path.getmtime(_inrelease)
            _dist_root = os.path.join(
                self.config.dir_repo, 'dists', _codename)
            for _root, _dirs, _files in os.walk(_dist_root):
                _cands = [_f for _f in _files
                          if _f.endswith(('.deb', '.udeb'))]
                # Dir mtime checked ONLY for dirs holding pool artifacts
                # (catches deletion-only changes).  Index-only dirs are
                # excluded: InRelease lands via temp-write + mv (mv
                # preserves the file's older mtime but bumps the dir),
                # so checking them would flag stale on every publish.
                _paths = [_root] if _cands else []
                _paths += [os.path.join(_root, _f) for _f in _cands]
                for _p in _paths:
                    try:
                        if os.path.getmtime(_p) > _inrel_mtime:
                            _need_index = True
                            _why = (f'stale — {os.path.basename(_p)} '
                                    f'newer than InRelease')
                            break
                    except OSError:
                        continue
                if _need_index:
                    break
        if _need_index:
            console.print(
                f"mirror publish: local InRelease {_why} — auto-indexing "
                "repo (folded `repo index full`)…", tui.COLOR_INFO)
            if not self.cmd_index_repo():
                console.print(
                    "mirror publish: auto-index failed (see log).  "
                    "`repo audit` to diagnose.", tui.COLOR_ERROR)
                return False
            if not os.path.isfile(_inrelease):
                console.print(
                    f"mirror publish: auto-index ran but InRelease still "
                    f"missing at {_inrelease}.", tui.COLOR_ERROR)
                return False
        # Resolve snapshot pin
        _snapshot_pin = self._snapshot_current() or ''
        # Resolve federation membership.  v3: per-peer records pulled
        # from local state files (each carries public_url +
        # public_proto from the operator-supplied --proto at
        # `mirror add` time) so a heterogeneous federation round-trips
        # the apt-readable URL per peer.  The federation gate inside
        # remote_publish still works on the URL projection — back-compat
        # `canonicalize_neighbours` does the dict-→-str collapse.
        _local_urls = _mirror.all_mirror_neighbour_records(self.config)
        # Release-media gate (gate-with-escape): a publish ships a coherent
        # release — repo + install media + a static index — so by default
        # it REFUSES unless the current version+snapshot live & installer
        # ISOs exist in image/.  `--no-iso` bypasses for an incremental
        # publish (reclaim / single-package hotfix / stale-index re-push);
        # the index then carries whatever ISO set is present.
        # Build mode publishes packages only — it never builds ISOs (chroot/
        # iso steps are refused in build mode) and its mirror snapshot may run
        # ahead of any ISO snapshot — so a build-mode peer implies --no-iso.
        _publish_mode = getattr(self.config, 'build_mode', 'distribution')
        _no_iso = ('--no-iso' in args or 'no-iso' in args
                   or _publish_mode == 'build')
        if _publish_mode == 'build' and not (
                '--no-iso' in args or 'no-iso' in args):
            console.print(
                "mirror publish: Mode = build — publishing packages only "
                "(--no-iso implied; ISOs come from a distribution build).",
                tui.COLOR_INFO)
            console.print(
                "  closure: the publish gate checks YOUR packages against the "
                "LOCAL pool — run `mirror pull` first so it's the merged "
                "mirror pool.  Complete federation closure is verified "
                "authoritatively by the distribution publisher's `mirror "
                "audit` on sync-back.", tui.COLOR_INFO)
        _pos_args = [_a for _a in args if _a not in ('--no-iso', 'no-iso')]
        _release_isos, _missing_isos = self._release_iso_descriptors()
        if _missing_isos and not _no_iso:
            console.print(
                "mirror publish: REFUSED — missing current-snapshot install "
                f"media: {', '.join(_missing_isos)}.  Build them "
                "(`iso build live` / `iso build installer`) so the mirror "
                "ships a coherent release, or `mirror publish --no-iso` to "
                "publish the repo only (the index keeps the prior ISO set).",
                tui.COLOR_ERROR)
            return False
        # Target selection
        _target = _pos_args[0] if _pos_args else None
        if _target is not None:
            if _mirror.read_mirror_state(self.config, _target) is None:
                console.print(
                    f"mirror publish: unknown mirror {_target!r}",
                    tui.COLOR_ERROR)
                return False
            _names = [_target]
        else:
            _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print(
                "mirror publish: no mirrors configured (use `mirror add`)",
                tui.COLOR_WARNING)
            return False
        # Sequential per-mirror publish.  Per-mirror flock isolates
        # concurrent peers from each other (multiple parallel publishes
        # to different mirrors would conflict locally on the staging
        # dir; serial keeps the semantics simple in this phase).
        _all_ok = True
        _published_any = False
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            assert _st is not None  # checked above / by list_mirrors
            _url = _st.get('url', '')
            _ssh_key = _st.get('ssh_key') or None
            # first-publish dist-mode gate.
            # When the local builder is in [Build] Mode = build
            # AND the target mirror has no coord-head on remote
            # (fresh bootstrap state), REFUSE.  Bootstrapping an
            # build-mode build into a virgin mirror would land a
            # partial subset that's almost certainly not installable —
            # the dist-mode bootstrap is what guarantees the initial
            # closure invariant.  Defensive getattr for test doubles.
            _mode = getattr(self.config, 'build_mode', 'distribution')
            if _mode == 'build':
                _has_head = self._mirror_remote_has_coord_head(_n, _st)
                if not _has_head:
                    console.print(
                        f"mirror publish {_n}: REFUSED — mirror has "
                        "no coord-head yet (first publish must come "
                        "from a distribution-mode build with `repo "
                        "audit` passing).  Run `mirror publish` from "
                        "a dist-mode host first, or set `[Build] "
                        "Mode = distribution` on this host.",
                        tui.COLOR_ERROR)
                    _all_ok = False
                    continue
            # Phase 8 gate: refuse to publish when this build's
            # snapshot.current is older than the mirror's `base` (the
            # mirror's archive floor).  Publishing pre-floor packages
            # would either be silently dropped on the next prune or
            # corrupt the +asg uN derivation.  `(unset)` mirror.base
            # = first-publish bootstrap; let it through.
            _mirror_base = (_st.get('base') or '').strip()
            if _mirror_base and _snapshot_pin \
                    and _snapshot_pin < _mirror_base:
                console.print(
                    f"mirror publish {_n}: REFUSED — build snapshot "
                    f"({_snapshot_pin}) is older than this mirror's "
                    f"archive floor (mirror.base = {_mirror_base}).  "
                    "Advance the build snapshot (`snapshot select <ts>`) "
                    "or wipe + re-add the mirror with a fresh base.",
                    tui.COLOR_ERROR)
                _all_ok = False
                continue
            # Derive pool + coord specs from the operator-registered POOL
            # URL.  Pool serves apt clients; sidecar lives at the sibling
            # `-coord` path on the same host.
            _pool_spec, _ssh_host = _mirror.rsync_spec_for_url(_url)
            _coord_url = _mirror.coord_root_for(_url)
            _coord_spec, _coord_ssh_host = _mirror.rsync_spec_for_url(
                _coord_url)
            # ssh_host derived from EITHER (they should agree); coord
            # tree is preferred since flock lives there.
            _ssh_host = _coord_ssh_host or _ssh_host
            _mode_tag = self.config.build_mode
            console.print(
                f"mirror publish {_n} [MODE={_mode_tag}]: → {_url}",
                tui.COLOR_HIGHLIGHT)
            # ProgressBar wired through on_progress.  Total updates
            # dynamically as we learn the count after step 5; until
            # then the bar shows 0/0.
            _bar = ProgressBar(
                label=f"publish .debs → {_n}", itr_label='',
                maxvalue=1, show_rate=False, label_width=34)
            def _progress(current, total, _filename, _ok, *,
                          _bar_ref=_bar):
                # Lazy-update max once we see the true total.  set_max
                # is the public setter; ProgressBar has no `.maxvalue`
                # attribute (stored as self._max).  set_max is
                # idempotent so unconditional-call is fine.
                if total:
                    _bar_ref.set_max(total)
                _bar_ref.step(1)
            def _on_status(_msg, *, _name_ref=_n):
                # Each publish step gets a visible line so the
                # operator sees forward progress instead of silence
                # (publish involves several minute-scale ssh/rsync
                # round trips; without this the TUI just shows the
                # initial banner until success/failure).
                console.print(f"  · {_msg}", tui.COLOR_INFO)
            # The publish-time closure gate needs the same install
            # corpus that `repo audit`'s dep gate uses — binary names
            # from dep_tree.selected_pkgs ∪ udeb_dep_tree.selected_pkgs.
            # Without this, the gate either over- or under-walks the
            # consumer surface and produces verdicts that contradict
            # `repo audit` against identical on-disk state.
            _install_corpus: 'frozenset[str]' = frozenset()
            if self.dep_tree is not None:
                _install_corpus |= frozenset(
                    self.dep_tree.selected_pkgs.keys())
            if self.udeb_dep_tree is not None:
                _install_corpus |= frozenset(
                    self.udeb_dep_tree.selected_pkgs.keys())
            try:
                # after a FULLY successful publish, stamp
                # published_at on each published source's build record.
                _lc_buildlog = os.path.join(self.config.dir_log, 'build')

                def _stamp_published(_pkgs: set,
                                     _bl: str = _lc_buildlog) -> None:
                    _now = utils._utc_now_iso()
                    for _pkg in sorted(_pkgs):
                        try:
                            utils.upsert_build_record(
                                _bl, _pkg, published_at=_now)
                        except OSError as _e:
                            logger.warning(
                                f"published_at stamp {_pkg}: {_e}")

                _ok, _detail = _publish.remote_publish(
                    builder_id=_bid, config=self.config,
                    private_key_path=_priv, public_key_path=_pub,
                    snapshot_pin=_snapshot_pin,
                    remote_coord_spec=_coord_spec,
                    inrelease_local_path=_inrelease,
                    read_build_record=utils.read_build_record,
                    local_mirror_urls=_local_urls,
                    ssh_host=_ssh_host,
                    ssh_key=_ssh_key,
                    pool_remote_spec=_pool_spec,
                    on_progress=_progress,
                    on_status=_on_status,
                    install_corpus=_install_corpus or None,
                    on_published=_stamp_published,
                    reclaim_intents=reclaim_intents,
                    local_ahead_fn=_mirror.local_ahead_candidates,
                )
            finally:
                _bar.close()
            console.print(
                f"  {_detail}",
                tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
            if not _ok:
                _all_ok = False
                continue
            _published_any = True
            # Success → bump mirror state
            import coord.schema as _schema
            # recompute mirror.base from the
            # post-publish claim ledger.  base = oldest snapshot
            # timestamp across all non-retracted claims on the mirror.
            # Combined with chunk 6's snapshot.current >= mirror.base
            # publish gate, this prevents back-publishing pre-floor
            # builds and gives operators a clear "what's the oldest
            # thing on this mirror" signal in mirror summary.
            _new_base = self._mirror_recompute_base(_n)
            _base_update: 'dict[str, object]' = {
                'current':           _snapshot_pin,
                'last_publish_at':   _publish._utc_now(),
                'neighbours_known':  _schema.canonicalize_neighbours(_local_urls),
            }
            # Only overwrite base when we have a real value — empty
            # means "no claims yet" (fresh mirror) and we should keep
            # the seed-at-add-time value.
            if _new_base:
                _base_update['base'] = _new_base
            _mirror.update_mirror_state(self.config, _n, **_base_update)

            # Stage 1: publish the static release index + ISOs to the pool
            # root (served at the mirror's public_url).  The installer-smoke
            # CI job reads releases.json to find + sha-verify the ISO.  Push
            # AFTER the repo/coord transaction so the index never references
            # a release the repo hasn't caught up to.
            _public = (_st.get('public_url') or '').strip()
            if _public:
                if not self._push_release_assets(
                        _public, _pool_spec, _ssh_key, _release_isos):
                    console.print(
                        f"  {_n}: release index/ISO push had errors "
                        "(see log) — repo published OK", tui.COLOR_WARNING)
                else:
                    _kinds = ', '.join(_d['kind'] for _d in _release_isos)
                    console.print(
                        f"  {_n}: published release index"
                        + (f" + {_kinds} image(s)" if _release_isos else "")
                        + f" → {_public}/", tui.COLOR_HIGHLIGHT)
            else:
                logger.info(
                    f"release index: mirror {_n} has no public_url — "
                    "skipping index/ISO push")

        # Refresh the local +asg uN authority from the just-published index.
        # The published.manifest is the bump ledger (published_ledger reads
        # it); without this it stays frozen and N can never advance past its
        # last value — the regression that left every asg package stuck at
        # u2 after MIRROR-01 removed the old repo-publish finalize path
        # (commit 8cc803b).  Sign failure is loud: a silent/empty manifest
        # corrupts the next publish's N derivation.
        if _published_any:
            import repo_audit
            _pkgs_text = repo_audit.local_published_packages_text(self.config)
            if _pkgs_text.strip():
                if repo_audit.write_published_manifest(self.config, _pkgs_text):
                    console.print(
                        "  refreshed local +asg manifest (bump authority)",
                        tui.COLOR_INFO)
                else:
                    console.print(
                        "  WARNING: published OK but the local +asg manifest "
                        "sign FAILED — next publish's N derivation may be "
                        "wrong.  Run `key verify`, then re-publish.",
                        tui.COLOR_WARNING)
                    _all_ok = False
        return _all_ok

    def cmd_mirror_pull(self, *args):
        """mirror pull [<name>] — fetch from one mirror, or all when no name.

        Per mirror:
          1. Fetch coord-head + keyring + claims to cache/mirror/<n>/fetched
          2. tier-1 verify coord-head; Ed25519-verify every claim sig
          3. Walk verified claims of OUR current snapshot pin:
               - skip if claim.builder == our builder-id (skip-own security
                 rule — we can't legitimately accept a peer's claim that
                 it's the file we built)
               - skip if local repo already has the file
               - else download .deb to its predicted local path; verify
                 Against the claim; abort that file on mismatch
          4. Report counts (downloaded, skipped-own, skipped-present,
             verify-mismatch).

        This is the read-only / forward-only side of the federation: we
        pull-back what peers have published.  It does NOT touch the
        remote (no flock, no push).
        """
        import mirror as _mirror
        import signing
        import coord.head as _head_mod
        import coord.identity as _id
        import coord.store as _store
        import coord.transport as _transport
        import coord.schema as _schema
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _, _pub = _keys
        _target = args[0] if args else None
        if _target is not None:
            if _mirror.read_mirror_state(self.config, _target) is None:
                console.print(
                    f"mirror pull: unknown mirror {_target!r}",
                    tui.COLOR_ERROR)
                return False
            _names = [_target]
        else:
            _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print(
                "mirror pull: no mirrors configured (use `mirror add`)",
                tui.COLOR_WARNING)
            return False
        _codename = str(self.config.build_codename).strip('"').strip("'")
        _signing_home = signing.signing_home(self.config)
        _snap = self._snapshot_current() or ''
        _all_ok = True
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            assert _st is not None
            _url = _st.get('url', '')
            _ssh_key = _st.get('ssh_key') or None
            # Pool spec for downloading .debs; coord spec for fetching
            # the sidecar tree.  Pool is the operator-registered URL;
            # coord is the `-coord` sibling.
            _pool_spec, _ = _mirror.rsync_spec_for_url(_url)
            _coord_url = _mirror.coord_root_for(_url)
            _coord_spec, _ = _mirror.rsync_spec_for_url(_coord_url)
            console.print(
                f"mirror pull {_n}: ← {_url}", tui.COLOR_HIGHLIGHT)
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _n, 'fetched')
            os.makedirs(_fetched, exist_ok=True)
            # 1. Fetch coord tree (sidecar, not pool)
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_fetched, remote_spec=_coord_spec, ssh_key=_ssh_key,
            )
            if not _ok:
                console.print(
                    f"  pull coord tree failed: {_detail}", tui.COLOR_ERROR)
                _all_ok = False
                continue
            # 2. Verify head + claims
            _head = _head_mod.read_coord_head(_fetched, _signing_home)
            if _head is None:
                console.print(
                    "  coord-head verify failed or absent — refusing to "
                    "trust the fetched tree.", tui.COLOR_ERROR)
                _all_ok = False
                continue
            # Auto-adopt the mirror's snapshot pin FORWARD (federation: the
            # mirror's snapshot is authoritative; a peer that advanced it and
            # published newer packages would otherwise be invisible to our
            # claim filter, which keys on the local pin).  Forward-only —
            # never roll our pin backward.
            _remote_snap = ''
            _hs = _head.get('snapshot')
            if isinstance(_hs, dict):
                _remote_snap = str(_hs.get('current') or '')
            _adopt = (_mirror.snapshot_adopt_forward(_snap, _remote_snap)
                      if getattr(self.config, 'snapshot_enabled', False)
                      else None)
            if _adopt:
                utils.write_snapshot_state(self.config, current=_adopt)
                # Keep build.conf's [Snapshot] Timestamp mirror honest NOW,
                # same as `snapshot select` — don't defer to the next startup's
                # reconcile (which would surface as a surprise build.conf
                # rewrite on a later unrelated run).
                utils.reconcile_snapshot_pin(self.config)
                self.flags.cache_ready = False
                self.flags.dep_check_ready = False
                console.print(
                    f"  snapshot: advanced local pin {_snap or '(unset)'} "
                    f"→ {_adopt} (from mirror {_n}) — re-run "
                    "`cache build` + `cache parse` to resolve at the new pin",
                    tui.COLOR_WARNING)
                _snap = _adopt
            # Refresh the canonical pkg.list/pool.list from the owner so
            # source-select changes propagate to every builder.
            self._apply_canonical_config(_fetched, _head)
            _keyring = _id.load_keyring(
                os.path.join(_fetched, 'keyring', 'builders'))
            _revoked = _head.get('revoked_builders') or {}
            _by_builder = _store.read_all_claims(
                os.path.join(_fetched, 'claims'), _keyring, _revoked)
            _total = sum(len(_v) for _v in _by_builder.values())
            console.print(
                f"  verified: {_total} claim(s) across "
                f"{len(_by_builder)} builder(s)")
            # 3. Decide the download set.  Preferred: the signed CLOSURE
            # LEDGER (head-pinned) — the latest-version-per-binary closure
            # across base..current, so a mixed-snapshot mirror pulls
            # completely.  Fallback (old owner, no ledger): the live-claim
            # set (supersession fold, NO snapshot filter — the obsolescence
            # fold already leaves one live version per filename).
            _dl = _skip_own = _skip_present = _mismatch = _failed = 0
            _refreshed = _no_claim = 0
            # collect successfully-downloaded
            # claims per package so we can write a single local
            # build.json record per source.  Indexed by package name;
            # each entry holds the list of (claim, owner_builder).
            _per_pkg_downloads: 'dict[str, list[tuple[dict, str]]]' = {}
            # Filename → (live claim, owner_builder), folding supersession so
            # the LIVE claim wins.  The join between content (ledger / live
            # claim) and provenance (who owns it → skip_own + pulled_from).
            _claim_by_fn: 'dict[str, tuple[dict, str]]' = {}
            for _builder, _claims in _by_builder.items():
                _dead = _schema.superseded_seqs(_claims)
                for _c in _claims:
                    if _schema.is_superseded_claim(_c, _dead):
                        continue
                    _cfn = _c.get('filename')
                    if isinstance(_cfn, str) and _cfn:
                        _claim_by_fn[_cfn] = (_c, _builder)

            def _pull_file(_fn, _expected_sha, _comp, _claim, _builder,
                           _is_reclaim,
                           _pool_spec=_pool_spec, _ssh_key=_ssh_key,
                           _per_pkg_downloads=_per_pkg_downloads):
                """Present-fast-path → download → sha-verify → record one file.
                Mutates the outer counters + _per_pkg_downloads.  The per-mirror
                _pool_spec/_ssh_key/_per_pkg_downloads are bound as defaults so
                the closure doesn't capture the enclosing loop var late (B023)."""
                nonlocal _dl, _skip_present, _mismatch, _failed, _refreshed
                # Component pinned on the claim/ledger (defaults to 'main' for
                # pre-component publishers).  Without it a non-free-firmware
                # pull lands at main/binary-arch/ and the remote URL 404s.
                _dst_dir = self.config.deb_dest_for_filename(_fn, _comp)
                _local_path = os.path.join(_dst_dir, _fn)
                if os.path.isfile(_local_path):
                    # a reclaim REWROTE the bytes under this filename — the
                    # present local copy may be stale.  Sha-check ONLY those
                    # (rare); the general present path stays hash-free.
                    _stale_reclaim = False
                    if _is_reclaim:
                        _have = utils.get_sha256(_local_path, use_cache=False)
                        _stale_reclaim = _have != _expected_sha
                    if not _stale_reclaim:
                        _skip_present += 1
                        _pkg_name = str(
                            _claim.get('package') or '') if _claim else ''
                        if _pkg_name:
                            _per_pkg_downloads.setdefault(
                                _pkg_name, []).append((_claim, _builder))
                        return
                    console.print(
                        f"  {_fn}: reclaimed upstream — local bytes "
                        "superseded, re-downloading")
                    _refreshed += 1
                # Source path on the mirror = same relative layout under
                # <pool_root>/dists/<codename>/<comp>/...
                _rel = os.path.relpath(_local_path, self.config.dir_repo)
                _remote_file = _pool_spec.rstrip('/') + '/' + _rel
                _ok, _detail = _transport.pull_single_file(
                    remote_spec=_remote_file, local_path=_local_path,
                    ssh_key=_ssh_key)
                if not _ok:
                    console.print(
                        f"  {_fn}: download failed — {_detail}",
                        tui.COLOR_ERROR)
                    _failed += 1
                    return
                _h = utils.get_sha256(_local_path, use_cache=False)
                if _h != _expected_sha:
                    console.print(
                        f"  {_fn}: SHA-256 mismatch (expected "
                        f"{(_expected_sha or '')[:12]} vs disk {_h[:12]}) "
                        "— removing.", tui.COLOR_ERROR)
                    try:
                        os.unlink(_local_path)
                    except OSError:
                        pass
                    _mismatch += 1
                    return
                _dl += 1
                _pkg_name = str(_claim.get('package') or '') if _claim else ''
                if _pkg_name:
                    _per_pkg_downloads.setdefault(_pkg_name, []).append(
                        (_claim, _builder))

            # Fetch + verify the signed closure ledger (head-pinned).
            import coord.config_manifest as _cfgman
            _ledger_sha = str(_head.get('closure_ledger_sha256') or '')
            _ledger = None
            if _ledger_sha:
                _lg_ok, _lg_detail, _ledger = (
                    _cfgman.read_verified_closure_ledger(_fetched, _ledger_sha))
                if not _lg_ok:
                    console.print(
                        f"  ledger: NOT applied ({_lg_detail}) — falling "
                        "back to live claims", tui.COLOR_WARNING)
                    _ledger = None

            # Byte-valued progress: the bar shows downloaded/total SIZE (auto
            # K/M/G) at MB/s, with the current binary package name as a fixed-
            # width label — far more meaningful than an item count for a
            # multi-GB fresh-peer adoption.  step() advances by each file's
            # byte size; the ProgressBar already humanises value/total and the
            # rate (`itr_label='B/s'` → "5.20MB/s").
            if _ledger is not None:
                _entries = _ledger.get('entries') or {}
                _total_b = sum(int(_e.get('size') or 0)
                               for _e in _entries.values())
                _bar = ProgressBar(
                    label=f'pull {_n}', itr_label='B/s',
                    maxvalue=max(_total_b, 1), show_rate=True,
                    label_width=26, bar_width=20)
                for _ent in _entries.values():
                    _fn = _ent.get('filename')
                    if not isinstance(_fn, str) or not _fn:
                        continue
                    _bar.label(_fn.split('_', 1)[0])     # binary package name
                    _claim, _builder = _claim_by_fn.get(_fn, (None, None))
                    if _claim is not None and _claim.get('builder') == _bid:
                        _skip_own += 1                   # a file WE built
                    elif _claim is None:
                        # ledger names a file no live claim covers — anomaly
                        # (audit catches it); can't record provenance, skip.
                        _no_claim += 1
                    else:
                        _pull_file(
                            _fn, str(_ent.get('sha256') or ''),
                            str(_ent.get('component') or 'main'),
                            _claim, _builder,
                            isinstance(_claim.get('reclaims_seq'), int))
                    _bar.step(int(_ent.get('size') or 0))
            else:
                # Fallback: walk the live-claim set.  NO snapshot-equality
                # filter — the supersession fold above already leaves one
                # live version per filename across base..current.
                _total_b = sum(int(_c.get('size') or 0)
                               for _c, _ in _claim_by_fn.values())
                _bar = ProgressBar(
                    label=f'pull {_n}', itr_label='B/s',
                    maxvalue=max(_total_b, 1), show_rate=True,
                    label_width=26, bar_width=20)
                for _fn, (_c, _builder) in _claim_by_fn.items():
                    _bar.label(_fn.split('_', 1)[0])     # binary package name
                    if _c.get('builder') == _bid:
                        _skip_own += 1
                    else:
                        _pull_file(
                            _fn, str(_c.get('sha256') or ''),
                            str(_c.get('component') or 'main'),
                            _c, _builder,
                            isinstance(_c.get('reclaims_seq'), int))
                    _bar.step(int(_c.get('size') or 0))
            # write local build.json record per
            # pulled package so source audit + repo audit see the .deb
            # as already-present (not needs_build).  Tunneled claims
            # land as phase=tunneled + republished_from carried over.
            # Non-tunneled claims land as phase=done + pulled_from
            # annotation so we can distinguish "we built it" from
            # "we pulled it" later.
            _bar.close()
            self._mirror_pull_write_build_records(
                _n, _per_pkg_downloads)
            console.print(
                f"  downloaded={_dl} skipped_own={_skip_own} "
                f"skipped_present={_skip_present} "
                f"verify_mismatch={_mismatch} failed={_failed}"
                + (f" refreshed={_refreshed}" if _refreshed else '')
                + (f" no_claim={_no_claim}" if _no_claim else ''),
                tui.COLOR_HIGHLIGHT if (_mismatch + _failed) == 0
                else tui.COLOR_ERROR)
            if _mismatch or _failed:
                _all_ok = False
        return _all_ok

    def cmd_mirror_reclaim(self, *args):
        """mirror reclaim [<source>|<file.deb|.udeb>] [<name>] [force]

        The sanctioned exception to the published-filename-
        is-frozen-bytes INVARIANT.  Supersedes OUR live published claim
        for the SAME filename with the local rebuild's sha and pushes
        the new bytes (overwriting the remote pool copy) — for content
        changes that are deliberately version-less: Position-X OOB
        normalization, disaster-recovery rebuilds.  Normal content
        changes bump the version and publish forward instead.

        With no target: LIST the local-ahead candidates per mirror and
        exit (dry-run).  With a target (source name, or an exact
        .deb/.udeb filename): confirm per-file old→new shas (YESNO;
        `force` skips), then run the full publish transaction with the
        resolved intents — flock, federation gate, hash-conflict scan,
        stale-index guard all apply, and each intent is re-validated
        against the just-fetched view inside the transaction
        (validate_reclaim_intents) so remote drift between listing and
        execution skips loudly instead of superseding the wrong claim.
        """
        import mirror as _mirror
        import signing
        import coord.head as _head_mod
        import coord.identity as _id
        import coord.store as _store
        import coord.transport as _transport
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _, _pub_key = _keys
        _force = 'force' in args
        _pos = [_a for _a in args if _a != 'force']
        _target = _pos[0] if _pos else None
        _mirror_name = _pos[1] if len(_pos) > 1 else None
        # `mirror reclaim <mirror-name>` (no reclaim target): a single
        # arg naming a configured mirror means "list candidates there".
        if (_target is not None and _mirror_name is None
                and _mirror.read_mirror_state(
                    self.config, _target) is not None):
            _mirror_name, _target = _target, None
        if _mirror_name is not None:
            if _mirror.read_mirror_state(self.config, _mirror_name) is None:
                console.print(
                    f"mirror reclaim: unknown mirror {_mirror_name!r}",
                    tui.COLOR_ERROR)
                return False
            _names = [_mirror_name]
        else:
            _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print(
                "mirror reclaim: no mirrors configured (use `mirror add`)",
                tui.COLOR_WARNING)
            return False
        _signing_home = signing.signing_home(self.config)
        _buildlog = os.path.join(self.config.dir_log, 'build')
        _all_ok = True
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            assert _st is not None
            _url = _st.get('url', '')
            _ssh_key = _st.get('ssh_key') or None
            _coord_url = _mirror.coord_root_for(_url)
            _coord_spec, _ = _mirror.rsync_spec_for_url(_coord_url)
            console.print(
                f"mirror reclaim {_n}: ← {_url}", tui.COLOR_HIGHLIGHT)
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _n, 'fetched')
            os.makedirs(_fetched, exist_ok=True)
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_fetched, remote_spec=_coord_spec,
                ssh_key=_ssh_key)
            if not _ok:
                console.print(
                    f"  pull coord tree failed: {_detail}", tui.COLOR_ERROR)
                _all_ok = False
                continue
            _head = _head_mod.read_coord_head(_fetched, _signing_home)
            if _head is None:
                console.print(
                    "  coord-head verify failed or absent — refusing to "
                    "trust the fetched tree.", tui.COLOR_ERROR)
                _all_ok = False
                continue
            _keyring = _id.load_keyring(
                os.path.join(_fetched, 'keyring', 'builders'))
            _by_builder = _store.read_all_claims(
                os.path.join(_fetched, 'claims'), _keyring,
                _head.get('revoked_builders') or {})
            _cands = _mirror.local_ahead_candidates(
                _by_builder, _bid, self.config.dir_repo, _buildlog)
            if not _cands:
                console.print(
                    "  no local-ahead candidates — local pool and remote "
                    "claims agree.")
                continue
            if _target is None:
                console.print(
                    f"  {len(_cands)} reclaim candidate(s) — re-run with "
                    "a <source>/<file> target to execute:")
                for _c in _cands:
                    console.print(
                        f"    {_c['filename']}  {_c['remote_sha'][:12]} → "
                        f"{_c['local_sha'][:12]}  (source: {_c['package']})")
                continue
            _sel = [_c for _c in _cands
                    if _c['filename'] == _target
                    or _c['package'] == _target]
            if not _sel:
                console.print(
                    f"  no candidate matches {_target!r} — bare "
                    "`mirror reclaim` lists them; a file qualifies only "
                    "when its on-disk sha matches the local build.json "
                    "and the remote claim is older.", tui.COLOR_WARNING)
                _all_ok = False
                continue
            console.print(
                f"  reclaiming {len(_sel)} file(s) — published bytes "
                "will be REWRITTEN under unchanged filenames:",
                tui.COLOR_WARNING)
            for _c in _sel:
                console.print(
                    f"    {_c['filename']}  {_c['remote_sha'][:12]} → "
                    f"{_c['local_sha'][:12]}")
            if not _force:
                _resp = Prompt(
                    PROMPT_YESNO,
                    f"Reclaim {len(_sel)} file(s) on {_n}?  This is the "
                    "sanctioned filename-immutability exception",
                ).get_response()
                if _resp.lower() not in ('y', 'yes'):
                    console.print("  aborted by operator")
                    continue
            _r = self.cmd_mirror_publish(_n, reclaim_intents=_sel)
            _all_ok = bool(_r) and _all_ok
        return _all_ok

    def cmd_mirror_withdraw_foreign(self, *args):
        """mirror withdraw-foreign [<name>] [--execute]

        Retract + prune every PUBLISHED claim WE own whose binary targets
        a FOREIGN architecture — the cross-toolchain by-products
        (binutils-aarch64-linux-gnu, the kfreebsd cross binutils,
        gcc-12-arm-linux-gnueabihf) a selected source (binutils, gcc-12)
        emits but that are NEVER part of the distribution.  This is the
        backfill that clears the historical `own_claim_disk_sha_mismatch`
        / `foreign_target_claim_published` findings; new builds no longer
        produce these claims (the claim-generation + repo gates).

        DRY-RUN by default: lists the foreign-target claims per mirror.
        `--execute` (or `force`): per file, append a signed filename-keyed
        retraction, delete the local .deb, then run `mirror publish
        --no-iso` so the tombstones propagate and the auto-re-index drops
        the binaries from the mirror's Packages.  Idempotent — an
        already-retracted file is skipped.

        The source's build record is left untouched: binutils / gcc-12
        stay selected and keep publishing their NATIVE binaries; the
        claim-generation filter simply never re-claims the foreign ones.
        """
        import mirror as _mirror
        import signing
        import arch_filter
        import coord.publish as _publish
        import coord.head as _head_mod
        import coord.identity as _id
        import coord.store as _store
        import coord.transport as _transport
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _priv, _pub = _keys
        _execute = '--execute' in args or 'force' in args
        _pos = [_a for _a in args
                if _a not in ('--execute', 'force', '--no-iso', 'no-iso')]
        _mirror_name = _pos[0] if _pos else None
        if _mirror_name is not None:
            if _mirror.read_mirror_state(self.config, _mirror_name) is None:
                console.print(
                    "mirror withdraw-foreign: unknown mirror "
                    f"{_mirror_name!r}", tui.COLOR_ERROR)
                return False
            _names = [_mirror_name]
        else:
            _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print(
                "mirror withdraw-foreign: no mirrors configured "
                "(use `mirror add`)", tui.COLOR_WARNING)
            return False
        _build_arch = getattr(self.config, 'arch', None)
        if not _build_arch:
            console.print(
                "mirror withdraw-foreign: no build ARCH configured — "
                "cannot determine foreign targets.", tui.COLOR_ERROR)
            return False
        _signing_home = signing.signing_home(self.config)
        _all_ok = True
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            assert _st is not None
            _url = _st.get('url', '')
            _ssh_key = _st.get('ssh_key') or None
            _coord_url = _mirror.coord_root_for(_url)
            _coord_spec, _ = _mirror.rsync_spec_for_url(_coord_url)
            console.print(
                f"mirror withdraw-foreign {_n}: ← {_url}",
                tui.COLOR_HIGHLIGHT)
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _n, 'fetched')
            os.makedirs(_fetched, exist_ok=True)
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_fetched, remote_spec=_coord_spec,
                ssh_key=_ssh_key)
            if not _ok:
                console.print(
                    f"  pull coord tree failed: {_detail}", tui.COLOR_ERROR)
                _all_ok = False
                continue
            _head = _head_mod.read_coord_head(_fetched, _signing_home)
            if _head is None:
                console.print(
                    "  coord-head verify failed or absent — refusing to "
                    "trust the fetched tree.", tui.COLOR_ERROR)
                _all_ok = False
                continue
            _keyring = _id.load_keyring(
                os.path.join(_fetched, 'keyring', 'builders'))
            _by_builder = _store.read_all_claims(
                os.path.join(_fetched, 'claims'), _keyring,
                _head.get('revoked_builders') or {})
            _owners = _store.project_owners(_by_builder)
            _foreign = sorted(
                _fn for _fn, _rec in _owners.items()
                if _rec.get('builder') == _bid
                and arch_filter.is_foreign_target_binary(
                    _fn.split('_', 1)[0], _build_arch))
            if not _foreign:
                console.print(
                    "  no owned foreign-target claims — nothing to withdraw.")
                continue
            console.print(
                f"  {len(_foreign)} owned foreign-target claim(s) "
                f"(cross-toolchains targeting non-{_build_arch}):",
                tui.COLOR_WARNING)
            for _fn in _foreign:
                console.print(f"    {_fn}")
            if not _execute:
                console.print(
                    "  DRY-RUN — no files were deleted, no claims retracted.  "
                    "Re-run with `--execute` to retract + prune.",
                    tui.COLOR_INFO)
                continue
            # EXECUTE — retract each filename, delete the local .deb.
            _retracted = 0
            for _fn in _foreign:
                _ok_r, _detail_r = _publish.retract_claim_by_filename(
                    builder_id=_bid, config=self.config,
                    private_key_path=_priv, public_key_path=_pub,
                    filename=_fn)
                if _ok_r:
                    _retracted += 1
                else:
                    console.print(
                        f"    skip {_fn}: {_detail_r}", tui.COLOR_INFO)
                # drop the local pool file (+ its .verified sidecar) so the
                # auto-re-index in publish removes it from Packages.
                for _d in self.config.all_deb_dirs():
                    _p = os.path.join(_d, _fn)
                    if os.path.isfile(_p):
                        try:
                            os.remove(_p)
                        except OSError as _e:
                            console.print(
                                f"    WARNING: cannot remove {_p}: {_e}",
                                tui.COLOR_WARNING)
                        try:
                            os.remove(_p + '.verified')
                        except OSError:
                            pass
                        break
            console.print(
                f"  retracted {_retracted}/{len(_foreign)} claim(s); "
                "publishing to propagate tombstones + re-index…",
                tui.COLOR_HIGHLIGHT)
            # Incremental publish (like reclaim/hotfix) — never rebuilds
            # ISOs; pushes the jsonl (now carrying the retractions) and
            # auto-re-indexes the just-changed pool.
            _r = self.cmd_mirror_publish(_n, '--no-iso')
            _all_ok = bool(_r) and _all_ok
        return _all_ok

    def _mirror_remote_has_coord_head(
        self, mirror_name: str, state: dict,
    ) -> bool:
        """Probe whether the mirror has a
        coord-head on the remote.  Returns True iff a verified head
        is fetchable; False on absent / verify-failed / network
        error (the latter caller treats as "we can't tell —
        conservative refuse")."""
        import signing
        import mirror as _mirror
        _url = state.get('url', '')
        _ssh_key = state.get('ssh_key') or None
        if not _url:
            return False
        _coord_url = _mirror.coord_root_for(_url)
        _stage = os.path.join(
            self.config.dir_cache, 'mirror', mirror_name, 'probe-head')
        try:
            os.makedirs(_stage, mode=0o755, exist_ok=True)
        except OSError:
            return False
        _ok, _det, _head = _mirror.probe_sidecar_head(
            _coord_url,
            signing_homedir=signing.signing_home(self.config),
            stage_dir=_stage,
            ssh_key=_ssh_key,
        )
        # probe_sidecar_head's three return shapes:
        #   (True, 'no head yet ...', None) → fresh remote, NO head
        #   (True, 'coord-head verified', dict) → head present
        #   (False, '...', None) → verify failed / unreachable
        return _ok and _head is not None

    def _mirror_recompute_base(self, mirror_name: str) -> str:
        """Recompute mirror.base from the latest
        fetched claims.  Returns the oldest snapshot timestamp across
        all non-retracted claims; empty string when the cache is empty
        (preserves the seed-at-add-time value via the merge in
        update_mirror_state).

        Reads from cache/mirror/<name>/fetched/claims/ which the
        publish path just refreshed before the per-file push.  We
        don't re-fetch: the per-publish flow already pulled the
        sidecar at Step 2 of remote_publish.
        """
        import coord.identity as _id
        import coord.store as _store
        import coord.schema as _schema
        _fetched = os.path.join(
            self.config.dir_cache, 'mirror', mirror_name, 'fetched')
        _claims_dir = os.path.join(_fetched, 'claims')
        _keyring_dir = os.path.join(_fetched, 'keyring', 'builders')
        if not os.path.isdir(_claims_dir):
            return ''
        try:
            _keyring = _id.load_keyring(_keyring_dir)
        except (OSError, ValueError):
            return ''
        try:
            _by_builder = _store.read_all_claims(_claims_dir, _keyring, {})
        except OSError:
            return ''
        _oldest: 'Optional[str]' = None
        for _bid, _claims in _by_builder.items():
            # / LEDGER-01: fold supersession back-refs so the base
            # advances past retracted/deprecated/obsoleted (and reclaimed)
            # snapshots — only LIVE claims pin the oldest snapshot floor.
            _dead = _schema.superseded_seqs(_claims)
            for _c in _claims:
                if _schema.is_superseded_claim(_c, _dead):
                    continue
                _ts = str(_c.get('snapshot') or '').strip()
                if not _ts:
                    continue
                if _oldest is None or _ts < _oldest:
                    _oldest = _ts
        return _oldest or ''

    def _mirror_pull_write_build_records(
        self, mirror_name: str,
        per_pkg: 'dict[str, list[tuple[dict, str]]]',
    ) -> None:
        """Per-package build.json writer for
        `cmd_mirror_pull`.

        For each source package in `per_pkg` (mapping
        package_name → list of (claim, owner_builder)), write (or
        update) a local `<pkg>.build.json` record reflecting the pulled
        state:

          - If any claim has `republished_from` (tunneled on mirror) →
            local record `phase='tunneled'`, `republished_from` copied
            verbatim per-filename.
          - Otherwise `phase='done'`.
          - EITHER way the record carries `pulled_from`
            `{mirror_name, owner_builder}`: these files were ADOPTED from
            the mirror, so `generate_pending_claims` skips them and the
            peer never re-publishes/re-claims them — INCLUDING adopted
            tunnels, which the original republisher still ships (a peer
            re-pushing byte-identical copies is pure duplication).  A
            SELF-tunnel (`cmd_tunnel_package`) carries NO `pulled_from`
            and stays claimable as the first shipper.

        SKIPS packages with no claims in `per_pkg`.  Skips on write
        failure (logged, but the pull itself succeeded — the .deb is
        on disk; the record is best-effort metadata).
        """
        if not per_pkg:
            return
        _buildlog = os.path.join(self.config.dir_log, 'build')
        try:
            os.makedirs(_buildlog, exist_ok=True)
        except OSError as _e:
            logger.warning(
                f"mirror pull: could not create buildlog dir "
                f"{_buildlog}: {_e}")
            return
        for _pkg_name, _items in per_pkg.items():
            if not _items:
                continue
            _claims = [_c for _c, _ in _items]
            _outputs = sorted({str(_c.get('filename') or '') for _c in _claims
                               if _c.get('filename')})
            _output_hashes = {
                str(_c.get('filename') or ''): str(_c.get('sha256') or '')
                for _c in _claims if _c.get('filename')
            }
            # republished_from is per-file; collect across the package's
            # claims.  Empty when none are tunneled.
            _republished_from: 'dict[str, dict]' = {}
            for _c in _claims:
                _rfrom = _c.get('republished_from')
                if isinstance(_rfrom, dict) and _rfrom:
                    _fn = str(_c.get('filename') or '')
                    if _fn:
                        _republished_from[_fn] = _rfrom
            _is_tunneled = bool(_republished_from)
            # Builder we ADOPTED these files from (the remote claim's
            # owner / republisher).  Captured for tunneled adoptions TOO:
            # a tunnel pulled off a mirror was adopted from whoever
            # republished it, so pulled_from must be set and the peer must
            # NOT re-publish it.  All claims for one source share an owner.
            _owner_builder = ''
            for _c, _bid_remote in _items:
                if _bid_remote:
                    _owner_builder = str(_bid_remote)
                    break
            _now = utils._utc_now_iso()
            # Use the first claim's metadata for the record header
            # (intended_version, built_version, finished/built_at).
            _head_claim = _claims[0]
            _built_version = str(_head_claim.get('built_version') or '')
            _intended_version = str(_head_claim.get('intended_version')
                                    or _built_version)
            # Idempotent: rewrite the record.  Existing local record
            # (if any) is overwritten — the .deb on disk is the
            # authoritative artefact; the build.json is a derived
            # description of it.
            # Component from the first claim that carries it; defaults
            # to 'main' for pre-component peer claims.  All claims for
            # one source share a component.
            _comp = 'main'
            for _c in _claims:
                _claim_comp = str(_c.get('component') or '')
                if _claim_comp:
                    _comp = _claim_comp
                    break
            _existing = utils.read_build_record(_buildlog, _pkg_name)
            if _existing is not None:
                _rec = dict(_existing)
            else:
                _rec = utils.new_build_record(
                    package=_pkg_name,
                    intended_version=_intended_version,
                    patch_set_hash='',  # we didn't build it
                    started=_now,
                    component=_comp,
                )
            _rec.update({
                'package':          _pkg_name,
                'intended_version': _intended_version,
                'built_version':    _built_version,
                'phase':            'tunneled' if _is_tunneled else 'done',
                'status':           'TUNNELED' if _is_tunneled else 'PASS',
                'finished':         _now,
                'elapsed_seconds':  0.0,
                'exit_code':        0,
                'oom_killed':       False,
                'output_count':     len(_outputs),
                'outputs':          _outputs,
                'output_hashes':    _output_hashes,
                'republished_from': _republished_from,
                # Set for tunneled adoptions too — see docstring: an
                # adopted tunnel is NOT re-claimed (only the original
                # republisher ships it); a self-tunnel has no pulled_from.
                'pulled_from':      {
                    'mirror_name':    mirror_name,
                    'owner_builder':  _owner_builder,
                },
                'component':        _comp,
            })
            try:
                utils.write_build_record(_buildlog, _rec)
            except OSError as _e:
                logger.warning(
                    f"mirror pull: write build.json for {_pkg_name}: {_e}")

    def cmd_mirror_audit(self, *args):
        """mirror audit [<name>] — federation consistency + signature integrity.

        Per mirror (or just <name>):
          1. Fetch coord-head + keyring + claims from the peer
          2. tier-1 verify coord-head; Ed25519-verify every claim line
          3. Federation neighbours match local config (CRITICAL on diff)
          4. Hash-conflict scan across builders (CRITICAL → operator
             must run `mirror conflict resolve`)
          5. Cross-mirror pool-SHA consistency (when 2+ mirrors and
             they overlap on the same filename, fail if SHAs disagree)

        Read-only; never writes to any mirror.  When more than one mirror
        is present, cross-mirror checks run AFTER per-mirror checks so
        per-mirror anomalies are surfaced first.
        """
        import mirror as _mirror
        import signing
        import coord.head as _head_mod
        import coord.identity as _id
        import coord.store as _store
        import coord.transport as _transport
        import coord.reconcile as _reconcile

        _target = args[0] if args else None
        if _target is not None:
            if _mirror.read_mirror_state(self.config, _target) is None:
                console.print(
                    f"mirror audit: unknown mirror {_target!r}",
                    tui.COLOR_ERROR)
                return False
            _names = [_target]
        else:
            _names = _mirror.list_mirrors(self.config)
        if not _names:
            console.print("mirror audit: no mirrors configured.",
                          tui.COLOR_WARNING)
            return True
        _local_urls = _mirror.all_mirror_urls(self.config)
        _signing_home = signing.signing_home(self.config)
        # Per-mirror collection so cross-mirror checks can compare.
        _per_mirror: 'list[dict]' = []
        _all_ok = True
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            assert _st is not None
            _url = _st.get('url', '')
            _ssh_key = _st.get('ssh_key') or None
            _coord_url = _mirror.coord_root_for(_url)
            _coord_spec, _ = _mirror.rsync_spec_for_url(_coord_url)
            console.print(f"[{_n}] {_url}", tui.COLOR_HIGHLIGHT)
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _n, 'fetched')
            os.makedirs(_fetched, exist_ok=True)
            _ok, _detail = _transport.pull_remote_coord(
                local_dest=_fetched, remote_spec=_coord_spec,
                ssh_key=_ssh_key,
            )
            if not _ok:
                console.print(
                    f"  CRITICAL  unreachable: {_detail}",
                    tui.COLOR_ERROR)
                _all_ok = False
                _per_mirror.append({'name': _n, 'head': None,
                                    'by_builder': {}, 'url': _url})
                continue
            _head = _head_mod.read_coord_head(_fetched, _signing_home)
            if _head is None:
                console.print(
                    "  CRITICAL  coord-head verify failed or absent",
                    tui.COLOR_ERROR)
                _all_ok = False
                _per_mirror.append({'name': _n, 'head': None,
                                    'by_builder': {}, 'url': _url})
                continue
            _keyring = _id.load_keyring(
                os.path.join(_fetched, 'keyring', 'builders'))
            _revoked = _head.get('revoked_builders') or {}
            _by_builder = _store.read_all_claims(
                os.path.join(_fetched, 'claims'), _keyring, _revoked)
            _per_mirror.append({'name': _n, 'head': _head,
                                'by_builder': _by_builder, 'url': _url})
            # Federation gate
            _fed = _reconcile.check_federation_consistency(_local_urls, _head)
            _fed_crit = [_f for _f in _fed if _f.severity == 'CRITICAL']
            if _fed_crit:
                for _f in _fed_crit:
                    console.print(
                        f"  {_f.severity:8s}  {_f.kind}: {_f.message}",
                        tui.COLOR_ERROR)
                _all_ok = False
            # Hash conflict scan
            _conf = _reconcile.detect_hash_conflicts(_by_builder)
            _conf_crit = [_f for _f in _conf if _f.severity == 'CRITICAL']
            for _f in _conf_crit:
                console.print(
                    f"  {_f.severity:8s}  {_f.kind}: {_f.message}",
                    tui.COLOR_ERROR)
                _all_ok = False
            # Integrity sweep — Phase 8: claim ↔ on-disk pool dir
            # listing cross-check (orphan .debs the sidecar doesn't
            # know about, claimed filenames missing from disk).  Costs
            # one ssh `find` per ssh mirror.
            _disk_findings = self._mirror_audit_disk_vs_claims(
                _n, _st, _by_builder)
            _disk_crit = [_f for _f in _disk_findings
                          if _f[0] == 'CRITICAL']
            for _sev, _kind, _msg in _disk_findings:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if _disk_crit:
                _all_ok = False
            # InRelease/Packages chain verification.  Pulls remote
            # InRelease, verifies its sha256 matches coord-head's pin,
            # parses its SHA256 block, pulls + verifies each main
            # Packages file, cross-checks every claim against the apt
            # index.  Catches: pool ↔ apt-index drift, claim references
            # that aren't in the published Packages, claim sha that
            # disagrees with what apt would serve.
            _codename = str(self.config.build_codename).strip('"').strip("'")
            _release, _ir_findings = _mirror.audit_inrelease_against_head(
                pool_url=_url, codename=_codename,
                expected_sha256=str(_head.get('inrelease_sha256') or ''),
                fetched_dir=os.path.join(_fetched, 'apt'),
                ssh_key=_ssh_key,
            )
            _ir_crit = [_f for _f in _ir_findings if _f[0] == 'CRITICAL']
            for _sev, _kind, _msg in _ir_findings:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if _ir_crit:
                _all_ok = False
            _pkg_idx: 'dict[str, dict]' = {}
            _pkg_findings: 'list[tuple[str, str, str]]' = []
            if _release is not None:
                # No components= → audit every binary Packages index the
                # verified InRelease declares (main + the udeb
                # debian-installer tree + doc/tests/firmware/…).  Self-
                # discovery means claims for files outside main no longer
                # false-flag, and intentionally-empty components produce no
                # spurious "missing pin" warnings.
                _pkg_idx, _pkg_findings = _mirror.audit_packages_chain(
                    pool_url=_url, codename=_codename, release=_release,
                    fetched_dir=os.path.join(_fetched, 'apt'),
                    ssh_key=_ssh_key,
                    arches=(self.config.arch,),
                )
            _pkg_crit = [_f for _f in _pkg_findings if _f[0] == 'CRITICAL']
            for _sev, _kind, _msg in _pkg_findings:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if _pkg_crit:
                _all_ok = False
            _claim_idx_crit: 'list' = []
            _ledger_crit: 'list' = []
            if _pkg_idx:
                _claim_idx_findings = _mirror.audit_claims_vs_packages(
                    _by_builder, _pkg_idx)
                _claim_idx_crit = [_f for _f in _claim_idx_findings
                                   if _f[0] == 'CRITICAL']
                # First 10 findings inlined; rest summarised so a single
                # corrupt index doesn't flood the screen.
                for _sev, _kind, _msg in _claim_idx_findings[:10]:
                    _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                              else tui.COLOR_WARNING)
                    _print_audit_finding(_sev, _kind, _msg, _color)
                if len(_claim_idx_findings) > 10:
                    console.print(
                        f"  …and {len(_claim_idx_findings) - 10} more "
                        "(use coord query for the full list)",
                        tui.COLOR_WARNING)
                if _claim_idx_crit:
                    _all_ok = False
                # Closure ledger validation (CLOSURE-LEDGER): re-resolve the
                # published closure over the verified Packages + confirm the
                # signed on-mirror ledger matches it.  CRITICAL gates the
                # audit; absent ledger (old owner) → no findings.
                import coord.config_manifest as _cfgman
                _ledger_sha = str(_head.get('closure_ledger_sha256') or '')
                _lg_ok, _lg_detail, _signed_ledger = (
                    _cfgman.read_verified_closure_ledger(_fetched, _ledger_sha))
                if _ledger_sha and not _lg_ok:
                    _print_audit_finding(
                        'CRITICAL', 'closure_ledger_unverified', _lg_detail,
                        tui.COLOR_ERROR)
                    _ledger_crit = [('CRITICAL', 'closure_ledger_unverified',
                                     _lg_detail)]
                    _all_ok = False
                elif _signed_ledger is not None:
                    import selection_lock as _sl_lg
                    _llock, _lst = _sl_lg.read_selection_state(self.config)
                    _cbins = (
                        set((_llock.get('closure') or {}).get('bins', {}))
                        if _lst == _sl_lg.STATUS_OK and _llock else set())
                    # concatenate the InRelease-verified Packages bytes (carry
                    # Depends) for the closure re-resolution.
                    _apt_dir = os.path.join(_fetched, 'apt')
                    _texts: 'list[str]' = []
                    try:
                        for _pf in sorted(os.listdir(_apt_dir)):
                            if not _pf.startswith('Packages.'):
                                continue
                            with open(os.path.join(_apt_dir, _pf),
                                      encoding='utf-8',
                                      errors='replace') as _fh:
                                _texts.append(_fh.read())
                    except OSError:
                        pass
                    _lg_findings = _mirror.audit_closure_ledger(
                        _signed_ledger, _pkg_idx, _cbins, '\n'.join(_texts))
                    _ledger_crit = [_f for _f in _lg_findings
                                    if _f[0] == 'CRITICAL']
                    for _sev, _kind, _msg in _lg_findings[:10]:
                        _print_audit_finding(
                            _sev, _kind, _msg,
                            tui.COLOR_ERROR if _sev == 'CRITICAL'
                            else tui.COLOR_WARNING)
                    if len(_lg_findings) > 10:
                        console.print(
                            f"  …and {len(_lg_findings) - 10} more closure "
                            "ledger finding(s)", tui.COLOR_WARNING)
                    if _ledger_crit:
                        _all_ok = False
            # Sidecar JSONL structural integrity — seq monotonicity
            # within each builder.  Independent of cryptographic
            # signature verification (already done by read_all_claims);
            # this catches replays + manual edits + truncations.
            _seq_findings = _mirror.audit_sidecar_seq_integrity(
                _by_builder)
            _seq_crit = [_f for _f in _seq_findings
                         if _f[0] == 'CRITICAL']
            for _sev, _kind, _msg in _seq_findings:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if _seq_crit:
                _all_ok = False
            # Ownership summary — surfaces who owns what across the
            # federation.  No findings emitted at this layer (hash
            # conflicts handled by detect_hash_conflicts above); the
            # summary is informational so the operator can spot a
            # surprising distribution at a glance.
            try:
                _our_bid = self._coord_builder_id()
            except (AttributeError, OSError):
                _our_bid = None
            # Our-own-claims on-disk rehash.  Costs O(our_claim_count)
            # disk reads but worth it: catches pool bitrot on our side
            # that the apt-index chain would silently propagate.
            # `buildlog_dir` lets the helper distinguish
            # "local build ahead of remote" (WARNING) from real bitrot
            # (CRITICAL).
            # follow-up: pass the signed selection's source set so the
            # auditor can downgrade a missing own-claim file to INFO when the
            # next `mirror publish` will RELEASE it (deprecate dropped sources
            # / obsolete superseded versions) instead of flagging CRITICAL —
            # so an operator who pruned-before-publishing (or forgot the
            # cleanup message) sees a calm "will be …d on next publish".
            _closure_srcs = None
            if _our_bid:
                import selection_lock as _sl0
                _lk0, _ls0 = _sl0.read_selection_state(self.config)
                if _ls0 == _sl0.STATUS_OK and _lk0 is not None:
                    _closure_srcs = set(
                        (_lk0.get('closure') or {}).get('srcs', {}))
            _own_disk_findings = _mirror.audit_own_claims_on_disk(
                _by_builder, _our_bid,
                local_repo_dir=self.config.dir_repo,
                buildlog_dir=os.path.join(self.config.dir_log, 'build'),
                closure_srcs=_closure_srcs)
            _own_disk_crit = [_f for _f in _own_disk_findings
                              if _f[0] == 'CRITICAL']
            for _sev, _kind, _msg in _own_disk_findings:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_INFO if _sev == 'INFO'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if _own_disk_crit:
                _all_ok = False
            # Foreign cross-toolchain assurance — any file we own+publish
            # whose binary targets a non-build arch is a by-product that
            # must never ship (the Issue-2 kfreebsd binutils case).  Phases
            # at claim/repo level prevent new ones; this catches historical
            # leaks.  Remedy: `mirror withdraw-foreign`.
            _foreign_findings = _mirror.audit_foreign_target_claims(
                _by_builder, _our_bid, self.config.arch)
            for _sev, _kind, _msg in _foreign_findings:
                _print_audit_finding(_sev, _kind, _msg, tui.COLOR_ERROR)
            if _foreign_findings:
                _all_ok = False
            # coherence: the signed selection.state ⟷ our published
            # claims.  CRITICAL when we still own+publish a file whose SOURCE
            # left the selection but was never deprecated (keyed on source, not
            # binary — a source build publishes -dev/-doc/-udeb variants that
            # are legitimately outside the install closure).
            if _our_bid:
                import selection_lock as _sl
                _lock, _lstatus = _sl.read_selection_state(self.config)
                if _lstatus == _sl.STATUS_OK and _lock is not None:
                    _closure_srcs = set(
                        (_lock.get('closure') or {}).get('srcs', {}))
                    _coh_findings = _sl.audit_selection_coherence(
                        _closure_srcs, _by_builder, _our_bid,
                        buildlog_dir=os.path.join(
                            self.config.dir_log, 'build'),
                        read_build_record=utils.read_build_record)
                    for _sev, _kind, _msg in _coh_findings:
                        _print_audit_finding(
                            _sev, _kind, _msg,
                            tui.COLOR_ERROR if _sev == 'CRITICAL'
                            else tui.COLOR_WARNING)
                    # One crisp action line instead of repeating the remedy
                    # on every pending finding.
                    _n_pending = sum(
                        1 for _f in _coh_findings
                        if _f[1] == 'selection_deprecation_pending')
                    if _n_pending:
                        console.print(
                            f"  {_n_pending} deprecation(s) pending — "
                            "`mirror publish` to propagate the release",
                            tui.COLOR_WARNING)
                    # Pending deprecations (WARNING) are the expected
                    # accept→publish window — only CRITICALs fail the audit.
                    if any(_f[0] == 'CRITICAL' for _f in _coh_findings):
                        _all_ok = False
                elif _lstatus in (_sl.STATUS_BADSIG, _sl.STATUS_MALFORMED):
                    console.print(
                        f"  CRITICAL  selection_state_{_lstatus}: the signed "
                        "selection authority is untrusted — `cache purge-state` "
                        "to re-baseline", tui.COLOR_ERROR)
                    _all_ok = False
            _own_summary, _ = _mirror.audit_ownership_summary(
                _by_builder, our_builder_id=_our_bid)
            _own_line = (
                f"  ownership   we_own={_own_summary['we_own']}  "
                f"peers_own={_own_summary['peers_own']}  "
                f"tunneled={_own_summary['tunneled']}  "
                f"total_filenames={_own_summary['total']}")
            if _own_summary['by_peer']:
                _peer_list = ', '.join(
                    f"{_pid}={_n}"
                    for _pid, _n in sorted(
                        _own_summary['by_peer'].items()))
                _own_line += f"  (peers: {_peer_list})"
            console.print(_own_line, tui.COLOR_INFO)
            _total = sum(len(_v) for _v in _by_builder.values())
            if (not _fed_crit and not _conf_crit and not _disk_crit
                    and not _ir_crit and not _pkg_crit
                    and not _claim_idx_crit and not _seq_crit
                    and not _own_disk_crit and not _ledger_crit):
                console.print("  audit ok", tui.COLOR_HIGHLIGHT)
                for _label, _result in (
                    ('claims',       f"{_total} across "
                                     f"{len(_by_builder)} builder(s)"),
                    ('federation',   "neighbours match local config"),
                    ('pool/sidecar', "on-disk pool matches sidecar"),
                    ('apt index',    "InRelease + Packages chain verified"),
                    ('sidecar seq',  "integrity ok"),
                ):
                    console.print(f"    {_label:<14}{_result}")
        # Cross-mirror pool-SHA consistency
        if len(_per_mirror) > 1:
            import coord.schema as _schema
            console.print("\ncross-mirror pool-SHA consistency:",
                          tui.COLOR_HIGHLIGHT)
            _seen: 'dict[str, list[tuple]]' = {}
            for _m in _per_mirror:
                for _bid, _claims in _m['by_builder'].items():
                    # fold supersession back-refs.  Without it, ONE
                    # mirror's jsonl contributes both the old published
                    # claim (old sha) and the new reclaim claim (new sha)
                    # for the same filename → a permanent false
                    # cross_mirror_pool_drift CRITICAL after any reclaim,
                    # contradicting the per-mirror detect_hash_conflicts
                    # (which DOES fold) on identical data.
                    _dead = _schema.superseded_seqs(_claims)
                    for _c in _claims:
                        if _schema.is_superseded_claim(_c, _dead):
                            continue
                        _fn = _c.get('filename')
                        _sha = _c.get('sha256') or ''
                        if isinstance(_fn, str) and _fn:
                            _seen.setdefault(_fn, []).append(
                                (_m['name'], _sha))
            _drift = 0
            for _fn, _entries in _seen.items():
                _shas = {_s for _, _s in _entries}
                if len(_shas) > 1:
                    _drift += 1
                    _per = ', '.join(f"{_m}={_s[:12]}" for _m, _s in _entries)
                    console.print(
                        f"  CRITICAL  cross_mirror_pool_drift {_fn}: {_per}",
                        tui.COLOR_ERROR)
                    _all_ok = False
            if _drift == 0:
                console.print(
                    f"  ok        {len(_seen)} filename(s) consistent "
                    "across mirrors")
            # Cross-mirror coord-head federation-membership drift.
            # Compares neighbours + revoked_builders sets pair-wise
            # against the first reachable mirror as reference.  Catches
            # the case where mirror A's coord-head claims federation
            # = {A, B, C} but mirror B's coord-head claims = {A, B} —
            # someone is operating on a stale or forked view.
            _head_drift = _mirror.audit_cross_mirror_head_drift(
                _per_mirror)
            for _sev, _kind, _msg in _head_drift:
                _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                          else tui.COLOR_WARNING)
                _print_audit_finding(_sev, _kind, _msg, _color)
            if any(_f[0] == 'CRITICAL' for _f in _head_drift):
                _all_ok = False
            elif _head_drift == []:
                console.print(
                    "  ok        coord-head federation membership "
                    "consistent across mirrors")
        # Federation walk — pull coord-heads of every neighbour
        # declared by a reachable mirror that ISN'T itself configured
        # locally.  Verifies the wider federation graph is symmetric
        # and signature-clean.  Single hop; the operator can extend by
        # adding peers as local mirrors.
        _walk_cache = os.path.join(
            self.config.dir_cache, 'mirror', '_federation_walk')
        os.makedirs(_walk_cache, exist_ok=True)
        # Use the first configured mirror's ssh key if available; most
        # federations share an admin key.  Multi-key federations need
        # per-peer key resolution which we defer until an operator asks.
        _walk_key = None
        for _m in _per_mirror:
            _st_walk = _mirror.read_mirror_state(self.config, _m['name'])
            if _st_walk and _st_walk.get('ssh_key'):
                _walk_key = _st_walk.get('ssh_key')
                break
        _walked, _walk_findings = _mirror.audit_federation_walk(
            _per_mirror, signing_home=_signing_home,
            cache_dir=_walk_cache, ssh_key=_walk_key,
        )
        if _walked or _walk_findings:
            console.print("\nfederation walk:", tui.COLOR_HIGHLIGHT)
        for _sev, _kind, _msg in _walk_findings:
            _color = (tui.COLOR_ERROR if _sev == 'CRITICAL'
                      else tui.COLOR_WARNING)
            _print_audit_finding(_sev, _kind, _msg, _color)
        if any(_f[0] == 'CRITICAL' for _f in _walk_findings):
            _all_ok = False
        if _walked and not any(
                _f[0] == 'CRITICAL' for _f in _walk_findings):
            console.print(
                f"  ok        {len(_walked)} non-local peer(s) "
                "reachable, signed, and symmetric")
        return _all_ok

    def cmd_mirror_query(self, *args):
        """mirror query <pkg> [<name>] — show every claim matching <pkg>.

        Walks cache/mirror/<name>/fetched/claims/ for each configured mirror
        (or just <name>); for each claim whose `package` field equals <pkg>,
        prints: filename, built_version, builder, snapshot, built_at, mirror.

        Read-only against the LAST fetched state.  Run `mirror pull <name>`
        (or `mirror audit`) first to refresh the local snapshot of remote
        sidecars before querying.
        """
        if not args:
            console.print(
                "Usage: mirror query <pkg> [<mirror-name>]",
                tui.COLOR_ERROR)
            return False
        _pkg = args[0]
        _name_filter = args[1] if len(args) > 1 else None
        import mirror as _mirror
        import coord.identity as _id
        import coord.store as _store
        import coord.schema as _schema
        _names = _mirror.list_mirrors(self.config)
        if _name_filter:
            if _name_filter not in _names:
                console.print(
                    f"mirror query: unknown mirror {_name_filter!r}",
                    tui.COLOR_ERROR)
                return False
            _names = [_name_filter]
        _hits = 0
        for _n in _names:
            _fetched = os.path.join(
                self.config.dir_cache, 'mirror', _n, 'fetched')
            _keyring_dir = os.path.join(_fetched, 'keyring', 'builders')
            _claims_dir = os.path.join(_fetched, 'claims')
            if not os.path.isdir(_claims_dir):
                continue
            _keyring = _id.load_keyring(_keyring_dir)
            _by_builder = _store.read_all_claims(_claims_dir, _keyring, {})
            for _bid, _claims in _by_builder.items():
                # fold supersession back-refs so a superseded
                # original published claim isn't shown as a current claim
                # alongside its successor (reclaim / obsolescence).
                _dead = _schema.superseded_seqs(_claims)
                for _c in _claims:
                    if _c.get('package') != _pkg:
                        continue
                    if _schema.is_superseded_claim(_c, _dead):
                        continue
                    if _hits == 0:
                        console.print(
                            f"{'mirror':<16s} {'builder':<16s} "
                            f"{'version':<24s} {'snapshot':<18s} "
                            f"{'built_at':<22s} filename",
                            tui.COLOR_INFO)
                    _hits += 1
                    console.print(
                        f"{_n:<16s} {_bid:<16s} "
                        f"{_c.get('built_version', ''):<24s} "
                        f"{_c.get('snapshot', ''):<18s} "
                        f"{_c.get('built_at', ''):<22s} "
                        f"{_c.get('filename', '')}")
        if _hits == 0:
            console.print(
                f"mirror query: no claim for package {_pkg!r} across "
                f"{len(_names)} mirror(s).  Run `mirror pull` to refresh "
                "the local view of remote sidecars.")
        return True

    def cmd_mirror_reconcile_neighbours(self, *args):
        """mirror reconcile-neighbours [<name>] — re-propagate the canonical
        federation membership to every peer (or just <name>).

        Pulls each peer's coord-head, compares its `neighbours` field to the
        local config's mirror URL set, and re-signs + pushes back if they
        differ.  Per-peer flock; tier-1 GPG signing happens locally (key
        never leaves the host).  Fail-loud on any unreachable peer.

        Typical operator triggers:
          - just after `mirror add <name> <url>` (the new peer's neighbours
            need to include every existing peer; existing peers need to
            include the newcomer)
          - just after `mirror remove <url>` (every remaining peer needs
            their neighbours updated to drop the removed URL)
          - federation-gate WARN/CRITICAL surfaced by `mirror audit`
        """
        import mirror as _mirror
        import signing as _signing
        _target = args[0] if args else None
        _ok, _summary, _results = _mirror.reconcile_neighbours(
            self.config,
            signing_homedir=_signing.signing_home(self.config),
            target_name=_target,
        )
        console.print(
            f"mirror reconcile-neighbours: {_summary}",
            tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
        for _r in _results:
            _color = (tui.COLOR_NORMAL if _r['ok'] and not _r['changed']
                      else tui.COLOR_HIGHLIGHT if _r['ok']
                      else tui.COLOR_ERROR)
            console.print(
                f"  {_r['name']:<24s}  {_r['detail']}", _color)
        return _ok

    def cmd_mirror_status(self, *args):
        """On-disk health overview.  Phase 1 surfaces what state is durable;
        network reachability + last-publish freshness land in Phase 3."""
        import mirror as _mirror
        import coord.reconcile as _reconcile
        import coord.store as _store
        # Identity + halt sentinel
        _bid = self._coord_builder_id()
        if _bid:
            console.print(
                f"builder-id: {_bid}", tui.COLOR_HIGHLIGHT)
            _claims_path = _store.claims_path(
                self.config.dir_coord_claims, _bid)
            if os.path.isfile(_claims_path):
                try:
                    with open(_claims_path, 'rb') as _fh:
                        _lines = sum(1 for _ in _fh)
                except OSError:
                    _lines = 0
                _seq = _store.max_seq(self.config.dir_coord_claims, _bid)
                console.print(
                    f"  claims jsonl: {_lines} line(s); last seq = {_seq}")
            else:
                console.print(f"  claims jsonl: <none at {_claims_path}>")
        else:
            console.print(
                "builder-id: <not initialized> — run `mirror init <id>`",
                tui.COLOR_WARNING)
        _halt = _reconcile.publish_halt_reason(self.config.dir_coord)
        if _halt is not None:
            console.print(
                f"PUBLISH_HALT: {_halt}", tui.COLOR_ERROR)
        else:
            console.print("PUBLISH_HALT: clear")
        # Per-mirror state
        _names = ([args[0]] if args
                  else _mirror.list_mirrors(self.config))
        if not _names:
            console.print("\nno mirrors configured.")
            return True
        console.print("\nmirrors:")
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            if _st is None:
                console.print(
                    f"  {_n}: NOT REGISTERED", tui.COLOR_ERROR)
                continue
            _published = bool(_st.get('last_publish_at'))
            _tag = 'PUBLISHED' if _published else 'NEVER PUBLISHED'
            console.print(
                f"  {_n}: {_tag}  "
                f"current={_st.get('current', '') or '(unset)'}")
        return True

    def _coord_builder_id(self) -> 'Optional[str]':
        """Read the persisted builder-id from coord/BUILDER_ID.  Returns
        None if not yet initialized.  Trimmed of trailing whitespace."""
        _path = os.path.join(self.config.dir_coord, 'BUILDER_ID')
        try:
            with open(_path, 'r', encoding='utf-8') as _fh:
                _bid = _fh.read().strip()
        except (OSError, FileNotFoundError):
            return None
        return _bid or None

    def cmd_mirror_init(self, *args):
        """Initialize this build host's mirror identity (Ed25519 keypair).

        Usage: mirror init <builder-id>

        Builder-id is operator-chosen, ASCII (no spaces, slashes, '..').
        Constructs an Ed25519 keypair at:
          coord/identity/<id>.pem  (private, mode 0600)
          coord/identity/<id>.pub  (public, mode 0644)

        Refuses to clobber an existing keypair — to rotate, delete
        coord/BUILDER_ID + coord/identity/<id>.* explicitly.  Writes the
        builder-id to coord/BUILDER_ID so subsequent `mirror publish` /
        `mirror pull` / `mirror audit` resolve it automatically.

        After init: the operator manually registers the public key on
        the mirror host (out-of-band) before the first `mirror publish`
        bootstraps the federation.
        """
        import coord.identity as _id
        if not args:
            console.print("Usage: mirror init <builder-id>",
                          tui.COLOR_ERROR)
            return False
        _bid = args[0]
        if (not _bid or '/' in _bid or '..' in _bid
                or _bid != _bid.strip()
                or any(_c.isspace() for _c in _bid)):
            console.print(
                f"mirror init: invalid builder-id {_bid!r} — "
                "use ASCII without spaces, slashes, '..'",
                tui.COLOR_ERROR)
            return False
        _existing = self._coord_builder_id()
        if _existing and _existing != _bid:
            console.print(
                f"mirror init: BUILDER_ID already set to {_existing!r}; "
                f"refusing to switch to {_bid!r}.  Delete coord/BUILDER_ID "
                "explicitly if rotation is intended.",
                tui.COLOR_ERROR)
            return False
        try:
            _priv, _pub = _id.generate_keypair(
                self.config.dir_coord_identity, _bid)
        except OSError as _e:
            console.print(f"mirror init: keypair generation failed: {_e}",
                          tui.COLOR_ERROR)
            return False
        _bid_path = os.path.join(self.config.dir_coord, 'BUILDER_ID')
        try:
            utils._atomic_write_bytes(
                _bid_path, (_bid + '\n').encode('utf-8'))
        except OSError as _e:
            console.print(f"mirror init: persist BUILDER_ID: {_e}",
                          tui.COLOR_ERROR)
            return False
        console.print(
            f"mirror init: builder-id = {_bid}", tui.COLOR_HIGHLIGHT)
        console.print(f"  private:  {_priv}")
        console.print(f"  public:   {_pub}")
        console.print(
            "\nRegister the public key on each mirror host (out-of-band) "
            "before first publish:")
        console.print(
            f"  scp {_pub} <mirror-host>:<mirror-coord-root>/"
            f"keyring/builders/{_bid}.pub")
        return True

    def cmd_mirror_builders(self, action: str = '', *args):
        """mirror builders <list|register> — federation builder registry.

        list      — local builder + the <id>.pub keyring fetched by the most
                    recent `mirror pull` / `mirror audit`.
        register  — register THIS builder on a mirror (upload our pubkey;
                    adopt the canonical pkg.list/pool.list).
        """
        _table = {
            'list':     'local builder + the registered keyring',
            'register': 'register this builder on a mirror (upload pubkey + '
                        'adopt canonical lists; needs signing key + SSH write)',
            'decommission': 'retire a builder <id>: revoke it so its claims '
                            'drop and its packages become no-owner (peers '
                            'take them)',
        }
        if action == 'register':
            return self.cmd_mirror_builders_register(*args)
        if action == 'decommission':
            return self.cmd_mirror_builders_decommission(*args)
        if action not in ('', 'list'):
            return self._group_help('mirror builders', _table, action)
        return self._cmd_builders_list()

    def _cmd_builders_list(self):
        """List registered builders: the local builder (coord/BUILDER_ID) +
        every <id>.pub in the fetched keyring."""
        import coord.identity as _id
        _self = self._coord_builder_id()
        if _self:
            console.print(f"local builder: {_self}", tui.COLOR_HIGHLIGHT)
            _pub = _id.builder_pub_path(self.config.dir_coord_identity, _self)
            if os.path.isfile(_pub):
                console.print(f"  pubkey: {_pub}")
            else:
                console.print(
                    f"  WARNING: pubkey missing at {_pub} — re-run "
                    "`mirror init <id>`",
                    tui.COLOR_WARNING)
        else:
            console.print(
                "local builder: <not initialized> — run `mirror init <id>`",
                tui.COLOR_WARNING)
        _keyring_dir = os.path.join(
            self.config.dir_coord_fetched, 'keyring', 'builders')
        _ring = _id.load_keyring(_keyring_dir)
        if _ring:
            console.print(
                f"\nregistered (from {_keyring_dir}):", tui.COLOR_HIGHLIGHT)
            for _bid, _path in sorted(_ring.items()):
                _marker = ' (self)' if _bid == _self else ''
                console.print(f"  {_bid}{_marker}  {_path}")
        else:
            console.print(
                f"\nno registered remote builders at {_keyring_dir}")
        return True

    def cmd_mirror_builders_register(self, *args):
        """mirror builders register <mirror> — register THIS builder on a
        mirror: upload our Ed25519 pubkey to keyring/builders/ so peers can
        verify our claims.  Gated on a verified tier-1 signing key AND SSH
        write access (the upload proves it)."""
        import mirror as _mirror
        import coord.transport as _transport
        import signing
        if not args:
            console.print("Usage: mirror builders register <mirror-name>")
            return False
        _name = args[0]
        # Gate: builder identity present (mirror init)
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _priv, _pub = _keys
        # Gate: tier-1 signing key verifiable (same gate as `mirror add`)
        _key_ok, _key_msg = signing.verify_key(self.config)
        if not _key_ok:
            console.print(
                "mirror builders register: REFUSED — tier-1 signing key not "
                f"verifiable ({_key_msg}).  Run `key generate` + `key verify` "
                "first.", tui.COLOR_ERROR)
            return False
        # Resolve the target mirror
        _st = _mirror.read_mirror_state(self.config, _name)
        if _st is None:
            console.print(
                f"mirror builders register: unknown mirror '{_name}' — run "
                "`mirror add` first (or `mirror list`).", tui.COLOR_ERROR)
            return False
        _coord_spec, _ = _mirror.rsync_spec_for_url(
            _mirror.coord_root_for(_st.get('url', '')))
        _ssh_key = _st.get('ssh_key') or None
        _remote_pub = (_coord_spec.rstrip('/')
                       + f'/keyring/builders/{_bid}.pub')
        console.print(
            f"mirror builders register: uploading {_bid}.pub -> {_name} "
            "keyring/builders/", tui.COLOR_HIGHLIGHT)
        _ok, _detail = _transport.push_jsonl(
            local_path=_pub, remote_spec=_remote_pub, ssh_key=_ssh_key)
        if not _ok:
            console.print(
                f"mirror builders register: pubkey upload FAILED ({_detail}) "
                f"— check SSH write access to {_name}.", tui.COLOR_ERROR)
            return False
        console.print(
            f"registered builder '{_bid}' on mirror '{_name}'.",
            tui.COLOR_HIGHLIGHT)
        # Adopt the owner's canonical pkg.list/pool.list (overwrite local on
        # register) so this builder matches the federation's selection.
        import coord.head as _head_mod
        _fetched = os.path.join(
            self.config.dir_cache, 'mirror', _name, 'fetched')
        os.makedirs(_fetched, exist_ok=True)
        _ok_f, _ = _transport.pull_remote_coord(
            local_dest=_fetched, remote_spec=_coord_spec, ssh_key=_ssh_key)
        if _ok_f:
            _h = _head_mod.read_coord_head(
                _fetched, signing.signing_home(self.config))
            self._apply_canonical_config(_fetched, _h)
            # Adopt the federation's snapshot pin from the head — a peer builds
            # against the MIRROR's snapshot, not the tracked build.conf default.
            if _h is not None:
                self._adopt_snapshot_forward_from_head(_h, f"mirror '{_name}'")
        return True

    def _adopt_snapshot_forward_from_head(self, head, source_label):
        """Adopt the federation's snapshot pin FORWARD from a verified coord
        head — the mirror's snapshot is authoritative; never roll backward.
        Writes snapshot.state + invalidates cache/dep flags.  Returns the
        adopted pin, or None when nothing changed.

        Uses the CHEAP local pin (snapshot.state) rather than the networked
        `_snapshot_current`, so a fresh peer (empty state) adopts the mirror's
        pin outright instead of comparing against the tracked build.conf
        baseline."""
        if not getattr(self.config, 'snapshot_enabled', False):
            return None
        import mirror as _mirror
        _remote = ''
        _hs = head.get('snapshot') if isinstance(head, dict) else None
        if isinstance(_hs, dict):
            _remote = str(_hs.get('current') or '')
        _local = str((utils.read_snapshot_state(self.config) or {}).get(
            'current') or '')
        _adopt = _mirror.snapshot_adopt_forward(_local, _remote)
        if not _adopt:
            return None
        utils.write_snapshot_state(self.config, current=_adopt)
        self.flags.cache_ready = False
        self.flags.dep_check_ready = False
        console.print(
            f"  snapshot: adopted {_adopt} from {source_label} "
            f"(was {_local or 'unset'}) — re-run `cache build` + `cache parse`",
            tui.COLOR_WARNING)
        return _adopt

    def _apply_canonical_config(self, fetched_dir, head):
        """Verify + apply the mirror's canonical pkg.list/pool.list (from the
        fetched coord tree) against the signed head's config_sha256.  Silent
        no-op when the head carries no config pin (owner hasn't published one
        yet); loud on a verified apply or a verify failure."""
        import coord.config_manifest as _cfgman
        _sha = head.get('config_sha256') if isinstance(head, dict) else None
        if not _sha:
            return
        _ok, _detail, _payload = _cfgman.apply_canonical_config(
            fetched_dir, str(_sha),
            self.config.pkglist_path, self.config.poollist_path,
            self.config.livelist_path, self.config.installerlist_path)
        console.print(
            f"  config: {_detail if _ok else 'NOT applied (' + _detail + ')'}",
            tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_WARNING)
        if _ok and _payload is not None:
            self._seed_selection_from_payload(_payload)
        if _ok:
            # Lists changed under us — force a re-resolve against them.
            self.flags.cache_ready = False
            self.flags.dep_check_ready = False

    def _seed_selection_from_payload(self, payload):
        """Seed config/selection.state from a federation-applied canonical
        config (the owner's pins+closure + this peer's just-written lists),
        signed with THIS peer's HMAC key.  The peer's next `cache parse` then
        adopts the federation selection instead of self-baselining.  Skipped in
        build mode (no selection lock there)."""
        if getattr(self.config, 'build_mode', 'distribution') == 'build':
            return
        import selection_lock as _sl
        try:
            _state = _sl.federation_state(self.config, payload)
            _sl.write_selection_state(self.config, _state)
            console.print("  selection: adopted federation selection.state",
                          tui.COLOR_INFO)
        except Exception as _e:                       # noqa: BLE001
            console.print(
                f"  selection: could not seed selection.state ({_e})",
                tui.COLOR_WARNING)

    def cmd_mirror_builders_decommission(self, *args):
        """mirror builders decommission <builder-id> [<mirror>] — retire a
        builder: add it to the coord-head's revoked_builders so its claims
        drop federation-wide and every package it owned becomes no-owner
        (peers take them on their next publish).  Needs the tier-1 signing
        key + SSH write.  Targets one mirror, or all if none given."""
        import mirror as _mirror
        import coord.publish as _publish
        import signing
        if not args:
            console.print(
                "Usage: mirror builders decommission <builder-id> [<mirror>]")
            return False
        _target_id = args[0]
        if _target_id == self._coord_builder_id():
            console.print(
                "mirror builders decommission: refusing to revoke the LOCAL "
                "builder — you would lock yourself out of publishing.",
                tui.COLOR_ERROR)
            return False
        _key_ok, _key_msg = signing.verify_key(self.config)
        if not _key_ok:
            console.print(
                "mirror builders decommission: REFUSED — tier-1 signing key "
                f"not verifiable ({_key_msg}).", tui.COLOR_ERROR)
            return False
        _resp = Prompt(
            PROMPT_YESNO,
            f"Revoke builder '{_target_id}' across the federation? Its "
            "claims drop and its packages become no-owner.").get_response()
        if not _resp:
            console.print("decommission: cancelled.")
            return False
        _names = [args[1]] if len(args) > 1 else _mirror.list_mirrors(self.config)
        if not _names:
            console.print("decommission: no mirrors configured.",
                          tui.COLOR_WARNING)
            return False
        _signing_home = signing.signing_home(self.config)
        _our_bid = self._coord_builder_id()
        _all_ok = True
        for _n in _names:
            _st = _mirror.read_mirror_state(self.config, _n)
            if _st is None:
                console.print(f"  unknown mirror '{_n}' — skipping",
                              tui.COLOR_WARNING)
                _all_ok = False
                continue
            _coord_spec, _ssh_host = _mirror.rsync_spec_for_url(
                _mirror.coord_root_for(_st.get('url', '')))
            _ok, _detail = _publish.revoke_builder(
                builder_id_to_revoke=_target_id,
                config=self.config,
                remote_coord_spec=_coord_spec,
                signing_homedir=_signing_home,
                ssh_host=_ssh_host,
                ssh_key=_st.get('ssh_key') or None,
                our_builder_id=_our_bid,
                on_status=lambda _m: console.print(f"  {_m}"))
            console.print(
                f"  {_n}: {_detail}",
                tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
            _all_ok = _all_ok and _ok
        return _all_ok

    def _coord_self_keys(self) -> 'Optional[tuple[str, str, str]]':
        """(builder_id, private_path, public_path) for this builder, or
        None if not yet initialized.  All three present + readable on
        success; one missing → None + warning."""
        import coord.identity as _id
        _bid = self._coord_builder_id()
        if not _bid:
            console.print(
                "mirror: builder not initialized — run `mirror init <id>`",
                tui.COLOR_ERROR)
            return None
        _priv = _id.builder_priv_path(self.config.dir_coord_identity, _bid)
        _pub = _id.builder_pub_path(self.config.dir_coord_identity, _bid)
        if not (os.path.isfile(_priv) and os.path.isfile(_pub)):
            console.print(
                f"mirror: keypair missing at {_priv} / {_pub} — re-run "
                "`mirror init <id>`",
                tui.COLOR_ERROR)
            return None
        return _bid, _priv, _pub

    def cmd_mirror_conflict(self, action: str = '', *args):
        """mirror conflict — operator-driven conflict resolution.

        Usage:
          mirror conflict resolve <pkg> [--keep <builder-id>]
              Retract our local claim for <pkg> if --keep names a
              different builder (we lose).  If --keep names us (or is
              omitted), no retraction; only PUBLISH_HALT is cleared.
              The kept builder's claim survives; the loser's claim
              is replaced with a signed retraction line.
        """
        if action == 'resolve':
            return self.cmd_mirror_conflict_resolve(*args)
        _table = {
            'resolve <pkg>': 'retract our claim for pkg; clear PUBLISH_HALT',
        }
        return self._group_help('mirror conflict', _table, action)

    def cmd_mirror_conflict_resolve(self, *args):
        """Operator-driven: retract our claim for `pkg` if --keep
        identifies a different builder; otherwise just clear the halt
        sentinel.

        Usage: mirror conflict resolve <pkg> [--keep <builder-id>]
        """
        import coord.publish as _publish
        if not args:
            console.print(
                "Usage: mirror conflict resolve <pkg> [--keep <builder-id>]",
                tui.COLOR_ERROR)
            return False
        _pkg = args[0]
        _keep = None
        _i = 1
        while _i < len(args):
            if args[_i] == '--keep' and _i + 1 < len(args):
                _keep = args[_i + 1]
                _i += 2
            else:
                _i += 1
        _keys = self._coord_self_keys()
        if _keys is None:
            return False
        _bid, _priv, _pub = _keys
        if _keep is not None and _keep != _bid:
            _ok, _detail = _publish.retract_claim(
                builder_id=_bid, config=self.config,
                private_key_path=_priv, public_key_path=_pub,
                package=_pkg, target_seq=None,
            )
            console.print(
                f"mirror conflict resolve: {_detail}",
                tui.COLOR_HIGHLIGHT if _ok else tui.COLOR_ERROR)
            if not _ok:
                return False
            # the build record mirrors the retraction (the
            # current episode is archived to history first).
            try:
                _bl = os.path.join(self.config.dir_log, 'build')
                _rec = utils.read_build_record(_bl, _pkg)
                if _rec is not None:
                    utils.roll_lifecycle_history(
                        _rec, state='retracted')
                    _rec.update({'selection': 'retracted',
                                 'retracted_at': utils._utc_now_iso()})
                    utils.write_build_record(_bl, _rec)
                else:
                    utils.upsert_build_record(
                        _bl, _pkg, selection='retracted',
                        retracted_at=utils._utc_now_iso())
            except OSError as _e:
                logger.warning(f"retract lifecycle stamp {_pkg}: {_e}")
        else:
            console.print(
                f"mirror conflict resolve: keep={_keep or _bid} (us); "
                "no retraction needed")
        # Clear PUBLISH_HALT — operator has triaged
        _halt_path = os.path.join(
            self.config.dir_coord,
            'PUBLISH_HALT',
        )
        try:
            os.unlink(_halt_path)
            console.print(f"  PUBLISH_HALT cleared at {_halt_path}",
                          tui.COLOR_HIGHLIGHT)
        except FileNotFoundError:
            console.print("  PUBLISH_HALT was already clear")
        except OSError as _e:
            console.print(f"  WARNING: could not remove {_halt_path}: {_e}",
                          tui.COLOR_WARNING)
        return True
