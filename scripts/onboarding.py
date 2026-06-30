"""First-run onboarding wizard (interactive only).

A fresh checkout has no machine identity: no build mode, no mirror, no
federation registration.  Rather than let the operator stumble through
`cache build` prompts and half-configured state, this wizard runs once on
the first interactive launch and establishes:

  - system Role: ``first`` (origin/owner — bootstraps the federation, always
    distribution mode) vs ``federation`` (a peer joining an existing mirror).
  - the federation registration handshake for a peer (MANDATORY): a guided,
    validate-as-you-go flow — public URL (probed for the asgard-mirror marker),
    SSH login/path/key (auth + write probed, key copied into config/), tier-1
    GPG private key (imported + verified against the mirror's signed coord-head),
    builder identity, ``mirror add`` + ``mirror builders register``.  Typing
    ``cancel`` at any prompt aborts cleanly; re-running ``configure`` restarts.
  - build Mode (a federation peer may pick build or distribution; a first
    system is forced to distribution — build mode needs a published baseline).
  - the build snapshot pin.

Everything is recorded in the UNTRACKED config/local.conf via
``utils.write_local_conf`` (SetupComplete + Role + Mode + the per-mirror
registration marker), so the wizard never re-prompts and a peer never has to
re-register.  Hooked from build.py:main() before the banner; skipped entirely
for headless / --cmd / --api / --yes runs and for an already-set-up box.
"""
import os

import signing
import tui
import utils
from tui import console, Prompt, PROMPT_INPUT, PROMPT_OPTIONS


# Commands allowed before the box is configured.  `configure` itself, the
# read-only inspectors (get/print), and the backend control/navigation
# built-ins (help/clear/history/quit/exit — you must always be able to read
# state and leave).  Everything else (the build pipeline, mirror ops, etc.) is
# gated and refused with a `configure` hint.
ALLOW_BEFORE_CONFIGURE = frozenset({
    'configure', 'get', 'print', 'version', 'config',
    'help', 'clear', 'history', 'quit', 'exit',
})


def command_allowed(config, name: str) -> bool:
    """Gate predicate (wired into the backends): any command is allowed once
    the box is configured; before that only the ALLOW_BEFORE_CONFIGURE set."""
    return (bool(getattr(config, 'setup_complete', False))
            or name in ALLOW_BEFORE_CONFIGURE)


def configured_summary(config) -> str:
    """One-line startup confirmation of the configured state, or a warning if
    something looks half-configured (e.g. a federation peer with no mirror
    registered)."""
    _role = getattr(config, 'system_role', '') or 'unknown'
    _mode = getattr(config, 'build_mode', 'distribution')
    import mirror as _mirror
    _regs = sorted(_mirror.get_registration(config).keys())
    if _role == 'federation' and not _regs:
        return ("Configured as a federation peer but NO mirror is registered "
                "— run `configure` to register.")
    _reg = f", registered: {', '.join(_regs)}" if _regs else ""
    return f"Configured: role={_role}, mode={_mode}{_reg}"


def _ask(ptype, message, options=None):
    """Prompt with a uniform `cancel` escape.  Returns None when the operator
    types cancel/q (caller aborts the flow); otherwise the stripped response.
    For OPTIONS, `cancel` is added as a selectable choice."""
    if ptype == PROMPT_OPTIONS and options:
        _resp = Prompt(ptype, message, list(options) + ['cancel']).get_response()
    else:
        _resp = Prompt(ptype, message).get_response()
    _resp = (_resp or '').strip()
    if _resp.lower() in ('cancel', 'q'):
        return None
    return _resp


def _ask_required(message):
    """Like `_ask(PROMPT_INPUT, …)` but re-prompts on an empty value (a blank
    required field must not slip through).  Returns None only on cancel."""
    while True:
        _r = _ask(PROMPT_INPUT, message)
        if _r is None:
            return None
        if _r:
            return _r
        console.print("  (required — enter a value or `cancel`)",
                      tui.COLOR_WARNING)


def _dist_id_of(config):
    """The distribution id used for the served repo path (DISTRIBUTION
    lowercased, alnum/_/- only) — matches scripts/mirror.py + prep-mirror.sh."""
    import re
    return re.sub(r'[^a-z0-9_-]', '',
                  str(getattr(config, 'build_distribution', '') or '').lower())


def run_onboarding(session) -> None:
    """Drive the `configure` wizard.  Runs as an ordinary command in the
    backend's command loop, so prompts work natively.  Persists SetupComplete
    (file + in-memory) only on full success; a cancelled/failed branch leaves
    the box un-configured so the gate keeps refusing until `configure` wins."""
    config = session.config
    console.print("")
    console.print("── Athena setup ──", tui.COLOR_HIGHLIGHT)

    _role = _ask(PROMPT_OPTIONS, "System role", options=['first', 'federation'])
    if _role == 'federation':
        _ok = _onboard_federation(session)
    elif _role == 'first':
        _ok = _onboard_first(session)
    else:
        _ok = False                       # cancel / no backend

    if not _ok:
        console.print("Not configured. Run `configure` to retry.",
                      tui.COLOR_WARNING)
        return

    # Per-machine settings the config split moved into local.conf — builder
    # Name, parallel Jobs, signing UID.  Lean: each is a single Enter-to-accept
    # prompt with a sensible default.  Change any later with `set <key>`.
    if not _prompt_machine_settings(session):
        console.print("Not configured. Run `configure` to retry.",
                      tui.COLOR_WARNING)
        return

    # Snapshot pin (a federation peer already adopted the mirror's pin during
    # register; this only prompts an origin / unadopted box).  Tolerant — the
    # cache-build prompt is the fallback.
    try:
        session._ensure_snapshot_pins()
    except Exception as _e:                       # noqa: BLE001 — best-effort
        console.print(f"(snapshot pin deferred to `cache build`: {_e})",
                      tui.COLOR_INFO)

    utils.write_local_conf(config, setup_complete=True)
    config.setup_complete = True          # in-memory: opens the command gate
    console.print("✓ setup complete — all commands enabled.",
                  tui.COLOR_HIGHLIGHT)

    # Post-setup sanity check: now that the snapshot is pinned and any mirror is
    # registered, validate the config + probe mirror reachability (setup_complete
    # is True, so `config check` runs the full snapshot + reachability pass).
    console.print("")
    try:
        session.cmd_config('check')
    except Exception as _e:               # noqa: BLE001 — best-effort
        console.print(f"(post-setup config check skipped: {_e})",
                      tui.COLOR_INFO)


def _prompt_machine_settings(session) -> bool:
    """Prompt for the per-machine build settings the config split moved into
    local.conf.  Only parallel Jobs is asked; the builder NAME and signing UID
    are DERIVED from identities the role flow already established (asking for
    them again would be confusing duplicates):

      - NAME mirrors the builder identity (coord/identity/<id>, the `Builder id`
        prompted in the role flow).  `set name` can override it later.
      - signing UID is read from the signing key actually on disk (origin named
        + generated it in _onboard_first; a federation peer imported the tier-1
        key under its own UID) — never a free-text value that could mismatch the
        key and break verify.

    Persists to local.conf + reflects into the live config.  False only on
    explicit cancel."""
    config = session.config
    # [Local] Name = the builder identity already established by the role flow
    # (federation prompts `Builder id`; origin runs _ensure_builder_identity).
    _name = ''
    try:
        _name = session._coord_builder_id() or ''
    except Exception:                     # noqa: BLE001 — best-effort
        _name = ''
    _name = _name or getattr(config, 'system_name', '')

    _default_jobs = min(os.cpu_count() or 1, 8)
    _jobs_raw = _ask(PROMPT_INPUT, f"Parallel build jobs [{_default_jobs}]")
    if _jobs_raw is None:
        return False
    try:
        _jobs = int(_jobs_raw) if _jobs_raw else _default_jobs
    except ValueError:
        console.print(
            f"  jobs: {_jobs_raw!r} is not an integer — using {_default_jobs}",
            tui.COLOR_WARNING)
        _jobs = _default_jobs
    _clamped = max(1, min(_jobs, 8))
    if _clamped != _jobs:
        # Mirror `set jobs`: surface the clamp instead of silently adjusting.
        console.print(
            f"  jobs clamped to {_clamped} (1-8, docker-py pool limit)",
            tui.COLOR_WARNING)
    _jobs = _clamped

    # Signing UID is NOT prompted here — it must match the key actually on disk.
    # Adopt the real UID of the signing key established by the role flow: the
    # origin named + generated it earlier (in _onboard_first), a federation peer
    # imported the tier-1 key with its own UID.  Fall back to the current/default
    # value only when no key exists yet (an origin that declined a mirror).
    _uid = signing.actual_signing_uid(config) or getattr(
        config, 'signing_key_uid', '') or 'Athena Build <athena@local>'

    utils.write_local_conf(config, name=(_name or None),
                           max_parallel_builds=_jobs, signing_key_uid=_uid)
    if _name:
        config.system_name = _name
    config.max_parallel_builds = _jobs
    config.signing_key_uid = _uid
    console.print(
        f"  machine: name={_name or '(builder id)'}, jobs={_jobs}, "
        f"signing={_uid}", tui.COLOR_INFO)
    return True


def _onboard_first(session) -> bool:
    """Origin/owner branch.  Role=first, mode forced to distribution; an
    optional publish mirror."""
    config = session.config
    console.print("First/origin system — mode forced to distribution.",
                  tui.COLOR_INFO)
    config.build_mode = 'distribution'
    config.system_role = 'first'
    utils.write_local_conf(config, role='first', mode='distribution')

    _enable = _ask(PROMPT_OPTIONS, "Enable a publish mirror now?",
                   options=['yes', 'no'])
    if _enable is None:
        return False
    if _enable != 'yes':
        console.print("No mirror — add one later with `mirror add`.",
                      tui.COLOR_INFO)
        return True

    _ok, _msg = signing.verify_key(config)
    if not _ok:
        # Name the key BEFORE generating it: the UID is baked in at gen time and
        # is how the key is looked up afterwards, so prompting after generation
        # could not rename it (and a config/key mismatch breaks verify).  Origin
        # only — a federation peer ADOPTS the imported tier-1 key's UID instead.
        _uid = _ask(PROMPT_INPUT,
                    f"Repo signing identity [{config.signing_key_uid}]")
        if _uid is None:
            return False
        if _uid:
            config.signing_key_uid = _uid
            utils.write_local_conf(config, signing_key_uid=_uid)
        console.print("Generating tier-1 signing key…", tui.COLOR_INFO)
        if session.cmd_key('generate') is False:
            console.print("✗ key generation failed", tui.COLOR_ERROR)
            return False
    if not _ensure_builder_identity(session):
        return False
    # Origin uses the legacy explicit add (no mirror to validate against yet).
    return bool(_add_mirror_interactive(session))


def _onboard_federation(session) -> bool:
    """Federation-peer branch: a guided, validate-as-you-go join.  Each input
    is checked against the live mirror before the next; keys are installed
    automatically; `cancel` at any prompt aborts (nothing durable past the
    idempotent key copy/import is written, so re-running restarts cleanly)."""
    config = session.config
    console.print("Federation peer — type `cancel` at any prompt to abort.",
                  tui.COLOR_INFO)

    # 1. Public URL → validate keylessly (mirror-info.json marker).  The repo
    # is served at http(s)://<host>/<dist-id> (e.g. .../asgard) — that path is
    # required.  Reconstruct a clean URL (drop any user@ the operator pasted
    # from the ssh form) before probing.
    _url = _ask_required("Mirror URL (http(s)://host/<dist-id>):")
    if _url is None:
        return False
    _proto, _host, _path = _parse_public_url(_url)
    if not _host:
        console.print("✗ enter an http(s)://host/<dist-id> URL (no user@)",
                      tui.COLOR_ERROR)
        return False
    _pub = f"{_proto}://{_host}{_path}"
    _ok, _det = session._mirror_is_prepared(_pub)
    if not _ok:
        console.print(f"✗ not a working Asgard mirror ({_det})", tui.COLOR_ERROR)
        if not _path:
            console.print("  (did you include the repo path, e.g. /asgard?)",
                          tui.COLOR_INFO)
        return False
    console.print(f"✓ working mirror ({_host})", tui.COLOR_INFO)

    # 2. SSH bits → copy key, validate auth + write.
    _login = _ask_required(f"SSH login user@host (user, host={_host}):")
    if _login is None:
        return False
    _user, _sshhost = _split_login(_login, _host)
    if not _user:
        console.print("✗ enter a user (or user@host)", tui.COLOR_ERROR)
        return False
    # Remote repo path = the repo's FILESYSTEM path on the mirror
    # (prep-mirror.sh's ~<user>/<dist-id>); offer that as the default.
    _dist_id = _dist_id_of(config)
    _default_path = f"/home/{_user}/{_dist_id}" if _dist_id else f"/home/{_user}"
    _rpath = _ask(PROMPT_INPUT, f"Remote repo path [{_default_path}]:")
    if _rpath is None:
        return False
    _rpath = _rpath or _default_path
    _keysrc = _ask_required("SSH key path:")
    if _keysrc is None:
        return False
    _name = _derive_name(_sshhost)
    _keydst = os.path.join(config.dir_config, f"{_name}.key")
    if not _copy_key(_keysrc, _keydst):
        console.print(f"✗ cannot read SSH key {_keysrc}", tui.COLOR_ERROR)
        return False
    import mirror as _m
    _ok, _det = _m.probe_dns_and_tcp(_sshhost, 22)
    if _ok:
        _ok, _det = _m.probe_ssh_auth(_sshhost, _user, _keydst)
    if _ok:
        _ok, _det = _m.probe_remote_writable(_sshhost, _user, _keydst, _rpath)
    if not _ok:
        console.print(f"✗ ssh check failed ({_det})", tui.COLOR_ERROR)
        return False
    console.print("✓ ssh access + write", tui.COLOR_INFO)

    # 3. GPG key → import + verify against the mirror's signed head.
    _gpgsrc = _ask_required("Tier-1 GPG private key path:")
    if _gpgsrc is None:
        return False
    _ssh_url = f"ssh://{_user}@{_sshhost}/{_rpath.lstrip('/')}"
    if not _validate_and_install_gpg(session, _gpgsrc, _ssh_url, _keydst):
        return False
    console.print("✓ gpg key matches the mirror (can publish)", tui.COLOR_INFO)
    console.print(f"keys installed: {_keydst} + signing keyring",
                  tui.COLOR_INFO)

    # 4. Builder identity (this peer's own Ed25519 key — per-machine, created
    # here if absent).  Check via _coord_builder_id (silent) NOT _coord_self_keys
    # (which prints a misleading "run `mirror init`" when none exists yet).
    if not session._coord_builder_id():
        import socket
        _default = _sanitise_bid(socket.gethostname())
        console.print("Creating this peer's builder identity…", tui.COLOR_INFO)
        _bid = _ask(PROMPT_INPUT, f"Builder id [{_default}]:")
        if _bid is None:
            return False
        _bid = _bid or _default
        if session.cmd_mirror('init', _bid) is False:
            console.print("✗ builder init failed", tui.COLOR_ERROR)
            return False

    # 5. Persist + register (we already probed → --no-probe).
    _host_type = 'ip' if _is_ip(_sshhost) else 'fqdn'
    if session.cmd_mirror('add', _host_type, _ssh_url, '--ssh-key', _keydst,
                          '--proto', _proto, '--name', _name,
                          '--no-probe') is False:
        console.print("✗ mirror add failed", tui.COLOR_ERROR)
        return False
    if session.cmd_mirror('builders', 'register', _name) is False:
        console.print("✗ registration failed", tui.COLOR_ERROR)
        return False
    console.print(f"✓ registered on {_name}", tui.COLOR_HIGHLIGHT)
    _keys = session._coord_self_keys()
    config.system_role = 'federation'
    utils.write_local_conf(config, role='federation')
    # Registration marker now lives in the signed mirror.conf, not local.conf.
    import mirror as _mirror
    if not _mirror.set_registration(config, _name, _keys[0] if _keys else ''):
        console.print(
            "✗ could not record registration in mirror.conf", tui.COLOR_ERROR)
        return False                      # stay un-configured + re-runnable

    # 6. Mode.
    _mode = _ask(PROMPT_OPTIONS, "Build mode",
                 options=['distribution', 'build'])
    if _mode is None:
        return False
    config.build_mode = _mode
    utils.write_local_conf(config, mode=_mode)
    console.print(f"✓ mode = {_mode}", tui.COLOR_INFO)
    return True


def _validate_and_install_gpg(session, gpg_src, ssh_url, ssh_key) -> bool:
    """Import the provided tier-1 key into a TEMP homedir, prove it verifies
    the mirror's signed coord-head, then commit it to signing_home + confirm
    the PRIVATE half is usable (publish-capable).  Single-line results."""
    import tempfile
    import mirror as _m
    import coord.transport as _t
    import coord.head as _h
    config = session.config
    if not os.path.isfile(gpg_src):
        console.print(f"✗ gpg key not found: {gpg_src}", tui.COLOR_ERROR)
        return False
    _coord_spec, _ = _m.rsync_spec_for_url(_m.coord_root_for(ssh_url))
    with tempfile.TemporaryDirectory() as _tmp:
        _ok, _det = signing.import_key(_tmp, gpg_src)
        if not _ok:
            console.print(f"✗ gpg import failed ({_det})", tui.COLOR_ERROR)
            return False
        _fetch = os.path.join(_tmp, 'fetched')
        _ok, _det = _t.pull_remote_coord(
            local_dest=_fetch, remote_spec=_coord_spec, ssh_key=ssh_key)
        if not _ok:
            console.print(f"✗ cannot fetch coord tree ({_det})",
                          tui.COLOR_ERROR)
            return False
        if not os.path.isfile(_h.coord_head_path(_fetch)):
            console.print("✗ mirror has no signed head yet (origin must "
                          "publish first)", tui.COLOR_ERROR)
            return False
        if _h.read_coord_head(_fetch, _tmp) is None:
            console.print("✗ gpg key does not match the mirror's signing key",
                          tui.COLOR_ERROR)
            return False
        # Validated → commit into the real signing keyring.
        _ok, _det = signing.import_key(signing.signing_home(config), gpg_src)
        if not _ok:
            console.print(f"✗ gpg install failed ({_det})", tui.COLOR_ERROR)
            return False
    _ok, _msg = signing.verify_key(config)
    if not _ok:
        console.print(f"✗ signing key not usable ({_msg})", tui.COLOR_ERROR)
        return False
    # Export the PUBLIC keyring so build_chroot can embed it (the installed
    # system trusts our mirror via `[signed-by=…]`).  generate_key does this for
    # an origin builder; a federation peer IMPORTED the key, so do it here too —
    # otherwise the keyring file is missing and build_chroot warns.
    _ek, _ed = signing.export_public_keyring(config)
    if not _ek:
        console.print(
            f"  warning: signing keyring export failed ({_ed}) — "
            "run `key export` before build_chroot", tui.COLOR_WARNING)
    return True


def _ensure_builder_identity(session) -> bool:
    """Make sure this box has an Ed25519 builder identity (mirror init).
    Used by the origin branch; the federation branch inlines this."""
    if session._coord_builder_id():       # silent check (no misleading print)
        return True
    import socket
    _default = _sanitise_bid(socket.gethostname())
    _bid = _ask(PROMPT_INPUT, f"Builder id [{_default}]:")
    if _bid is None:
        return False
    _bid = _bid or _default
    if session.cmd_mirror('init', _bid) is False:
        console.print("✗ builder init failed", tui.COLOR_ERROR)
        return False
    return True


def _add_mirror_interactive(session) -> str:
    """Legacy explicit mirror add (origin branch only — no live mirror to
    validate against yet).  Returns the mirror name, or '' on abort."""
    _name = _ask(PROMPT_INPUT, "Mirror name (e.g. 'origin'):")
    if not _name:
        return ''
    _host_type = _ask(PROMPT_OPTIONS, "Host type",
                      options=['ip', 'fqdn', 'local'])
    if _host_type is None:
        return ''
    _url = _ask(PROMPT_INPUT, "Publish URL (ssh://user@host/path or file://):")
    if not _url:
        return ''
    _args = [_host_type, _url, '--name', _name]
    if _host_type != 'local' and not _url.startswith('file://'):
        _key = _ask(PROMPT_INPUT, "SSH key path:")
        if _key:
            _args += ['--ssh-key', _key]
        _proto = _ask(PROMPT_OPTIONS, "Apt URL scheme", options=['http', 'https'])
        if _proto is None:
            return ''
        _args += ['--proto', _proto]
    if session.cmd_mirror('add', *_args) is False:
        console.print("✗ mirror add failed", tui.COLOR_ERROR)
        return ''
    console.print(f"✓ mirror '{_name}' added", tui.COLOR_HIGHLIGHT)
    return _name


def _parse_public_url(url):
    """(proto, host, path) from an http(s)://[user@]host[/path] URL; userinfo
    is dropped (host comes from urlparse.hostname).  ('', '', '') if not http(s)."""
    import urllib.parse as _up
    _p = _up.urlparse(url)
    if _p.scheme not in ('http', 'https') or not _p.hostname:
        return '', '', ''
    return _p.scheme, _p.hostname, _p.path.rstrip('/')


def _split_login(login, default_host):
    """'user@host' or bare 'user' → (user, host-or-default)."""
    if '@' in login:
        _u, _h = login.split('@', 1)
        return _u, (_h or default_host)
    return login, default_host


def _derive_name(host):
    """Mirror name from host: dots/colons → '-' (matches mirror add)."""
    return host.replace('.', '-').replace(':', '-')


def _copy_key(src, dst):
    """Copy an SSH key file to dst with 0600.  False if src unreadable.
    Thin wrapper over utils.copy_ssh_key (the shared implementation)."""
    return utils.copy_ssh_key(src, dst)


def _is_ip(host):
    import ipaddress
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _sanitise_bid(name):
    import re
    _b = re.sub(r'[^A-Za-z0-9._-]', '-', name or 'peer').strip('-')
    return _b or 'peer'
