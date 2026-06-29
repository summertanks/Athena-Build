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
marker would have thrown away.

Where one binary pins an *exact* version of a sibling built from the same
source, that pin is updated to the sibling's exact final version — patch level
and all — so the two always match on disk.

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
