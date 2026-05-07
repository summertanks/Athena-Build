# Athena-Build — Project TODO

> Serialized, trackable backlog produced from a maturity / stability /
> completeness review of the source tree at the date below. Each task has a
> stable ID (`AREA-NN`); cite the ID when committing or referencing work.
> Update the **Status** column inline rather than renumbering.

- **Review date:** 2026-05-07
- **Reviewer:** Claude (read-only audit of `master` @ `62287ce`)
- **Tree size:** ~7,600 LOC (≈6,200 Python, 281 Bash)
- **Pipeline state:** end-to-end working — `build_cache → parse_dependency →
  source_download → build_container → source_build → build_chroot
  (+verify) → build_iso`
- **Status legend:** `todo | wip | done | blocked | wontfix`
- **Severity legend:** `P0` blocker, `P1` important, `P2` quality-of-life,
  `P3` future / nice-to-have

---

## 0. Analysis summary

The project is more mature than the README suggests. The pipeline already
produces a bootable hybrid BIOS/EFI ISO from-source on Debian bookworm,
with multi-mirror APT cache, snapshot.debian.org pinning, dep-drift
verification against on-disk `.deb` files, a curses TUI with live widgets,
and a chroot-aware install path that handles `usrmerge`, debconf
pre-seeding, the libc bootstrap circular-dep, post-install patch overlays,
and an 8-check chroot verifier that gates ISO build.

**Strengths**

- Clear pipeline-stage architecture (`BuildFlags` gates each step).
- Comments and docstrings explain *why*, not just *what* — load-bearing
  invariants are documented inline (e.g. `_check_dep_drift`,
  `_build_chroot_directories`, snapshot resolution).
- Multi-mirror ingest (main / updates / security) with per-mirror SHA256
  gating and per-source `_mirror` stamping so downloads use the correct pool.
- Sound chroot bring-up: real chroot for `dpkg --configure -a` once dpkg is
  unpacked, chrootless fallback only for the bootstrap rounds.
- Tests + smoke driver exist (`tests/test_module.py`, `tests/smoke_dep_drift.py`).
- Reproducibility hook is in place via Snapshot pinning (off by default).

**Top risks (expanded as P0/P1 tasks below)**

1. **No GPG verification of `InRelease`** — `Cache.__get_files` trusts
   whatever `deb.debian.org` serves; a MITM or compromised mirror can
   inject arbitrary `Packages` entries.
2. **`MaxParallelBuilds` is a lie** — config field exists, parser reads it,
   `source_build` is strictly serial.
3. **`--force-depends` in `dpkg --configure -a`** is acknowledged
   `TEMPORARY` in code (`buildsystem.py:419`, `:428`) and silently masks
   real dep skew. Should fail loudly once snapshot pinning is the default.
4. **No APT-repo metadata** generated for the built `.debs` — `repo/` is a
   bag of files, not a usable apt source. Blocks the “build a derivative
   distribution” end-state.
5. **No installer image** — `build_iso` only produces a *live* ISO via
   `live-boot`. The README/user goal of “including installation” needs a
   d-i / Calamares / custom-installer path.
6. **Module-level globals in `build.py`** (`build_config`, `build_cache`,
   `dependency_tree`, `build_container`, `console`, `_tui`) make the code
   hard to test and reason about; nothing else can drive the pipeline
   programmatically without standing up the whole TUI.
7. **`tui.console` plumbing** is split between `tui.console` (module) and
   `console` (in `build.py`); during early init the order of assignments
   determines who wins. Easy to break by accident.

---

## 1. Stability & correctness — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| STA-01 | P0 | done | Verify mirror `InRelease` GPG signatures against a pinned keyring before trusting `Packages` / `Sources` indices. *(implemented via `utils.verify_inrelease()` + `[Security]` config; default uses host-provided debian-archive-keyring; tests in `test_module.py`)* |
| STA-02 | P1 | done | Remove `--force-depends` from `_configure_chroot` (`buildsystem.py:424,434`) and surface the real dep error. Pre-req: STA-03. *(both chroot and chrootless dpkg invocations now run without --force-depends; STA-03's snapshot pinning + the existing `_check_dep_drift`/`_verify_dep_resolution` chain ensure unresolved deps are caught and raised before configure.  `_ensure_initramfs` repurposed from "workaround for force-depends" to defence-in-depth.)* |
| STA-03 | P1 | done | Make snapshot pinning the **default** in `config/build.conf` so cache and live mirror cannot drift between cache build and source build. *(shipped `[Snapshot] Enabled = true`; lock-in test `test_shipped_build_conf_has_snapshot_enabled` blocks regressions; smoke confirmed snapshot.d.o → InRelease verify chain end-to-end)* |
| STA-04 | P1 | done | Validate downloaded files at the moment they finish writing in `download_source` — currently size and `sha256` are checked, but a non-200 response writes a 0-byte file and the loop continues; surface a clear error for the user. |
| STA-05 | P1 | done | Hardening of `BuildContainer.build` — wrap `client.containers.run` in a try / `finally container.remove()` so failed runs don’t leak Docker containers. Also log container ID on failure. |
| STA-06 | P1 | done | `BuildContainer.__init__` is annotated `-> bool` but returns nothing. Remove the misleading annotation. |
| STA-07 | P1 | done | `cmd_build_chroot` writes the sudo password to `subprocess.run(input=...)` and keeps it in `BuildSystem.__password` for the whole run. Audit lifetime: zero it after `verify_chroot` completes; consider switching to `sudo -A` with an askpass helper. *(scrub_password() + try/finally in cmd_build_chroot/cmd_build_iso/cmd_verify_chroot; .password property raises after scrub. sudo -A askpass left as a future task — bigger refactor with sudo TTL trade-offs.)* |
| STA-08 | P1 | todo | `pre_install` `cmd_list` uses Python-formatted shell strings with `shlex.split` — paths are not quoted. Replace with argv lists (`['sudo','-S','ln','-sfv',...]`). |
| STA-09 | P1 | todo | `Cache.__get_files`: when `_release_url` returns a 0-byte / non-200, current code writes the file then bails generically — preserve and surface HTTP status. |
| STA-10 | P1 | done | `BuildSystem._mount_chroot_fs` — log the mount target and check `/proc/self/mountinfo` after mounting to confirm; current code only warns on `_proc.returncode != 0`. *(uses `os.path.ismount()` rather than parsing mountinfo — stdlib, simpler, reliable for procfs/sysfs/bind targets; raises on failure so partial-mount state aborts the chroot build cleanly via build_chroot's existing finally → _umount_chroot_fs)* |
| STA-11 | P2 | todo | `dependencytree.py:124` references `self._VALID_CONSTRAINTS` from inside `add_lookahead` but the class attribute is defined further down (`:157`) — works only because Python looks it up at call time. Move definition above first use. |
| STA-12 | P2 | todo | `cache.py:323-333` GCC-base selection uses string startswith / split heuristics — replace with `apt_pkg.parse_depends` on Provides graph, or an explicit allowlist in config. |
| STA-13 | P2 | todo | `_verify_chroot` reads `/etc/os-release` with a bare `open(_os_release).read()` — wrap in `with open()` or it leaks a file descriptor on the chroot path. |
| STA-14 | P2 | todo | `download_file` HEAD followed by GET means two requests per file; some mirrors throttle. Consolidate by using the `Content-Length` from the first GET response. |
| STA-15 | P2 | todo | `parse_sources` regex `\+b\d+(?=_\w+\.u?deb$)` (`dependencytree.py:578`) and the inline copy in `tests/smoke_dep_drift.py:190` are *different*. Extract a single helper, use it in both, add a test. |
| STA-16 | P2 | todo | `Tui._activetab` silently resets state on inconsistency; should log to file too so we can post-mortem the cause. |
| STA-17 | P2 | todo | Bare `except Exception` paths in `Cache.__build_cache` and `package.py` swallow `KeyboardInterrupt`-adjacent errors — narrow them or rethrow `BaseException`. |

## 2. Conformity to Debian/Ubuntu process — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| CONF-01 | P1 | todo | After `source_build`, run `dpkg-scanpackages` / `apt-ftparchive` on `repo/` to generate `Packages`, `Packages.gz`, `Release`, `Release.gpg`, `InRelease`. Without this, the built bag of `.debs` is not a usable apt source for anyone (or any later install step) — this is the gate to a real derivative distro. |
| CONF-02 | P1 | todo | Generate and include a project-owned signing key; sign the built `Release` file. Document key rotation. |
| CONF-03 | P1 | todo | Honour the Debian source format: today `BuildContainer.build` calls `dpkg-source -x` then patches and runs `dpkg-buildpackage -us -uc -nc`. For a real distro derivative, the per-package patches should land in `debian/patches/` (quilt format) with a versioned `debian/changelog` entry, then `dpkg-buildpackage` produces a properly-named binNMU `.deb`. Document the chosen path (we are *not* doing it today) and decide whether to keep the current “patch outside debian/” approach. |
| CONF-04 | P1 | todo | Adopt the Debian build-profile vocabulary properly: `BuildProfiles = nodoc, nocheck` in `[Source]` is wired to `DEB_BUILD_OPTIONS` *and* `DEB_BUILD_PROFILES`, but those are different namespaces. `nodoc`/`nocheck` are options; `noudeb`/`stage1`/`stage2`/etc. are profiles. Split into two config keys and validate. |
| CONF-05 | P1 | todo | Source patches under `patch/source/<pkg>/<version>/9001-*.patch` work, but the convention has no DEP-3 metadata required by Debian — add a check that every patch has a DEP-3 header (`Description:`, `Origin:`, `Forwarded:`) before it is applied. |
| CONF-06 | P2 | todo | Adopt `reprotest` (or equivalent) to verify built `.debs` are reproducible across two runs of `source_build` from the same snapshot. |
| CONF-07 | P2 | todo | Generate an SBOM (CycloneDX or SPDX) listing every source name, version, and Debian patchset hash that went into the build. Required for any “downstream distro” aspirations. |

## 3. Completeness — P1 (toward derivative-distro state)

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| COMP-01 | P1 | todo | **Installer ISO** path. Today `build_iso` produces a live ISO only. To match the “CentOS-from-Red-Hat” goal: integrate either `debian-installer` (preferred, native), `Calamares` (graphical, used by many derivatives), or a custom dialog/whiptail installer. Decide on one approach and add `build_installer_iso` as a separate command. |
| COMP-02 | P1 | todo | **Repository publishing** — given CONF-01, expose a `publish_repo` command that copies `repo/` + generated metadata to a configured destination (local dir, S3-compatible bucket, rsync target). Without this the project cannot be *consumed*. |
| COMP-03 | P1 | todo | **Parallel `source_build`** — `MaxParallelBuilds` is read from config but ignored. Implement a worker-pool that respects build-dep ordering (a topological-sort batching like `BuildSystem.get_install_sequence` already exists; reuse it). |
| COMP-04 | P1 | todo | **Architecture support** beyond `amd64`. The code reads `arch` from config but `Dockerfile`, `pkg.list` (`linux-image-amd64`, `grub-efi-amd64`), and `build_iso` ISO name are amd64-hardcoded. Decide on second-arch target (likely `arm64`), parameterise. |
| COMP-05 | P1 | todo | **Idempotent re-runs** — `cmd_build_cache`, `cmd_parse_dependency` etc. all reset their `BuildFlags` and rerun from scratch. For long resolves this is wasteful; add an explicit `clean` command and treat repeats as no-ops when cache/dep tree are still valid. |
| COMP-06 | P1 | todo | **Package-set TUI** — today the user edits `config/pkg.list` by hand. Write a `select_packages` TUI command that lets the operator toggle packages with available metadata (size, deps), commits the result back to `pkg.list`. (Buildroot has this; we should match.) |
| COMP-07 | P2 | todo | **Cross-build container per release** — `Dockerfile` is currently hard-pinned to `bookworm`. Auto-rebuild a per-release image (`bookworm`, `trixie`, `noble`, …) when `CONTAINER_RELEASE` changes; keep them in parallel so the user can cross-target. |
| COMP-08 | P2 | todo | **System-level firstboot** — `systemd-firstboot --root-password=root` ships an ISO with `root:root`. Replace with a firstboot wizard or, at minimum, randomize and print the password during ISO build. |
| COMP-09 | P2 | todo | **Disk-image (raw / qcow2)** output alongside the ISO, for direct VM/cloud use. |
| COMP-10 | P3 | todo | **Architecture for OS-release branding** — `id`, `id_like`, `vendor` are hardcoded to Athena strings in `generate_system_configs`. Move to `[Build]` config so a real derivative does not have to fork the source. |

## 4. Architecture & coding practices — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| ARCH-01 | P1 | todo | Eliminate module-level globals in `build.py` (`build_config`, `build_cache`, `dependency_tree`, `build_container`, `console`, `_tui`). Encapsulate in a `BuildSession` object that the TUI command handlers receive as `self`. Unblocks unit tests that don’t need a TUI. |
| ARCH-02 | P1 | todo | `tui.console` is mutable module state assigned during init in `build.py:971`; the rest of the code does `from tui import console` and gets the bound object — but if `tui.console = Console()` runs *after* import, callers see the original. Fix by exposing a `set_console()` setter or by always going through `tui.console.print(...)` (already true in places, inconsistent). |
| ARCH-03 | P1 | todo | Decouple `Console`, `Spinner`, `ProgressBar`, `Prompt` from a singleton `tui_instance` — accept it explicitly. Today `tests/smoke_dep_drift.py` has to monkey-patch the module to test anything. |
| ARCH-04 | P1 | todo | Split `buildsystem.py` (1,460 LOC, 25k+ tokens) into: `chroot.py`, `iso.py`, `dep_drift.py`. The file currently crosses two-tab read budgets. |
| ARCH-05 | P2 | todo | Add `pyproject.toml` with `setuptools`/`hatch` so `pip install -e .` works and entry points are declared. |
| ARCH-06 | P2 | todo | Add `mypy` + `ruff` (or `flake8`+`black`) config and run them in CI; type hints exist but are inconsistent (e.g. `BuildContainer.__init__ -> bool`). |
| ARCH-07 | P2 | todo | Decide on a logging story: today output is split between `print()` for stdout, `tui.console.print()` for the curses console tab, `console.error/warning/info` for the log tab, and ad-hoc files (`chroot-install.log`, `mksquashfs.log`, etc.). Wrap with a single `logging` adapter that routes by level. |
| ARCH-08 | P2 | todo | `utils.Tree` is a generic tree but is only used by `BuildSystem.build_chroot` and `get_install_sequence`. Either move into `buildsystem.py` (private), or keep generic but add tests. |
| ARCH-09 | P2 | todo | Replace `BuildContainer.is_ar_file` (re-implements `ar` parsing) with `dpkg-deb -W` or `python-debian.debfile` — fewer LOC, more correct. |
| ARCH-10 | P2 | todo | Drop the unused `config/requirements.txt` (rich, tqdm, gnupg, docker) — `py_requirements.txt` is the source of truth used by `build-system.sh`. |
| ARCH-11 | P3 | todo | Consolidate `Mirror` URL building (`url`, `dist_url`, `packages_path`, `sources_path`) with a single Pydantic-style validated model. |

## 5. Tests & CI — P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| TEST-01 | P1 | todo | Adopt `pytest` and split `tests/test_module.py` (8 tests) into `tests/test_<module>.py` per module — currently every test is in one file with a hand-rolled runner. |
| TEST-02 | P1 | todo | Add unit tests for `DependencyTree.parse_dependency` — auto-pick path, alt-deps, virtual-package resolution, version constraint propagation. None exist today. |
| TEST-03 | P1 | todo | Add unit tests for `BuildSystem.get_install_sequence` and the unpack-forest logic — circular pre-deps, missing-from-selected, gcc-base bootstrap, edge cases. |
| TEST-04 | P1 | todo | Add tests for `strip_build_version` covering `+bN`, `+deb12uN`, mixed-suffix, multi-`_` filenames. The smoke driver has its own (different) regex that must match production. |
| TEST-05 | P1 | todo | Add a fixture `Cache` built from a tiny in-tree `Packages` / `Sources` blob so cache/dep-tree tests run offline. |
| TEST-06 | P1 | todo | GitHub Actions (or equivalent) CI: lint, unit tests, smoke driver against the in-tree fixture. No CI exists today. |
| TEST-07 | P2 | todo | Integration test: `cmd_auto_run` against a fixture mirror inside Docker, asserting the chroot 8-check verifier returns all green. Tag as `slow` so it only runs nightly / on demand. |
| TEST-08 | P2 | todo | Property test for `Mirror.with_snapshot` — no-op on `None`, idempotent, preserves `suite`. |

## 6. Documentation — P1 / P2

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| DOC-01 | P1 | todo | `README.md` ends mid-section (`### RHEL/Debian/Ubuntu` / `### Stiched together` are empty headings; “Building Image / Intro” is just `...`). Finish the operator guide: prereqs, first run, common failure modes, where logs live. |
| DOC-02 | P1 | todo | A `docs/architecture.md` describing the pipeline stages and `BuildFlags` contract. Today this knowledge is only in commit messages and inline docstrings. |
| DOC-03 | P1 | todo | A `docs/patching.md` formalising the `patch/source/<pkg>/<ver>/9001-*.patch` and `patch/{pre,post}-install/` conventions. The README has fragments. |
| DOC-04 | P2 | todo | `docs/release.md` describing how to cut a derivative distro release once CONF-01..03 land (signing key, snapshot timestamp, pkg.list freeze). |
| DOC-05 | P2 | todo | Move the inline “Installing Docker” block out of `README.md` into `docs/install-docker.md`. |

## 7. Security & supply-chain — P0 / P1

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| SEC-01 | P0 | done | (Same as STA-01) Verify `InRelease` GPG signatures with a pinned keyring before trusting any mirror data. |
| SEC-02 | P1 | todo | The Docker image grants `athena ALL=(ALL) NOPASSWD:ALL`. Acceptable for a build sandbox, but document the threat model and ensure the image is *never* exposed to a network the host doesn’t control. |
| SEC-03 | P1 | todo | Source archives from snapshot.debian.org are downloaded over plain `http://` (`Mirror.baseurl = http://snapshot.debian.org/archive`). Switch to `https://` everywhere — snapshot supports it. |
| SEC-04 | P1 | todo | `systemd-firstboot --root-password=root` (`buildsystem.py:1254`). Replace with a generated random password printed to console and written to the ISO label, or force a reset on first boot. |
| SEC-05 | P2 | todo | The build container runs `apt-get install -y` on whatever the resolved build-deps are — without dep-graph review. Acceptable today, but add an opt-in “show me what is about to be installed” gate for hostile-mirror scenarios. |
| SEC-06 | P2 | done | The smoke driver and tests do not exercise GPG paths; once SEC-01 lands, add fixtures with a known-bad signature to confirm the verifier rejects it. *(landed alongside STA-01 — see `test_verify_inrelease_*` in `tests/test_module.py`)* |

## 8. Operator UX — P2 / P3

| ID    | Sev | Status | Title |
|-------|-----|--------|-------|
| UX-01  | P2 | todo | `print` command takes only `config|required|important|selected`. Add `print sources`, `print mirrors`, `print snapshot`. |
| UX-02  | P2 | todo | `cmd_auto_run` does not respect prerequisites if any earlier step fails — it still tries the next step and produces a confusing error. Make it bail at the first failed flag. |
| UX-03  | P2 | todo | Emit a final summary at end of `auto_run` (counts of cached/built/tunneled/failed, ISO path, total wall time). |
| UX-04  | P2 | todo | Persist `BuildFlags` to disk between runs so a re-launched TUI can resume mid-pipeline. |
| UX-05  | P3 | todo | Replace the curses TUI with a Textual-based one (or expose a non-TUI CLI mode for headless / CI use). The curses code is solid but Textual would simplify the prompt/widget plumbing significantly. |
| UX-06  | P3 | todo | Localised messages — today everything is English-only. |

## 9. House-cleaning — P3

| ID     | Sev | Status | Title |
|--------|-----|--------|-------|
| HK-01  | P3 | todo | Remove the dead `get_install_sequence` if it is truly unreached after `build_chroot` migrated to the unpack-forest model — or wire it back in for the post-final-configure pass and add a test. |
| HK-02  | P3 | todo | Drop `cmd_print`’s commented-out `# TODO` items in `build.py`; turn them into entries in this file (UX-01) and remove the inline TODOs. |
| HK-03  | P3 | todo | Empty `config/testpkg.list` — either populate (intent: a tiny smoke pkg list) or remove. |
| HK-04  | P3 | todo | `scripts/__pycache__/` is committed-adjacent (in tree) — confirm `.gitignore` covers it (it does), but inspect whether any stray `.pyc` was ever committed (`git log -- '*.pyc'`). |

---

## How to update this file

- When you start a task, change its status to `wip` and (optionally) add a
  parenthetical with the commit / PR.
- When you finish, change to `done` and append the closing commit hash in
  italics on the same row (Markdown table cells accept inline emphasis).
- Do **not** delete completed tasks — they form the audit trail.
- New tasks: append to the relevant section with the next free ID
  (`STA-18`, `COMP-11`, etc.). Never re-use an ID.
- If priorities change, edit the `Sev` column; do not move rows between
  sections.

