# Version numbering

Every package we ship carries a version string, and that string quietly does
two important jobs. It tells the package manager whether one build is newer
than another (so upgrades flow in the right direction), and it decides which of
two packages with the same name wins when both are available. Get it wrong and
upgrades stall, security fixes don't land, or the wrong build installs.

Getting it *stable* matters even more than getting it right once. The moment a
version is published to real machines, the rules that produced it are frozen:
changing them later would re-order releases that already shipped. So this
document is written to be exact.

It comes in three parts. The first explains the idea and the reasoning behind
it. The second is the precise reference — what version each kind of package
gets, and every edge case that the rules have to handle. The third is a short
set of recipes for the situations that come up in practice.

The whole scheme rests on one sentence:

> We rebuild every package from its original source, so we keep the original
> version almost unchanged — we only rename the one marker that means
> "this is a post-release update", swapping the upstream project's marker for
> our own.

---

## Part 1 — Understanding the scheme

### How a version string is built

A typical version looks like `2:1.2.3-4`. It has up to three pieces:

- the **upstream version** (`1.2.3`) — the software's own version, chosen by
  the people who wrote it;
- the **revision** (`-4`) — how many times the package was re-packaged around
  that same upstream version, without the upstream code changing;
- an optional **epoch** (`2:`) — a rarely-used override that forces ordering.
  It exists for when a version scheme changes in a way that would otherwise
  sort *backwards* (for example, software that moves from a date like
  `20240101` to a tidy `1.0`, which compares as *older*). An epoch says "trust
  me, this is newer." Once added it can never be removed, so it is a last
  resort.

On top of that, the upstream packaging adds build-time markers that the
original maintainers control and we do not:

- `+debNuM` — a **stable update**: a security fix or bug-fix re-issued after
  the release shipped. `N` is the release number it targets and `M` counts the
  updates, so `+deb12u3` is "the third update for release 12".
- `+bN` — a **binary rebuild**: the package was rebuilt with *no source change
  at all*, usually because a library it links against changed elsewhere.
- `~bpoN+M` — a **backport**: the same source recompiled for an *older*
  release.

Each of these markers exists because the original distribution is
*re-distributing its own binaries* and has to record what it changed and why.

### Why we change the version at all

We are not re-distributing someone else's binaries — we **rebuild every
package from source**. That changes what the markers mean for us:

- A **stable update** (`+debNuM`) reflects a real change to the source: the
  maintainers added patches after release. When we rebuild that source we are
  genuinely shipping those changes, so we keep a marker of our own.
- A **binary rebuild** (`+bN`) reflects *no* source change. We already rebuild
  everything against our own libraries, so it simply doesn't apply to us — we
  drop it and ship the plain version.
- A **backport** (`~bpoN+M`) targets a different release than the one we build,
  so it's not relevant either — we drop it.
- A **new revision** (`-4` becoming `-5`) is part of the version itself, not a
  strippable marker; we build it and ship it as the version it is.

So the only marker that needs translating is the stable-update one. Everything
else is either part of the real version (keep it) or a re-distribution artifact
that doesn't apply to us (drop it).

### The core move: rename the update marker in place

When a package carries a trailing stable-update marker, we rewrite *only that
marker* into our own equivalent and leave the rest of the version exactly as it
was. Our marker is `+<tag><release>u<K>`:

- **tag** — a short label derived from the distribution's name (Asgard gives
  `asg`). Rename the distribution and this label changes with it.
- **release** — the distribution's major release number (`1`).
- **K** — the update number, taken *unchanged* from the upstream marker.

So a faithful rebuild of `1.2.3-4+deb12u3` ships as `1.2.3-4+asg1u3`. The base
version, the revision, and the update number `3` all survive untouched; only
`+deb12u` became `+asg1u`.

A package with no stable-update marker is already pristine, and a faithful
rebuild of it ships exactly as it is — `1.2.3-4` stays `1.2.3-4`, with no marker
added at all.

### Why keep the update number instead of counting our own

The update number is the heart of the scheme, and keeping it from upstream
(rather than counting "our Nth build") buys two things.

**Order follows content.** Because `+asg1u2` is built from update 2 and
`+asg1u3` from update 3, the version order matches the *content* order. Update
2 always sorts below update 3, no matter which we built first. That is what
makes a genuine downgrade expressible: shipping the older content produces a
lower version, so the package manager can see it as a real, lower target — not
just a different opaque string.

**No bookkeeping.** The version of a faithful rebuild can be worked out from
the upstream version alone. Nothing has to remember "how many times have we
shipped this" — there is no counter to keep in sync, and a fresh machine
computes the same answer as an old one. The only thing we *do* have to remember
is whether we added changes of our own, which the next section covers.

---

## Part 2 — Reference: the exact rules

### The four kinds of package

Every package falls into one of four kinds, and each gets its version a
slightly different way.

| Kind | What it is | Version it ships at | Example |
|---|---|---|---|
| **Rebuilt, faithful** | rebuilt from upstream, unchanged by us | upstream version with the update marker translated (or pristine) | `1.2.3-4+deb12u3` → `1.2.3-4+asg1u3` |
| **Rebuilt, patched** | rebuilt from upstream with our own source changes | as above, plus our patch level | `1.2.3-4+deb12u3` → `1.2.3-4+asg1u3+p1` |
| **Fork** | a package we have taken ownership of | hand-set in the package's own changelog, ending in `+athenaN` | `12.4+athena2` |
| **Tunnelled** | shipped as upstream's own signed binary, not rebuilt | upstream version with the update marker translated; the binary itself untouched | `3.x~deb12u1` → `3.x~asg1u1` |

The rest of this part is the detail behind those rows.

### Translating the update marker (the precise rule)

The translation applies to **one** marker only: a stable-update marker at the
very **end** of the version. It keeps the marker's leading sign and its update
number, and changes only the distribution label:

- `+debNuK` at the end → `+<tag><release>uK`
- `~debNuK` at the end → `~<tag><release>uK`

Everything before that trailing marker is left exactly as it is. In particular:

- **An epoch is always preserved.** `7:5.1.9-0+deb12u1` becomes
  `7:5.1.9-0+asg1u1`.
- **An *embedded* update marker is not touched.** Some packages carry a stable
  marker in the *middle* of their version, as part of the upstream identity
  rather than as a trailing update. For example a signed boot component whose
  upstream version is `1.44~1+deb12u1+15.8-1~deb12u1` has a `+deb12u1` buried in
  the middle and a `~deb12u1` at the end. Only the trailing one is an update
  marker, so only it is translated: the result is
  `1.44~1+deb12u1+15.8-1~asg1u1`. The embedded `+deb12u1` stays, because it is
  part of what the package actually is.
- **A backport of an update stacks both markers — only the trailing one
  translates.** When a newer release's update is backported, the version reads
  like `6.16-1+deb13u1~deb12u1`: the embedded `+deb13u1` names the content
  (release 13's first update), the trailing `~deb12u1` is the update marker for
  the release actually being served. Only the trailing marker translates:
  `6.16-1+deb13u1~asg1u1`. (Anything that reduces such a version back to its
  base for comparison must strip *both* layers — the machinery does, via
  `pristine_base`.)
- **Real version detail is kept verbatim.** Markers that look similar but
  describe genuine source identity — a non-maintainer source change, a dotted
  revision like `-3.3`, an upstream-supplied `+really…`, a `+dfsg` repackaging
  tag — are all part of the version and are never stripped or translated.

### The two sign forms: above and below pristine

The leading sign of the marker is meaningful, and it is preserved on purpose.

- A `+deb` update sorts **above** the plain version, so its translation
  `+asg…` also sorts above the plain version. `1.2.3-4 < 1.2.3-4+asg1u3`.
- A `~deb` update is the upstream project's way of sorting a build **below**
  the plain version (the `~` sign sorts before everything). Its translation
  `~asg…` keeps that: `2.4.67-1~asg1u2 < 2.4.67-1`. This is faithful to what
  upstream intended, and successive updates still order correctly among
  themselves (`~asg1u2 < ~asg1u3`).

### Our own changes: the patch level

When we apply our own source changes on top of upstream — a real edit to the
code, not just a re-packaging — the result is genuinely different from
upstream, so it earns a patch level: `+pP`, where `P` counts our patch
revisions.

The patch level always sits **inside** the update marker, so that it sorts
above the un-patched build but **below the next upstream update**:

| Situation | Version |
|---|---|
| faithful update | `1.2.3-4+asg1u3` |
| our patch on that update | `1.2.3-4+asg1u3+p1` |
| our second patch revision on it | `1.2.3-4+asg1u3+p2` |
| **our patch on a *pristine* base** | `1.2.3-4+asg1u0+p1` |

That last row is the case to watch. When upstream has *no* update marker, there
is nothing to anchor the patch to, so an update marker with update number `0`
is synthesized first — `u0` simply means "no upstream update, our change number
1". This is not cosmetic: without the anchor, the patch suffix would sort
*above* every real update (a quirk of how the marker letters compare), and the
patched build would never be superseded by a later upstream fix or by a rebase
of our own patch. With the `u0` anchor it sorts exactly where it should:

```
1.2.3-4  <  1.2.3-4+asg1u0+p1  <  1.2.3-4+asg1u1  <  1.2.3-4+asg1u1+p1
```

The patch level is the same for every binary produced from one source, so the
cross-references between those binaries stay consistent.

How the patch level advances over time:

- the first patch on a given base is `+p1`;
- editing the patch while still on the same base advances it (`+p2`, `+p3`, …);
- removing our patch entirely drops back to a faithful build (no `+p`);
- moving to a *new* base — a new upstream version — resets the count, because
  the patch is being re-applied onto fresh ground.

### Binary rebuilds, and forcing one ourselves

A binary rebuild marker (`+bN`) never appears on something we build from
source: we always compile from the source, and the source version doesn't carry
it. So our rebuilt packages never carry `+bN` of upstream's.

Occasionally we need to *deliberately* re-issue a package with no source change
— for instance to rebuild it against a changed library — and mark that as a
distinct build. That is a forced rebuild, and it adds **our own** `+bN`. Like
the patch level, it is anchored inside the update marker so it sorts below the
next upstream update (a forced rebuild of a pristine base is
`1.2.3-4+asg1u0+b1`).

### Dependencies

A version is only half the story; the version *constraints* a package places on
its dependencies have to stay consistent with it.

Every version constraint is translated the **same way** the package versions
are. If a package depends on `library (>= 1.2.3-4+deb12u3)`, that becomes
`library (>= 1.2.3-4+asg1u3)` — which our rebuilt `library` at
`1.2.3-4+asg1u3` satisfies exactly. Translating both sides identically is what
keeps the dependency graph resolvable; it also preserves the *intent* of the
constraint (it still asks for "at least update 3"), which simply dropping the
marker would have thrown away. (A floor written with a trailing `~` —
`>= …+deb12u1~` — keeps its `~` through the translation, so it still sorts
just below the version it guards.)

Constraint bounds get **one extra step** that the package's own version does
not. A bound references *another* package's version, and upstream control
files routinely pin the version a buildd happened to rebuild —
`library (= 1.5-3+b2)` — or a backported one (`~bpoN+M`). Those layers
describe upstream's *binary rebuilds*, and no such version can ever exist in
our repo: we rebuild everything from source, so our `library` ships at plain
`1.5-3`. A bound still carrying the layer would be unsatisfiable forever. So
before the update marker is translated, a trailing `+bN` / `~bpoN+M` is
stripped from **every** constraint bound — `(= 1.5-3+b2)` becomes `(= 1.5-3)`,
which our rebuilt `library` matches exactly. This applies to all operators,
`=` pins included: an exact pin on another source's package must land on the
version that source actually ships at after *its* markers are dropped.

Three guards keep that strip precise:

- a bound that already carries **our own** marker chain (`…+asg1u3+b1`) is one
  of *our* forced rebuilds, not upstream's buildd artifact — it is left
  untouched, so re-running the translation can never eat a legitimate pin;
- a bound whose **target is a tunnelled binary** is exempt from the strip
  (translated only): the tunnelled package ships its upstream `+bN` version
  *verbatim*, so the bound must keep referencing it.  This is what keeps a
  tunnelled source's frozen sibling pins (firmware's
  `firmware-linux (= …+b1)` chain) resolvable when upstream issues a binary
  rebuild of it.  The build passes the tunnelled binary names into the
  rewrite (`keep_binnmu_names`);
- the strip applies to constraint *bounds* only, never to a package's own
  version field — a tunnelled package keeps its upstream `+bN` identity (see
  *tunnelling*, Part 3).

This closed a real, measured gap: across the full 66,252-binary upstream
universe, stripping the layer from bounds repaired 3,193 previously
unresolvable dependencies — almost all `-dev` packages pinning their library
at `(= X+bN)` — and broke none.

Where one binary pins an *exact* version of a sibling built from the same
source, that pin is updated to the sibling's exact final version — patch level
and all — so the two always match on disk.

One family of packages encodes its sibling pins in a form the translation
alone cannot satisfy — a "same upstream, any revision" bracket that stops
holding once the re-distribution markers are stripped.  Appendix B covers
that idiom, the rewrite that handles it, and the analysis behind it.

### Putting it together

For a rebuilt or tunnelled package, the version is built up in this order:

1. start from the upstream version;
2. translate a trailing update marker (`+debNuK` → `+asg<release>uK`), or leave
   the version pristine if there is none;
3. if we patched it, anchor and append the patch level (`+p<P>`, with a
   synthesized `u0` when the base is pristine);
4. if it is a forced rebuild, anchor and append `+b<N>`.

Worked examples:

| Upstream version | Did we patch? | Ships as |
|---|---|---|
| `0.7.4-20` | no | `0.7.4-20` |
| `1.2.3-4+deb12u3` | no | `1.2.3-4+asg1u3` |
| `1.2.3-4+deb12u4` | no | `1.2.3-4+asg1u4` |
| `1.2.3-4+deb12u2` | no | `1.2.3-4+asg1u2` (a real, lower downgrade target) |
| `2.4.67-1~deb12u2` | no | `2.4.67-1~asg1u2` (below pristine) |
| `7:5.1.9-0+deb12u1` | no | `7:5.1.9-0+asg1u1` (epoch kept) |
| `1.2.3-4` | yes | `1.2.3-4+asg1u0+p1` |
| `1.2.3-4+deb12u3` | yes | `1.2.3-4+asg1u3+p1` |

And the resulting order, smallest to largest:

```
1.2.3-4
1.2.3-4+asg1u0+p1
1.2.3-4+asg1u2
1.2.3-4+asg1u3
1.2.3-4+asg1u3+p1
1.2.3-4+asg1u4
```

---

## Part 3 — How to handle the cases that come up

### Maintain a fork

A fork is a package we have taken ownership of and will keep changing — usually
for branding, or because we ship a permanently different version of it. Its
changes live in its own source tree rather than as patches on top of upstream.

A fork's version is **set by hand in its own changelog**, ending in a fork
marker `+athenaN`, with `N` bumped each time the fork changes:

```
base-files (12.4+athena1) ...
base-files (12.4+athena2)   ← next fork revision
```

The fork marker sorts above the rebuilt-from-upstream marker, which is what
makes the fork win when both could exist. A fork is also the *only* provider of
its name — we never ship a rebuilt upstream version of it alongside — so at
install time the package manager only ever sees the fork.

There is one rule the build enforces: the fork's version must out-rank the
upstream version it replaces. If upstream issues a new release that would
out-rank the fork, the build stops and asks you to rebase the fork onto that
new upstream and re-cut its version. This is deliberate: it stops a fork from
silently drifting behind an upstream security release. When that happens, pull
the new upstream source, re-apply the fork's changes on top, and bump the fork
marker.

### Ship a signed binary unchanged (tunnelling)

A few packages must ship as the upstream project's *own* signed binary —
processor microcode and the cryptographically-signed boot components — because
the signed file itself is the thing we want, byte for byte. These are
"tunnelled": copied in rather than rebuilt.

A tunnelled package still gets its version marker translated (so it sits in our
namespace like everything else), but only its metadata is rewritten — the signed
payload is never touched, so the signature stays valid. Two consequences follow
from the fact that we did not rebuild it:

- A binary rebuild marker (`+bN`) on a tunnelled package is **kept**. Because we
  didn't recompile it, the exact version pins between its sibling binaries are
  frozen as upstream wrote them, and those pins reference the `+bN`. Keeping it
  is what keeps them resolvable.
- Its dependency constraints are translated just like any other package's, so
  it slots into the dependency graph normally.

### Ship an older version (a downgrade)

Because version order follows content order, expressing a downgrade is easy:
build the older content and it naturally produces a lower version
(`+asg1u2` sits below `+asg1u3`). The package manager can then see it as a
genuine, lower target.

*Applying* a downgrade on already-installed machines is a deliberate operation,
not something that happens automatically. The newer build, being a higher
version, remains the default the package manager would pick, so a one-off
downgrade is held in place either by pinning that package to the older version
or by withdrawing the newer build. Building the older content also means
sourcing it from the point in time it came from. None of this is automatic on
purpose — a silent in-place downgrade could ripple through everything that
depends on the package.

### Force a rebuild with no source change

When you need to re-issue a package against a changed dependency without any
source change of its own, force a rebuild. It ships at the same base version
with our own `+bN` appended (anchored inside the update marker), so it sorts
above the previous build and below the next upstream update.

---

## A note on terms

- **pristine** — the plain upstream version with every re-distribution marker
  removed; the version a faithful, unchanged rebuild ships at.
- **update marker** — the trailing `+debNuK` / `~debNuK` that upstream uses for
  a post-release stable update, and which we translate to `+asg…` / `~asg…`.
- **patch level** (`+pP`) — a count of our own source changes on a given base.
- **fork** — a package we own and version by hand, ending in `+athenaN`.
- **tunnelled** — shipped as upstream's own signed binary, with only its
  metadata re-versioned.

---

## Appendix A — the build-time pseudocode

This is the canonical algorithm the build follows, kept here in sync with the
implementation. It is deliberately written in working mnemonics (`sidecar`,
`build_source`, `Patch_Bump_Count`, …) rather than the reader-facing prose
above; the prose is the explanation, this is the recipe. **If the pseudocode is
ever updated, keep these same mnemonics.**

Two corrections from the first draft are folded in and marked `[FIX]`: anchoring
the patch/force suffix inside the update namespace, and restamping same-source
sibling pins to the sibling's exact final version (so a patched source whose
binaries carry different bases cannot ship an unsatisfiable pin).

```
PREFIX  = BUMP_PREFIX if set, else first_letters(DISTRIBUTION)   # Asgard -> asg
RELEASE = VERSION                                                # R, e.g. 1 (thor 1)

# ── Pass 1: decide what to rebuild, and our patch level P, per Source ──
For each Package in selected:

    Source = Package.Source
    sidecar(Source).rebuild = no
    sidecar(Source).built   = no

    # Split off a binary-only rebuild marker (kept; epoch kept):
    #   1:1.2.3-4+deb12u4+b2 -> version = 1:1.2.3-4+deb12u4, binNMU = +b2
    #   1:1.2.3-4+b1         -> version = 1:1.2.3-4,         binNMU = +b1
    #   1:1.2.3-4+deb12u4    -> version = 1:1.2.3-4+deb12u4, binNMU = empty
    #   1:1.2.3-4~bpo1+M     -> version = 1:1.2.3-4,         binNMU = ~bpo1+M
    version = Package.version.strip_binNMU
    binNMU  = Package.getbinNMU                  # +bN or ~bpoN+M, if present

    if Package is tunneled:
        For each binary in Source:
            if binary not on disk:
                download binary by binary.upstream_filename
        continue

    # first time we see this Source
    if Source not in sidecar:
        sidecar(Source).rebuild      = yes
        sidecar(Source).bN_BumpCount = 0
        if exists Source.patch_set:
            sidecar(Source).patch_set        = current_patch_set
            sidecar(Source).Patch_Bump_Count = 1
        else
            sidecar(Source).Patch_Bump_Count = 0
            sidecar(Source).patch_set        = empty

    # upstream Source version changed (base OR nmu; `>` only — downgrades are
    # a deliberate, forced operation, handled separately)
    else if Source.version > sidecar(Source).previous_version:
        sidecar(Source).rebuild = yes
        if exists Source.patch_set:
            sidecar(Source).Patch_Bump_Count = 1   # re-baseline: our patch #1 on the new base
        else
            sidecar(Source).Patch_Bump_Count = 0

    # same Source version, but OUR patch set changed
    else if Source.patch_set changed:
        sidecar(Source).rebuild = yes
        if exists Source.patch_set:
            sidecar(Source).Patch_Bump_Count++     # next patch revision
        else
            sidecar(Source).Patch_Bump_Count = 0   # patch removed -> faithful

    # only a binNMU/backport moved -> no rebuild (we build from source)
    else if binNMU != empty:
        sidecar(Source).rebuild      = no
        sidecar(Source).bN_BumpCount = 0

# ── Pass 2: build each Source once ──
For each Package in selected:
    Source = Package.Source
    if sidecar(Source).rebuild == yes:
        sidecar(Source).rebuild = no             # dedup: build the Source once
        build_source(Source, tunneled = no, force = no)
    else if Package is tunneled:
        if sidecar(Source).built == no:
            build_source(Source, tunneled = yes, force = no)


function transpose(version):
    # Rewrite ONLY a TRAILING debNuN marker to our own; the leading +/~ sign and
    # everything else (epoch, an embedded +deb, +nmuN, dotted revision) are kept.
    #   1.2.3-4+deb12u3 -> 1.2.3-4+asg1u3 ;  2.4.67-1~deb12u2 -> 2.4.67-1~asg1u2
    replace(version, trailing <debNuN>, <PREFIX><RELEASE>u<N>)
    return version

function append_our_suffix(version, Source, force):
    # [FIX] Both +p and +b sort ABOVE the +asg namespace, so attaching them to a
    # bare base would outrank every upstream update.  Anchor them: when there is
    # no trailing +<PREFIX><RELEASE>uN / ~<PREFIX><RELEASE>uN marker (pristine
    # base), synthesize u0 first, so they sort BELOW the next upstream update.
    P  = sidecar(Source).Patch_Bump_Count
    bN = sidecar(Source).bN_BumpCount
    if (P > 0 or force == yes) and version has no trailing [+~]<PREFIX><RELEASE>uN:
        version = version + +<PREFIX><RELEASE>u0
    if P > 0:
        version = version + +p<P>            # our patch level
    if force == yes:
        version = version + +b<bN>           # our forced rebuild (binNMU)
    return version                           # order: transpose -> +pP -> +bN

# Force a rebuild (no source change) — our own binNMU, applied to every binary.
function force_build(Package):
    build_source(Package.Source, tunneled = no, force = yes)


function build_source(Source, tunneled, force):
    if sidecar(Source).built == yes:
        return

    if tunneled == false:
        Build Source (with patches)
        if force == yes:
            sidecar(Source).bN_BumpCount++
        else
            sidecar(Source).bN_BumpCount = 0

    for each binary in Source:
        binary.X-upstream-version = binary.version        # provenance (pre-transpose, per binary)
        binary.version = append_our_suffix(transpose(binary.version), Source, force)

        for each dep in binary:
            # [FIX] Constraint bounds name OTHER packages' versions: a Debian
            # binNMU/backport layer (+bN / ~bpoN+M) on a bound references a
            # buildd rebuild that does not exist in our repo, so strip it
            # BEFORE transposing — for EVERY operator, `=` pins included.
            # EXEMPT: a bound targeting a TUNNELLED binary (keep_binnmu_names)
            # — the target ships its +bN verbatim, so the bound keeps it.
            # GUARD: a bound already carrying our own [+~]<PREFIX><RELEASE>uN
            # (+pP/+bN) chain is OUR forced-rebuild pin — never strip it.
            if dep.name not in tunneled_binaries
                    and dep.version has no trailing [+~]<PREFIX><RELEASE>uN chain:
                dep_version = dep.version.strip_binNMU
            else
                dep_version = dep.version
            dep_version = transpose(dep_version)           # trailing debNuN -> our marker
            # [FIX] A same-source sibling pin must land on the sibling's EXACT
            # final version (uniform P + force across the Source, incl. the u0
            # anchor) — matched BY NAME so siblings on a DIFFERENT base are
            # handled.  The old form added only +bN under force and dropped +pP.
            if dep.name in Source.binaries:
                dep_version = append_our_suffix(dep_version, Source, force)
            dep.version = dep_version

        sign binary

    # persist state for next time
    sidecar(Source).previous_version = Source.version
    sidecar(Source).patch_set        = current_patch_set
    sidecar(Source).built            = yes
```

(A fork is its own provider and is versioned by hand in its changelog, so it
does not pass through `append_our_suffix` — its `+athenaN` is the recorded
version; `transpose` still applies to a trailing upstream marker.)

---

## Appendix B — the same-upstream-sibling constraint idiom

Reference notes for the pair-rewriter pass in `bump._rewrite_control_text` —
the shared control-text scaffold behind both `strip_nmu_from_control_text`
and `transpose_control_text` — why it exists, why we rewrite the way we do,
what the alternatives were, and what the risk analysis covered.

### The problem

Stripping the re-distribution markers (the design above) interacts badly
with one dependency pattern.  A subset of upstream Debian packages encode
an "any debian-revision of the same upstream" constraint between sibling
binaries using the pattern:

```
Depends: ..., X (>> V), X (<< V-.)
```

where `V` is the source's bare upstream version (substituted from
`${source:Upstream-Version}` at build time).  Per Debian Policy §5.6.12,
the missing debian-revision in `V` is treated as `'0'`, so:

- `apt_pkg.version_compare('V-0', 'V')` → `0` (equal)
- `apt_pkg.check_dep('V-0', '>>', 'V')` → `False`

Upstream Debian builds at `V-0+debNuN` (or similar), which IS strictly
greater than bare `V`.  After our strip, our sibling binary lands at `V-0`,
which apt treats as equal.  The `>>` half of the constraint fails:

```
ERROR: dependency git -> git-man unresolved
  git Depends: git-man (>> 1:2.39.5) — no satisfying pkg in repo/
```

The audit reports it; at install time apt would also reject.

### What the idiom is FOR

The maintainer's intent: lock sibling binary X to the same upstream as the
consumer, while permitting per-binary debian-revision drift.  In upstream
Debian, this lets a documentation-only point bump of `git-man` (e.g.
`-1` → `-2`) ship without forcing a rebuild of `git` itself.  Our atomic
build pipeline does not exercise this flexibility — `dpkg-buildpackage`
always emits all sibling binaries together at the same Version field.  So
the "loose" lower bound was never load-bearing for us.

### Repo prevalence

A scan of every `.deb` in `repo/main` (2026-05-21) for the AND-pair
pattern `X (>> V), X (<< V-.)`:

| Source family | Binaries using the idiom |
|---|---|
| git | 11 (git, git-man, git-all, gitk, gitweb, git-cvs, git-svn, git-daemon-{run,sysvinit}, git-email, git-gui, git-mediawiki) |
| (all other sources) | 0 |

The pattern is a git-family convention.  Other sources use `(=
${binary:Version})` for sibling locks, which works transparently
because sibling binaries share Version after strip.

### The four rewrite options considered

| Option | What it does | Pros | Cons |
|---|---|---|---|
| A. Per-pkg DEP-3 patch | Edit `debian/control`: `>>` → `>=` | Minimal diff; surgical | Per-version path; doesn't generalise |
| B. Pipeline rewrite `>>` → `>=` | At strip time, swap operator only | Generic; small code; preserves "any debrev" semantics | Lower bound slightly broader than upstream-intent |
| C. Pipeline rewrite to `(= our_version)` | At strip time, collapse pair into exact-sibling-version | Tightest; encodes our atomic-build invariant; no per-pkg patches | Slightly bigger code (pair detection + format roundtrip) |
| D. Selectively bump target binary | Keep `+debNuN` on idiom targets only | Preserves upstream constraint as-is | Re-introduces bump machinery we removed; cascading to N-deep transitive targets; filename normalisation upheaval |

### Impact analysis of Option C (pair → `(= our_version)`)

The rewrite is **strictly bound** to the detected pair pattern.  Trigger
conditions:

1. Field is `Depends` or `Pre-Depends`
2. Two AND-level entries (not OR-alternatives) target the **same name** X
3. One entry has operator `>>` with version V
4. The other entry has operator `<<` with version exactly `V-.`

If any of these fail, the constraints are left unchanged.

**Scenarios that could newly satisfy the rewritten `(= our_version)`.**
`our_version` is our binary's own (post-strip) Version field, e.g.
`1:2.39.5-0` for git.  The rewritten constraint accepts only that exact
value.  This is **strictly narrower** than both the original `(>> V),
(<< V-.)` (which accepted any `V-N` for N ≥ 1) and the simpler `>=`
relaxation (which would have accepted V, V-0, V-1, ...).

| Test value | Original verdict | Option C verdict |
|---|---|---|
| `V-0` (our binary) | reject | **accept** |
| `V-1`, `V-2`, ... `V-Z` | accept | reject (different from ours) |
| `V+suffix` (any +debNuN, +bN) | accept | reject (different from ours) |
| Different upstream (`V+1`, `V-1` upstream) | reject | reject |

Option C **rejects** values the original accepted (e.g. `V-1`).  In
practice this is harmless because:

- Our pipeline never produces `V-1` — every sibling-binary build is at the
  same exact Version.
- If a hand-crafted .deb at `V-1` landed in `repo/main`, the audit would
  flag it as a duplicate of our `V-0` (highest-version dedup), and dpkg
  at install time would prefer the higher version.  So the operator
  always sees and resolves the duplicate before it can be picked up.

**Cross-source binary collision.**  Debian namespace is single-owner per
binary name.  Two different sources cannot both produce `git-man`.  apt's
resolver picks one canonical version per name.  In our self-contained
repo, there is no alternative archive that could provide a parallel
git-man.  Not realisable.

**Snapshot rollover mid-source-build.**  `dpkg-buildpackage` is atomic
per source.  git's source emits git AND git-man in one invocation,
sharing the exact Version field.  Stale artefacts from a previous
snapshot are handled by repo dedup (highest wins).  Cross-snapshot
mismatches are detected by other mechanisms (dep_drift) before they can
be installed.

**Stale duplicate in repo.**  `repo_audit.scan_repo_state` keeps only
the highest version per name; `dpkg-scanpackages` does the same; dpkg
prefers highest at install.  Whichever copy is highest wins; behaviour
is identical between the original and rewritten constraints.

**Versioned Provides from a third party.**  Original `>> V` rejected any
`(= V)` Provides; rewritten `(= V-0)` accepts only an exact-version
Provides match.  Scanning our repo (2026-05-21): zero packages declare a
versioned Provides for git, git-man, bubblewrap, or libdpkg-perl.  Same
outcome for both constraint shapes in practice.

**Hand-crafted .deb at bare `V`.**  `dpkg-buildpackage` never emits a
Version field without a debian-revision.  Bare-`V` could only appear via
a manually-dropped .deb.  Option C rejects it (exact-match to our `V-0`);
original rejected it as well (via `>>`).  Both safe.

### What "designed to run with" preserves

The maintainer's hard intent — same upstream V, ABI-compatible sibling —
is preserved by Option C in a stricter form: not just "same upstream"
but "same source-build instance".  No looser, no looser-by-accident.

The maintainer's soft intent — "any debrev that's not the unpackaged
upstream" — was a defensive heuristic against manually-built siblings.
That threat doesn't apply in any apt-installable workflow.  Discarding
it loses nothing operational.

### Why Option C over Option B (the simpler `>=` swap)

- **Tighter**: B accepts the value `V-0` plus theoretically `V-1`, `V-2`
  etc.  C accepts only our binary's exact version.  Less surface area
  for accidental satisfier injection.
- **Self-documenting**: `(= 1:2.39.5-0)` reads as "must be the exact
  same as us"; `(>= 1:2.39.5)` reads as "must be greater than upstream",
  which is conceptually misleading after our strip.
- **No semantic relaxation**: C says "lock to our version", which IS the
  invariant our build pipeline preserves.  B says "any version above
  upstream", which is a broader claim.

The minor cost of C is one extra round-trip through `PkgRelation.parse`
and `PkgRelation.str` per Depends field — negligible vs. dpkg-deb's
own pack/unpack.

### Implementation summary

`bump._rewrite_sibling_idiom_in_text` is called by the shared
`_rewrite_control_text` scaffold (so both `strip_nmu_from_control_text`
and `transpose_control_text` get it) after the per-constraint
version-string rewrite pass.  It:

1. Parses each Depends / Pre-Depends field via
   `debian.deb822.PkgRelation.parse_relations`
2. Walks the AND-level relation list
3. For each single-entry `(X >> V)` AND-group, looks for a matching
   single-entry `(X << V-.)` AND-group with the same name and the
   `-.` upper-bound version
4. When the pair matches, rewrites the `>>` entry to
   `(X = our_version)` and removes the `<<` entry
5. Re-serialises via `PkgRelation.str`

`our_version` is the binary's own Version field, extracted AFTER the
version rewrite pass (the pristine stripped value on the strip path, the
transposed value on the transpose path).  The same atomic source-build
invariant means this matches every sibling's Version.

### When this can be retired

If the strip policy ever reverts to carrying a controlled bump layer,
the original idiom satisfies again without rewrite, and the
pair-rewriter can be removed.  Until then, this is a permanent part of
the strip pipeline.
