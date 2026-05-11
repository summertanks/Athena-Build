# installer/disk/

Files shipped on the ISO root under `.disk/` — convention used by d-i's
`cdrom-detect` and `base-installer` to recognise the disc as a valid
Athena installer and locate install metadata.

Every file in this dir (except `*.md`) is copied verbatim to
`<iso-root>/.disk/<filename>` at iso-build time.  Engine never inspects
contents — operator can rewrite freely.

## v1 files

| File              | Purpose                                             |
|-------------------|-----------------------------------------------------|
| `info`            | One-line text identifier shown by some installers; cdrom-detect parses the QUOTED portion as the codename and uses it to locate `/cdrom/dists/<codename>/Release`.  Without a matching dists/<codename>/, cdrom-detect rejects the disc with "Error reading Release file; unable to determine distribution".  Supports `${codename}` and `${version}` placeholders — the engine substitutes them at iso-build time from `build.conf [Build] CODENAME` and `VERSION` so .disk/info stays in sync with the dists/ subdir name that `_generate_apt_repo` produces. |
| `base_installable`| Empty sentinel file.  base-installer checks `if [ -f /cdrom/.disk/base_installable ]` before debootstrapping — its presence asserts "this disc contains a debootstrappable base system". |
| `base_components` | Single line `main`.  base-installer reads this to know which debootstrap components are present in `/cdrom/pool/`.  Athena's single-component repo always has just `main`. |

## Placeholder substitution

`${codename}` and `${version}` in any file under `installer/disk/` are
substituted at iso-build time with values from `build.conf`.  Uses
`string.Template.safe_substitute` — unknown placeholders pass through
unchanged.  Operator can add `${codename}` / `${version}` wherever
useful.

## Authoritative reference

- base-installer's library.sh checks for these files
- Ubuntu's .disk/ documentation:
  <https://help.ubuntu.com/community/InstallCDCustomization>
- For Athena, edit `info` to taste — the format is loose; the only hard
  requirement is non-emptiness (cdrom-detect's grep needs *something*).
