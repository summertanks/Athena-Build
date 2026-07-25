"""Per-architecture facts — the single authority (COMP-04 D2).

The dpkg architecture token is NOT the kernel flavor: dpkg arch
``i386`` boots kernel flavor ``686-pae``, while ``amd64``/``arm64``
happen to reuse their dpkg token.  Before this module, every image
builder collapsed that distinction into the one literal ``amd64``
(the kernel-deb regex existed in three copies).  Everything that
varies BY TARGET ARCH — kernel meta/flavor names, the GRUB toolset
and install targets, EFI removable-media names, microcode roots,
boot-console fallbacks, the d-i stock seed list — lives here and
ONLY here; consumers ask ``profile(config.arch)`` instead of
hardcoding.

Boot-model notes pinned by tests:
  - amd64/i386 media are hybrid BIOS+EFI (grub-pc-bin present,
    ``bios_boot=True`` → BIOS-Boot partition + i386-pc grub-install).
  - arm64 media are GRUB-EFI-only (Debian's own arm64 model:
    ``BOOTAA64.EFI``, no BIOS El-Torito, no i386-pc) →
    ``bios_boot=False`` and every BIOS branch must be skipped.
  - Microcode is an x86 concept; arm64 ships none.
"""

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ArchProfile:
    arch: str                       # dpkg architecture token
    kernel_flavor: str              # kernel image-name flavor suffix
    kernel_meta: str                # metapackage pulling the real kernel
    kernel_headers_meta: str
    grub_bins: 'tuple[str, ...]'    # grub packages the ISO container needs
    grub_efi_target: str            # grub-install --target for EFI
    bios_boot: bool                 # BIOS path exists (El-Torito/i386-pc)
    efi_removable_name: str         # EFI/BOOT/<name> on removable media
    microcode: 'tuple[str, ...]'    # microcode roots ('' on non-x86)
    console_extra: str              # extra kernel cmdline for consoles
    di_stock_seed: str              # upstream d-i pkg-lists cdrom seed
    # Execution model (COMP-04 stage 1.5, B2/B9):
    linux_personality: str          # setarch personality wrapping native
                                    # builds on a WIDER host ('linux32'
                                    # for i386-on-amd64 — Debian's i386
                                    # buildds always run linux32 so
                                    # uname -m reports i686); '' = none
    host_arches: 'tuple[str, ...]'  # host dpkg arches that execute this
                                    # target natively (no emulation)
    # Derived, compiled once per profile:
    kernel_pkg_re: 're.Pattern[str]' = field(init=False)
    kernel_name_re: 're.Pattern[str]' = field(init=False)
    kernel_deb_glob: str = field(init=False)

    def __post_init__(self):
        _flavor = re.escape(self.kernel_flavor)
        # Real kernel .debs carry a numeric ABI + our flavor:
        #   linux-image-6.1.0-47-amd64_6.1.170-3_amd64.deb
        # Metas (linux-image-amd64), other flavors (-rt-, -cloud-) and
        # -dbg do not match — same exclusions the historical
        # iso_installer._KERNEL_PKG_RE enforced for amd64.
        object.__setattr__(self, 'kernel_pkg_re', re.compile(
            rf'^linux-image-(\d+\.\d+\.\d+-\d+)-{_flavor}_'))
        # Installed-package name form (cache keys, dpkg name):
        object.__setattr__(self, 'kernel_name_re', re.compile(
            rf'^linux-image-\d+\.\d+\.\d+-\d+-{_flavor}$'))
        object.__setattr__(
            self, 'kernel_deb_glob',
            f'linux-image-*-{self.kernel_flavor}*.deb')


_PROFILES: 'dict[str, ArchProfile]' = {
    'amd64': ArchProfile(
        arch='amd64',
        kernel_flavor='amd64',
        kernel_meta='linux-image-amd64',
        kernel_headers_meta='linux-headers-amd64',
        grub_bins=('grub-pc-bin', 'grub-efi-amd64-bin'),
        grub_efi_target='x86_64-efi',
        bios_boot=True,
        efi_removable_name='BOOTX64.EFI',
        microcode=('intel-microcode', 'amd64-microcode'),
        console_extra='',
        di_stock_seed='cdrom/amd64.cfg',
        linux_personality='',
        host_arches=('amd64',),
    ),
    'i386': ArchProfile(
        arch='i386',
        kernel_flavor='686-pae',
        kernel_meta='linux-image-686-pae',
        kernel_headers_meta='linux-headers-686-pae',
        grub_bins=('grub-pc-bin', 'grub-efi-ia32-bin'),
        grub_efi_target='i386-efi',
        bios_boot=True,
        efi_removable_name='BOOTIA32.EFI',
        microcode=('intel-microcode', 'amd64-microcode'),
        console_extra='',
        di_stock_seed='cdrom/i386.cfg',
        linux_personality='linux32',
        host_arches=('amd64', 'i386'),
    ),
    'arm64': ArchProfile(
        arch='arm64',
        kernel_flavor='arm64',
        kernel_meta='linux-image-arm64',
        kernel_headers_meta='linux-headers-arm64',
        grub_bins=('grub-efi-arm64-bin',),
        grub_efi_target='arm64-efi',
        bios_boot=False,
        efi_removable_name='BOOTAA64.EFI',
        microcode=(),
        console_extra='console=ttyAMA0',
        di_stock_seed='cdrom/arm64.cfg',
        linux_personality='',
        host_arches=('arm64',),
    ),
}


def profile(arch: str) -> ArchProfile:
    """The ArchProfile for a dpkg arch token.  Raises ValueError on an
    unsupported arch so a typo in distro.conf fails at first use, not
    as a silent amd64 fallback."""
    try:
        return _PROFILES[arch]
    except KeyError:
        raise ValueError(
            f"unsupported architecture {arch!r} — supported: "
            f"{', '.join(sorted(_PROFILES))} (extend arch_profile._PROFILES "
            f"to add one)") from None


def supported_arches() -> 'tuple[str, ...]':
    return tuple(sorted(_PROFILES))


# ── [arch] seed-list qualifiers (COMP-04 D5, stage 2) ───────────────────
# Seed lists stay single-file across arches; an entry may carry a
# dpkg-style architecture restriction:
#     linux-image-amd64 [amd64]
#     grub-pc-bin [amd64 i386]
#     linux-image-arm64 [arm64]
#     foo [!arm64]
# Unqualified lines apply everywhere (today's behavior — the 400+
# existing entries need no migration).  Semantics follow dpkg
# Build-Depends restrictions: a positive list applies when ANY token
# matches the target arch; an all-negated list applies when NO negated
# token matches; mixing negated and plain tokens is an error.  Tokens
# may be dpkg wildcards (linux-any, any-amd64) — matched via dpkg's
# own arch table.

_QUALIFIER_RE = re.compile(r'^(?P<entry>.*?)\s*\[(?P<tokens>[^\]]*)\]\s*$')

_ARCH_TABLE = None


def _arch_table():
    global _ARCH_TABLE
    if _ARCH_TABLE is None:
        from debian.debian_support import DpkgArchTable
        _ARCH_TABLE = DpkgArchTable.load_arch_table()
    return _ARCH_TABLE


def _token_matches(token: str, arch: str) -> bool:
    if token == arch or token == 'any':
        return True
    _r = _arch_table().matches_architecture(arch, token)
    return bool(_r)


def split_qualifier(line: str) -> 'tuple[str, tuple[str, ...]]':
    """Split a seed-list line into (entry, restriction-tokens).  A line
    with no [ ... ] suffix returns (line, ()) — applies to every arch.
    An empty restriction (`foo []`) is an error (silently applying
    nowhere OR everywhere would both surprise)."""
    _m = _QUALIFIER_RE.match(line.strip())
    if not _m:
        return line.strip(), ()
    _entry = _m.group('entry').strip()
    _tokens = tuple(_t for _t in _m.group('tokens').split() if _t)
    if not _tokens:
        raise ValueError(
            f"empty [arch] restriction on seed entry {_entry!r}")
    _negs = [_t for _t in _tokens if _t.startswith('!')]
    if _negs and len(_negs) != len(_tokens):
        raise ValueError(
            f"seed entry {_entry!r} mixes negated and plain arch tokens "
            f"{_tokens} — dpkg restriction lists are all-or-none negated")
    return _entry, _tokens


def entry_applies(line: str, arch: str) -> 'tuple[bool, str]':
    """(applies, bare-entry) for a seed-list line against a target
    arch.  The bare entry has the restriction stripped either way, so
    callers filter and normalize in one step."""
    _entry, _tokens = split_qualifier(line)
    if not _tokens:
        return True, _entry
    if _tokens[0].startswith('!'):
        return (not any(_token_matches(_t[1:], arch) for _t in _tokens),
                _entry)
    return any(_token_matches(_t, arch) for _t in _tokens), _entry


def filter_seed_lines(lines: 'list[str]', arch: str) -> 'list[str]':
    """Order-preserving [arch]-filter over already-stripped seed
    entries: keeps lines applying to *arch*, restriction stripped."""
    _out: 'list[str]' = []
    for _l in lines:
        _ok, _entry = entry_applies(_l, arch)
        if _ok and _entry:
            _out.append(_entry)
    return _out


_HOST_ARCH: 'str | None' = None


def host_arch() -> str:
    """The build host's dpkg architecture (cached).  Falls back to a
    platform.machine() mapping when dpkg is absent (non-Debian host —
    the preflight will almost certainly fail later anyway, but the
    refusal message stays honest)."""
    global _HOST_ARCH
    if _HOST_ARCH is None:
        import subprocess
        try:
            _HOST_ARCH = subprocess.run(
                ['dpkg', '--print-architecture'],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or ''
        except (OSError, subprocess.SubprocessError):
            _HOST_ARCH = ''
        if not _HOST_ARCH:
            import platform
            _HOST_ARCH = {
                'x86_64': 'amd64', 'i686': 'i386', 'aarch64': 'arm64',
            }.get(platform.machine(), platform.machine())
    return _HOST_ARCH


def assert_host_compatible(target_arch: str) -> None:
    """COMP-04 B9 host-arch preflight: refuse loudly AT STARTUP when
    the host cannot natively execute the target arch (an arm64 checkout
    on an amd64 host would otherwise die mid-build with Exec format
    error inside a chroot/container).  i386-on-amd64 is native (32-bit
    personality) and allowed by the profile's host_arches.

    Escape hatch for selection-only analysis runs (cache build/parse
    touch no target-arch binaries): ATHENA_ALLOW_FOREIGN_ARCH=1
    downgrades the refusal to a warning."""
    import os
    _host = host_arch()
    _prof = profile(target_arch)
    if _host in _prof.host_arches:
        return
    _msg = (
        f"target arch {target_arch!r} cannot execute on this "
        f"{_host!r} host (native hosts: {', '.join(_prof.host_arches)})"
        " — builds/chroots/images would die with Exec format error."
        "  Run this checkout on a matching builder, or set"
        " ATHENA_ALLOW_FOREIGN_ARCH=1 for selection-only analysis.")
    if os.environ.get('ATHENA_ALLOW_FOREIGN_ARCH') == '1':
        import logging
        logging.getLogger('athena.utils').warning(
            f"host-arch preflight OVERRIDDEN: {_msg}")
        return
    raise RuntimeError(_msg)
