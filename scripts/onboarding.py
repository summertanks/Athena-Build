"""First-run onboarding wizard (interactive only).

A fresh checkout has no machine identity: no build mode, no mirror, no
federation registration.  Rather than let the operator stumble through
`cache build` prompts and half-configured state, this wizard runs once on
the first interactive launch and establishes:

  - system Role: ``first`` (origin/owner — bootstraps the federation, always
    distribution mode) vs ``federation`` (a peer joining an existing mirror).
  - the federation registration handshake for a peer (MANDATORY): the tier-1
    signing key must already be imported (verified up front), a builder
    identity, ``mirror add`` and ``mirror builders register`` — all validated
    before the box is considered set up.
  - build Mode (a federation peer may pick build or distribution; a first
    system is forced to distribution — build mode needs a published baseline).
  - the build snapshot pin.

Everything is recorded in the UNTRACKED config/local.conf via
``utils.write_local_conf`` (SetupComplete + Role + Mode + the per-mirror
registration marker), so the wizard never re-prompts and a peer never has to
re-register.  Hooked from build.py:main() before the banner; skipped entirely
for headless / --cmd / --api / --yes runs and for an already-set-up box.
"""
import signing
import tui
import utils
from tui import (console, Prompt, PROMPT_YESNO, PROMPT_INPUT,
                 PROMPT_OPTIONS)


# Commands allowed before the box is configured.  Everything else is gated
# (the build pipeline, mirror ops, etc.) and refused with a `configure` hint.
ALLOW_BEFORE_CONFIGURE = frozenset({'configure', 'get', 'print'})


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
    _local = utils.read_local_conf(config)
    _regs = (_local.options('Registration')
             if _local.has_section('Registration') else [])
    if _role == 'federation' and not _regs:
        return ("Configured as a federation peer but NO mirror is registered "
                "— run `configure` to register.")
    _reg = f", registered: {', '.join(_regs)}" if _regs else ""
    return f"Configured: role={_role}, mode={_mode}{_reg}"


def _is_yes(resp: str) -> bool:
    return resp.strip().lower() in ('y', 'yes')


def run_onboarding(session) -> None:
    """Drive the configuration questionnaire (the `configure` command).  Runs
    as an ordinary command in the backend's command loop, so its prompts work
    natively.  Persists SetupComplete (file + in-memory) only when a branch
    fully succeeds; on an aborted/failed branch it leaves the box un-configured
    so the command gate keeps refusing until `configure` succeeds."""
    config = session.config
    console.print("")
    console.print("── Athena first-run setup ──────────────────────",
                  tui.COLOR_HIGHLIGHT)
    console.print(
        "This box isn't set up yet.  A few questions to establish its role.",
        tui.COLOR_INFO)

    _role = Prompt(
        PROMPT_OPTIONS,
        "Is this the FIRST/origin build system, or a FEDERATION peer joining "
        "an existing mirror?",
        options=['first', 'federation']).get_response()

    if _role == 'federation':
        _ok = _onboard_federation(session)
    elif _role == 'first':
        _ok = _onboard_first(session)
    else:
        # Empty response (no backend) — treat as abort, don't mark complete.
        _ok = False

    if not _ok:
        console.print(
            "Setup not completed — you'll be asked again next launch.",
            tui.COLOR_WARNING)
        return

    # Snapshot pin, now that identity exists.  Tolerant: the cache-build
    # prompt is the fallback if this can't run here.
    try:
        session._ensure_snapshot_pins()
    except Exception as _e:                       # noqa: BLE001 — best-effort
        console.print(
            f"(snapshot pin deferred to first `cache build`: {_e})",
            tui.COLOR_INFO)

    utils.write_local_conf(config, setup_complete=True)
    config.setup_complete = True          # in-memory: opens the command gate now
    console.print(
        "Setup complete — recorded in config/local.conf.  All commands are now "
        "enabled; re-run `configure` any time to add a mirror or change mode.",
        tui.COLOR_HIGHLIGHT)


def _onboard_first(session) -> bool:
    """Origin/owner branch.  Role=first, mode forced to distribution; an
    optional publish mirror (tier-1 key + identity + `mirror add`)."""
    config = session.config
    console.print(
        "First/origin system: build mode is forced to DISTRIBUTION (build "
        "mode needs a published baseline from an origin — it can't be the "
        "origin itself).", tui.COLOR_INFO)
    config.build_mode = 'distribution'
    config.system_role = 'first'
    utils.write_local_conf(config, role='first', mode='distribution')

    _enable = Prompt(
        PROMPT_YESNO,
        "Enable a publish mirror now? (generates the tier-1 signing key and "
        "registers a mirror endpoint)").get_response()
    if not _is_yes(_enable):
        console.print(
            "No mirror configured — add one later with `mirror add`.",
            tui.COLOR_INFO)
        return True

    # Tier-1 signing key — generate if not already usable.
    _ok, _msg = signing.verify_key(config)
    if not _ok:
        console.print(f"Generating tier-1 signing key ({_msg})…",
                      tui.COLOR_INFO)
        if session.cmd_key('generate') is False:
            console.print("Key generation failed — aborting mirror setup.",
                          tui.COLOR_ERROR)
            return False
    if not _ensure_builder_identity(session):
        return False
    return bool(_add_mirror_interactive(session, register=False))


def _onboard_federation(session) -> bool:
    """Federation-peer branch.  Registration is MANDATORY and its
    prerequisites are validated BEFORE anything is written."""
    config = session.config
    console.print(
        "Federation peer: you must register to the mirror.  Prerequisites — "
        "the tier-1 signing key copied from the first system, an SSH key with "
        "write access, and the mirror URL.", tui.COLOR_INFO)

    # Validate the tier-1 key UP FRONT — register/verify need the PRIVATE key.
    _ok, _msg = signing.verify_key(config)
    if not _ok:
        console.print(
            f"Tier-1 signing key not usable ({_msg}).  Copy the first "
            "system's signing key into this box's keyring and re-run setup "
            "(see docs/mirror-setup.md).", tui.COLOR_ERROR)
        return False
    console.print("Tier-1 signing key verified.", tui.COLOR_INFO)

    if not _ensure_builder_identity(session):
        return False
    _name = _add_mirror_interactive(session, register=True)
    if not _name:
        return False

    # Record the registration marker so we never re-register this mirror.
    _keys = session._coord_self_keys()
    _bid = _keys[0] if _keys else ''
    config.system_role = 'federation'
    utils.write_local_conf(config, role='federation',
                           registration={_name: _bid})

    # A registered peer may now choose its mode.
    _mode = Prompt(
        PROMPT_OPTIONS,
        "Build mode for this peer — `distribution` (full corpus) or `build` "
        "(just config/build_pkg.list)?",
        options=['distribution', 'build']).get_response()
    if _mode not in ('distribution', 'build'):
        _mode = 'distribution'
    config.build_mode = _mode
    utils.write_local_conf(config, mode=_mode)
    console.print(f"Mode set to {_mode}.", tui.COLOR_INFO)
    return True


def _ensure_builder_identity(session) -> bool:
    """Make sure this box has an Ed25519 builder identity (mirror init)."""
    if session._coord_self_keys() is not None:
        return True
    _bid = Prompt(
        PROMPT_INPUT,
        "Builder id for this box (ASCII, no spaces/slashes, e.g. "
        "'wsl-peer-1'):").get_response().strip()
    if not _bid:
        console.print("No builder id given — aborting.", tui.COLOR_ERROR)
        return False
    if session.cmd_mirror('init', _bid) is False:
        console.print("Builder init failed — aborting.", tui.COLOR_ERROR)
        return False
    return True


def _add_mirror_interactive(session, *, register: bool) -> str:
    """Collect mirror endpoint details, run `mirror add`, and (for a peer)
    `mirror builders register`.  Returns the mirror name on success, '' on
    failure/abort."""
    _name = Prompt(
        PROMPT_INPUT,
        "Short name for this mirror (e.g. 'origin'):").get_response().strip()
    if not _name:
        console.print("No mirror name — aborting.", tui.COLOR_ERROR)
        return ''
    _host_type = Prompt(
        PROMPT_OPTIONS,
        "Mirror host type",
        options=['ip', 'fqdn', 'local']).get_response()
    _url = Prompt(
        PROMPT_INPUT,
        "Publish URL (ssh://user@host/path, or file:///abs/path for local):"
    ).get_response().strip()
    if not _url:
        console.print("No URL — aborting.", tui.COLOR_ERROR)
        return ''

    _args = [_host_type, _url, '--name', _name]
    if _host_type != 'local' and not _url.startswith('file://'):
        _key = Prompt(
            PROMPT_INPUT,
            "Path to the SSH key for this mirror (e.g. "
            "config/repo_asgard.key):").get_response().strip()
        if _key:
            _args += ['--ssh-key', _key]
        _proto = Prompt(
            PROMPT_OPTIONS,
            "Apt URL scheme the mirror is served over (for the installed "
            "system's sources.list)",
            options=['http', 'https']).get_response()
        _args += ['--proto', _proto]

    if session.cmd_mirror('add', *_args) is False:
        console.print("mirror add failed — aborting setup.", tui.COLOR_ERROR)
        return ''

    if register:
        if session.cmd_mirror('builders', 'register', _name) is False:
            console.print(
                "mirror registration failed — check SSH write access and the "
                "tier-1 key, then re-run setup.", tui.COLOR_ERROR)
            return ''
        console.print(f"Registered on mirror '{_name}'.", tui.COLOR_HIGHLIGHT)
    return _name
