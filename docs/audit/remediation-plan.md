# Audit remediation plan — 2026-06-29

Derived from the pre-build-cycle audit (`docs/audit/2026-06-29-prebuild-codebase-audit.md`)
and a follow-up **adversarial verification** of all 199 single-pass medium/low findings
(one read-only skeptic per finding, prompted to refute; 199 agents, ~4.1M tokens).
Every item links its tracking issue. Severities below are the **adversarially-corrected**
severities, which differ from the raw audit where verification softened a claim.

**Verdict tally (199 single-pass findings):** 141 confirmed · 48 partial · 10 refuted; 15 closed as not-a-defect (refuted or severity→none); 20 medium→low downgrades; 0 upgrades.

**Remediation status (checkboxes below reflect GitHub issue state):**
- **All confirmed correctness bugs fixed** — Tier 0 (4), Tier 0b (6), Tier 1 (8),
  Tier 2 (32), Tier 3 (29) = **79 fixed, merged to `master`, and closed**, plus the
  Tier-4 safe-cleanup subset (12). +58 regression tests; triad green at every merge.
- **#6 + coverage gaps #15–#17** are fixed on `feat/update-mode-transpose` (ticked
  here; the issues close when that branch merges, since their code isn't on master).
- **Open / deferred:** the remaining Tier-4 refactors & judgment calls (~63), the
  T5 test gaps, and **#80 / #92 / #98** (deferred — real but in delicate
  federation/tunnel code with no integration harness here). **#160** was closed as
  not-planned (verifier refuted the fix).

> Order of work: **Tier 0 before the next build cycle.** Tiers 1–2 are real correctness
> fixes; Tier 3 is real-but-low-reachability (do opportunistically); Tier 4 is cleanup;
> Tier 5 is test debt. Closed/refuted findings are listed in the appendix for the record.

## Tier 0 — Build-cycle blockers (fix before the next build)

Confirmed real **and** reachable, and on the build/publish/onboarding path.

- [x] **#18** `scripts/apt_repo.py:307-311` — generate_repo_indexes udeb scan omits allow_empty=True (sibling generate_apt_repo sets it) — empty debian-installer/ dir aborts the whole publish
  - _Fix:_ Pass allow_empty=True to the udeb _scan_packages_to call (matching generate_apt_repo line 133), or pre-count .udeb files and skip the subdir when zero, mirroring the _deb_count guard used for the binary component.
- [x] **#7** `scripts/buildcontainer.py` — Segregate rollback moves an already-published .deb out of the repo (append-only violation, data loss)
  - _Fix:_ Track kept-existing duplicates separately from genuinely-moved files (e.g. append `_dst` to a `_kept_existing` list, not `_moved_paths`, in the collision branch), and have the rollback loop reverse ONLY genuinely-moved entries. The kept-exi
- [x] **#99** `scripts/coord/reconcile.py:542-550` — publish_halt_reason fails OPEN on an unreadable HALT sentinel
  - _Fix:_ On a non-FileNotFoundError OSError, return a non-None sentinel string (e.g. 'PUBLISH_HALT present but unreadable: <err>') so the caller refuses; only a genuine FileNotFoundError should map to None.
- [x] **#8** `scripts/signing.py` — verify_key filters by config.signing_key_uid, not the key actually present — breaks federation peer onboarding when origin UID != peer default
  - _Fix:_ In verify_key, resolve the on-disk UID first (uid = actual_signing_uid(config) or config.signing_key_uid) and use that for both the existence check and `--local-user`; or gate on get_key_info via the unfiltered key. Alternatively fix the ca

**Adversarially demoted from the audit's headline blockers** (do NOT prioritise — verification
found them not reachable on a live path):

- [x] **#148** `scripts/or_resolve.py:83-104` — seeds iterated twice — a generator yields an empty closure on the default (real_pkgs=None) path _(partial: real but not reachable)_
  - _Fix:_ Materialize once at the top of resolve_closure: `_seeds = list(seeds)` and use `_seeds` for both _infer_real and the _pending comprehension (or narrow the annotation to Sequence[str]).
- [x] **#23** `scripts/build.py:713` — patch_list sort key (x[:5]) diverges from buildcontainer's full-name sort feeding an order-sensitive hash _(partial: real but not reachable)_
  - _Fix:_ Sort by the full filename: `_src.patch_list = sorted(_patch_files)` (drop the key=lambda x: x[:5]) so build.py and buildcontainer.py:1052 produce identical ordering and matching hashes. Full-name sort still preserves the numeric-prefix orde
- [ ] **#98** `scripts/coord/reconcile.py:130-203` — audit_local hash-verifies only ONE binary per multi-binary source (lossy projection) _(partial: real but not reachable)_
  - _Fix:_ Drive C-1/C-2/C-3 from a filename-keyed live view (e.g. store.iter_live_claims_by_filename or project_owners filtered to builder_id) instead of project_live_claims, so every binary filename is hash-verified and accounted for.

## Tier 0b — Verified high/critical (the rest of the adversarially-verified set)

- [x] **#6** `scripts/bump.py` — _needs_bump_build shim-signed parity: predictor reconstructs from source suffix not binary's own version (dormant)
- [x] **#9** `scripts/bump_version.py` — --freeze-stamp commit fails: _buildstamp.py is gitignored, git add aborts
  - _Fix:_ Drop _BUILDSTAMP_PY from the commit set: `_files = [_PYPROJECT, _VERSION_PY]` unconditionally. The freeze-stamp file is meant to stay untracked; rewriting it on disk (done at line 170) is sufficient.
- [x] **#14** `scripts/commands/cohorts.py` — _tunnel_filenames_for_source picks source_hashtable[0] (parse order), not the resolved/highest-version source
  - _Fix:_ Prefer the already-resolved source: look up `self.dep_tree.selected_srcs` / `self.udeb_dep_tree.selected_srcs` for src_name first; failing that use `max(_cands, key=lambda s: s.version)` (mirroring cmd_source.py:1457 and cmd_virtual's _look
- [x] **#10** `scripts/dependencytree.py` — OR-grouped Pre-Depends (alt_pre_depends) are never resolved or validated
  - _Fix:_ Treat alt_pre_depends like alt_depends: in parse_dependency feed `_selected_pkg.alt_depends + _selected_pkg.alt_pre_depends` through the alternative-selection loop (line 588/590), and in validate_selection iterate `alt_depends + alt_pre_dep
- [x] **#11** `scripts/fork_mirror.py` — Empty/malformed changelog raises IndexError that escapes the narrow except and aborts the entire cache build
  - _Fix:_ Broaden the except at both call sites (and/or inside _read_pkg_version) to also catch IndexError and debian.changelog.ChangelogParseError — or mirror _compute_dep_hash's `except (OSError, Exception)` — and `continue`, so a broken fork is lo
- [x] **#12** `scripts/identity_scan.py` — IndexError when 'apt-install' appears only in an inline comment
  - _Fix:_ After computing `head`, guard: `if 'apt-install' not in head: return None` before the split (the comment-stripped command is what matters), or wrap the split and return None on absence.
- [x] **#13** `scripts/webapi/readers.py` — mirror_state() reads legacy mirror.<name>.state files, ignoring the authoritative signed mirror.conf
  - _Fix:_ Replace the .state globbing with mirror.py's authoritative loader (load_mirror_conf / the registered-map accessor), returning the verified mirrors dict from mirror.conf and surfacing its verify status; drop the direct .state reads.

## Tier 1 — Confirmed correctness, medium severity

- [x] **#27** `scripts/build_closure.py:94, 198` — Closure expansion order is set-iteration dependent → non-deterministic OR-group resolution
  - _Fix:_ Process the frontier deterministically: seed with `sorted(_inset)` and either use a deque FIFO or sort newly-added picks before extending; pure-virtual single-name picks are already deterministic via `sorted(_providers)[0]`, so sorting the 
- [x] **#34** `scripts/buildlog.py:138` — write() opens output without encoding='utf-8'; non-ASCII content lost under C/ASCII locale
  - _Fix:_ open(_tmp, 'w', encoding='utf-8') (matching utils.py). Optionally errors='replace' for extra safety.
- [x] **#43** `scripts/cache.py:651-658, 985-991` — UnicodeDecodeError from index parse escapes the OSError-only guard
  - _Fix:_ Either pin readfile to `encoding='utf-8'` (Debian indices are UTF-8) with `errors='replace'` as a fallback, and/or broaden the two `except OSError` clauses here to `except (OSError, ValueError)` so a decode failure becomes a graceful error_
- [x] **#48** `scripts/chroot.py:1505-1569` — systemd-firstboot --setup-machine-id bakes a FIXED /etc/machine-id, overriding the documented empty-first-boot design
  - _Fix:_ Drop `--setup-machine-id` from the systemd-firstboot invocation (the empty /etc/machine-id at line 1505 already gives correct first-boot generation), or re-truncate /etc/machine-id to empty immediately after the firstboot call so each insta
- [x] **#64** `scripts/commands/cmd_mirror.py:2202` — mirror reclaim omits --no-iso, so the ISO release-media gate refuses it
  - _Fix:_ Call `self.cmd_mirror_publish(_n, '--no-iso', reclaim_intents=_sel)` at line 2202 so a reclaim publishes the repo/claims without requiring current-snapshot install media.
- [x] **#83** `scripts/commands/cmd_virtual.py:90-104` — validate's _canon_map diverges from synth's filtered universe (Provides-alias misattribution)
  - _Fix:_ Build _canon_map from the already-filtered _universe (for each binary pick the apt-highest version key in _universe[bn] and read its 'Source', falling back to bn) instead of re-walking the raw hashtables and taking _rec[0]; this reuses from
- [x] **#86** `scripts/coord/config_manifest.py:136-142` — Partial list application leaves pkg.list overwritten but reported as NOT applied
  - _Fix:_ Make application all-or-nothing: write each list to a temp file in the same dir, fsync, and os.replace() them only after all four succeed (atomic rename); or stage into a list of (path,text) and write under a try that restores prior content
- [x] **#104** `scripts/coord/store.py:373-391` — project_owners ranks cross-builder claims by builder-local seq, so a higher-seq DEPRECATION can outrank a live PUBLISHED takeover
  - _Fix:_ Make the winner key prefer a live PUBLISHED claim over a DEPRECATION/OBSOLETE marker for the same filename before falling back to seq (e.g. key = (state_rank, -seq, builder) where published outranks marker states), or break cross-builder ti

## Tier 2 — Confirmed correctness, low severity

- [x] **#37** `scripts/buildsystem.py:104` — Password-failure error reads stdout, but sudo writes diagnostics to stderr
  - _Fix:_ Use `_proc.stderr.strip()` (optionally `or _proc.stdout.strip()`) in both the __init__ (line 104) and for_iso (line 188) validation error messages, matching the wipe handler at line 134.
- [x] **#47** `scripts/cache.py:925-933` — Provides-injected versions can inflate fork version and defeat the collision gate
  - _Fix:_ When computing the fork's version for the gate, restrict to records whose real `_pkg.package == _name` (e.g. iterate the Package objects in the version lists and filter by `.package`) rather than trusting every key, which may be a Provides 
- [x] **#53** `scripts/cli.py:355, 380` — quit/exit detection drifts between _dispatch_one (first token) and the loop break-checks (full line)
  - _Fix:_ Make _dispatch_one the single source of truth for the quit/exit decision — e.g. return a small sentinel/enum (RAN/EMPTY/QUIT/FAILED) instead of a bool, or have callers test `line.split()[:1] == ['quit']`-style first-token match consistent w
- [x] **#59** `scripts/commands/cmd_build.py:1045-1052` — _verify_chroot check 2 ignores subprocess returncode → false PASS on a failed dpkg call
  - _Fix:_ Fold the returncode into the ok condition: `_check('All packages fully installed', _r.returncode == 0 and not _incomplete, ...)` and surface a distinct detail (e.g. dpkg stderr) when returncode != 0. Add a test asserting check 2 FAILs when 
- [x] **#61** `scripts/commands/cmd_cache.py:247` — Build-mode parse leaks OSError + leaves spinner running (dist path guards it)
  - _Fix:_ Wrap the parse_build_pkg_list call (line 247) in try/except OSError mirroring lines 485-492: on error print a console error, log it, call _spiner.done() (or move the whole build-mode branch under the spinner's cleanup), set dep_check_ready 
- [x] **#68** `scripts/commands/cmd_repo.py:866` — Cleanup report understates scanned components (omits main-udeb)
  - _Fix:_ Include `main-udeb` in the component list string (e.g. `{main,main-udeb,doc,dbgsym,tests}`), ideally derived from `utils._STALE_SCAN_SUBDIRS` so it can't drift again.
- [ ] **#69** `scripts/commands/cmd_run.py:314` — cmd_set truncates multi-token values; `set signing-uid 'Name <email>'` is non-functional
  - _Fix:_ In cmd_set, build the value from the remaining tokens: `_value = ' '.join(args[1:])` (and strip surrounding quotes), so space-containing values like signing-uid reach the handler intact.
- [x] **#71** `scripts/commands/cmd_run.py:185` — Machine-local setters mutate self.config before an unguarded write_local_conf, unlike _set_mode
  - _Fix:_ Mirror _set_mode: wrap each write_local_conf in try/except OSError and emit a 'could not persist to local.conf' warning rather than letting it bubble to the generic dispatcher handler.
- [x] **#76** `scripts/commands/cmd_source.py:1007` — Audit suggests a non-runnable `source build (pool)` remediation command
  - _Fix:_ Map 'pool' to the actual command that builds pool extras (e.g. `source build all`) or annotate explicitly that pool sources are only built via `source build all`.
- [x] **#96** `scripts/coord/publish.py:1366-1402` — Step 6c obsolescence can re-assert ownership over a file Step 6b just deprecated
  - _Fix:_ Exclude filenames deprecated in this same publish from the 6c obsolescence input (or merge the 6b deprecation claims into `_view` so they win the fold), so a released file is not re-owned as an obsolete prune-candidate.
- [x] **#106** `scripts/coord/store.py:96` — append_claim ignores os.write return value, defeating the 'complete line in one write()' atomicity invariant on a short write
  - _Fix:_ Loop os.write until len(_line) bytes are written (write-all helper), or write to a temp file and os.rename for true atomicity; raise on short write so disk-full surfaces instead of producing a torn line.
- [x] **#108** `scripts/coord/transport.py:119-134` — _run_rsync_streaming drops the final un-newline-terminated line from the failure tail
  - _Fix:_ After `_proc.wait()`, if `_buf.strip()` is non-empty and its first token is not a digit, append `_buf.strip().decode('utf-8','replace')` to `_tail` before building the detail string.
- [x] **#111** `scripts/dep_drift.py:326-327` — Hard-dep violation message renders empty constraint as '( )'
  - _Fix:_ Mirror the line-333 conditional: emit `{_dep[0]}` alone when `_dep[2]` is empty, else `{_dep[0]} ({_dep[2]} {_dep[1]})`.
- [x] **#117** `scripts/diag_installer_status.py:108-114` — Char position reported is into the joined value, not the source line
  - _Fix:_ Track position per raw line, or report the line number alongside the offset.
- [x] **#120** `scripts/disk_image.py:586-605` — Sparse _raw intermediate is never removed on failure paths
  - _Fix:_ In the finally block, attempt `os.unlink(_raw)` (guarded by os.path.exists / OSError) when the function did not reach a successful convert, or document that the raw is intentionally retained for post-mortem.
- [x] **#121** `scripts/disk_image.py:569-578` — e2fsck exit code 3 (errors-corrected + reboot-bit) falls through both branches silently
  - _Fix:_ Treat the low bits explicitly: `if _fsck.returncode & 0b11 and _fsck.returncode < 4` (or `in (1,2,3)`) for the cleaned case before the `>= 4` serious check.
- [x] **#126** `scripts/fork_mirror.py:942` — _write_release hardcodes 'Architectures: amd64 all' regardless of buildconfig.arch
  - _Fix:_ Pass build_arch into _write_release and emit f'Architectures: {build_arch} all'.
- [ ] **#127** `scripts/identity_scan.py:270-283` — Redirect/pipe attached to a token without a space is treated as a package name
  - _Fix:_ Strip leading redirect operators from each token (e.g. break when `_t` starts with any of < > | & after stripping leading digits), or split tokens on embedded redirect chars before the per-token classification.
- [x] **#143** `scripts/mirror.py:729-735` — Read-only/status helpers trigger a file WRITE via the legacy-migration path
  - _Fix:_ Either document the one-time migration write explicitly at each read-named caller, or split migration into an explicit step invoked only from a write/setup path so status/read helpers never write.
- [x] **#145** `scripts/onboarding.py:363` — set_registration return value ignored in federation flow
  - _Fix:_ Capture the return and, if False, console.print an error and `return False` so the wizard stays un-configured and re-runnable, consistent with the other guarded steps.
- [x] **#165** `scripts/remote_localmirror.py:202` — total_size:null crashes here but is guarded one line above
  - _Fix:_ Make line 202 match line 187: `_cum_total = int(plan.get('total_size', 0) or 0) or 1`.
- [x] **#168** `scripts/remote_orchestrate.py:354-360` — _open_tunnel omits StrictHostKeyChecking=accept-new, diverging from _ssh_base contract
  - _Fix:_ Add '-o','StrictHostKeyChecking=accept-new' to the _open_tunnel argv (or build it from a shared helper) so all ssh sites are consistent with the _ssh_base docstring.
- [x] **#171** `scripts/repo_audit.py:168-210` — scan_repo_state docstring + ValueError message omit the valid 'main-udeb' label it explicitly supports
  - _Fix:_ Update the docstring label list and the ValueError text to include 'main-udeb' (ideally derive the message from utils.deb_dir_for's accepted keys to avoid future drift).
- [x] **#172** `scripts/repo_audit.py:1040-1042` — audit_dep_closure docstring claims weak list holds 4-tuples; it emits 3-tuples
  - _Fix:_ Reword the docstring to state unresolved is (pkg, field, relation_str, why) 4-tuples and weak is (pkg, field, relation_str) 3-tuples.
- [x] **#173** `scripts/sbom.py:16,98` — "empty hash for pristine sources" is false — pristine emits sha256-of-empty constant
  - _Fix:_ Either set athena:patch-set-hash to '' when not _patch_files, or fix the docstring/comment to say pristine sources carry the canonical empty-digest sha256 rather than an empty string.
- [x] **#176** `scripts/select_packages.py:51-52, 130-131` — POOL_GROUP '(pool)' CAN collide with a real [(pool)] section, contrary to the inline claim
  - _Fix:_ Either validate/reject a real group literally named '(pool)' in _load_model (raise or rename), or detect the clash before the line-130 overwrite and warn; at minimum correct the comment to state the collision is possible and unhandled.
- [x] **#179** `scripts/selection_lock.py:91-93` — Transient OS read errors are reported as STATUS_MALFORMED (treated as tamper)
  - _Fix:_ Distinguish transient I/O from corruption: either re-raise/propagate the OSError (fail loudly without re-baseline guidance) or add a STATUS_IOERROR that callers treat as retryable rather than as untrusted/tamper. At minimum, do not steer th
- [x] **#180** `scripts/selection_lock.py:352-356` — classify() returns two dicts that alias the SAME empty set objects for added/removed
  - _Fix:_ Return freshly constructed dicts with independent sets, e.g. `return ACTION_BOOTSTRAP, {'bins': set(), 'srcs': set()}, {'bins': set(), 'srcs': set()}` (or a `_fresh_empty()` helper), instead of two shallow copies of one shared template.
- [x] **#186** `scripts/tui/dispatcher.py:238-243` — console_mark() can raise CancelledError on shutdown instead of returning 0
  - _Fix:_ Catch CancelledError alongside _FutureTimeout in the console_mark() loop and return 0 (and similarly decide/ document the request_prompt() cancel path).
- [ ] **#189** `scripts/tui/facade.py:164-168` — PROMPT_OPTIONS under stdin EOF returns an unvalidated (empty) answer, crashing consumers that index by it
  - _Fix:_ On the EOF branch, treat PROMPT_OPTIONS like a hard abort: either raise a clean controlled error (e.g. Exit/abort) or return a sentinel the consumers check, rather than returning an out-of-range string that callers feed to int()/list-index.
- [x] **#191** `scripts/tui/render.py:283-293` — Console widgets float under content instead of anchoring to bottom rows when buffer is short
  - _Fix:_ If widgets must occupy the bottom band, pad `row` up to max_y - widget_rows before drawing widget_strs (e.g. row = max(row, max_y - widget_rows)), or draw widgets at explicit rows max_y-widget_rows+i.
- [x] **#208** `scripts/webapi/auth.py:18` — Comment '32 bytes → 64 hex chars' is wrong for token_urlsafe
  - _Fix:_ Reword to '32 bytes → ~43 url-safe base64 chars' to match token_urlsafe.
- [x] **#210** `scripts/webapi/jobs.py:72,85-87` — _jobs dict grows unbounded for the life of the API session
  - _Fix:_ Cap retained completed jobs (e.g. ring buffer / prune done|error jobs older than N or beyond a max count) under _jobs_lock after each job finishes.
- [x] **#214** `scripts/webapi/readers.py:170` — read_artifact tail 'truncated' compares byte size to character-length sum
  - _Fix:_ Track whether _tail_lines actually hit file start (pos>0 when the loop exited) and report truncated from that, or compare byte counts consistently (sum of len(_l.encode())+1).
- [x] **#215** `scripts/webapi/readers.py:348-349, 361` — buildlog/vbuildlog opened without a context manager in progress()
  - _Fix:_ Wrap both reads in `with open(...) as _fh: _txt = _fh.read()` to match the rest of the module.

## Tier 3 — Real but low-reachability / overstated (partial verdicts)

- [x] **#21** `scripts/arch_filter.py:110-134` — check=True makes one bad arch abort the whole map, silently disabling the filter for the process _(partial: real but not reachable)_
  - _Fix:_ Wrap only the inner per-arch `dpkg-architecture -a` call in its own try/except and `continue` on failure, so a single arch error degrades that arch, not the entire filter.
- [x] **#23** `scripts/build.py:713` — patch_list sort key (x[:5]) diverges from buildcontainer's full-name sort feeding an order-sensitive hash _(partial: real but not reachable)_
  - _Fix:_ Sort by the full filename: `_src.patch_list = sorted(_patch_files)` (drop the key=lambda x: x[:5]) so build.py and buildcontainer.py:1052 produce identical ordering and matching hashes. Full-name sort still preserves the numeric-prefix orde
- [x] **#26** `scripts/build.py:1600-1678` — cmd_container_remote_add leaks the copied SSH key when add_remote fails _(partial: real but not reachable)_
  - _Fix:_ On the add_remote failure path, best-effort os.remove(_keydst) (mirroring delete's cleanup) before returning.
- [x] **#36** `scripts/buildlog.py:89-124` — Accumulation methods format values outside the try/except, contradicting the 'never propagates' invariant _(partial: real but not reachable)_
  - _Fix:_ Either wrap the format calls in the same best-effort try (e.g. format inside _append-protected helpers) or soften the docstring claim to 'best-effort given normally-stringable values'.
- [x] **#40** `scripts/bump.py:785` — transposed_version docstring example contradicts actual code + its own test _(partial: real but overstated)_
  - _Fix:_ Change the line-785 example to `transposed_version('1.0-2', 'asg', 1, force_bn=1) → '1.0-2+asg1u0+b1'` to match _append_patch_force and the existing tests.
- [x] **#55** `scripts/commands/cmd_audit.py:785-800` — Stale-files row colors green (ok=True) when only malformed (corrupt) .debs are present _(partial: real but overstated)_
  - _Fix:_ Make the row consistent with the gate: `ok=(_n_stale == 0 and not _malformed)`, so malformed artifacts color the row amber the way the preflight gate counts them.
- [x] **#72** `scripts/commands/cmd_snapshot.py:269-310` — `_snapshot_select_force` omits the eager build.conf reconcile its forward-only sibling does, leaving the visible config stale _(partial: real but overstated)_
  - _Fix:_ After `append_snapshot_history` in `_snapshot_select_force`, call `_synced = utils.reconcile_snapshot_pin(self.config)` and surface it in the confirmation line, mirroring `_set_snapshot_pin` (so backtracks keep build.conf honest immediately
- [x] **#75** `scripts/commands/cmd_source.py:1413-1417` — `.disabled` marker text leaves literal `{pkg}` unexpanded (missing f-prefix) _(partial: real but overstated)_
  - _Fix:_ Add the `f` prefix to the line 1416 fragment: `f'run `source fork {pkg} enabled` to '` (or make the whole write a single f-string / .format).
- [x] **#78** `scripts/commands/cmd_supply_chain.py:242` — artifact lookup omits the `or {}` fallback used everywhere else → AttributeError on null artifact _(partial: real but not reachable)_
  - _Fix:_ Change line 242 to `len({(_m.get('artifact', {}) or {}).get('name', '') for _m in _matches})` to match the `or {}` idiom at lines 237/260/261.
- [ ] **#80** `scripts/commands/cmd_tunnel.py:299-308, 354, 367, 449-452` — Non-integer [Build] VERSION fallback ships +deb-named debs but records pristine built_version (inconsistent record + misleading message) _(partial: real but overstated)_
  - _Fix:_ In the `_release is None` branch, record built_version equal to the actual (un-transposed) upstream version and skip the '→ pristine' arrow, or hard-fail the tunnel when VERSION is not an integer rather than silently shipping upstream-named
- [x] **#88** `scripts/coord/head.py:70-103` — write_coord_head is non-atomic: a transient gpg failure destroys the previously-valid signed head _(partial: real but overstated)_
  - _Fix:_ Write JSON to a temp file in coord_dir, sign the temp, then os.replace() both the .json and .sig into place only after the signature succeeds — so a sign failure leaves the prior good head untouched.
- [ ] **#92** `scripts/coord/policy.py:32` — ORPHAN_WARN_AFTER_DAYS documents a 14-day INFO grace period that is never implemented; orphans always WARN immediately _(partial: real but overstated)_
  - _Fix:_ Either (a) implement the threshold in reconcile.py C-1: stat the claim/record mtime, emit INFO when age < ORPHAN_WARN_AFTER_DAYS*86400 and WARN otherwise, or (b) if immediate WARN is intended, delete the constant and rewrite the policy.py c
- [x] **#95** `scripts/coord/publish.py:157-158` — Drift detection treats whole-binary pool absence as drift, bypassing the pulled_from no-reclaim guard _(partial: real but overstated)_
  - _Fix:_ Require actual presence before declaring drift: `_latest = _pool_latest.get(bn); _latest is not None and _fn != _latest`. A fully-absent declared binary then no longer flips drift (owned sources still drop it via the push-fail path), and a 
- [ ] **#98** `scripts/coord/reconcile.py:130-203` — audit_local hash-verifies only ONE binary per multi-binary source (lossy projection) _(partial: real but not reachable)_
  - _Fix:_ Drive C-1/C-2/C-3 from a filename-keyed live view (e.g. store.iter_live_claims_by_filename or project_owners filtered to builder_id) instead of project_live_claims, so every binary filename is hash-verified and accounted for.
- [x] **#107** `scripts/coord/store.py:43-103` — No atomic seq allocation: concurrent same-builder appends can duplicate seq _(partial: real but not reachable)_
  - _Fix:_ Allocate seq under the same flock inside append_claim (re-read max_seq while holding LOCK_EX and stamp claim['seq']), or document that concurrent same-builder publish is unsupported and enforce a single-publisher lock at the command layer.
- [x] **#114** `scripts/diag_installer_status.py:142` — errors='replace' defeats the script's own non-ASCII byte detection _(partial: real but overstated)_
  - _Fix:_ Read in binary ('rb') and decode latin-1 (1:1 byte->codepoint) so ord(_c) is the true byte value; or report the original byte offset/value explicitly.
- [x] **#116** `scripts/diag_installer_status.py:128-132` — EMPTY-VALUE heuristic flags legitimately empty dpkg fields _(partial: real but overstated)_
  - _Fix:_ Document as heuristic in output, or restrict EMPTY-VALUE to fields known to require values (Package, Version, Description synopsis).
- [x] **#130** `scripts/installer_chroot.py:259-260` — _resolve_udeb_files docstring cites strip_build_version but code uses normalize_repo_filename + find_matching_artifact _(partial: real but not reachable)_
  - _Fix:_ Update the docstring to reference normalize_repo_filename (+bN/+debNuN strip) and find_matching_artifact (pristine-or-+asg-stamp match).
- [x] **#133** `scripts/iso_installer.py:871-872` — deb_whitelist=None silently bypasses exclude_names (superseded-fork exclusion) _(partial: real but not reachable)_
  - _Fix:_ Apply the exclude_names filter to the legacy path too: in the `deb_whitelist is None` branch, drop entries whose parsed package name is in exclude_names before returning, rather than short-circuiting past the exclusion logic.
- [x] **#139** `scripts/local_mirror.py:280` — Release hardcodes `Architectures: amd64 all` _(partial: real but not reachable)_
  - _Fix:_ Derive the arch line from the host/dpkg arch (or scan the present .deb Architecture fields) instead of hardcoding amd64.
- [x] **#144** `scripts/onboarding.py:184-191` — Jobs input silently clamped/defaulted, diverging from `set jobs` which rejects _(partial: real but overstated)_
  - _Fix:_ Mirror _set_jobs: on non-int or out-of-range, print a one-line warning (e.g. ' jobs clamped to 8 (docker-py pool limit)') before persisting, so the wizard and `set jobs` behave consistently.
- [x] **#148** `scripts/or_resolve.py:83-104` — seeds iterated twice — a generator yields an empty closure on the default (real_pkgs=None) path _(partial: real but not reachable)_
  - _Fix:_ Materialize once at the top of resolve_closure: `_seeds = list(seeds)` and use `_seeds` for both _infer_real and the _pending comprehension (or narrow the annotation to Sequence[str]).
- [x] **#149** `scripts/or_resolve.py:33, 121-134` — Docstring claims 'minimal closure' but the per-group min() tie-break is not minimal _(partial: real but overstated)_
  - _Fix:_ Reword the docstring to claim 'a deterministic, order-independent closure' rather than 'the minimal closure', or change Pass B to prefer an alternative that satisfies the most outstanding groups before falling back to first-declared.
- [x] **#159** `scripts/remote_agent.py:106-124` — read_log next-offset uses pre-read getsize, not frm+len(data) → duplicate log bytes on concurrent growth _(partial: real but overstated)_
  - _Fix:_ Return `(_data, frm + len(_data), _size)` (or set `_size = frm + len(_data)` after the read). This is lossless and overlap-free regardless of concurrent growth, and still satisfies test_remote_agent_helpers_name_and_log_offsets (frm+len==12
- [x] **#160** `scripts/remote_agent.py:289-290 / 368-374` — scripts/remote_agent.py:289-290 / 368-374 — Empty token fails OPEN — token-file read error yields an unauthenticated agent _(partial: real but overstated)_
  - _Fix:_ Fail closed: if the resolved token is empty, either refuse to start the server (sys.exit non-zero) or make `_auth_ok` return False whenever `self._srv().token == ''`.
- [x] **#177** `scripts/select_packages.py:501-504` — _save writes pool.list unconditionally, creating a spurious empty file that discard cannot roll back _(partial: real but not reachable)_
  - _Fix:_ Skip the pool.list write when the pool tier is empty AND the file does not already exist, or have the discard path in cmd_cache remove files it created; track created-vs-modified in the backup map.
- [x] **#181** `scripts/signing.py:158-163` — format_gpg_time leaks OverflowError on out-of-range epoch (only ValueError/OSError caught) _(partial: real but not reachable)_
  - _Fix:_ Add OverflowError to the except tuple: `except (ValueError, OSError, OverflowError): return s`.
- [x] **#184** `scripts/tasksel_desc.py:28` — _sanitize keeps non-whitespace ASCII control chars (ord 0-8, 11, 14-31) _(partial: real but not reachable)_
  - _Fix:_ Tighten the keep-condition to printable ASCII, e.g. drop any `_ch` where `ord(_ch) < 32 or ord(_ch) > 126`, before the whitespace-collapse.
- [x] **#194** `scripts/tui/tui.py:298-306` — SystemExit with a non-int code is mapped to exit 0 (success), masking failure _(partial: real but not reachable)_
  - _Fix:_ Mirror CPython semantics: treat non-None, non-int code as 1 (failure) rather than 0, e.g. `code = _se.code; self.exit(code if isinstance(code,int) else (0 if code is None else 1))`.
- [x] **#196** `scripts/tui/widgets.py:96-103` — set_max lowering below current _value overflows the fixed-width bar _(partial: real but not reachable)_
  - _Fix:_ Clamp in set_max (self._max = max(1, value, self._value)) or clamp filled = min(filled, self._bar_width) in __str__.
- [x] **#205** `scripts/webapi/__init__.py:214-224` — SSE stream can drop the final output line(s) due to drain-then-state ordering _(partial: real but overstated)_
  - _Fix:_ After detecting a terminal state, perform one final drain before emitting the end event, e.g. inside the `if _job.state in ('done','error')` block re-read `_out = _job.output` and `while _sent < len(_out): yield ...; _sent += 1` before the 
- [x] **#207** `scripts/webapi/auth.py:34-38` — Concurrent first-start loser raises instead of getting the winner's key _(partial: real but overstated)_
  - _Fix:_ On FileExistsError, retry load_api_key in a short bounded poll loop (e.g. a few attempts with tiny sleep) until a non-empty key appears before giving up and raising; or write to a temp file and os.rename into place so the file is never obse
- [x] **#213** `scripts/webapi/readers.py:303-330` — progress() completion rate/ETA counts every record touched in the window, not just completed builds _(partial: real but overstated)_
  - _Fix:_ Restrict the rate window to terminal/success phases (only count mtimes of records whose phase indicates completion, e.g. 'done'/'built'), not all records.

## Tier 4 — Cleanup (dead-code, redundancy, optimization)

- [ ] **#20** `scripts/arch_filter.py:110-119` — Map build spawns 206 dpkg-architecture subprocesses (~4.4s) on first use, per process
  - _Fix:_ Derive the triplet map by parsing /usr/share/dpkg/tupletable (joined with the already-read cputable) instead of forking dpkg-architecture once per arch; fall back to the subprocess path only if the table file is absent.
- [x] **#24** `scripts/build.py:1125` — Redundant re-import of local_mirror inside cmd_init_remote_container
  - _Fix:_ Delete line 1125; the module-level import already binds the name.
- [ ] **#25** `scripts/build.py:563` — cmd_clean_buildroot bypasses the documented sudo funnel and never scrubs the password
  - _Fix:_ Route cmd_clean_buildroot through _collect_validated_sudo_password (or at minimum scrub _password before return), and update the funnel docstring to reflect the two intentional bypasses.
- [ ] **#28** `scripts/build_closure.py:187-195` — compute_build_closure ignores arch + build-profile restrictions that the cmd_cache resolver honours _(partial: real but overstated)_
  - _Fix:_ Either have local_mirror feed compute_build_closure the already-arch/profile-filtered groups (reuse Source.build_depends output), or extend _pick/_rels to honour rel['arch'] against the target arch and rel['restrictions'] against active pro
- [ ] **#31** `scripts/build_closure.py:106` — Provides set rebuilt per-relation inside the install-closure hot loop
  - _Fix:_ Convert provides_index values to sets once at the top of compute_build_closure/_install_expand (or accept a pre-set-ified index), then intersect directly without per-iteration set() construction.
- [x] **#33** `scripts/buildcontainer.py:142, 174-175` — self.build_path / self.build_profiles / self.build_options stored but never read
  - _Fix:_ Drop the three unused instance attributes (or, if kept for API symmetry, add a comment noting they are intentionally unused); self.repo_path is also only referenced in comments but is harmless to keep.
- [ ] **#38** `scripts/buildsystem.py:83-104` — sudo env-var pickup + prompt + validation duplicated verbatim in __init__ and for_iso
  - _Fix:_ Extract a private staticmethod/helper on BuildSystem (e.g. `_collect_and_validate_sudo()`) and call it from both __init__ and for_iso so the env pickup, prompt, and validation live in one place.
- [x] **#39** `scripts/buildsystem.py:83` — Redundant `import os as _os` shadows the module-level `os` import
  - _Fix:_ Drop the two `import os as _os` lines and call `os.environ.pop('ATHENA_SUDO_PASSWORD', None)` directly.
- [ ] **#41** `scripts/bump.py:381-491, 812-887` — strip_nmu_from_control_text and transpose_control_text duplicate ~50 lines of field-walk/X-field/sibling-idiom logic _(partial: real but overstated)_
  - _Fix:_ Extract the shared scaffold into one helper parameterized by the per-version rewrite callable (e.g. `_rewrite_control_text(content, version_op)`), and have both public functions pass strip_nmu_suffix / partial(transpose, prefix, release).
- [ ] **#44** `scripts/cache.py:158, 161-162` — release_info / pkg_list / src_list initialised but never populated or read
  - _Fix:_ Delete the three unused attribute initialisations (and the corresponding test stub lines) to remove confusion about a cache 'package list' that is never built.
- [x] **#45** `scripts/cache.py:86` — Cache._VALID_CONSTRAINTS class attribute is unused
  - _Fix:_ Remove the unused class attribute (and its comment), or have _lookup_packages reference it if a single source of truth on Cache is wanted.
- [ ] **#46** `scripts/cache.py:854-893, 919-938` — Fork-source-drift audit duplicates the collision-gate comparison
  - _Fix:_ Extract a single helper `_dropped_entries_at_or_above_fork(drops, fork_versions_for(name))` returning the offending tuples, and have both the advisory source audit and the fatal binary/udeb gate call it.
- [ ] **#50** `scripts/chroot.py:486,570-576` — _configure_chroot is only ever called with is_final=True; the is_final=False branch and default are dead
  - _Fix:_ Either drop the is_final parameter and inline the final-pass behavior, or keep it but delete the unreachable else branch; if retained for symmetry add a test that exercises is_final=False.
- [ ] **#51** `scripts/chroot.py:1139-1141` — _get_deb_files triggers a full repo os.listdir per +asg-stamped package on every unpack/retry pass
  - _Fix:_ Build a one-time {pristine_base: path} index of _main (and _doc) at the start of build_chroot and look up stamped variants from it, instead of re-listdir'ing the repo dir for every stamped package on every pass.
- [ ] **#52** `scripts/chroot.py:526-583` — Command construction and 'Setting up' parse duplicated between _configure_chroot and _configure_packages
  - _Fix:_ Extract a shared `_dpkg_configure_argv(named=None, force_deps=False)` builder and a `_parse_setting_up(stdout)` helper used by both methods.
- [ ] **#56** `scripts/commands/cmd_audit.py:785` — audit stale-count includes foreign but preflight excludes it (sibling drift)
  - _Fix:_ Align the two: drop `len(_foreign)` from the audit row's gating _n_stale (keep it in the informational text/`_frn` suffix only), and add `len(_malformed)` so both functions use the same orphan+drift+malformed composition.
- [ ] **#57** `scripts/commands/cmd_audit.py:943-944` — Unreachable final `else` branch in gap classification
  - _Fix:_ Drop the unreachable `else` (or fold branch4 into it as `else: _transitional`) and simplify branch3's condition to `elif _in_up:`.
- [ ] **#58** `scripts/commands/cmd_audit.py:44-48` — Misleading 'tiebreak prefers consumer sorts first' comment in _dedupe_bidirectional_conflicts
  - _Fix:_ Either implement the documented behavior (on collision, replace with the entry whose consumer sorts lexicographically first) or correct the comment/docstring to state it keeps the first-encountered entry.
- [ ] **#62** `scripts/commands/cmd_cache.py:878` — `if self.config.build_mode != 'build'` guard is always true (unreachable in build mode)
  - _Fix:_ Drop the redundant condition (de-indent the closure-guard block) or replace it with an `assert self.config.build_mode != 'build'` to document the invariant without implying a live alternative path.
- [ ] **#66** `scripts/commands/cmd_mirror.py:1745-1801` — Four near-identical coord-fetch + head-verify + keyring-bind + read_all_claims blocks
  - _Fix:_ Extract a helper e.g. `_fetch_and_verify(name, st) -> (head, by_builder, fetched) | None` and call it from all four commands so the FED-03-D keyring binding is applied uniformly.
- [ ] **#67** `scripts/commands/cmd_repo.py:435` — Strip completion message points to removed `repo audit_nmu` command
  - _Fix:_ Change the hint to `Run `repo audit` to confirm zero residue.` (NMU residue is now part of the unified audit).
- [ ] **#70** `scripts/commands/cmd_run.py:116-205` — Boolean-parse block duplicated verbatim across three setters
  - _Fix:_ Extract a `_parse_bool(value) -> Optional[bool]` helper returning None on invalid input and have all three call it, keeping their differing post-set side effects local.
- [ ] **#73** `scripts/commands/cmd_snapshot.py:214-223` — Redundant double GET to snapshot_timestamp_api (latest + candidates) in list/interactive-select
  - _Fix:_ Fetch the result once (or add a helper returning (latest, between)) and derive both `_latest` and the in-range candidates from a single response.
- [ ] **#77** `scripts/commands/cmd_source.py:928-931` — cmd_source_audit recomputes update-pending + workload twice per run
  - _Fix:_ Compute `_update_build_pending()` and the floor workload once in cmd_source_audit and pass the results into `_print_next_run_build_queue` instead of recomputing.
- [ ] **#82** `scripts/commands/cmd_tunnel.py:238-261` — Stale-file wipe calls os.listdir(_dst_dir) once per binary — quadratic for large firmware sources
  - _Fix:_ Snapshot `os.listdir(_dst_dir)` per component dir once before the download loop (or memoize by dir), then filter from the cached list inside the loop.
- [ ] **#84** `scripts/commands/cmd_virtual.py:369` — Redundant `if self.dep_tree is not None` guard
  - _Fix:_ Drop the guard (or merge with the udeb_dep_tree block) since the None case is already short-circuited at the top of the method.
- [ ] **#87** `scripts/coord/head.py:156-206` — is_fresh has zero consumers — staleness/rollback policy is never enforced
  - _Fix:_ Either wire is_fresh into the pull/publish read paths (call it right after read_coord_head with the freshly-fetched InRelease sha + Date), or, if the rollback defense is intentionally deferred, drop is_fresh and its docstring promise so it 
- [ ] **#90** `scripts/coord/head.py:75-83` — homedir-missing branch leaves an orphan stale .sig, diverging from the mirrored repo_audit scrub pattern
  - _Fix:_ In the homedir-missing branch, also unlink the .sig (or factor a single _scrub() helper removing both _path and _sig, matching repo_audit) so failures never leave a dangling signature.
- [ ] **#91** `scripts/coord/identity.py:217-235` — verify_claim_against_keyring is never called; real read-time verification reimplements it inline
  - _Fix:_ Either delete verify_claim_against_keyring (and fix the store.py:14 docstring to reference the inline read_all_claims path), or refactor read_all_claims/read_builder_claims to call it so the single keyring+revoked+verify policy lives in one
- [x] **#93** `scripts/coord/policy.py:41` — TUNNEL_REPUBLISH_OK is an unconsumed constant; its documented 'audit should flag' safety belt does not exist
  - _Fix:_ Remove the constant and its comment, or wire it into the tunnel/republished_from audit path it claims to govern. At minimum stop documenting behavior that no code performs.
- [ ] **#94** `scripts/coord/policy.py:10` — Module docstring references a COORD_HEAD_FRESHNESS knob that is not defined in the file _(partial: real but overstated)_
  - _Fix:_ Update the docstring to name the real knob (COORD_HEAD_MAX_AGE_SECONDS) or drop the stale COORD_HEAD_FRESHNESS line.
- [ ] **#97** `scripts/coord/publish.py:1223` — Redundant nested `pool_remote_spec is not None` checks
  - _Fix:_ Drop the inner `pool_remote_spec is not None` clauses (keep `_codename` on line 1275); they add no guard inside the enclosing block.
- [ ] **#100** `scripts/coord/reconcile.py:104, 211, 386` — audit_local / audit_cross / audit_repo have no production caller
  - _Fix:_ Either wire these three into a `coord audit` command (and fix finding #1 first) or mark them explicitly as staged-for-P3 primitives; correct the stale 'cmd_coord_audit (P1 wired)' docstring claim on line 24.
- [ ] **#105** `scripts/coord/store.py:265-277` — Cross-builder merge + conflict-key branch in project_live_claims is never reached (only ever called single-builder) and is untested
  - _Fix:_ Either drop/guard the cross-builder branch with a comment that no caller passes >1 builder, or add a multi-builder test asserting the conflict-key shape so the documented behavior is pinned before any future multi-builder caller relies on i
- [x] **#109** `scripts/dep_drift.py:13-14, 74-76` — Vestigial mixin type stubs and stale module docstring
  - _Fix:_ Drop the unused `_dir_repo`, `_config`, and `strip_build_version` stubs (or replace with `_dir_repo_main`/`normalize_repo_filename`, which are the real dependencies) and update the docstring lines 13-14 to name the attributes actually used.
- [ ] **#112** `scripts/dependencytree.py:1229-1253` — __getstate__/__setstate__ pickle support is unreachable (resume layer removed)
  - _Fix:_ Either delete __getstate__/__setstate__ and the pickle-safety comments, or add a regression test that round-trips a DependencyTree so the dormant code is exercised and stays correct if revived.
- [ ] **#115** `scripts/diag_installer_status.py:92-93` — Unused lineno parameter in audit_stanza
  - _Fix:_ Drop the lineno parameter from audit_stanza and the call site, or actually use it in issue messages.
- [ ] **#119** `scripts/disk_image.py:815-820` — _convert_to_qcow2 takes an unused password param with a stale chown docstring
  - _Fix:_ Drop the `password` parameter (and its argument at the call site) or, if kept for signature parity, replace the misleading 'see below' chown comment with a note that no privileged step is needed here.
- [ ] **#122** `scripts/disk_image.py:423,453` — _has_bios_modules invoked twice (one os.path.isdir for the spinner label, one for the branch)
  - _Fix:_ Compute `_bios = _has_bios_modules(_mnt)` once before the spinner and reuse it at both sites.
- [ ] **#124** `scripts/fork_mirror.py:804-805` — _SUBSTVAR_RE is recompiled on every binary stanza
  - _Fix:_ Hoist `import re` to the module imports and define _SUBSTVAR_RE = re.compile(r'\$\{[^}]+\}') at module scope.
- [ ] **#125** `scripts/fork_mirror.py:289-339, 358-377, 704-750` — debian/control is independently opened and parsed in three helpers
  - _Fix:_ Factor a single `_parse_control(pkg_dir) -> list[Deb822]` helper and derive binary names / dep-hash / stanzas from it, removing the bespoke startswith parser.
- [ ] **#128** `scripts/identity_scan.py:182` — UnicodeDecodeError except clause is unreachable
  - _Fix:_ Drop UnicodeDecodeError from the except tuple (cosmetic), leaving `except OSError`.
- [ ] **#129** `scripts/installer_chroot.py:477-486` — _allow_path first computation is dead in the only live call path
  - _Fix:_ Drop the lines 477-478 pre-computation and just compute _allow_path = os.path.normpath(os.path.join(installer_dir, 'strip-hooks-allowlist')); keep the basename guard only if a non-'installer' caller is genuinely expected (none exists today)
- [ ] **#131** `scripts/installer_chroot.py:291` — find_matching_artifact triggers an os.listdir per stamped/missing udeb
  - _Fix:_ If profiling shows it matters, build a single dirlist/base-index map once before the loop and look up stamped variants from it; otherwise leave as-is (correctness is fine).
- [ ] **#132** `scripts/iso.py:151-152` — Quote-stripping is already done at config load — inline strip is dead-defensive
  - _Fix:_ Use `_name = cfg.build_distribution` / `_version = cfg.build_version` directly (they are already _strip_quotes'd at load), removing the inline strip; or if defensive stripping is desired, call the shared utils helper rather than re-implemen
- [ ] **#136** `scripts/iso_installer.py:803-806` — _debian_version_cmp re-imports apt_pkg and calls init_system() on every comparison
  - _Fix:_ Import apt_pkg once at module load (or memoize a module-level flag) and call init_system() a single time, then have _debian_version_cmp call version_compare directly.
- [ ] **#137** `scripts/local_mirror.py:97-163` — plan() `config` parameter is never used
  - _Fix:_ Drop the `config` parameter (and update the three call sites + tests), or use it if an arch/suite was intended to come from config.
- [ ] **#138** `scripts/local_mirror.py:355-361` — Final `return f'{_f:.1f}TB'` in human_size is unreachable
  - _Fix:_ Remove line 361 (the post-loop return), or restructure the loop so the final fallthrough is the TB case.
- [ ] **#140** `scripts/mirror.py:1042-1050` — apt_pkg import + init_system() called inside the per-claim collision loop
  - _Fix:_ Hoist `import apt_pkg` and a single `apt_pkg.init_system()` to the top of project_post_publish_state (before the builder/claims loops), matching the pattern already used in audit_closure_ledger.
- [ ] **#141** `scripts/mirror.py:832-839` — read_mirror_state reloads + HMAC-verifies the entire mirror.conf on every call; callers invoke it in O(mirrors) loops
  - _Fix:_ Add a thin helper that calls load_mirror_conf once and iterates `_doc['mirrors']`, and have the loop-based functions (all_mirror_urls, all_mirror_neighbour_records, find_mirror_by_url, add_mirror's duplicate-URL scan) use it instead of per-
- [x] **#146** `scripts/onboarding.py:172, 502` — Redundant local re-imports of already top-level modules
  - _Fix:_ Drop the local re-imports and use the module-level `os` / `utils`.
- [ ] **#151** `scripts/or_resolve.py:121-134` — Pass B re-scans the whole closure and re-parses every package's OR groups on every outer iteration, pulling at most one group per iteration
  - _Fix:_ Cache per-package or_groups once, and either resolve all currently-unsatisfied groups whose chosen alt doesn't change others in a single pass, or track only groups touched by newly-added packages instead of rescanning the full closure each 
- [ ] **#155** `scripts/print_commands.py:1159-1167` — `print provides` can never show multiple providers — always reports 'no contention'
  - _Fix:_ Source the providers from the APT cache's virtual/provides index (e.g. cache.package_hashtable / a provides map listing all candidates for a virtual name) rather than from selected_pkgs, which only retains the single resolved winner. Altern
- [ ] **#156** `scripts/print_commands.py:695-722` — `_print_build_times` and `_summary_build_times_section` re-load and re-parse all build records independently _(partial: real but not reachable)_
  - _Fix:_ Extract a single helper that returns normalized (elapsed, pkg, status, version) rows and have both call sites consume it, so coercion rules and the verified-record read are shared and consistent.
- [ ] **#157** `scripts/release_index.py:96` — Unreachable fallback in _human_size
  - _Fix:_ Drop the dead `return f"{n} B"` line (or keep only if you intend to defend against an empty units tuple, which is not the case here).
- [x] **#161** `scripts/remote_agent.py:50` — _TERMINAL set is defined but never referenced
  - _Fix:_ Either delete `_TERMINAL`, or use it in run_build/elsewhere as the single source of terminal-phase truth (and reuse it for the duplicated literal tuples in callers).
- [ ] **#163** `scripts/remote_build.py:172-175` — Artifact-collection glob duplicated verbatim in remote_agent.py
  - _Fix:_ Expose a shared helper in remote_build.py, e.g. `def collect_outputs(out_dir) -> list[str]` returning the sorted basenames, and call it from both main() and remote_agent.py's post-build block.
- [ ] **#166** `scripts/remote_localmirror.py:98-99` — 416 'already complete' branch is unreachable given upstream guards
  - _Fix:_ Either drop the dead sub-condition or document it as purely defensive; no functional change needed since the fall-through still returns a sane failure.
- [x] **#170** `scripts/repo_audit.py:881, 1043, 1173` — PkgRelation re-imported locally three times despite module-level import
  - _Fix:_ Delete the three function-local 'from debian.deb822 import PkgRelation' lines; the module-level import already covers them.
- [ ] **#175** `scripts/sbom.py:167-172` — _strip_quotes re-applied to values BuildConfig already quote-strips at load
  - _Fix:_ Drop the redundant _strip_quotes wrapping here (use str()), or add a comment that it is defensive belt-and-suspenders; keep one canonical stripping site.
- [ ] **#178** `scripts/select_packages.py:123-125, 350` — Redundant re-parsing/re-flattening on the render hot path
  - _Fix:_ Cache the flattened rows and invalidate on model mutation (toggle/add/drop); pass the entry's selected flag into _Row so _format_pkg_row need not re-scan; share one file read between the two parse helpers.
- [x] **#182** `scripts/signing.py:114-116` — Unreachable `if not parts: continue` guard in parse_secret_keys_colons
  - _Fix:_ Remove the dead `if not parts: continue` lines (cosmetic; no behavior change).
- [ ] **#183** `scripts/surfaces.py:98-120` — Extras fixpoint rescans the entire closure every round instead of only newly added nodes
  - _Fix:_ Track a `_frontier` of newly added nodes (the union of `_wanted` and the hard-deps added in the inner `while _stack` drain) and scan only `_frontier`'s recommends in the next round, instead of `for _n in _closure`. This is build-time, low-f
- [ ] **#187** `scripts/tui/dispatcher.py:150-155` — IDLE_TIMEOUT / WIDGET_IDLE_TIMEOUT values never affect the actual loop wait _(partial: real but overstated)_
  - _Fix:_ Either drop/relabel IDLE_TIMEOUT & WIDGET_IDLE_TIMEOUT (they no longer set the wait) or remove the INPUT_POLL_MS clamp if longer idle sleeps were intended; fix the '10 Hz' comment to 20 Hz.
- [ ] **#188** `scripts/tui/events.py:173-178` — ALL_EVENT_TYPES is unused (no test or discovery consumer)
  - _Fix:_ Either add a test asserting ALL_EVENT_TYPES matches the dispatcher._handle isinstance chain (catching a forgotten handler when a new event is added), or drop the list if discovery is not actually needed.
- [ ] **#190** `scripts/tui/logging_bridge.py:24-29` — _tab_for_logger ignores its `name` argument (always returns 'log')
  - _Fix:_ Either keep the parameter as an intentional interface stub with a comment, or drop the param and inline a 'log' constant; update the stale routing language in the pinning test's docstring so it does not imply _tab_for_logger differentiates 
- [x] **#195** `scripts/tui/tui.py:251` — Redundant local `import curses` inside attr_reverse
  - _Fix:_ Drop the local `import curses` on line 251 and reference the module-level import.
- [ ] **#199** `scripts/utils.py:3943,4010` — parse_pkg_list_groups + parse_pkg_list_group_meta re-read the same file and recompute section detection
  - _Fix:_ Either parse once into a combined (groups, meta) structure shared by both call sites, or have the meta parser accept already-read lines, so a single read/scan serves both.
- [ ] **#200** `scripts/utils.py:1451-1457` — lifecycle_touch_selected create-path reads the build record twice
  - _Fix:_ For the create branch, construct + write via new_build_record/write_build_record directly (or have upsert accept an already-loaded record), avoiding the second read_build_record round-trip.
- [ ] **#201** `scripts/virtual_build.py:855,877-886,898` — `was_patched`/`_at_build_delta` in validate are dead — always overridden by `override_patch_level`
  - _Fix:_ Drop `_was_patched`, the `_at_build_delta` accumulation, and the `was_patched=` argument; if validate should match buildcontainer's flooring, pass `override_patch_level=max(_rec_patch_level, 1 if _was_patched else 0)` instead.
- [ ] **#202** `scripts/virtual_build.py:482,505,523-525` — `asg_ledger` parameter is fully vestigial and forces a wasted disk read at the call site
  - _Fix:_ Remove the `asg_ledger` parameter from both functions and delete the now-unnecessary published_ledger() call in cmd_virtual.py.
- [ ] **#203** `scripts/virtual_build.py:265-267,1296-1303` — Each dep-relation field is PkgRelation-parsed/serialized up to three times in the full-repo audit
  - _Fix:_ Merge _transpose_relation and _rewrite_sibling_pins into a single parse→mutate→serialize pass (the global pass must stay separate since it runs after dedup).
- [x] **#204** `scripts/virtual_build.py:917-933` — `_strip_asg` applied on top of `pristine_base` is a no-op
  - _Fix:_ Drop the redundant `_strip_asg(...)` wrapper in _filename_signature; pristine_base already removes the asg layer.
- [ ] **#209** `scripts/webapi/jobs.py:159-164` — except PromptRequired branch is unreachable; structured prompt_required contract is not delivered _(partial: real but overstated)_
  - _Fix:_ Either re-raise PromptRequired out of _dispatch_one's generic handler (special-case it in cli.py before `except Exception`), or drop the dead except branch here and instead detect the prompt-required condition from the captured ERROR line /
- [ ] **#212** `scripts/webapi/jobs.py:79-81` — known_command re-parses the command already split/validated at the route
  - _Fix:_ Have the route pass the already-extracted first token, or expose known_command(first_token) taking the parsed verb, to avoid the second split.

## Tier 5 — Test coverage

Version-scheme coverage gaps (verified):

- [x] **#15** `tests/test_module.py` — Tunnel keep-+bN (missing)
- [x] **#16** `tests/test_module.py` — Fork changelog-verbatim no-op (partial)
- [x] **#17** `tests/test_module.py` — Sibling '=' pin rewrite carrying +bN (missing)

Missing-test findings (medium/low):

- [ ] **#19** `scripts/apt_repo.py:306` — No test exercises an empty-but-present debian-installer/ udeb dir in generate_repo_indexes
  - _Fix:_ Add a case: create dists/<suite>/main/debian-installer/binary-amd64/ with no .udeb, have the scan mock emit empty stdout for udeb argv, and assert generate_repo_indexes still returns True (or skips cleanly) rather than failing.
- [ ] **#22** `scripts/arch_filter.py:152-162` — No test for the populated-map-but-no-arch-table degrade branch
  - _Fix:_ Add a test that patches _arch_table to return None while leaving _maps populated and asserts a known-foreign name (e.g. binutils-aarch64-linux-gnu) returns False (KEEP), documenting the degrade contract.
- [ ] **#29** `scripts/build_closure.py:77-79, 105-108` — Provides-based resolution path has zero test coverage
  - _Fix:_ Add a compute_build_closure test with a non-empty provides_index where (a) a direct build-dep is a pure virtual satisfied only via a provider, and (b) a transitive Depends is already satisfied by a provider already in the set, asserting bot
- [ ] **#30** `scripts/build_closure.py:145-149, 153` — classify_tiers transit-through-non-member and toolchain/language overlap untested
  - _Fix:_ Add an adjacency where a toolchain seed reaches a member only through a non-member intermediary, and a member reachable from both seed sets, asserting it lands in toolchain not language.
- [ ] **#32** `scripts/buildcontainer.py:24422-24474` — Rollback test does not cover the dup-then-failure case that triggers the data-loss bug
  - _Fix:_ Add a test: pre-publish file A in dest, place rebuilt-dup A plus a second file B in scratch where B's dest forces an OSError; assert the published A still exists in its dest dir after the call (and is not relocated into scratch).
- [ ] **#35** `scripts/buildlog.py:128-143` — No test exercises write() under a non-UTF-8 locale despite non-ASCII payload
  - _Fix:_ Add a test that monkeypatches locale.getpreferredencoding to return 'ascii' (or opens via a forced ascii encoder) and asserts the file with '→'/'…' content is still written and readable.
- [ ] **#42** `scripts/bump_version.py:169-184` — No test covers the --freeze-stamp commit path
  - _Fix:_ Add a test that runs main(['patch','--freeze-stamp']) against a temp git repo with scripts/_buildstamp.py gitignored and asserts the commit succeeds.
- [ ] **#54** `scripts/cli.py:417-421` — command_gate refusal path has no test coverage
  - _Fix:_ Add a test: construct a Cli via object.__new__, register a fake command, set command_gate=lambda c: c=='configure', assert dispatching the fake command returns False + emits the 'configure first' ERROR, while 'help' still runs and 'quit' st
- [ ] **#63** `scripts/commands/cmd_cache.py:271` — No test covers highest-version pick across multiple cached versions in build mode
  - _Fix:_ Add a build-mode test that puts two versions of one package in the offline cache (e.g. 1.9-1 and 1.10-1) and asserts selected_pkgs holds the Debian-higher 1.10-1, locking in Version-object ordering against an accidental string-max regressio
- [ ] **#65** `scripts/commands/cmd_mirror.py:36436` — Reclaim test asserts the buggy publish args, locking in the ISO-gate regression
  - _Fix:_ Once line 2202 forwards `--no-iso`, update the assertion to `assert _a == ('m1', '--no-iso')` and add a case asserting reclaim still succeeds when _release_iso_descriptors reports missing ISOs.
- [ ] **#74** `scripts/commands/cmd_snapshot.py:466-507` — `_cmd_snapshot_workload` has no test coverage
  - _Fix:_ Add a test stubbing `_workload_current_to_target`/`_preflight_stamp_invariant` to cover: dep_check_ready=False early-out, current==target short-circuit, malformed target rejection, and the names+guard rendering.
- [ ] **#79** `scripts/commands/cmd_supply_chain.py:226-270` — grype JSON-render/severity-summary path has no test
  - _Fix:_ Factor lines 226-270 into a pure helper taking the parsed grype doc and add a unit test feeding a synthetic doc (one match with `"artifact": null`, one with missing `fix`) — testable without a grype binary.
- [ ] **#81** `scripts/commands/cmd_tunnel.py:159-473` — No behavioral test exercises _do_tunnel; only a getsource string assertion
  - _Fix:_ Add a test that constructs a fake src_pkg + a tmp pool with a real dpkg-deb-built .deb, runs _do_tunnel, and asserts: transposed on-disk filename, republished_from provenance keyed by final name, and build-record outputs == on-disk names.
- [ ] **#85** `scripts/commands/cohorts.py:25325-25383` — No test covers multi-version source_hashtable selection in _tunnel_filenames_for_source
  - _Fix:_ Add a test with `source_hashtable = {'x': [older_src, newer_src]}` (parse order older-first) where newer_src declares an extra/renamed binary, asserting the predicted filename set matches the selected/highest-version source, not _cands[0].
- [ ] **#101** `scripts/coord/reconcile.py:139-194` — No test covers audit_local/audit_cross with a multi-binary source
  - _Fix:_ Add an audit_local test with two published claims (same package+built_version, distinct filenames, both present in pool with matching hashes) and assert BOTH are hash-verified and neither is reported as unclaimed_pool_file.
- [ ] **#103** `scripts/coord/schema.py:597-617` — canonicalize_neighbour_records dedup is order-dependent first-wins; test claims (untested) dict-overrides-str
  - _Fix:_ Either (a) add a test feeding ['ssh://b/p', {'url':'ssh://b/p','public_url':'https://b'}] and assert the intended winner, then make dedup prefer the record with non-empty public_url/public_proto on collision; or (b) correct the test docstri
- [ ] **#110** `scripts/dep_drift.py:219-223` — Version-sync (the 144-spurious-mismatch fix) has no test
  - _Fix:_ Add a test that runs `_check_dep_drift` over a canonical pkg whose cache Version is unstripped (e.g. 0.8-10+deb12u1) and disk Version stripped (0.8-10), then asserts `_pkg_obj.version`/`_pkg_obj['Version']` equal the disk value.
- [ ] **#118** `scripts/diag_installer_status.py:45-89` — Core parser parse_stanzas has no test
  - _Fix:_ Add a small tests fixture exercising: blank-line separation, leading-continuation-with-no-field (line 74), bad non-header line (line 83), and trailing stanza with no terminating newline (line 87).
- [ ] **#135** `scripts/iso_installer.py:5563-5582 (tests/test_module.py)` — scripts/iso_installer.py:5563-5582 (tests/test_module.py) — No test covers exclude_names combined with deb_whitelist=None
  - _Fix:_ Add a test calling _select_pool_files([repo], deb_whitelist=None, exclude_names={'apt-setup-udeb'}) and assert the excluded binary is dropped (or, if bypass is intended, assert+document that it is kept).
- [ ] **#147** `scripts/onboarding.py:188-191` — No test covers jobs out-of-range / non-numeric clamp behavior
  - _Fix:_ Add a wizard test that scripts a jobs answer of e.g. '16' and 'abc' and asserts the persisted MaxParallelBuilds is 8 / default respectively.
- [ ] **#152** `scripts/or_resolve.py:37649-37673` — No test exercises multi-group / non-minimal OR interaction or generator seeds _(partial: real but overstated)_
  - _Fix:_ Add a test with deps {'p':[('a','b')],'q':[('b','c')],...} asserting the documented invariant, and a test calling resolve_closure((s for s in ['xorg']), ...) to pin generator behavior.
- [ ] **#153** `scripts/package.py:385-446` — add_constraint conflict-resolution matrix and VersionConstraint have no real unit test
  - _Fix:_ Add a parametrized test over representative (new,old) operator pairs asserting the resulting stored constraint (nc/xg/eq/err semantics), plus a `constraints_satisfied` test covering the Provides-version satisfaction path (lines 364-382).
- [ ] **#158** `scripts/release_index.py:127-130` — Empty-ISO HTML branch untested
  - _Fix:_ Add an assertion that render_index_html(manifest with isos=[]) contains the 'No ISO images published' placeholder and no <table>.
- [ ] **#162** `scripts/remote_agent.py:106-124` — No test covers read_log under concurrent file growth (the offset bug above)
  - _Fix:_ Add a case that writes N bytes, monkeypatches/forces a larger read window, and asserts the returned next-offset equals frm + len(returned_bytes) (no overlap on the subsequent poll).
- [ ] **#164** `scripts/remote_localmirror.py:225-257` — Range-resume / 200-restart byte accounting is untested
  - _Fix:_ Add a test that pre-writes a truncated partial of one entry (via a file:// or mocked urlopen returning 206), asserts the download completes and result counts are correct; and a second mocked case where the server returns 200 to a Range requ
- [ ] **#167** `scripts/remote_orchestrate.py:463-472` — Partial scp-down recovery (exit 12) and SIGINT abort (130) paths are untested
  - _Fix:_ Add a test where /status reports outputs=['a.deb','b.deb'] but the out/*.deb scp materializes only one file, asserting (12, [...]) ; and a test driving _AgentHandle.terminate()/abort to assert (130, []).
- [ ] **#169** `scripts/repo_audit.py:900-959` — No test covers audit_nmu_residue on an anchored +asg<R>uK+bN built package _(partial: real but not reachable)_
  - _Fix:_ Add a RepoState case with a built pkg at '1.2.3-4+asg1u0+b1' and '1.2.3-4+asg1u3+p1+b1' (no tunnel set) and assert audit_nmu_residue returns no finding for it, plus a sibling Depends '(= 1.2.3-4+asg1u3+b1)' likewise clean, while a real '...
- [ ] **#185** `scripts/tasksel_desc.py:55` — No test covers meta value being None or a group present in groups but absent from meta with empty seeds _(partial: real but not reachable)_
  - _Fix:_ Add a case with `_meta={'gnome-desktop': None}` asserting it falls back to the title without raising, locking in the `or {}` guard.
- [ ] **#192** `scripts/tui/state.py:54-97, 161-187` — TabState eviction/scroll bookkeeping and CmdLine history have no test coverage _(partial: real but overstated)_
  - _Fix:_ Add unit tests: (a) append past MAX_BUFFER_LINES with scroll_offset>0 asserting offset decremented by evicted display rows and clamped at 0; (b) history Up/Up/Down/Down sequence asserting hist_draft is stashed on first Up and restored exact
- [ ] **#206** `scripts/webapi/__init__.py:206-227` — SSE /jobs/{job_id}/stream endpoint has no test
  - _Fix:_ Add a TestClient test that submits a job whose handler emits several lines then finishes, opens the stream, and asserts every emitted line plus the trailing `event: end` are received (covers both ordering and the end sentinel).
- [ ] **#216** `scripts/webapi/readers.py:206-254, 372-389` — mirror_state(), repo_summary(), phase_counts() and read_flags() have no direct tests
  - _Fix:_ Add tests covering mirror_state against a mirror.conf-backed registry (including a mirror with no legacy .state file), plus repo_summary/phase_counts/read_flags happy + empty-dir paths.

## Appendix — Closed (refuted or corrected to severity none)

These were closed on GitHub with the `refuted` label and the verifier's reasoning.

- [x] **#49** `scripts/chroot.py:763-769` — _compute_install_batches install_set surface-filter branch has no direct test
  - _Fix:_ Add a test passing install_set explicitly (e.g. a graph where install_set excludes some selected packages) asserting only install_set∩canonical−seed members appear in the batches and that legacy _extras/_group_extras/_pool_extras are NOT re
- [x] **#60** `scripts/commands/cmd_build.py:1048` — check 2 counts held/deinstall-marked packages as 'incomplete' (endswith '\tinstall')
  - _Fix:_ Only flag genuinely-incomplete states: e.g. keep packages whose status is not in {install, hold} (held == installed), or parse the second column explicitly via `l.split('\t')` and compare the state token rather than using endswith.
- [x] **#89** `scripts/coord/head.py:188-195` — is_fresh expects ISO-8601 for inrelease_date_iso, but the only InRelease Date producer yields RFC2822 — predates check silently disabled
  - _Fix:_ Parse the InRelease Date with email.utils.parsedate_to_datetime (RFC2822) before passing in, or have is_fresh accept the raw Date header and parse RFC2822 itself; on a parse failure treat it as a hard fail rather than silently skipping the 
- [x] **#102** `scripts/coord/reconcile.py:180-194` — audit_local emits one INFO Finding per unclaimed pool file (unbounded)
  - _Fix:_ Aggregate unclaimed pool files into a single count-bearing INFO Finding (or cap the per-file emission), rather than one Finding per file.
- [x] **#113** `scripts/dependencytree.py:376-380` — resolve_packages re-resolves raw (unstripped) seed names, unlike add_lookahead
  - _Fix:_ Filter/strip the seed list once at the top of resolve_packages (mirror add_lookahead's `if not name or name.isspace(): continue` + strip) before both add_lookahead and the parse_dependency comprehension consume it.
- [x] **#123** `scripts/fork_mirror.py:667` — No test covers a fork with an empty/malformed changelog
  - _Fix:_ Add a test that drops a fork tree with debian/control present and an empty (0-byte) debian/changelog, then asserts generate_fork_mirror logs+skips that pkg and does not raise.
- [x] **#134** `scripts/iso_installer.py:883-889` — udebs are never version-deduplicated, unlike .debs _(partial: real but overstated)_
  - _Fix:_ If pool size or stale-udeb selection matters, run udebs through the same highest-version-per-name dedup as .debs; otherwise document explicitly that multi-version udeb shipping is intentional and relies on apt picking the highest.
- [x] **#142** `scripts/mirror.py:1049` — Inconsistent exception scope around apt_pkg.version_compare vs the sibling site _(partial: real but not reachable)_
  - _Fix:_ Pick one convention for both sites; given the inputs are controlled Debian versions, a narrow `except (SystemError, TypeError, ValueError)` (or a shared helper) applied to both keeps them aligned.
- [x] **#150** `scripts/or_resolve.py:55-61, 133-134` — Empty OR group is never satisfiable and crashes with IndexError
  - _Fix:_ Skip empty groups in _or_groups: `if not isinstance(_d, str) and len(_d): _out.append(tuple(_d))`.
- [x] **#154** `scripts/package.py:184` — Priority field not guarded against present-but-None, unlike every other field _(partial: real but not reachable)_
  - _Fix:_ Mirror the defensive pattern: `if (self.get('Priority') or '').strip(): self.priority = self['Priority'].strip() else: self.priority = 'optional'`.
- [x] **#174** `scripts/sbom.py:135-221` — generate_cdx has no functional test — only a structural source-grep assertion
  - _Fix:_ Add a unit test that builds two stub Source objects (one in both trees to exercise setdefault dedup, one udeb-only) plus a stub BuildConfig, calls generate_cdx to a temp path, and asserts bomFormat/specVersion/component count/PURL/patch pro
- [x] **#193** `scripts/tui/state.py:224-231` — active_tab_name() mutates state (selects a tab) without setting dirty _(partial: real but not reachable)_
  - _Fix:_ Set self.dirty = True in the recovery branch before returning, or move recovery into a clearly mutating helper and keep active_tab_name() read-only.
- [x] **#197** `scripts/tui/widgets.py:178-183` — Spinner.__str__ mutates animation state (impure render) _(partial: real but not reachable)_
  - _Fix:_ Drive frame advancement from an explicit tick()/advance() called once per dispatcher wake, keeping __str__ pure.
- [x] **#198** `scripts/utils.py:1176,1232` — read_build_history collapses multiple failed runs of one package via read-time ts fallback
  - _Fix:_ Use a stable per-record timestamp as the fallback instead of read-time now, e.g. ts = record.get('finished') or record.get('started') or _utc_now_iso(); and/or widen the dedup key to include record.get('started')/exit_code so distinct runs 
- [x] **#211** `scripts/webapi/jobs.py:137-169` — No test coverage for the job loop, drain-then-stop, or prompt handling
  - _Fix:_ Add a unit test that submits a command whose handler calls self.prompt(), runs one wait() iteration with a sentinel, and asserts the resulting job.state/error — which would immediately expose the unreachable branch.
