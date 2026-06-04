# Athena-Build — natural-English pseudocode

A reader-friendly walkthrough of every Python module in `scripts/`, written
in natural English rather than code. Module names appear only in comments at
the head of each section, never inline. Use this document to follow the
logic flow without reading the source. Pair it with `docs/diagrams/build-fsm.png`
for the high-level state machine and `TODO.md` for open work.

Sections are ordered roughly along the build pipeline: configuration and
foundation first, then data layer, then orchestrator, then output paths.

---

## utils.py — foundation: config, mirrors, NMU/asg versioning, snapshots, GPG, hashing

**Purpose:** the toolbox every other module imports. Defines `BuildConfig` (the
canonical input the rest of the system runs against), `Mirror` (an immutable
description of one apt source), the family of NMU/binNMU/backport suffix
strippers, the `+asg<R>u<N>` versioning primitives, snapshot timestamp
resolution against snapshot.debian.org, the InRelease GPG verifier, and
several small helpers (file download, SHA256 with sidecar cache, pkg.list
parser, tree-hash).

### `classify_repo_subdir(filename)`
Decide which subdirectory under `repo/` a `.deb`/`.udeb` belongs in by
looking at the package-name suffix (the text before the first underscore).
Names ending in `-doc` go to `doc/`; `-dbgsym` go to `dbgsym/`; `-test`
or `-tests` go to `tests/`. Everything else, including `-dev` and
`-udeb`, goes to `main/`. `-dev` is deliberately kept in `main` because
install-corpus packages hard-depend on `-dev` at runtime.

### `strip_build_version(filename)` and `strip_nmu_suffix(version)`
Take a Debian package filename or version string and remove the trailing
NMU/binNMU/backport layer (forms like `+bN`, `+debNuN`, `~debNuN`,
`~bpoN+N`, `+rpiN`, `+rptN`, legacy `-Nb`). Layers can stack; trailing
pre-release tildes attached to an NMU suffix are also consumed. The
result is the pristine source version.

### `strip_nmu_from_control_text(content)` and `strip_nmu_from_deb(deb_path)`
Rewrite the Version field and every version constraint in
Depends/Pre-Depends/Recommends/Suggests/Enhances/Provides/Conflicts/Breaks/
Replaces of a Debian control file (or a whole `.deb` opened via `dpkg-deb -R`)
to its pristine-stripped form. Also detect and collapse the upstream
sibling-pin idiom `X (>> V), X (<< V-.)` into `X (= our_version)` so the
post-strip artifact remains self-consistent within our repo. Idempotent.

### `+asg<R>u<N>` family — `apply_asg_suffix`, `pristine_base`, `match_pristine_base`, `find_matching_artifact`, `highest_asg_update`, `asg_next_n`
The Athena update-version layer. R is the integer release ([Build] VERSION);
N is the per-file update counter, monotonic per (pristine base, release).
- `apply_asg_suffix(base, R, N)` produces `base+asg<R>u<N>`.
- `pristine_base(version)` strips BOTH the asg layer and any upstream NMU
  layer, yielding the source base — the grouping key for N.
- `match_pristine_base(predicted_fn, ondisk_fn)` says whether an on-disk
  file is the dep-tree's predicted pristine filename or a stamped
  variant of it.
- `find_matching_artifact(dst_dir, predicted_filename)` returns the
  actual path of either an exact pristine match or an `+asg`-stamped one.
  This is what prevents the historical rebuild-loop (the predictor
  emits pristine names; the artifact may carry a stamp).
- `highest_asg_update(published_versions, base, R)` looks across already-
  published versions for `base+asg<R>u<N>` and returns the largest N
  (zero if none).
- `asg_next_n(published_versions, base, R)` is one past that — the
  number to stamp on the next update of this binary at this release.

### `restamp_asg_deb(deb_path, R, N)`
After NMU strip has already taken a `.deb` to its pristine base, open it,
rewrite the Version + the sibling-pin sibling constraints to
`base+asg<R>u<N>`, rebuild the archive, and rename the file on disk.
No changelog entry is created — this is a pure binary relabel.

### `Mirror` dataclass
Immutable, frozen description of one apt mirror entry:
- `id`, `baseurl`, `baseid`, `release`, `suffix`, `component`, `arch`.
- `suite` = release + suffix (e.g. `bookworm`, `bookworm-security`).
- `url` = baseurl + baseid.
- `is_flat` is true when component is empty (the fork mirror's flat
  `file://` shape).
- `dist_url`, `packages_path`, `sources_path`, `udeb_packages_path`
  compose the on-mirror file paths.
- `with_snapshot(ts, baseurl)` returns a copy rewritten to
  `<baseurl>/<baseid>/<ts>/dists/...`; file:// mirrors pass through.
Construction validates non-empty fields, URL scheme, and the suffix
shape; invalid input raises immediately.

### `verify_inrelease(signed_path, keyring_path, work_dir)`
GPG-verify an InRelease file using a one-shot gnupg homedir. The keyring
is imported once per `(keyring, work_dir)` pair and memoised at module
scope (importing the Debian keyring per mirror is expensive). Returns
`(ok, detail)`.

### `download_file(url, filename)`
Download a URL to a local path. Supports `http(s)://` (HEAD then GET
with progress bar) and `file://` (shutil.copyfile fast path). Returns
`(bytes_written, detail)`. Detailed error reasons are returned so the
operator sees the actual failure cause rather than a generic message.

### `download_source(dependency_tree, dir_download)`
Iterate every selected source's files, route each download through its
origin Mirror's pool URL (sources in bookworm-security live under a
different `baseid` than main), verify expected-size and SHA256 before
counting it as fetched. Skips files whose on-disk SHA256 already matches.

### `get_sha256(filepath, use_cache=True)` and `_compute_sha256(filepath)`
SHA-256 with a per-file sidecar (`<filepath>.verified` carrying
`<size> <mtime_ns> <hex>`). When size/mtime match, the cached hash is
returned without re-reading the file. Cache invariant: in our pipeline
files are never edited in place — downloads write fresh files with new
mtimes — so stat agreement implies content unchanged. `use_cache=False`
forces a fresh compute (used after writing a file when we want to
round-trip it).

### `compute_tree_hash(root, skip_dirs)` and `patch_set_hash(patch_dir, patch_files)`
SHA-256 over (relative_path + NUL + content + NUL) per file in sorted
order. `compute_tree_hash` walks recursively (skipping `.git`,
`__pycache__`); `patch_set_hash` operates on a given ordered file list.
Used by fork-mirror invalidation and the `_refresh_patches` mtime-vs-content
distinction respectively.

### `_query_snapshot_latest(api_url, archive_keys)` and `resolve_snapshot_timestamp(config)`
Ask snapshot.debian.org for the latest timestamp covering every archive
in `archive_keys` (Debian + Debian-security), pick the minimum (so every
archive has coverage), and persist the resolved timestamp under
`cache/snapshot.timestamp`. `resolve_snapshot_timestamp` honours, in
order: snapshot pinning disabled → None; operator override in
`config/snapshot.state.current`; cached resolved 'latest'; an explicit
timestamp from `[Snapshot] Timestamp`. Memoised per
`(state_file, config_ts)` so repeated calls in one process don't re-query.

### `_validate_snapshot_timestamp(ts, mirrors, snapshot_baseurl)`
HEAD-validate that every mirror has an InRelease available at the given
snapshot. Catches typos and timestamps that predate a given suite.

### `list_snapshots_between(config, after_ts, upto_ts)`
Return all distinct snapshot timestamps strictly after `after_ts` and
up to `upto_ts`, unioned across archive keys. Used by the operator-facing
`snapshot list` picker.

### `read_snapshot_state(config)` and `write_snapshot_state(config, ...)`
Manage `config/snapshot.state` as JSON: `base` (archive floor),
`current` (operator-selected pin), `published` (what the remote/local
ledger reflects), `external` (runtime override of [Repo] ExternalEnabled).
Living under `config/` rather than `cache/` so `clean cache` can't wipe it.

### `BuildConfig`
- Validates argv (working dir + paths to build.conf and the FIVE .list
  files: pkg, live, installer, pool, indl).  `--indl-list` defaults
  to `config/indl.list`; only consumed when `[Build] Mode = individual`.
- Parses build.conf via configparser. Reads `[Build]`, `[Base]`,
  every `[Mirror.*]` (constructing Mirror instances), `[Snapshot]`,
  `[Source]`, `[Security]`, and `[Directories]`.  The only `[Repo]`
  key still parsed is `SigningKeyUid`.
- Computes the working tree layout: `dir_download`, `dir_log`,
  `dir_cache`, `dir_temp`, `dir_source`, `dir_repo` and its nested
  per-component subdirs (`dir_repo_main`, `dir_repo_main_udeb`,
  `dir_repo_main_source`, `dir_repo_doc`, `dir_repo_dbgsym`,
  `dir_repo_tests`, `dir_repo_contrib`, `dir_repo_non_free`,
  `dir_repo_non_free_firmware`), `dir_image`, `dir_buildroot` and its
  two sibling chroots (`dir_chroot` for live, `dir_chroot_installer`
  for d-i), `dir_patch` and its children, `dir_fork` and its source
  and built-output children, `dir_gnupg` (mode 0700), `dir_coord` (the
  signed-sidecar tree for federation claims), and `dir_publish`.
- mkdir+writability-checks every directory it owns. Auto-cleans empty
  pre-Stage-D dirs at the repo root.
- Three-layer identity: `build_distribution` (display name),
  `build_base_id` (lowercase distribution ID), `build_codename`
  (release codename, used for the apt suite name).
- `[Build]` knobs include `DISTRIBUTION`, `CODENAME`, `VERSION`,
  `IncludeRecommends`, `MaxParallelBuilds`, `DiskImageSizeGB`, and
  `Mode` (`'distribution'` or `'individual'`; default `distribution`;
  unknown values rejected with a clear `error_str`).
- `[Source]` knobs: `SkipTest`, `Tunneled` (comma-separated source
  names), `BuildProfiles`, `BuildOptions`.
- `[Security]` knobs: `Keyring` (defaults to host debian-archive-keyring),
  `Disabled`.
- `[Snapshot]` knobs: `Enabled`, `Timestamp`, `BaseUrl`, `TimestampApi`,
  `ArchiveKeys`.
- `deb_dir_for(label)` maps a label (main / main-udeb / doc / dbgsym /
  tests / contrib / non-free / non-free-firmware) to the on-disk dir.
- `deb_dest_for_filename(filename, component='main')` composes
  `classify_repo_subdir` with the udeb special case and the non-main
  component override. The one helper callers use to find "where does
  THIS .deb/.udeb go?"
- `all_deb_dirs()` returns the binary dirs maintenance walks should
  visit (main / main-udeb / doc / dbgsym / tests). Deliberately excludes
  contrib / non-free / non-free-firmware so `package strip` can't NMU-strip
  tunneled pristine passthrough binaries.

### `parse_pkg_list_groups(path)` and `parse_pkg_list_group_meta(path)`
Read an INI-style pkg.list (legacy flat list also supported as an
implicit `[base]` group). Returns dict-of-lists in declaration order;
also extracts `## Description: ...` lines under each `[group]` header as
group metadata used by tasksel `.desc` generation.

### `parse_indl_list(path)`
Read `config/indl.list` — flat list of binary package names, one per
line, `#` comments and blank lines tolerated, inline comments allowed
(`firefox-esr # OOMs on host A`). Dedups while preserving first-seen
order. Missing file → empty list (caller checks `[Build] Mode ==
'individual'` separately to decide whether empty is fatal).

### `new_build_record(*, package, intended_version, patch_set_hash, started=None)` and the build-record family
Local-side sidecar that records every source build attempt.  Schema is
versioned in `BUILD_RECORD_SCHEMA_VERSION` (= 3).

Fields (the entry-phase shape):

- `schema_version`, `package`, `intended_version`, `built_version`,
  `patch_set_hash`, `phase` (`entry` → `container_exited` →
  `segregated` → `normalized` → `done | failed | tunneled`), `status`,
  `started`, `finished`, `elapsed_seconds`, `exit_code`, `oom_killed`,
  `output_count`, `outputs` (list of `.deb`/`.udeb` filenames),
  `output_hashes` ({filename: sha256_hex}).
- `republished_from` ({filename: {url, upstream_sha256}}) — populated
  by `cmd_tunnel_package` for tunneled passthrough.  Threads through
  `coord.publish.generate_pending_claims` into the claim's
  `republished_from` field so the federation marks tunneled packages
  as no-owner.
- `pulled_from` ({mirror_name, owner_builder} | None) — populated by
  `cmd_mirror_pull` for `.deb`s pulled from a peer's mirror.
  Distinguishes "we built it" from "we pulled it" without losing the
  record.

`write_build_record(buildlog_dir, record)`: HMAC-signs with a
session-local key under `log/build/.metrics.hmac.key`, then atomic
write (tempfile + fsync + os.replace) so a crash mid-write leaves the
prior valid file intact.

`read_build_record(buildlog_dir, package)`: HMAC-verify; returns
`None` on any verify failure (treated as missing → forces rebuild via
the existing classifier).

`update_build_record(buildlog_dir, package, **fields)`: read-merge-sign-
write convenience.  Phase transitions go through here; auto-fills
status when phase reaches a terminal value.

`backfill_output_hashes(buildlog_dir, repo_root)`: idempotent
walker that upgrades older records to current schema, hashing
emitted `.deb`s under `repo_root` via `get_sha256`'s cached digest
and initialising the federation fields (`republished_from={}`,
`pulled_from=None`).

---

## package.py — Package / Source: parsed Debian control records

**Purpose:** subclasses `debian.deb822.Packages` and `Sources` to add
parsed dep/relation fields, virtual-Provides version logic, build-dep
expansion that walks alternative chains.

### `Package(section)`
- Reads required fields (Package / Version / Architecture); marks
  invalid if absent.
- Parses every relation field (Depends, Pre-Depends, Recommends,
  Suggests, Provides, Replaces, Conflicts, Breaks, Enhances,
  Built-Using) via `apt_pkg.parse_depends` into structured tuples.
- Splits Depends into single-entry vs alternative (`|`) groups; same for
  Pre-Depends.
- `priority` defaults to `'optional'` if the field is absent.
- `_mirror` is populated post-parse by the Cache when stamping origin.
- Hashable on `(package, version, arch)`.
- `explicit_provides_version(name)` returns the version explicitly
  declared in a Provides clause for `name` (None when unversioned —
  per Debian Policy §7.5 unversioned Provides cannot satisfy a versioned
  constraint).
- `get_provides()` yields `(name, version)` per Provides entry,
  substituting the package's own Version when the Provides itself is
  unversioned (the version-aware satisfies path uses this).
- `constraints_satisfied` and `add_constraint` accumulate version-
  constraint observations against this package while dep resolution
  proceeds; the property reports whether the union of constraints is
  satisfiable by this package's actual version.

### `Source(section)`
- Same shape but for a Debian source-package record. Tracks `files`
  (.dsc, .orig.tar.*, .debian.tar.*) with size + SHA256, and `arch`
  (the source's declared compatible-arch list).
- `build_depends(arch, active_profiles, cache=None)` evaluates Build-
  Depends with build-profile annotations (`<!nodoc>` etc.) — drops a
  dep group when the active profile set kills every alt in the group.
  When a cache is provided, virtual targets are expanded into the
  alternative chain of their real providers (multi-provider build-deps
  like `libcurl4-dev`).
- `download_size` totals the file sizes for the resolver's progress bar.

### `VersionConstraint(version, constraint)`
A `(version, op)` pair with `is_satisfied_by(candidate)` for the
constraint-tracking machinery. Treats `>` and `>>` (and `<`/`<<`)
identically, per Debian's legacy-operator handling.

---

## cache.py — multi-mirror apt cache + collision gate + UdebCacheView

**Purpose:** fetch and parse `Packages` + `Sources` indexes from every
configured mirror, GPG-verify their InRelease, build one merged in-memory
universe keyed by package name. Two parallel hashtables: deb world and
udeb world. Fork mirror is prepended so its records win same-name collisions
against upstream — with an end-of-build gate that fails the cache when
upstream's dropped version would have dominated.

### `_lookup_packages(hashtable, name, version=None, constraint='')`
Shared backend that returns all matching Package objects from a
hashtable. With a version constraint, uses `apt_pkg.check_dep` against
each candidate. Uses `.get()` to avoid creating empty entries via
bracket access on defaultdicts.

### `Cache.__init__(buildconfig)`
- Loads the dpkg arch table.
- Resolves the snapshot timestamp; rewrites every configured mirror's
  baseurl to point at the snapshot service (file:// passes through).
- If `fork/source/` has any source trees, generate-and-register a
  file:// fork Mirror prepended ahead of the upstream mirrors.
- Calls `__get_files` (fetch + GPG verify + decompress every per-mirror
  Packages/Sources, plus optional udeb Packages where the mirror
  publishes one).
- Calls `__build_cache(arch)` to parse and ingest, then runs the
  collision gate.

### `__get_files`
For each mirror in order:
- Compute its dist URL. file:// mirrors fetch a plain `Release`; others
  fetch `InRelease`.
- Download Release/InRelease. If security is enabled and the mirror is
  not file://, verify the inline GPG signature against the configured
  keyring; abort the cache on failure.
- Parse the Release block, extract the SHA256 entries.
- For each of `Packages` + `Sources`: check whether the uncompressed
  file already matches the expected SHA256 (skip download), else pick a
  compression variant the mirror advertises (xz preferred, gz, bz2),
  download it, decompress to the canonical path.
- Optionally fetch the d-i Packages index (`<comp>/debian-installer/...`)
  the same way; absence is fine (updates/security typically don't ship
  udebs).

### `__build_cache(arch)`
For each mirror, parse the per-mirror Packages file into Package objects,
arch-filter, fork-supersede-track. Then parse Sources the same way. Then
parse the optional udeb Packages files. After all mirrors are walked,
de-dup the required/important lists, run the gcc-major dedup (keep only
the latest gcc-N major in `self.required`), then run the collision
gate.

### `_verify_no_fork_collisions`
For every upstream record dropped by the fork-supersede walk, compare
the fork's version against the dropped upstream version. Fail the cache
when any dropped upstream version is greater-or-equal to the fork's
version — the fork would be silently regressing. Mitigation guidance
in the error message: rebase the fork onto upstream's new source, or
rename the fork. NEVER bump the local version as a workaround.

### `UdebCacheView`
A thin composition over Cache that swaps `package_hashtable` for the
udeb hashtable. Lets DependencyTree resolve the udeb world by passing
in this view instead of the real Cache, with no class subclassing.

---

## dependencytree.py — recursive dep resolution + virtual-Provides + cohort fields

**Purpose:** given a list of seed names, produce the closed transitive
dep set against a Cache (or UdebCacheView). Tracks per-pkg version
constraints, virtual-Provides aliases, alternative chains, and the live /
installer / pool / extras cohort partitions.

### `_auto_pick_candidate(candidates, prefer_name=None)`
- Group candidates by Package name; per-name keep the highest version.
- Prefer real `Package == prefer_name` (apt's rule: a real pkg wins over
  a virtual Provides of the same name).
- If only one Package name remains after collapse, auto-pick it.
- Otherwise return (None, collapsed) so the caller prompts (or the udeb-
  tree's highest-version fallback fires).

### `DependencyTree.__init__(cache, ...)`
- Wires the cache; allocates `selected_pkgs`, `selected_srcs`, the
  cohort fields (`live_exclusive_pkg_names`, `installer_exclusive_pkg_names`,
  `pool_extras_pkg_names`, `extras_pkg_names`, `pkg_group_pkg_names`,
  `pkg_group_meta`), the per-tree `src_pkg_files` predictor map.
- `auto_pick_highest_when_ambiguous`: on the udeb tree, multi-name
  candidates that aren't unambiguous get the highest-version-across-names
  fallback (kernel-ABI udeb variants).

### `add_lookahead(packages, check_conflicts=True)`
Seed the per-pkg `__lookahead` map with the chosen Package version for
every requested name. For each name: look up candidates; collapse;
auto-pick or prompt (or pool.list path skips conflict checks). Conflict-
check against entries already in lookahead — block on real-package
conflicts that satisfy the conflict-version constraint, skip virtual-
alias-only matches.

### `resolve_packages(packages, check_conflicts=True)` and `parse_dependency(name, version=None, constraint='')`
- `resolve_packages` is the operator-facing entry: lookahead-seed the
  list, then recursively `parse_dependency` each one.
- `parse_dependency` walks the candidate set with these cases:
  1. Already in selected_pkgs and satisfies (or virtual-Provides
     satisfies) → return it.
  2. A candidate is already selected by real name → return that.
  3. The lookahead has exactly one match (after same-Package collapse) →
     pick it.
  4. Zero candidates → return None (caller warns).
  5. One candidate → pick it.
  6. Multiple → auto-pick on same-name collapse, else prompt.
- Insert the picked Package into selected_pkgs BEFORE recursing (cycle
  protection). Also register every Provides virtual name → same Package.
- Build the dep list = depends + pre_depends + first-satisfying
  alternative-of (alt_depends + alt_pre_depends). Include recommends
  if `select_recommended=True`.
- For each dep: parse out version + operator, recurse. Record forward
  and reverse edges (`depends_on` / `depended_by`). Add a constraint
  observation on the resolved provider.

### `_version_for_constraint_target(entry_key, target_name)`
Return the right version string to compare against a constraint on
`target_name`: the package's own Version if it IS `target_name`,
otherwise the explicitly-declared Provides version (None when the
Provides is unversioned, per Policy §7.5).

### `validate_selection()`
Iterate Breaks and Conflicts across every canonical entry in
selected_pkgs:
- Skip self-conflict-via-Provides (a package that Provides X and
  Breaks X is the canonical fork-replaces-upstream idiom).
- Skip pairs where either side is in `pool_extras_pkg_names` (those
  ship in the cdrom pool only; apt arbitrates at install time on
  target).
- Per Policy §7.5: an unversioned Provides cannot satisfy a versioned
  Breaks/Conflicts constraint.
- Otherwise run `apt_pkg.check_dep` on the actual or provided version;
  report.

### `pull_recommends_extras()` and `derive_extras_src_names()`
After the resolve passes, pull depth-1 Recommends of selected pkgs
into `extras_pkg_names` (downloaded but not installed). `derive_extras_src_names`
projects this onto sources — only sources whose every produced binary
is extras-only get added; mixed sources stay in the normal build flow.

### `derive_subset_exclusive_src_names()`
After parse_sources has mapped binaries to their producing sources, split
sources into `live_exclusive_src_names`, `installer_exclusive_src_names`,
`pool_extras_src_names` based on the corresponding binary-name cohort
membership. Returns three sets.

### `parse_sources()`
For every selected binary, walk the cache.source_hashtable to find its
producing source. Record source → list-of-predicted-binary-filenames in
the per-tree `src_pkg_files` map (post-normalized for NMU stripping, so
the predictor produces pristine names that match what the source-build
pipeline actually emits).

---

## fork_mirror.py — fork/source/ → file:// apt mirror

**Purpose:** for each `fork/source/<pkg>/` tree, run `dpkg-source -b` to
produce a .dsc + tarballs under `fork/source/repo/`, then synthesise
Packages / Packages-udeb / Sources / Release for a flat `file://` apt
mirror at `fork/`. The Cache prepends this mirror first so fork records
supersede same-named upstream records.

### `generate_fork_mirror(buildconfig)`
- Discover every `fork/source/<pkg>/` tree.
- For each: compute tree-hash + dep-hash, compare against persisted
  sidecars. If changed, wipe downstream artifacts (built .debs in
  `repo/`, source tarballs in `source/`, build logs) — same package
  name across all maintenance dirs.
- Run `dpkg-source -b` per tree; outputs land in `fork/source/repo/`.
- Synthesise the four index files at `fork/` root + plain `Release`
  with MD5 + SHA256 blocks formatted to mimic apt-ftparchive's output.
- Persist the new tree-hash + dep-hash sidecars.

### `register_fork_mirror(mirrors, buildconfig)`
Prepend a flat-layout Mirror with `baseurl='file://<working_dir>'`,
component='', release='./' — Cache's first mirror in iteration order.

### Index helpers
`_build_packages_stanzas` / `_build_sources_stanzas` use python-debian
to parse the original control + dsc, transform fields (arch normalised
to build arch, Filename: pointing at the fork's flat pool, Substvars
like `${shlibs:Depends}` stripped — they'd be stale here). All-zero
MD5/SHA256 placeholders are written into the stanzas because the Cache
never tunnels fork packages.

---

## mirror.py — publish-target umbrella + federation primitives

**Purpose:** per-mirror durable state (one `config/mirror.<name>.state`
file per configured publish target) and the non-network helpers behind
the `mirror` command umbrella.  Owns the federation-record shape
(per-peer apt URLs in `coord-head.neighbours`) and the ownership,
installability, and `mirror.base` advance plumbing.

State-file shape (`config/mirror.<name>.state`, JSON):

```
{
  "name", "url", "type" ("ssh"|"local"), "ssh_key",
  "host", "host_type" ("ip"|"fqdn"|"local"),
  "public_proto", "public_url",
  "base", "current", "last_publish_at",
  "neighbours_known": [...]
}
```

### `add_mirror(config, *, name, url, type, ssh_key, seed_pin, host, host_type, public_proto, public_url)`
Atomic write of one `config/mirror.<name>.state` file.  Refuses
duplicate names or URLs across the local set.  Normalises the publish
URL via `_normalize_url` (ssh:// + file:// only; any other scheme is
rejected).  Seeds `base` and `current` from the operator-supplied
snapshot pin so a new mirror starts at parity.

### `_normalize_url(url)`, `_extract_host_from_ssh_url(url)`, `_extract_user_from_ssh_url(url)`, `_extract_path_from_ssh_url(url)`
URL-parsing primitives.  IPv6 brackets are stripped from the extracted
host so `ipaddress.ip_address` accepts it; `derive_public_url` re-
brackets per RFC 3986 §3.2.2 when building the apt URL.

### `validate_host_for_type(url, host_type)`, `derive_name_from_url(url, host_type)`, `derive_public_url(url, dist_id, proto)`
The `mirror add ip|fqdn|local <url>` user-typed surface.  Cross-checks
the host portion against the operator-supplied keyword, derives a
filename-safe name (dots / colons → dashes), composes the apt-readable
URL as `<proto>://<host>/<dist-id-lowercased>`.

### `read_mirror_state` / `write_mirror_state` / `update_mirror_state`
JSON read-merge-write with `utils._atomic_write_bytes` so a crash
mid-write leaves the prior file intact.

### `list_mirrors(config)`, `find_mirror_by_url(config, url)`, `remove_mirror(config, *, url_or_name)`, `delete_mirror_state(config, name)`
Inventory + lookup + LOCAL-only removal helpers.  `mirror remove` does
not touch the remote — federation propagation is via
`reconcile_neighbours`.

### `all_mirror_urls(config)` and `all_mirror_neighbour_records(config)`
URL-only projection (federation-gate / drift detection) vs. v3 record
projection ({url, public_url, public_proto} per peer; publish writers).

### `neighbours_drift(config, name)`
Compares a mirror's last-seen `neighbours_known` against the local
config's mirror URL set; returns `(tag, missing_on_peer, extra_on_peer)`
where tag is `unpublished` / `in-sync` / `drift`.  Surfaced inline by
`mirror list`.

### Probe pipeline (used by `cmd_mirror_add`)
- `probe_dns_and_tcp(host, port, timeout_s)` — DNS resolve + TCP
  connect.
- `probe_ssh_auth(host, user, ssh_key, timeout_s)` — non-interactive
  `ssh -o BatchMode=yes echo ok` against the publish account.
- `probe_remote_writable(host, user, ssh_key, remote_pool_path)` —
  single-call `mkdir -p` + `test -w` on pool + `-coord` sibling.
- `probe_http_inrelease(public_url, codename, timeout_s)` — HEAD on
  `<public_url>/dists/<codename>/InRelease`; 200 = mirror has a
  published Release, 404 = empty (first-publish bootstrap is fine).
- `probe_sidecar_head(coord_url, signing_homedir, stage_dir, ssh_key)`
  — pull peer's `coord-head.json[.sig]`, GPG-verify against local
  tier-1 keyring; three outcomes (no head yet / verified / verify-
  failed).  Verify-failed is the federation key-mismatch gate.
- `discover_federation_peers(head, signing_homedir, stage_root, ssh_key)`
  — recurse `probe_sidecar_head` over every URL in the peer's
  `coord-head.neighbours`; classify reachable/verified/bootstrap-
  pending/unreachable; surfaces upstream's per-peer `public_url` /
  `public_proto` records to `cmd_mirror_add`.

### `reconcile_neighbours(config, *, signing_homedir, target_name, flock_timeout)`
Fan-out: for every peer in the local config, pull their coord-head,
compare neighbours against local set, rewrite + re-sign + push back
under remote flock.  Returns `(overall_ok, summary, results)`.
Unreachable peer = overall failure (operator retries when network is
back).

### `project_post_publish_state(local_state, remote_by_builder)`
Returns a `repo_audit.RepoState`-shaped projection of the mirror's
post-publish state.  Starts from the LOCAL repo's RepoState (which
carries full Depends/Provides), layers in remote claims as satisfier-
only entries (no Depends — they can't be consumers but they CAN
satisfy other pkgs' deps).  Multi-version-aware: when the same package
appears at local 1.0 and remote 2.0, the higher version wins (matches
what apt resolves a versioned `Depends:` against).

### `find_publish_closure_breaks(local_state, remote_by_builder, our_pending_pkg_names)`
The publish-time installability gate.  Wraps `repo_audit.audit_dep_closure`
with `consumer_set` = our pending packages, so the closure walk is
bounded to what we're about to push.  Returns a list of
`(pkg, field, relation_str, why)` findings; empty = our publish does
not break the mirror.  Wired into `coord.publish.remote_publish`
between the ownership filter and pool push; non-empty list → REFUSE
with up to 5 detail lines inlined.

---

## scripts/coord/ — federation sidecar (8 modules)

**Purpose:** the on-the-wire signed sidecar layer of the mirror
federation: per-builder claim records (`<root>/claims/<id>.jsonl`,
Ed25519-signed per line), tier-1 GPG-signed coord-head pinning the
canonical state, federation-consistency reconciliation, transport
primitives (rsync + ssh flock), and the 11-step publish state machine.

### `coord/schema.py`
- `new_claim(*, builder, seq, package, ...)` — per-`.deb` claim record.
  Fields: `v`, `builder`, `seq`, `package`, `intended_version`,
  `built_version`, `filename`, `sha256`, `size`, `snapshot`,
  `built_at`, `claim_state` (`pending` / `published` / `retracted`),
  `republished_from` (optional dict {url, upstream_sha256} for
  tunneled passthrough — federation treats it as "no owner"), `sig`
  (Ed25519, filled in by signer).
- `new_retraction(*, builder, seq, package, retracts_seq, …)` —
  builder-signed tombstone referencing the seq of the claim being
  withdrawn.
- `new_coord_head(*, inrelease_sha256, snapshot, last_seqs, head_time, neighbours, revoked_builders)`
  — the federation's canonical signed state.  `neighbours` is
  `list[dict]` of `{url, public_url, public_proto}` per peer.
- `canonicalize_neighbour_records(items)` — normaliser; accepts either
  bare URL strings or full dicts; emits sorted list[dict]; dedups by
  canonical url.
- `neighbour_urls(items)` — flat list[str] projection for federation-
  gate set-comparison (dicts aren't hashable).
- `canonicalize_neighbours(urls)` — alias for `neighbour_urls`; the
  legacy name federation-gate callers use.

### `coord/identity.py`
Ed25519 keypair management for the per-builder claim signer.  Creates
`coord/identity/<builder-id>.{pem,pub}` (0600 / 0644).  Loads the
peer keyring (`<root>/keyring/builders/<id>.pub`) for verifying
fetched claims.

### `coord/store.py`
- `append_claim_line(claims_dir, builder_id, claim)` — JSONL append
  with per-builder file lock.
- `max_seq(claims_dir, builder_id)` — highest seq in our local jsonl;
  used to assign the next claim's seq.
- `read_builder_claims(claims_dir, builder_id, public_key_path)` —
  verify + load one builder's claims.
- `read_all_claims(claims_dir, keyring, revoked)` — verify + load
  every peer's claims; respects revocation.
- `iter_live_claims_by_filename(by_builder)` — generator over
  non-retracted claims; same-builder pending-vs-published collisions
  resolved.
- `project_live_claims(by_builder)` — collapse to `{(pkg, ver): claim}`
  with cross-builder hash conflicts keyed by `(pkg, ver+'!'+builder)`.
- `project_owners(by_builder)` — per-filename ownership view:
  `{filename: {builder|None, version, sha256, claim_state, seq,
  republished_from, claim}}`.  Picks the latest non-retracted claim
  per filename via (`-seq`, `builder`) sort; `builder=None` ⇔ claim
  has `republished_from` set (tunneled).  Hash conflicts NOT resolved
  here — that's `detect_hash_conflicts`'s job; this projection trusts
  the gate ran upstream.

### `coord/policy.py`
Constants: `HASH_CONFLICT_POLICY = 'BLOCK'`, `COORD_HEAD_MAX_AGE_SECONDS`,
`PUBLISH_HALT` file convention.

### `coord/head.py`
- `read_coord_head(coord_dir, signing_homedir)` — fetch + verify
  `coord-head.json[.sig]` against the tier-1 GPG keyring; returns the
  parsed dict on success, `None` on any verify failure (the federation
  key-mismatch gate fires here).
- `write_coord_head(coord_dir, head, signing_homedir)` — canonical
  JSON write + tier-1 GPG-clearsign; removes any prior `.sig` and
  refuses to leave an unsigned manifest on signing failure
  (fail-closed).
- `is_fresh(head, inrelease_sha256, inrelease_date_iso)` — refuse
  rollback (head pins a different InRelease) and stale head (older
  than InRelease Date or beyond `COORD_HEAD_MAX_AGE_SECONDS`).

### `coord/reconcile.py`
- `audit_local(...)` — pool ↔ this-builder claims (orphans, hash
  drift, missing-on-disk).
- `audit_cross(...)` — `build.json` ↔ all-builder jsonl audit.
- `detect_hash_conflicts(by_builder)` — fires on same `(pkg, ver)`,
  different SHA across builders; `CRITICAL` writes `PUBLISH_HALT`.
  Same SHA across builders = `INFO reproducible_duplicate`.
- `check_federation_consistency(local_mirror_urls, head)` — compares
  the URL projection of `head.neighbours` against local config; any
  diff = `CRITICAL` (publish-time BLOCK; operator runs
  `mirror reconcile-neighbours`).
- `publish_halt_reason` / `write_publish_halt` — sentinel file
  read/write at `<coord-dir>/PUBLISH_HALT`.

### `coord/transport.py`
Rsync + ssh flock primitives for the publish/pull/reconcile paths:
- `pull_remote_coord(local_dest, remote_spec, ssh_key)` — rsync the
  sidecar tree (`coord-head.json[.sig]`, `keyring/`, `claims/`) down
  to a local staging dir.
- `push_jsonl(local_path, remote_spec, ssh_key)` — single-file rsync
  for jsonl append + per-builder pubkey upload.
- `push_single_deb(local_path, remote_dir_spec, ssh_key)` — per-`.deb`
  push for the publish pool step.
- `push_coord_head(local_coord_dir, remote_dir_spec, ssh_key)` — push
  the freshly-signed head back.
- `remote_flock_acquire(ssh_host, lock_path, timeout_sec, ssh_key)` /
  `remote_flock_release(lock_proc)` — wraps `ssh <host> flock -w …`
  so the publish/reconcile transaction is single-writer on the remote
  side.

### `coord/publish.py`
11-step publish transaction (the read-side of all the above modules):

1. Pre-flight `PUBLISH_HALT` check.
2. Remote flock acquire (ssh mirrors only; local-fs skips).
3. Pull remote coord tree.
4. Tier-1 verify coord-head; load keyring; read all claims.
5. Hash-conflict scan (`detect_hash_conflicts`); CRITICAL writes
   `PUBLISH_HALT` and aborts.
   - Ownership filter: `project_owners` →
     `filter_pending_by_ownership` applies the ownership decision
     matrix.  Per-filename: no-owner / us / tunneled → KEEP;
     other-owner higher-version → KEEP+transfer; other-owner
     same-or-lower → BLOCK with `ownership_blocked` finding.
   - Installability gate: `mirror.find_publish_closure_breaks` walks
     the post-publish state; any unresolved hard Depends → ABORT with
     `mirror_closure_break: <detail>` (up to 5 findings inlined).
6. Per-file `.deb` push — only pkgs that passed both ownership + the
   closure gate above.
7. Sign + append every pending claim to LOCAL jsonl with state
   `published`.
8. Push jsonl + pubkey to remote.
9. Re-sign coord-head pinning current InRelease sha + updated
   `last_seqs[builder]` + unchanged neighbours.
10. Push coord-head + flock release.
11. Local mirror state update: write `current`, `last_publish_at`,
    `neighbours_known`, and recompute `base` to the oldest snapshot ts
    across the post-publish claim ledger (per-builder retraction folds
    are applied first so a retracted-then-republished entry doesn't
    drag the floor back).

`generate_pending_claims(*, builder_id, buildlog_dir, claims_dir, public_key_path, snapshot_pin, read_build_record)`
— walk every `<pkg>.build.json` whose phase is `done` or `tunneled`,
emit one unsigned claim per output filename that isn't already in this
builder's live jsonl.  Reads `build.json.republished_from` per output
and passes the matching entry to `schema.new_claim`, so tunneled
outputs round-trip through the federation as "no owner".

`filter_pending_by_ownership(builder_id, candidates, existing_owners)`:
returns `(kept_pending, blocked_findings)` applying the ownership
decision matrix.  Version compare via `apt_pkg.version_compare`.

---

## buildcontainer.py — Docker-isolated source builds + post-build NMU strip + asg stamp

**Purpose:** wrap `dpkg-buildpackage` in a per-package Docker container
pinned to the build snapshot. Per build: write snapshot-pinned
sources.list, install build-deps, copy source, apply patches-applied +
custom patches, token-subst (`@DISTRIBUTION@/@BASE_ID@/@CODENAME@`),
build, segregate outputs to component dirs, strip NMU and stamp
`+asg<R>u<N>` as needed.

### `BuildContainer.__init__(config, docker_server=None, cache=None)`
- Snapshot-pin the mirrors the container's apt will use (memoised
  resolution).
- Guard the Docker daemon URL: refuse anything that isn't loopback,
  unix-socket, or explicitly TLS-marked.
- Connect to docker daemon (external if specified safely, else local).
- Compute Dockerfile sha256; if the cached image's stored hash doesn't
  match (or no image exists), rebuild it.

### `build(src_pkg, profiles_override=None, options_override=None)`
- Build-dep enumeration via `Source.build_depends` with the active
  profiles (override or config default). Single-alt deps go into a
  plain apt-install batch; multi-alt deps become `|| fallback` shell
  chains.
- Find the .dsc file in `src_pkg.files`.
- Read the live patch list from `patch/source/<src>/<ver>/` at build
  time (NOT the cached `src_pkg.patch_list` — operator may have added
  patches after the last dep-parse run).
- Build the container command string:
  - set -e + pipefail.
  - Write snapshot-pinned sources.list (with Pin-Priority 1001 +
    [check-valid-until=no]).
  - `apt-get update`; `apt-get -y --allow-downgrades dist-upgrade`
    (align preinstalled libs with snapshot before dpkg-shlibdeps reads
    .shlibs).
  - Install plain build-deps; run multi-alt chains.
  - `cd /home/athena`; copy source files; `dpkg-source -x <dsc>`;
    cd into the unpacked dir.
  - Apply `debian/patches-applied/` (the pam case — quilt's main
    series is debian/patches/ and dh_quilt_patch silently no-ops
    when .pc/ is pinned to debian/patches).
  - Apply our custom `/patch/*.patch` files in order.
  - Token-subst pass: grep-filter for files containing `@(DISTRIBUTION|
    BASE_ID|CODENAME)@` across debian/, data/, tasks/; sed -i them
    with values from BuildConfig. Two non-obvious shell choices:
    `if ... then ... fi` guards for optional dirs (so a missing
    data/ or tasks/ doesn't kill set -e under pipefail) and a
    `(grep ... || true)` rescue so grep's exit=1-on-no-matches
    doesn't kill the pipeline.
  - `DEB_BUILD_OPTIONS=... DEB_BUILD_PROFILES=... ATHENA_CODENAME=...
    dpkg-checkbuilddeps; dpkg-buildpackage -a <arch> -b -us -uc -nc`.
  - Copy *.deb and *.udeb into `/repo/` (mounted from host).
- Run the container with `/source`, `/repo`, and `/patch` bind mounts.
- Stream container logs to `log/build/<src>` file.
- On exit: write `.result` (PASS/FAIL) and `.patchhash` sidecars.
- On PASS: call `_segregate_built_artifacts` and `_normalize_built_artifacts`.
- Container is always force-removed in `finally`.

### `_segregate_built_artifacts(src_pkg)`
- Find every just-emitted .deb/.udeb at the repo root (which `dpkg-buildpackage`
  + `cp /repo/` landed there).
- Resolve the source's apt component from `src_pkg._mirror.component`
  (defaulting to 'main' for forks / locally-discovered).
- Route each file via `config.deb_dest_for_filename(filename, component)`
  to its nested dir (main → dists/<codename>/main/binary-<arch>/, etc.).
- Append-only invariant: if a same-name file already exists at the
  destination, KEEP the existing artifact and drop the rebuilt
  duplicate at repo root (byte-identical rebuild on a published file).
- Return absolute post-move paths so the normaliser only touches the
  just-emitted set, not the whole repo.

### `_normalize_built_artifacts(src_pkg, emitted_paths, was_patched)`
For each just-emitted artifact: strip NMU via `utils.strip_nmu_from_deb`,
then (if a remote ledger is loaded and this source rebuild is part of an
update delta) stamp `+asg<R>u<N>` via `utils.restamp_asg_deb`.

### `check_build(src_pkg, expected_files)`
- expected_files non-empty.
- `.result` reads PASS or TUNNELED.
- Every main-classified file in expected_files is present at its routed
  location (accepting either pristine or `+asg<R>u<N>`-stamped via
  `utils.find_matching_artifact`) AND is a valid ar archive.
- Side artifacts (-dev/-doc/-dbgsym/-tests) are NOT gated — missing
  ones don't trigger rebuild.

### `verify_pkg_artifact(deb_path, expected_filename, repo_state=None)`
Open a .deb, walk its internal control, verify Package/Version/Arch
match the filename and every hard Depends resolves against the
RepoState (or cache). Used by the content-integrity audit section.

### `run_grub_mkrescue(staging_dir, output_iso, ...)`
Runs grub-mkrescue INSIDE the build container so the ISO embeds
the container's GRUB toolchain (snapshot-pinned), not the host's.

---

## chroot.py — live chroot orchestration (mixins for BuildSystem)

**Purpose:** install built `.deb`s into `buildroot/live/` in topo-sorted
batches, bootstrap dpkg + libc, run post-install patch overlays, run
the 8-check chroot verifier. Composed into `BuildSystem` via mixin.

### `_ChrootMixin.build_chroot(chroot, dependency_tree, dir_repo_main, ...)`
- Wipe + recreate the chroot dir (sudo for root-owned remnants; mkdir
  user-owned).
- `_init_dpkg_database`: create minimal /var/lib/dpkg structure so
  `dpkg --root` accepts the chroot before any packages are unpacked.
  Also writes a minimal `/etc/debconf.conf` so debconf takes defaults.
- libc seed: unpack libc6 (+ libc6-udeb's deps if any) before
  configuring anything — solves the bootstrap circularity.
- Topo-sort the install corpus via the install-batches computer.
- For each batch: `dpkg --unpack` the .debs, then `chroot <dir> dpkg
  --configure -a` to configure them. Track failures per batch.
- After every batch: apply post-install patches from
  `patch/post-install/` that match installed packages.
- Generate system configs (hostname, motd, /etc/issue, /etc/os-release,
  /etc/fstab, etc.) via `_generate_system_configs`.
- Install the signing pubkey at
  `/usr/share/keyrings/athena-archive-keyring.gpg` (if generated).
- If `[Repo] AptSourceURL` is set, write
  `/etc/apt/sources.list.d/athena.list` with `[signed-by=...]`.

### `_compute_install_batches(canonical_pkgs)`
Topological sort of dependency edges into install batches: each batch
contains pkgs whose unmet deps are all in earlier batches. Detects and
handles canonical libdevmapper/dmsetup/systemd cycles via a terminal
force-depends batch.

### `_write_chroot_file(rel_path, content)`
Atomic write into the (root-owned) chroot via a host tempfile + `sudo cp`.

---

## installer_chroot.py — d-i installer chroot (udeb-only unpack)

**Purpose:** parallel to `chroot.py` but for `buildroot/installer/` —
the udeb closure that becomes the installer ramdisk. NO postinst
configure (deferred-postinst model: udebs unpack now, their maintainer
scripts run at first boot via rootskel + main-menu).

### `build_installer_chroot(udeb_tree, dir_udebs, dir_chroot_installer, installer_dir, password, codename='thor')`
- Wipe + recreate.
- Bootstrap minimal dpkg dirs (so `dpkg --unpack` succeeds).
- Resolve udeb files for every entry in `udeb_tree.selected_pkgs`.
- `dpkg --root --force-* --unpack` the entire closure.
- `_assert_apt_setup_generators`: verify both `40cdrom` and `50mirror`
  apt-setup generators are present (sanity gate against fork-Provides
  drops).
- `_strip_debian_residue_hooks`: surgically remove specific hooks that
  bake Debian identity into the installed system (currently
  `20install-hwpackages` from hw-detect + `50save-logs`).
- `_register_self_in_dpkg_status`: write a synthetic `Package:
  debian-installer` stanza so the installer can verify its own
  configuration.
- `_apply_installer_overlay`: walk `_OVERLAY_MAP` (preseed.cfg,
  cdebconf.conf-if-present, debug syslog tail, finish-install hooks for
  the default apt source and the cdrom disable) and `sudo cp -p` each
  source-file into its target chroot path. Missing source files are
  silently skipped — the documented contract.
- Report stats.

---

## iso_installer.py — installer ISO assembly

**Purpose:** stage a chroot + kernel/initrd + pool + apt-repo metadata +
GRUB into a hybrid BIOS+EFI bootable ISO via `grub-mkrescue`.

### `build_installer_iso(dir_chroot_installer, dir_repo, dir_image, installer_dir, ...)`
- Prepare a staging tree.
- Find the kernel inside the chroot via version-aware sort (NOT
  lexicographic — `6.10` < `6.9` would mis-pick) and pair the matching
  initrd by suffix.
- Stage the kernel + initrd into staging.
- Stage the pool: `_stage_pool(staging, [dir_repo_main, dir_repo_main_udeb,
  *dir_repo_extras], deb_whitelist)` — non-recursive listdir over each
  component dir, filter by the whitelist, copy into `staging/pool/`.
  Non-main component dirs MUST be passed via `dir_repo_extras` since
  `os.listdir` doesn't recurse.
- Generate apt-repo metadata: `apt_repo.generate_apt_repo` produces the
  single-suite `main`-only Release/Packages/Sources for the flat pool.
- Sign the Release files (detached `.gpg` + clearsigned `InRelease`).
- Export the pubkey to `.disk/archive-key.gpg` for the installer's
  base-installer hook to copy into `/target/etc/apt/trusted.gpg.d/`.
- Stage `installer/disk/{info, base_components, base_installable}`,
  `installer/boot/{grub.cfg, grub-background.png}`, the kernel /
  initrd, etc.
- Run grub-mkrescue inside the build container (so the embedded GRUB
  toolchain matches the snapshot pin).

---

## iso.py — live ISO assembly

**Purpose:** wrap the live chroot into a squashfs and produce a hybrid
BIOS/EFI bootable ISO. Much simpler than the installer ISO — no installer
ramdisk, no pool indexing.

### `build_live_iso(chroot, dir_image, container, password, ...)`
- Stage the squashfs root + a minimal grub.cfg with the live-boot
  kernel cmdline.
- Run mksquashfs over the chroot.
- Run grub-mkrescue inside the container to emit the hybrid ISO.

---

## disk_image.py — qcow2 disk image

**Purpose:** master a pre-installed bootable qcow2 disk image from the
verified live chroot (COMP-09). Same gate as live ISO; output is a
single qcow2 instead of an ISO.

### `build_disk_image(chroot, dir_image, password, size_gb, ...)`
- `qemu-img create` a sparse qcow2 of the requested size.
- Format (ESP + ext4); mount via loopback.
- rsync the chroot tree into the ext4 root.
- chroot in via `mount --bind` + `chroot ... grub-install`; install
  GRUB to the ESP.
- Generate `/etc/fstab` (with the ESP at fs_passno=0 — never 1, only
  / is 1, to avoid racing fsck.vfat vs fsck.ext4).
- Unmount; the qcow2 is ready to boot.

---

## buildsystem.py — BuildSystem composer (chroot + iso + dep_drift)

**Purpose:** compose `_ChrootMixin`, `_IsoMixin`, `_DepDriftMixin` into
one `BuildSystem` class. Owns sudo-password lifecycle and pre_install
gates.

### `BuildSystem(dep_tree, config)` and `BuildSystem.for_iso(config)`
- Validate sudo via `sudo -S -v` against the cached password.
- If the chroot is non-empty, prompt the operator to wipe (sudo find
  -delete).
- Wire instance state (config dirs, dep_tree reference, password).
- The `for_iso` factory uses `cls.__new__` so callers that only need
  iso work don't pay for dep-drift setup.

### `.password` property
Returns the cached password. After `.scrub_password()` it raises,
preventing accidental reuse beyond the intended scope.

---

## dep_drift.py — drift verification between cache versions and on-disk .debs

**Purpose:** for each canonical package selected by the dep tree, read
its on-disk `.deb` (via `dpkg-deb -f`) and update the in-memory Package
object's Depends/Pre-Depends/Version to match the artifact. Then verify
every dep resolves to something else in the selected closure.

### `_check_dep_drift`
For each canonical pkg name:
- Find the artifact via `utils.find_matching_artifact` (pristine or
  `+asg`-stamped).
- Run `dpkg-deb -f` to get its actual control.
- Patch the in-memory Package's dep fields + Version with what the
  artifact actually says.

### `_verify_dep_resolution`
For each canonical pkg, walk every dep tuple and look up the provider
in selected_pkgs (canonical or virtual). Use
`DependencyTree._version_for_constraint_target` for virtual aliases.
Report any unresolved or version-skewed dep.

---

## repo_audit.py — RepoState + audit primitives + ledger

**Purpose:** single source of truth for repo/ state. One
`dpkg-scanpackages` + `apt_pkg.TagFile` parse, cached per-(repo_max_mtime,
dir-hash). Three audit primitives + the published-manifest authority for
+asg uN derivation.

### `scan_repo_state(config, subdir='main', refresh=False)`
- Walk the target subdir (via `config.deb_dir_for`) to find max mtime.
- Build the cache filename from the subdir hash + max mtime.
- Reuse cached `audit-Packages-<subdir>-<hash>` if fresh; otherwise
  shell `dpkg-scanpackages` into a tempfile then `sudo install` it.
- Parse via `apt_pkg.TagFile`; keep only `_FIELDS_TO_KEEP` (Package,
  Version, Architecture, Filename, Source, all relation fields).
- Build a `provides_index` mapping virtual_name → [(provider, version)]
  for Policy §7.5 resolution.
- Wrap in a frozen `RepoState`.

### `audit_dep_closure(state, consumer_set=None)`
For each pkg in scope, for each Depends/Pre-Depends group, attempt to
satisfy via `_rel_satisfied_in_scope` (which honours provides). Report
unresolved and weak (Recommends).

### `audit_conflict_cohort(state, cohort)`
For pkgs in the cohort, check Conflicts/Breaks. Filter self-conflict-via-
Provides. Return `(pkg, field, other, relation_str)` tuples.

### `audit_nmu_residue(state, tunnel_sources)`
For every binary in state.packages, sweep Version + every relation
field's constraint version for residual NMU suffix (`+bN`, `+debNuN`,
`~bpoN+N`, `+rpiN`, legacy `-Nb`). Skip binaries whose Source is in
`tunnel_sources` (those are pristine passthrough and MUST keep their
upstream suffix).

### Published-manifest authority
- `local_manifest_path(config)` = `<dir_config>/published.manifest`.
- `_write_signed_manifest` signs via gpg; `_read_signed_manifest`
  verifies. (TODO STA-19: today both fall through on broad exception
  rather than fail closed — track for the trust-degradation fix.)
- `published_ledger(config)` → dict[pkg_name → list[version]] from the
  local signed manifest.
- `fetch_remote_ledger(config)` HTTP-fetch the remote
  `dists/<codename>/main/binary-<arch>/Packages.gz`; only `main` (non-
  main components are local-only).
- `published_base_versions(ledger)`: dict[pkg → max pristine_base] —
  used for "did upstream advance?" detection.

---

## apt_repo.py — Packages/Sources/Release/InRelease writers + remote publish

**Purpose:** generate apt-repo metadata for two distinct audiences:
the single-suite flat-pool ISO/cdrom (`generate_apt_repo`), and the
multi-suite multi-component network published repo (`generate_repo_indexes`).
Also: append-only merge with the remote ledger, GPG sign Release files,
remote reindex + sign over SSH.

### `generate_apt_repo(staging, suite, codename, version, password)`
Single-suite, `main`-only, FLAT pool layout (the ISO/cdrom).
- mkdir the three subdir tree (binary-<arch>/, debian-installer/
  binary-<arch>/, source/).
- `_scan_packages_to` over the flat `pool/` → main Packages.
- `_scan_packages_to(..., udeb=True, allow_empty=True)` → udeb Packages.
- `_scan_sources_to` over `pool/` → Sources.
- `_write_subdir_release` for each subdir (Suite/Codename/Component/Arch).
- `_generate_top_release` walks the suite tree, hashes everything, writes
  the top Release. Output goes to a `tempfile.mkstemp(dir=staging,
  prefix='.release-tmp-')` BEFORE the walk, then `sudo mv` into place —
  this avoids the apt-ftparchive self-reference race (writing to the
  same tree being walked hashes the partial output).

### `generate_repo_indexes(repo_root, suites_spec, codename_for_suite, version, arch, password, signing_homedir=None, ...)`
Multi-suite, multi-component, IN-PLACE on `repo_root/dists/`. The
network publish path. For each suite:
- For each component listed in `suites_spec[suite]`: skip if the
  binary-<arch>/ dir is missing or empty; otherwise scan binary +
  optional udeb + optional source; write per-subdir Release files;
  add to `_populated_components`.
- Generate the top Release with `Components: <_populated_components>`
  (skip the suite entirely if no components populated).
- If a signing homedir was provided, sign Release.gpg + InRelease.

### `merge_packages_indexes(remote_text, local_text)`
Union two Packages indexes, dedup by (Package, Version). Local stanzas
win on collision; every remote-only version is PRESERVED. The append-
only multi-version invariant for the published main archive.

### `merge_remote_index(repo_root, suite, ..., remote_packages_path, signing_homedir)`
- For the main component: union local + remote main Packages via
  `merge_packages_indexes` and write the combined stanza set.
- For contrib / non-free / non-free-firmware: local-only scan
  (pristine passthrough — no append-only history).
- Per-subdir Release files. Top Release. Sign.

### `remote_reindex_and_sign(staging, ssh_cmd, userhost, remote_root, ...)`
Drive `_scan_packages_to` over SSH against the remote VM; sign the
Release; ship signing artefacts back. Used by `cmd_repo_publish ssh`
after the rsync.

### `sign_release_files(staging, suite, signing_homedir, password)`
`gpg --detach-sign` → Release.gpg, `gpg --clear-sign` → InRelease,
against the configured key in the signing gnupg homedir.

### `export_pubkey_to_staging(staging, signing_pubkey_path, password)`
`sudo cp` the pubkey to `.disk/archive-key.gpg` for the installer
ramdisk to pick up at install time.

---

## signing.py — project signing keypair management

**Purpose:** generate, locate, inspect, and roundtrip-verify the
project's RSA-4096 signing keypair under `<dir_gnupg>/signing/`.

### `generate_key(config, _key_length=4096)`
`gpg --batch --gen-key` with `%no-protection` (no passphrase). Exports
the public key to `<signing>/athena-archive-keyring.gpg`. Returns True
on success.

### `verify_key(config) → (ok, message)`
Sign + verify a test payload using the project key. Surfaces failure
reasons (no key, agent issues, expiration).

### `get_key_info(config)`
Read `gpg --list-secret-keys --with-colons` and parse the fingerprint,
uid, created, expires fields.

### `parse_secret_keys_colons(output)`
Helper that walks the gpg colon-format output, returns the most recent
sec stanza's identity.

---

## select_packages.py — interactive curses package selector

**Purpose:** lets the operator toggle entries in `config/pkg.list` from
the TUI. Lazy BFS over Depends to estimate closure size per package.

### `SelectPackages(config, cache, tui_inst).activate()`
- Loads the group model from pkg.list.
- Owns its own tab buffer.
- Background worker computes closures on demand.

### Key handlers
- Arrows: navigate within and between groups.
- Space: toggle current.
- a: add a new package (text input via Prompt).
- s: save (atomic write via `<path>.tmp` + os.replace).
- q: quit (prompt if unsaved).

### `write_pkg_list(path, groups, meta)`
Serialise the groups dict back to INI form with `## Description:` lines.

---

## print_commands.py — `print <category>` handlers + autorun summary

**Purpose:** all read-only views over BuildSession state. Adding a new
category = one row in `CATEGORIES`.

Categories include: state, config, packages, sources, extras, live,
installer, pool, build, tunneled, fork, container, snapshot, mirrors,
signing, repo, stats, help.

### `dispatch(session, category, *extras)`
Look up category in CATEGORIES; call the handler with the session +
extras. Unknown categories print help.

### `summary(session, *, timing)`
Emit the post-autorun summary: build counts (built / tunneled / failed /
skipped), wall time, the aborted-at stage if any, the BuildFlags
snapshot.

---

## cli.py — headless CLI backend

**Purpose:** stand-in for `Tui` selected via `--headless`. Implements
the same `tui.tui_instance` console facade so all callers (Console,
Spinner, ProgressBar, Prompt) work unchanged. Output: stdout for
operator-facing content, stderr for diagnostic logger noise.

### `Cli()`
- Wire as `tui.tui_instance`.
- Bind logging.
- Track registered commands.
- Provide `print`, `INFO/WARNING/ERROR`, `prompt` (input/getpass for
  password), `add_widget`/`del_widget` (one-line `[start]`/`[done: N/M]`
  markers), `console_mark`/`console_trim_to` (no-ops — no scrollback
  to trim), `register_command`, `run`/`wait`/`exit`/`sig_shutdown`.

---

## tui/ — curses event-dispatcher TUI

The package is decomposed into single-responsibility submodules.

### tui/state.py — pure data model
Frozen TabState + CmdLine + State dataclasses. `MAX_BUFFER_LINES =
10000` per tab silent drop-oldest. DEFAULT_TABS = (console, log,
cache, build, chroot, iso) mapping to F1..F6. `dirty` flag is the
redraw signal.

### tui/events.py — event types
Frozen dataclass per event: KeyEvent, PrintEvent, LogEvent, StatusEvent,
WidgetAdd, WidgetRemove, PromptRequest, TabActivate/Add/Remove,
ClearTab, ConsoleMark, ConsoleTrim, SetTabBuffer, Shutdown.

### tui/dispatcher.py — event loop
Single-threaded Queue consumer. Sole mutator of State. Owns pending-
prompt, per-tab key interceptor, command-names completion source.
Render is called only when `state.dirty`.

### tui/render.py — curses output
Pure renderer. Owns curses windows, panels, color setup, layout, resize
handling, "too small" overlay, teardown. `_safe_addstr` reserves the
last column to prevent forced scroll. attr_for_color(0) returns 0 so
uncoloured prints skip color resolution.

### tui/facade.py — Console + Prompt classes
Late-binding singleton resolver. Console exposes print, INFO/WARNING/
ERROR, console_mark/console_trim_to. Prompt validates loop for
YESNO/OPTIONS, echoes the answer back on completion.

### tui/widgets.py — ProgressBar + Spinner
Resolve `tui_instance` at construction; register via add_widget/
del_widget. ProgressBar steps + closes; Spinner ticks animation
deadline + `done()` prints checkmark line.

### tui/input_pump.py — keyboard pump thread
Daemon `tui-input` thread blocks on `stdscr.getkey()` and posts
KeyEvent per keystroke.

### tui/logging_bridge.py — logging integration
Routes `'athena'` logger records to dispatcher events (or direct Cli
method calls). Defines `DISPLAY` custom level and `LOGGER_NAME`.

### tui/wrap.py — line wrap helpers
Pure character (not word) wrap. Designed for URLs, hex digests, stack
frames. Empty entries always cost 1 display row so caller counts stay
consistent.

### tui/tui.py — top-level orchestrator
Composes Renderer + Dispatcher + input pump + shell + psutil status
monitor threads. Singleton enforced. Thread names: `tui-status`,
`tui-shell`, `tui-input`, `tui-dispatch`.

### tui/__init__.py — package surface
Re-exports the public names (Tui, Console, Prompt, PROMPT_*, COLOR_*,
SEVERITY_*, ProgressBar, Spinner, setup_logging, setup_file_logging,
Exit, register_command, tui_instance).

---

## build.py — top-level orchestrator (BuildFlags + BuildSession + cmd_* + main)

**Purpose:** the entry point. Owns BuildFlags (12 pipeline-stage gates)
and BuildSession (cache / dep_tree / udeb_dep_tree / container / flags
references). Registers every cmd_* handler with the TUI/CLI backend.

### `BuildFlags`
Twelve boolean milestones: cache_ready, dep_check_ready, download_ready,
build_container_ready, source_build_ready, signing_key_verified,
chroot_ready, chroot_verified, chroot_installer_ready, iso_live_ready,
iso_installer_ready, iso_disk_ready. Each `cmd_*` sets its flag on
success and gates on prerequisites.

### `BuildSession(config, tui_inst)`
- `config`, `tui`, `cache`, `dep_tree`, `udeb_dep_tree`, `container`,
  `flags`, `last_source_build_counts`.
- The 60+ cmd_* methods — dispatched via group helpers (cmd_cache,
  cmd_source, etc.) into the concrete handlers.

### Top-level commands (13), each with `_group_help` tables
- cache: build / purge / parse / select / info
- clean: cache / source / repo / buildroot / image / download / container / all
- patch: refresh
- source: sync / build / audit / repair / fork / tunnel
- repo: index / publish / audit / repair / refresh / external
- snapshot: list / advance / workload / base
- container: init / purge
- chroot: build (live | installer) / verify
- iso: build (live | installer | disk)
- key: generate / verify
- autorun: live / installer / disk / individual
- mirror: add / list / remove / publish / pull / audit / summary /
          reconcile-neighbours / show
- print: state / config / packages / sources / extras / live / installer /
         pool / build / tunneled / fork / container / snapshot / mirrors /
         signing / repo / stats / help

### Key flows

**cmd_build_cache:**
- Idempotency: skip if cache_ready and Cache exists, unless `force`.
- Ensure snapshot pins (operator-prompted on first run).
- Resolve snapshot timestamp.
- Construct Cache (which downloads + verifies + parses every mirror).
- Set cache_ready on success.

**cmd_parse_dependency (7 passes):**
- Pass I: required.
- Pass II: important.
- Pass III: pkg.list groups in declaration order. Each group's canonical
  delta is recorded in `pkg_group_pkg_names[<group>]`. Non-base groups
  are unioned into `pkg_group_extras_pkg_names`.
- Pass IV: live.list — delta against pkg-closure goes to
  `live_exclusive_pkg_names`.
- Pass V: installer.list deb arm — delta to `installer_exclusive_pkg_names`.
- Pass VI: udeb world via UdebCacheView — `udeb_dep_tree.selected_pkgs`.
- Pass VII: pool.list with `check_conflicts=False` so mutually-conflicting
  bootloader metas can coexist; populates `pool_extras_pkg_names`.
- Then: `validate_selection` (Breaks + Conflicts; pool extras bypassed),
  parse_sources for both trees, derive_extras_src_names,
  derive_subset_exclusive_src_names, set dep_check_ready.

**cmd_parse_dependency — individual mode:**
When `config.build_mode == 'individual'`, Passes III–VII are SKIPPED.
The flow is:
- Pass I/II remain (required + important — they're harmless and the
  source builder still needs core build-deps).
- `selected_pkgs` is populated directly from `parse_indl_list(indl_path)`:
  each binary is looked up in `cache.package_hashtable` (latest version
  picked) and inserted into `dep_tree.selected_pkgs`; NO recursive
  `Depends:` walk. The build host's job is to build the named packages;
  runtime closure is the mirror's invariant (gated at publish), not
  the build host's.
- `selected_srcs` derives 1:1 from `selected_pkgs` via `Package.source`
  / fallback to name; then `parse_sources` walks each source's
  `Build-Depends` so the BuildContainer has its build deps.
- `dep_tree.indl_src_names` is just an alias for `selected_srcs.keys()`.
- `validate_selection` and the extras/exclusive projections are SKIPPED
  in indl mode (they presuppose a full closure).

**cmd_source_sync:**
Iterate over both trees' selected_srcs; `utils.download_source` for the
union. Sets download_ready.

**cmd_init_container:**
Construct `BuildContainer(config, docker_server, cache)` — guards the
docker URL, ensures the build image is present (rebuild on Dockerfile
hash drift). Sets build_container_ready.

**cmd_source_build [force] [subset|<names>] [[profiles]]:**
- Parse args via the static `_parse_source_build_args` helper.
- In individual mode, the legal scopes shrink to `all` (== everything
  in `indl.list`) and explicit names; `pkg` / `live` / `installer` /
  `recommended` raise an eager arg-validation error pointing at the
  mode.
- Auto-detect update-mode: when a published base exists and current
  snapshot is ahead, rebuild the delta with `+asg<R>u<N>` stamping.
- Pick the package set per subset (pkg / live / installer / recommended /
  all / explicit names). Each subset is a slice of the unified corpus.
- For each src_pkg:
  - Skip if in `cache.skip_src`.
  - If tunneled: call `_do_tunnel` (which uses the upstream Filename:
    from the cache, NOT the strip-NMU pristine name — those would 404
    on snapshot.debian.org or silently fetch wrong unstable binaries).
  - Else: check_build to skip rebuild if .result is fresh; else
    `container.build(src_pkg, profiles_override, options_override)`,
    then re-check_build to confirm artifacts landed.
  - Track built / tunneled / failed / skipped counts.
- Record counts on session.last_source_build_counts. Set source_build_ready.

**cmd_build_chroot_live:**
- `_refuse_in_individual_mode('chroot build live')` early-return if
  `config.build_mode == 'individual'` — single-package builds don't
  produce a closed runtime set.
- Gate on source_build_ready + signing key verified.
- Pre-flight: `_preflight_audit_source` (build state of every selected
  source — gates on hard findings, soft findings just warn); 
  `_preflight_audit_repo` (the consolidated repo audit's three install-
  correctness checks).
- Compose BuildSystem(dep_tree, config); call build_chroot.
- Run verify_chroot automatically (the 8 checks). Sets chroot_ready and
  chroot_verified on success.

**cmd_build_chroot_installer:**
Parallel but against udeb_dep_tree and `build_installer_chroot`. Same
indl-mode refuse at the top. No verify_chroot equivalent (the installer
doesn't have a configured-state to verify). Sets chroot_installer_ready.

**cmd_build_iso_live / cmd_build_iso_installer / cmd_build_iso_disk:**
Drive `iso.build_live_iso`, `iso_installer.build_installer_iso`,
`disk_image.build_disk_image` respectively. All three start with the
indl-mode refuse. Sets the corresponding flag.

**cmd_audit [verbose|strict|refresh|quick|<target>]:**
Six sections in order: DEP GATE / LIVE CONFLICTS / INSTALLER CONFLICTS /
STALE FILES / CONTENT INTEGRITY / NMU RESIDUE. Per-cohort when dep_tree
is built; whole-repo fallback otherwise.

**cmd_index_repo [minimal]:**
`apt_repo.generate_repo_indexes` over `repo/` with the configured
suites_spec. `minimal` produces a runtime-only subset (excludes `-dbg`,
`-dbgsym`, `*-source` per `apt_repo.deb_excluded_from_minimal`).

**cmd_repo_publish (ssh|local|git) [full|minimal]:**
- `ssh`: rsync over SSH to the configured PublishSshTarget, then
  `remote_reindex_and_sign` for the multi-component network publish.
- `local`: update only the local signed manifest (testing without a VM).
- `git`: stage `publish/` for a git push (CI publish path).

**cmd_snapshot list|advance|base|workload:**
- `list`: show snapshots between current and the latest available.
- `advance <ts>`: validate + write to snapshot.state.current; clear
  resolve memo.
- `base <ts>`: set the archive floor (forward-only check).
- `workload [since <ts>]`: diff current → target snapshot and report
  the source set that would rebuild.

**autorun (live | installer | disk | individual):**
Walk the shared early stages (cache → cache parse → source sync →
container init → source build pkg → subset-specific source build),
then the divergent terminal stages (chroot build {live|installer} →
chroot verify if live → iso build {live|installer|disk}). Bails on
first failure. Emits the autorun summary at the end.

`autorun individual` is the indl-mode variant: chain is cache →
cache parse (indl branch) → source sync → container init → source
build (defaulting to everything in `indl.list`). Stops at
`source_build_ready` — no chroot/ISO stages. Operator typically chains
into `mirror publish` separately once `source audit` is clean.

### `cmd_mirror_*` family

The `mirror` command umbrella drives the federation: peer-config CRUD,
publish, pull, audit, federation reconciliation.

- `cmd_mirror_add ssh|local <url> [name=…] [host=ip|fqdn|local] [public_proto=…]`
  Probe pipeline (DNS+TCP → SSH auth → remote writable → InRelease HEAD
  → coord-head verify → federation discovery), persists a new
  `config/mirror.<name>.state`. First add against a fresh remote seeds
  `base`/`current` from the operator's snapshot pin; subsequent adds
  fetch the existing coord-head and adopt its `last_publish_at` as the
  starting `current`.

- `cmd_mirror_list` — JSON-like inventory; per-mirror state with
  `neighbours_drift` tag (unpublished / in-sync / drift).

- `cmd_mirror_remove <url|name>` — LOCAL-only state-file removal; does
  not touch the peer.

- `cmd_mirror_publish [<name>]` — orchestrator over
  `coord.publish.remote_publish`. Pre-loop:
  - Mode banner (`MODE: distribution|individual`).
  - First-publish gate: refuses indl-mode publish to a mirror whose
    remote has no `coord-head.json` yet.
  - Loops over local config; per-mirror runs federation-gate
    (`check_federation_consistency`) and then the 11-step publish
    transaction. Failure-tolerant across peers (one peer failure does
    not abort the others).

- `cmd_mirror_pull [<name>] [<pkg>...]` — rsync the delta of `pool/`
  + any new claims from the named mirror; for every newly-arrived
  `.deb` writes a local `build.json` record:
  - tunneled-on-mirror (claim has `republished_from`) →
    `phase='tunneled'`, copy `republished_from`.
  - owned-by-other → `phase='done'` + `pulled_from = {mirror_name,
    owner_builder, upstream_sha256}`.
  - Our own claims are SKIPPED (we built it; rebuilding by pull would
    drop our ownership).

- `cmd_mirror_audit [<name>]` — pull peer's coord tree + claims, run
  `coord.reconcile.audit_cross`, then closure-projection of the
  mirror's post-publish state and report unresolved hard `Depends:`.
  Read-only command — does NOT write to `PUBLISH_HALT`.

- `cmd_mirror_summary [<name>]` — counts: total claims; per-builder
  claim counts; ownership breakdown (`we_own`, `they_own`, `tunneled`);
  hash-conflict / closure-break totals.

- `cmd_mirror_reconcile_neighbours [<name>]` — calls
  `mirror.reconcile_neighbours`; fans out per peer to push the local
  neighbour set into their coord-head. Used after `mirror add` /
  `mirror remove` to propagate federation membership.

- `cmd_mirror_show <name>` — pretty-print the per-mirror state file +
  the last-pulled coord-head snapshot (federation membership +
  `last_seqs` per builder + freshness check).

### `cmd_tunnel_package` (`source tunnel <pkg>`)

Writes the local `build.json` record with `phase='tunneled'` and
`republished_from = {url, upstream_sha256}`. The claim emitter in
`coord.publish.generate_pending_claims` reads that field per-output
and propagates it onto the federation claim — so tunneled-locally
becomes tunneled-on-mirror (no-owner) without losing provenance.

### `main(banner)`
- Detect `--headless`; strip from argv before BuildConfig sees it.
- `apt_pkg.init_system()`.
- BuildConfig() — validates config + creates dirs.
- Instantiate Tui or Cli backend; register as `tui.tui_instance`.
- Setup file logging.
- Construct BuildSession.
- Register every cmd via tui.register_command.
- Print logo + identity banner.
- `tui_inst.wait()` — blocks on event loop until exit.

---

## diag_installer_status.py — standalone dpkg-status diagnostic

**Purpose:** parse `buildroot/installer/var/lib/dpkg/status` the way
libdebian-installer does and report stanzas that would trip its rfc822
parser. Operator-invoked diagnostic only; no callers in `scripts/`.

---

## Persistent data structures

The following table catalogs every persistent file or directory the
build system reads or writes outside the volatile `cache/` / `tmp/` /
`buildroot/` trees that `clean` can wipe. Format: each row gives the
canonical path under the working directory, the producer (which command
or module writes it), and the consumer (what depends on it).

| Path | Format | Producer | Consumer | Notes |
|------|--------|----------|----------|-------|
| `config/build.conf` | INI | operator | `utils.BuildConfig.__init__` | The canonical input. Defines upstream mirrors, snapshot, [Build] identity, signing-key uid. |
| `config/pkg.list` | INI-groups | operator | `cmd_parse_dependency` Pass III | Drives the pkg-tier corpus. |
| `config/live.list` | flat | operator | Pass IV | Live ISO extras. |
| `config/installer.list` | flat | operator | Pass V + Pass VI | Mixed deb / udeb installer corpus. |
| `config/pool.list` | flat | operator | Pass VII | Conflicts-skip pool extras. |
| `config/snapshot.state` | JSON | `snapshot select`, `snapshot advance` | `utils.resolve_snapshot_timestamp`, `repo_audit.published_ledger` | Operator pin, durable across `clean cache`. Single field today: `current`. |
| `config/mirror.<name>.state` | JSON | `mirror add`, `mirror remove`, `mirror publish` (last_publish_at, neighbours_known) | `mirror.read_mirror_state`, `cmd_mirror_publish`, `cmd_mirror_audit` | Per-mirror durable state — one file per configured publish target. Fields: url, type, ssh_key, base, current, last_publish_at, neighbours_known. |
| `config/published.manifest` (+ `.sig`) | Packages text + ASCII-armored gpg detached sig | `mirror publish` via `repo_audit._write_signed_manifest` (union of all enabled mirrors' Packages) | `repo_audit.published_ledger` → `utils.highest_asg_update` | Authority for +asg uN bump-N derivation. Read by every subsequent publish to pick the next N. |
| `config/repo_*.key` | OpenSSH private key | operator out-of-band | `mirror publish` (per-mirror `--ssh-key` at `mirror add` time) | gitignored. |
| `cache/snapshot.timestamp` | one-line UTC TS | `utils.resolve_snapshot_timestamp` when 'latest' resolves | self (reproducibility on subsequent runs) | Volatile: `clean cache` wipes. Re-resolved on next run. |
| `cache/<uri>` | Packages / Sources / Release | `Cache.__get_files` | `Cache.__build_cache` | Per-mirror, named by `apt_pkg.uri_to_filename` so multi-mirror don't collide. |
| `source/<pkg>_<ver>.{dsc,tar.*}` | upstream Debian source | `cmd_source_sync` → `utils.download_source` | `BuildContainer.build` | SHA256-verified against the InRelease-signed Sources index. |
| `repo/dists/<codename>/main/binary-<arch>/*.deb` | binary pkg | `BuildContainer.build` → `_segregate_built_artifacts` (or tunnel) | live chroot + installer pool + apt clients | Append-only invariant: existing wins on collision. |
| `repo/dists/<codename>/main/debian-installer/binary-<arch>/*.udeb` | installer pkg | same | installer chroot | Parallel udeb namespace. |
| `repo/dists/<codename>/main/source/*.{dsc,tar.*}` | source | `cmd_source_sync` copies; not recompiled in repo | network publish | |
| `repo/dists/<codename>/{doc,tests}/binary-<arch>/*.deb` | side artifact | same | apt clients on demand | Not pre-installed anywhere. |
| `repo/dists/<codename>-debug/main/binary-<arch>/*.deb` | dbgsym | same | apt clients on demand | Separate debug suite per Debian convention. |
| `repo/dists/<codename>/{contrib,non-free,non-free-firmware}/binary-<arch>/*.deb` | tunneled passthrough | `_do_tunnel` (Filename: from cache) | network publish + installer pool | Pristine — never NMU-stripped or asg-stamped. Excluded from `all_deb_dirs()` walks. |
| `repo/dists/<codename>/Release` (+ `.gpg`, + `InRelease`) | apt-ftparchive output | `cmd_index_repo` → `apt_repo.generate_repo_indexes` | apt clients | Top-Release written via tempfile-then-mv to avoid self-reference race. |
| `fork/source/<pkg>/` | Debian source tree | operator | `fork_mirror.generate_fork_mirror` | Each pkg shipped through the same source-build path as upstream — produces .debs/.udebs in the same repo/ tree. |
| `fork/source/repo/<pkg>_<ver>.{dsc,tar.*}` | `dpkg-source -b` output | `fork_mirror._generate_source_packages` | Cache (via the file:// fork Mirror) | |
| `fork/source/repo/<pkg>.tree-hash` + `.dep-hash` | sha256 sidecar | `fork_mirror._persist_tree_hash` | `_check_and_invalidate_fork_pkg` | Drives invalidation when a fork tree changes. |
| `fork/Packages` + `Packages-udeb` + `Sources` (+ `.gz`) + `Release` | apt-ftparchive-shaped (hand-rolled) | `fork_mirror._build_*` + `_write_release` | Cache | Flat-layout file:// mirror; component=''. |
| `log/build/<src>` | stdout/stderr stream | `BuildContainer.build` | operator post-mortem | Per-source build log. |
| `log/build/<src>.result` | one-word: PASS / FAIL / TUNNELED | `BuildContainer.build` (or `_do_tunnel`) | `check_build` skip gate | Drives source_build idempotency. |
| `log/build/<src>.patchhash` | hex sha256 | `BuildContainer.build` | `_refresh_patches` (build.py) | Distinguishes header-only patch edits (no rebuild) from content edits. |
| `log/build-YYYY-MM-DDTHH-MM-SS.log` | log output | `utils.setup_file_logging` | operator | One file per `build-system.sh` invocation. |
| `gnupg/` (mode 0700) | gnupg homedir for InRelease verification | `BuildConfig` + `utils.verify_inrelease` | `Cache.__get_files` | Imports the Debian keyring; isolated from the host. |
| `gnupg/signing/` (mode 0700) | gnupg homedir for the project key | `signing.generate_key` | `signing.verify_key`, `apt_repo.sign_release_files`, `repo_audit._write_signed_manifest` | Separate trust scope. |
| `gnupg/signing/athena-archive-keyring.gpg` | exported pubkey | `signing.generate_key` | `chroot._install_signing_keyring`, `apt_repo.export_pubkey_to_staging` | The pubkey shipped into the live chroot, the target system, and the installer ISO at `.disk/archive-key.gpg`. |
| `image/<distribution>-<version>-<arch>.iso` | hybrid BIOS+EFI ISO | `cmd_build_iso_live` or `cmd_build_iso_installer` | end user | Final shipped artifact. |
| `image/<distribution>-<version>-<arch>.qcow2` | qcow2 disk image | `cmd_build_iso_disk` | end user | Pre-installed bootable VM image. |
| `image/<iso>.user` | one-line | iso build | end user | Per-build random username for the live boot (security). |
| `publish/` | mirror-shaped tree (runtime subset) | `cmd_index_repo_minimal` | `cmd_repo_publish ssh\|local minimal` | Minimal-publish staging — `.debs` from `repo/main/binary-<arch>/` minus `-dbg/-dbgsym/-source/`. |
| `patch/source/<pkg>/<ver>/9001-*.patch` | unified diff with DEP-3 header | operator | `BuildContainer.build` (live read at build time) | DEP-3 header validated by `utils.check_dep3_header`. |
| `patch/pre-install/<pkg>/*.{sh,patch}` | shell or patch | operator | `_ChrootMixin.pre_install` | Applied to the chroot before the matching package is installed. |
| `patch/post-install/<pkg>/*.{sh,patch}` | shell or patch | operator | `_ChrootMixin._apply_post_install_patches` | Applied after the package is installed. |
| `installer/preseed/preseed.cfg` | d-i preseed | operator | `installer_chroot._apply_installer_overlay` | Baked into the installer initrd. |
| `installer/finish-install/{05athena-default-source, 11athena-disable-cdrom}` | shell hook | operator | overlay map | Run on /target after install. |
| `installer/boot/{grub.cfg, grub-background.png}` | GRUB config + splash | operator | `iso_installer._stage_grub_cfg` | Installer ISO boot menu. |
| `installer/disk/{info, base_components, base_installable}` | text + empty sentinel | operator | `iso_installer.build_installer_iso` | `.disk/` content on the installer ISO. |
| `installer/debug/syslog-to-serial.sh` | shell startup hook | operator (deletable) | overlay map | Tails d-i syslog to /dev/ttyS0 for QEMU serial capture. |
| `installer/cdebconf/README.md` | docs only | operator | n/a | Documents that cdebconf-udeb's baked default wins. |
| `<source>.verified` (sidecar) | `<size> <mtime_ns> <sha256>` | `utils.get_sha256` (use_cache=True) | self | Per-file sha256 cache. Same shape applies to repo/ binaries that participate in sidecar caching. |
| `docs/done.md` | markdown | operator (close tickets) | operator | Audit-trail archive of closed work. |
| `TODO.md` | markdown | operator | operator | Open backlog with stable IDs (`STA-NN`, `CONF-NN`, etc.). |
| `docs/plans/<id>-*.md` | markdown | operator | operator | Per-area implementation plans. |
| `docs/diagrams/build-fsm.dot` (+ `.png`) | Graphviz | operator (regenerated via `dot`) | operator | Pipeline state machine. Source of truth for the PNG. |

---

## `source audit` vs `source build all` (UPDATE mode) — why the counts differ

Operators routinely see a smaller "needs rebuild" count from `source
audit` than from `source build all` when the snapshot has moved and a
prior publish exists.  Concrete example after a snapshot advance:

```
> source audit
    857  ok                                       864  total
      1  needs_build: linux
      2  stale_pass: bind9, linux-signed-amd64
      4  tunneled

> source build all
source build: UPDATE mode — published 20260517T203347Z → current
  20260529T081521Z; rebuilding the changed source delta (+asg-stamped,
  per-file N) plus any other source needing a build.
  11 changed source(s): bind9, evince, firefox-esr, gnutls28, haveged,
  krb5, libgcrypt20, linux, linux-signed-amd64, rsync, samba
```

The two commands ask different questions; both answers are correct.

**`source audit` asks**: *is my LOCAL on-disk repo self-consistent
against the current cache?*

- For each `selected_src`, predict the pristine binary filename
  (`<pkg>_<pristine-version>_<arch>.deb`), check it on disk via
  `find_matching_artifact`, validate with `verify_pkg_artifact`.
- The `strip_nmu_at_build` policy ([[strip-nmu-at-build]]) strips
  `+debNuN`, `~bpoN+N`, `+bN`, `+rpiN`, legacy `-Nb` from every
  produced binary's Version field, so the predicted filename is the
  PRISTINE upstream version.
- Therefore, a Debian security bump that only moves the NMU counter
  (`+deb12u14 → +deb12u15`) does NOT change the predicted pristine
  binary name → the existing `.deb` still matches → audit says **ok**.
- Only sources where the **pristine base** genuinely shifted
  (`linux 6.1.146-1 → 6.1.147-1`, or a `-1 → -2` Debian-revision bump)
  produce a different predicted name and surface as `needs_build` or
  `stale_pass`.

**`source build all` in UPDATE mode asks**: *between the LAST PUBLISH
snapshot and the CURRENT snapshot, which sources need a
`+asg<R>u<N>`-stamped rebuild so the next publish advertises the
security delta?*

- Triggered automatically by `_update_build_pending()` when any
  enabled mirror's recorded `current` lags behind `snapshot.state.current`
  (i.e. the operator has advanced the pin since the last
  `mirror publish` to at least one peer).
- `_workload_since_snapshot()` compares the source version at the
  published snapshot to the source version at the current snapshot.
  ANY move — including pure `+deb12u14 → +deb12u15` security bumps
  that strip to the same pristine binary — is in the workload.
- `_do_update_build()` rebuilds each workload source and the post-
  build stamper applies `+asg<R>u<N>` ([[+asg-update-versioning]]) so
  downstream apt clients see a new version available and pull it,
  even when the underlying binary is byte-identical to what was last
  shipped.

The 11-vs-3 split in the example breaks down as:

| Category | Sources | Why audit didn't flag them |
|----------|---------|-----------------------------|
| Pristine base changed | `linux`, `bind9`, `linux-signed-amd64` | (audit DID flag these — `needs_build` + 2 `stale_pass`) |
| Debian `+deb12uN` security delta only | `evince`, `firefox-esr`, `gnutls28`, `haveged`, `krb5`, `libgcrypt20`, `rsync`, `samba` | strip_nmu means the predicted pristine filename matches on-disk → audit ok |

The split is intentional, not redundant: `audit` tracks **local
correctness** (will a fresh install boot? — yes, every predicted .deb
is present and matches the cache), `update mode` tracks **published
delta** (do we owe our apt subscribers a security re-issue? — yes,
8 sources moved upstream and we need to mint stamped binaries to
advertise that).

Aligning them would require disabling `strip_nmu_from_deb`, which
would re-introduce Debian's release-cycle metadata
(`+deb12uN` / `~bpoN+N`) into our archive — violating the
pristine-upstream invariant on which the [[+asg-update-versioning]]
scheme is built (asg-stamps assume a clean pristine base to suffix).

Where each lives in code:
- `audit`: `repo_audit.audit_*` family + `BuildContainer.check_build`
  + `find_matching_artifact` (in `utils.py`).
- update workload: `BuildSession._workload_since_snapshot()` (in
  `build.py`) — diffs published-snapshot Sources index vs current.
- update orchestration: `BuildSession._do_update_build()` (in
  `build.py`) — loads `asg_ledger` from `config/published.manifest`,
  then drives `cmd_source_build` over the workload with bump-aware
  skip semantics ([[bump-target-build-loop]]).

---

## Reading guide

To trace a typical build end-to-end:

1. Operator runs `./build-system.sh` → `build.py:main` constructs
   BuildConfig, Tui, BuildSession, registers commands, blocks.
2. Operator types `autorun live`. The autorun driver walks:
   `cmd_build_cache` → `cmd_parse_dependency` → `cmd_source_sync` →
   `cmd_init_container` → `cmd_source_build` (pkg) → `cmd_source_build`
   (live) → `cmd_build_chroot_live` (signs the key, audits, builds the
   chroot, verifies) → `cmd_build_iso_live`.
3. Each stage sets a BuildFlag; the next stage gates on the previous
   flag.
4. `cmd_build_cache` constructs `Cache(config)` — per-mirror download +
   GPG verify + decompress + parse + fork-supersede + collision gate.
5. `cmd_parse_dependency` builds two `DependencyTree` instances — one
   over the deb world, one over the udeb world — via seven resolution
   passes, then validates the selection.
6. `cmd_source_sync` downloads every selected source's tarballs via
   `utils.download_source`.
7. `cmd_init_container` constructs `BuildContainer(config, cache)` —
   pulls or rebuilds the Docker image.
8. `cmd_source_build` iterates source packages. For each: check the
   `.result` cache to skip; tunnel pristine binaries; or run
   `BuildContainer.build` which sets up a per-package container, applies
   patches + token-substitutions, runs `dpkg-buildpackage`, segregates
   the outputs to component dirs, and runs NMU strip + asg stamp.
9. `cmd_build_chroot_live` composes `BuildSystem(dep_tree, config)` and
   runs the chroot build — topo-sorted install batches with libc seed,
   post-install patches, system-configs, signing keyring install. Auto-
   runs the 8-check verify at the end.
10. `cmd_build_iso_live` calls `iso.build_live_iso` — squashfs over the
    chroot + grub-mkrescue (in container) → hybrid BIOS/EFI ISO under
    `image/`.

For a complete picture of state transitions and reset edges, see the
FSM diagram in `docs/diagrams/build-fsm.dot`.
