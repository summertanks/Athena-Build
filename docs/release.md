# Athena-Build — cutting a release

How to take the toolchain from "working build" to "tagged, signed,
published Asgard release that an installed system can `apt update` from".
This is the operator runbook; see [`docs/architecture.md`](architecture.md)
for what the pipeline does at each step.

Most of the moving parts (snapshot pin, signing key, repo publish,
manifest discipline) have their own files — this doc is the **sequence
+ checklist**, not a re-derivation.

## Preconditions

Before cutting a release, confirm:

- [ ] **CONF-01** done — repo layout under `dists/<codename>{,-debug}/`.  Done.
- [ ] **CONF-02** done — signing key + Release signing wired.  Done.
- [ ] **CONF-03**: source-format / quilt patch landing.  **Open**.
      For now, patches live under `patch/source/<pkg>/<ver>/` outside
      `debian/patches/`; see [`docs/patching.md`](patching.md).  Affects
      the *form* of patches but not the release flow itself.
- [ ] `config/build.conf` reflects the target release identity:
      `[Build] NAME` / `DISTRIBUTION` / `CODENAME` / `VERSION`.
- [ ] No staged changes you don't want shipped — `git status` clean.

## 1. Pick the snapshot

The release is anchored to a single `snapshot.debian.org` timestamp.  Every
build / cache / dep-resolve happens against that snapshot, so two operators
running the same release on different days produce identical artifacts.

```ini
# config/build.conf
[Snapshot]
Enabled   = true                        # default; off bypasses pinning
Timestamp = 20260514T083402Z            # or `latest` (resolved once + cached)
```

To advance to a newer snapshot (security uplift): use `snapshot select
current <ts | latest>`.  See memory `project_upd01_update_architecture`
for the base / published / current pin model and the
`config/snapshot.state` durable store.

For a brand-new release, set `Timestamp = latest` and let the first
`cache build` resolve + memoise.  Once the release is cut, **pin** the
literal timestamp explicitly so re-builds remain bit-for-bit reproducible.

## 2. Generate (or rotate) the signing key

The local signed manifest, every `Release`/`InRelease` we ship, and the
keyring that lands on installed systems all flow from one project key.

```
[Repo]
SigningKeyUid = Athena Build <athena@local>
```

```bash
key generate          # one-time; refuses to overwrite without `force`
key verify            # roundtrip sign+verify; prints fingerprint + UID + dates
print signing         # snapshot only, no roundtrip
```

The keypair lives under `gnupg/signing/`; the exported pubkey is at
`gnupg/signing/athena-archive-keyring.gpg`.  `chroot build`'s
`_install_signing_keyring` step copies it into the chroot at
`/usr/share/keyrings/athena-archive-keyring.gpg` (the Debian distro-key
location), and `iso_installer.py:_export_pubkey_to_staging` ships it at
`.disk/archive-key.gpg` on the installer ISO.

**Rotation**: `key generate force` regenerates.  Any prior-signed
`InRelease` files become invalid against the new pubkey — operators with
the old keyring installed will see signature errors on `apt update` until
they pick up the new pubkey (either via re-install or by re-adding it to
`/etc/apt/trusted.gpg.d/`).  Plan rotation around release boundaries.

## 3. Freeze the package lists

The three package lists are the bill of materials for the release:

| File | Selects |
|---|---|
| `config/pkg.list` | The pkg.list closure — base system + tasksel groups (everything in the live image's `dpkg -l`) |
| `config/live.list` | Live-extras — packages pulled into the live chroot but not in the base set |
| `config/installer.list` | Mixed-universe (`.deb` + `.udeb`) — sources the installer ramdisk builds from |
| `config/pool.list` | Third-tier — ships in `/cdrom/pool/` but never installed in any chroot (alternative bootloaders, opportunistic firmware) |

For the release, treat these as *frozen* (don't add new names mid-cycle
without an explicit decision).  Tasksel group keys in `pkg.list` must
mirror `fork/source/athena-tasksel/tasks/<group>` Key entries — pinned
by `test_athena_tasksel_task_keys_mirror_pkg_list_groups`.

## 4. Walk the pipeline

For a clean release cut, drive every stage explicitly so you see each
gate fire.  The `autorun` chains are equivalent and quicker:

```
autorun live          # cache → parse → sync → container → build → chroot → iso live
autorun installer     # same early stages, diverges at installer subset
autorun disk          # live chroot → qcow2
```

Or stage by stage:

```
cache build
cache parse
source sync
container init
source build all          # bare pkg.list closure
source build live         # + live extras
source build installer    # + udeb closure for the installer
chroot build live         # also runs chroot verify; gates on chroot_verified
chroot build installer
iso build live            # bootable hybrid BIOS+EFI ISO
iso build installer
iso build disk            # optional: qcow2 pre-installed image
```

Each stage produces a `BuildFlags` flag; the next stage refuses to run
without it.  See [`docs/architecture.md`](architecture.md#buildflags--the-stage-gate).

### Monitoring

- `print state` — pipeline state, flag readout, counts.
- `print summary` — per-stage timing once autorun completes.
- `log/build/<pkg>` — raw container stdout per source build.
- `log/build/<pkg>.result` — `PASS` / `FAIL`.
- `log/build-YYYY-MM-DDTHH-MM-SS.log` — full structured log.

## 5. Verify the build

Two independent gates before publishing:

1. **`chroot verify`** — 8 checks: filesystem layout, dpkg state,
   keyring presence, identity stripped, etc.  Built into `chroot build`;
   re-run via `chroot verify` if you want a clean re-check.
2. **`repo audit`** — closure + conflict + stale-files + content
   integrity + NMU residue across `repo/`.  Optional `quick` to skip the
   ~30s integrity scan.

For installer-ISO smoke-testing locally:

```
tests/installer_smoke/run.py --mode quick      # ~3 min, "does d-i boot?"
tests/installer_smoke/run.py --mode full       # ~15-30 min, unattended install
```

Real hardware testing remains the bar for declaring COMP-01 done (see
COMP-01h in TODO.md).

## 6. Index and sign the repo

```
repo index full     # apt-ftparchive + GPG-sign Release/InRelease for every suite
```

`repo index full` runs `dpkg-scanpackages` over `repo/dists/<codename>{,-debug}/`,
writes per-subdir `Release`, generates the top-level `Release` via
`apt-ftparchive release`, then signs `Release` (detached `Release.gpg`) and
produces clearsigned `InRelease` using the project key.

For an offline release (no remote transport ever), this is sufficient —
the local signed manifest reflects the indexed pool and operators can
manually move the tree to wherever they need it.

## 7. Publish

Two transports (S3 was scoped out; see done.md COMP-02 + memory
`project_comp02_s3_publish_dropped`):

```
repo publish ssh full                       # rsync + reindex-on-VM
repo publish local /mnt/usb/asgard full     # local-fs copy + in-process reindex
```

Both: ADDITIVE upload, reindex at destination, sign locally, refresh local
manifest, publish-before-prune.  Full operator guide:
[`docs/repo-publish-vm-setup.md`](repo-publish-vm-setup.md).

After publish, point the installed system at it:

```ini
# config/build.conf
[Repo]
AptSourceURL = http://your.repo.example/asgard/
```

On the next `chroot build`, the installed system gets
`/etc/apt/sources.list.d/athena.list`:

```
deb [signed-by=/usr/share/keyrings/athena-archive-keyring.gpg] \
    http://your.repo.example/asgard/ thor main
```

## 8. Verify the published repo

```
repo audit external                              # over HTTP via AptSourceURL
repo audit external ssh                          # reconcile manifest vs remote
repo summary ssh                                 # files / sig / date / pins
repo summary local /mnt/usb/asgard               # same for local destinations
```

`repo audit external` is the "what apt clients actually see" check; it
fetches `InRelease`, verifies the signature against our pubkey, and
checks every index in the signed SHA256 block.

## 9. Tag and record

```bash
# Working-state tag — exact tree that produced the verified ISO
git tag -a working-<release>-<date> -m "verified end-to-end on BIOS + EFI"
git push --tags
```

Update [`README.md`](../README.md) **Project maturity** section if the
release crosses a threshold (e.g. "first end-to-end install on real
hardware" — COMP-01h).

If you bumped `[Build] VERSION`, that becomes the new `R` in
`+asg<R>u<N>` for subsequent updates — N resets to 0 per binary at each
release boundary.  Memory: `project_upd01_update_architecture`.

## 10. Cutting an update vs. a release

| | Release | Update |
|---|---|---|
| Trigger | New `[Build] VERSION` or major scope change | Snapshot advance with security/NMU uplift |
| Snapshot | New pin (possibly `latest` → resolved + frozen) | `snapshot select current <new-ts>` |
| Driver | Operator-driven manual walk | `repo refresh` (thin wrapper: source sync → source build all → repo publish ssh full) |
| Version stamping | Pristine upstream + reset to `+asg<R>u1` per binary | `+asg<R>uN`, N = manifest's max + 1 per binary |
| ISO | Re-cut | Not produced — update flows through apt-only |
| Consumer impact | Fresh install / re-install | `apt update && apt upgrade` |

For the update flow specifically, see memory entry
`project_upd01_update_architecture` — it's the canonical reference for
the append-only / publish-before-prune / per-file N derivation rules.

## Common pitfalls

| Symptom | Likely cause / fix |
|---|---|
| `cache build` re-runs after snapshot change | Expected — snapshot pin changed, cache is invalidated.  Force is unnecessary. |
| `repo audit external` flags signature failure | Pubkey on the consuming side is stale.  Re-roll the chroot (`chroot build` re-installs current keyring) or copy the keyring out manually. |
| `source build` rebuilds the same package every run | Stale `.result` from a prior force; or pristine filename matches a previously-stamped `+asg uN` (see `find_matching_artifact`).  `package strip` + `package audit_nmu` confirms repo state. |
| Update mode lists more packages than `source audit` does | Expected — `source audit` reports needs_build via filename match; update mode adds bump-targets whose pristine filename collides with a prior build.  See `docs/pseudocode.md` "source audit vs source build all". |
| `repo publish` fails at "remote re-index" | `dpkg-dev` not installed on the VM.  `sudo apt-get install -y dpkg-dev`. |
| Real-hardware install fails where VMware succeeds | Firmware (microcode / wifi / video).  Check `installer/finish-install/` ordering and `pool.list` firmware entries; COMP-01h is the umbrella ticket. |

## Cross-references

- [`docs/architecture.md`](architecture.md) — pipeline stages + BuildFlags.
- [`docs/patching.md`](patching.md) — patch tree conventions.
- [`docs/repo-publish-vm-setup.md`](repo-publish-vm-setup.md) — operator
  guide for ssh + local publish + `repo summary`.
- [`docs/branding-methodology.md`](branding-methodology.md) — identity
  override mechanisms.
- [`docs/security.md`](security.md) — what's signed, what's verified, what isn't.
- Memory: `project_upd01_update_architecture`,
  `project_self_contained_repo`, `project_three_layer_identity`,
  `feedback_strip_nmu_at_build`.
