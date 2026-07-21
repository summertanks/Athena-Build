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
