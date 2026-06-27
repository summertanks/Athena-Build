# Version numbering — what gets bumped, and when

Every package needs a version string, and that string decides two things: whether `apt` sees a package as newer than what's installed, and which of two packages "wins" when both exist. This doc states the **basic rules** Athena follows. The fiddly edge cases live in linked deep-dives at the end; start here.

The guiding idea is simple: **Athena rebuilds from source, so it ships the *pristine* upstream version wherever it can, and adds a small, predictable suffix only when the package is genuinely different from upstream.**

## A 60-second Debian-version primer

A Debian version has up to three parts — `[epoch:]upstream-version-revision`, for example `2:1.2.3-4`:

- the **upstream version** (`1.2.3`) — the software's own version;
- the **Debian revision** (`-4`) — how many times Debian has re-packaged that same upstream version;
- an optional **epoch** (`2:`) — a rarely-used override that forces ordering. It exists for when a version scheme changes in a way that would otherwise sort *backwards* (e.g. upstream switches from a date like `20240101` to `1.0`, which compares as *older*). Bumping the epoch says "trust me, this is newer." Epoch defaults to `0`, is compared before everything else, and once added never goes away.

On top of that, Debian appends build-time suffixes you don't control:

- `+deb12u1` — a security / point-release update for Debian 12.
- `+b1` — a "binNMU": a rebuild with no source change.
- `~bpo12+1` — a backport.

Ubuntu adds its own (`...ubuntu1`); other derivatives add theirs. These suffixes exist because each distro is *re-distributing Debian's binaries* and has to mark its changes. Athena is different: it **rebuilds the source itself**, so it strips those build-time suffixes back to the pristine version and, if needed, adds *one* suffix of its own. Throughout, Athena leaves the **epoch** and the **upstream version** untouched — it only ever strips or adds the *trailing suffix*. (Epoch is preserved verbatim through every strip and stamp; `2:1.2.3-4+b1` becomes `2:1.2.3-4`, never `1.2.3-4`.)

## Why Debian adds these suffixes — and what each means for us

Each suffix encodes *why* Debian re-issued the package. Athena doesn't read changelogs or judge "is this security" — it simply recognises these suffixes by their **shape in the version string**, and that shape is what decides whether its rebuild counts as "changed":

- **`+deb12u1` — a stable update (a security fix or point-release bug-fix).** Debian re-issued the *source* after release, and `+debNuN` is its standard tag for "an update to the original" (`deb12` = Debian 12 / bookworm, `u1` = update 1). Athena knows nothing about *why* — it just sees the `+debNuN` tag on the source version, which by Debian's convention means the source changed after release. So its rebuild genuinely differs from the original `1.2.3-4`, and it gets marked.
- **`+b1` — a binary-only rebuild ("binNMU").** **No source change at all** — Debian just rebuilt the package against an updated library elsewhere in the archive. Athena already rebuilds everything from source against its *own* libraries, so this reason simply doesn't apply to us — strip it, ship pristine.
- **`~bpo12+1` — a backport.** The same package rebuilt for a *different* (older) release. Not relevant to the release you're targeting — strip it.
- **A new revision (`-4` → `-5`) — the maintainer changed the packaging.** This is part of the upstream version itself, not a strippable suffix; Athena builds it and ships it as the version it is.

> **"Same upstream version — so why rebuild at all?"** Because `+debNuN` is a new Debian *source* upload: the upstream tarball (`1.2.3`) is unchanged, but Debian added patches to the packaging, so the source *package* (`1.2.3-4+deb12u1`) genuinely differs from `1.2.3-4`. Athena resolves to that updated source and builds it — that's how the fix gets in — then normalises only the version *label* back to `1.2.3-4`, marking it `+asg` to record that the build carries post-release changes. The one case where the source really is identical is `+bN` (a binNMU — a binary-only rebuild against some other changed library); that is exactly why those ship pristine and unmarked.

The pattern to hold onto: **a suffix that reflects a real change to the source → Athena's rebuild is also a real change, so it's marked; a suffix that's only a re-distribution artifact → stripped, and the rebuild ships pristine.** The next sections turn that single idea into concrete rules.

## The four cases

Every package Athena ships falls into one of four cases. This is the whole model:

| The package is… | Version it ships at | Example |
|---|---|---|
| **Rebuilt from upstream, unchanged** | the **pristine** upstream version (Debian's build suffixes stripped) | `1.2.3-4+deb12u2` → **`1.2.3-4`** |
| **Rebuilt from upstream, *changed*** | pristine version **+ `+asg<R>u<N>`** | `1.2.3-4` → **`1.2.3-4+asg1u1`** |
| **An Athena fork** (you took the package over) | upstream version **+ `+athenaN`**, set by hand | **`12.4+deb12u14+athena1`** |
| **Tunnelled** (shipped as-is from Debian, not rebuilt) | Debian's exact version, **untouched** | `3.20250311.1+deb12u1` (kept) |

The rest of this doc just explains those four rows.

## Rule 1 — rebuilt packages start pristine

When Athena rebuilds an upstream package, it first **strips Debian's build-time suffixes** (`+bN`, `+debNuN`, `~bpoN+N`, …) to recover the plain upstream version. A package that we rebuilt but did **not** change ships at exactly that pristine version — no Athena marking at all.

Why: a pristine version is honest (it says "this is upstream 1.2.3-4, rebuilt"), and it keeps security tooling working — vulnerability scanners match the real upstream version. (The original upstream string is preserved in an `X-Athena-Upstream-Version` field inside the package for provenance.)

## Rule 2 — a real change earns an `+asg<R>u<N>` suffix

If a rebuilt package is genuinely **different** from pristine upstream, Athena stamps it with **`+asg<R>u<N>`**:

- **`asg`** — a short tag derived from your **distribution name** (Asgard → `asg`); it marks the build's origin. Rename your distro and this tag changes with it — a distro called *Valhalla* would stamp `+val…`. This doc assumes Asgard, so you'll see `asg` throughout.
- **`R`** — your **release number** (`VERSION` in `distro.conf`; `1` for thor 1).
- **`N`** — an **update counter** for that package, starting at 1 and going up each time you ship a new version of it within release `R`.

So `1.2.3-4+asg1u2` reads as "Athena's 2nd update of this package, in release 1, built from upstream 1.2.3-4." It sorts *above* pristine `1.2.3-4` (so it's seen as newer) and *below* Debian's own `+deb…` (so a later real Debian security update still wins).

> Why `+asg<R>u<N>` and not `+thor<N>`? Because codenames don't sort — `loki1` would compare *less than* `thor1` and break update ordering across releases. A plain release **number** always orders correctly.

**A package counts as "changed" when:**

1. **you patched it** (your source changes — clearly different bytes), or
2. **upstream's version carried a build suffix** (e.g. it was a `+deb12u3` security build — stripping it could collide with the original, so we mark ours), or
3. **it's the next in an existing update line** (you've already shipped `+asg1u1` for it, so the rebuild continues as `+asg1u2`).

If none of those hold — a plain rebuild with no real difference — it ships **pristine** (Rule 1). Notably, *only* rewriting an internal dependency version does **not** count as a change.

All binaries produced from one source get the **same `N`**, so their cross-references stay consistent.

## Rule 3 — forks are versioned by hand as `+athenaN`

When you take over a package to change it permanently (usually for branding) it becomes a **fork** under `fork/source/<pkg>/`. You set its version yourself in its `debian/changelog`, as the upstream version **+ `+athenaN`** — bumping `N` each time you change the fork:

```
base-files (12.4+deb12u14+athena1) thor; urgency=low
base-files (12.4+deb12u14+athena2) thor; urgency=low   ← next fork revision
```

Forks do **not** get the automatic `+asg…` stamp — their version is whatever the changelog says. The build's **collision gate** checks that your fork's version outranks the upstream package it replaces, so your version always wins; if upstream ever overtakes it, the build fails loudly rather than silently shipping upstream's.

## Rule 4 — tunnelled packages are left exactly as Debian shipped them

A few packages are deliberately **not** rebuilt — CPU microcode and cryptographically-signed boot components — because Debian's official signed binary is the thing you want. These are "tunnelled": copied straight into your repo with **Debian's exact version untouched**, so the file still matches its signature.

## The bump decision in detail

Rule 2 said *when* a rebuild is marked; here is the exact decision the build makes. Four things could trigger an `+asg` stamp — three do, and one deliberately does **not**:

| Case | Trigger | Real change to the package's own bytes? | Stamped `+asg`? |
|---|---|:--:|:--:|
| **A** | You patched the source | Yes — your changes | **Yes** |
| **B** | The source version carried a Debian suffix (`+debNuN`, …) | Yes — a post-release source | **Yes** |
| **C** | The build only rewrote a **dependency version** (see below) | **No** — bytes identical to upstream | **No** |
| **D** | This package already has an `+asg` version published (an update line) | Yes — keep ordering and pins consistent | **Yes** |

**Why Case C is not a stamp.** When Athena builds on a Debian base, the build tools record each dependency's *floor* against whatever is installed in the build container — which carries Debian's security suffixes. Stripping those back to pristine is mandatory: a leftover floor like `perl (>= 5.36.0-7+deb12u3)` cannot be satisfied by our own `+asg` packages (which sort *below* `+deb`), and the whole repo would be uninstallable. But that strip only moved a dependency *floor* — the package's own bytes are identical to pristine upstream, so it is **not** a real change and earns no stamp:

```
build container's perl:    5.36.0-7+deb12u3
generated dependency:      perl (>= 5.36.0-7+deb12u3)
after the mandatory strip: perl (>= 5.36.0-7)      ← floor normalised; our bytes unchanged → no +asg
```

(This case exists only because Athena currently builds *on* Debian. Once Athena builds on Athena, there are no Debian suffixes left to strip from a generated dependency, and Case C disappears on its own.)

## A caveat — cross-package `=` pins

Stripping a dependency back to pristine is safe for an "*at least* version X" floor (`>=`): our `X+asg1u3` still satisfies `>= X`. It can break only for an **exact** pin (`=`) that points *across* packages:

```
package A depends:  B (= X+deb12u7)   → strip →   B (= X)
our repo has B at:  X+asg1u3           →   X ≠ X+asg1u3   → A can't find a matching B
```

Floors (`>=`) are safe, and same-source sibling pins are safe (they are rewritten to the stamped version automatically). Only a cross-package exact pin, stripped to bare pristine against an `+asg`-stamped target, can dangle — and `repo audit` flags exactly this, with the fix (relax it to `>=`, or pin the `+asg` version). Like Case C, it vanishes under self-hosting.

## Known rough edge — the fork-version scheme (CONF-14)

Today's fork versions (Rule 3) bake the upstream security suffix into the fork version: `12.4+deb12u14+athena1`. That works, but it means **every upstream security release forces a fork rebase** — when upstream moves to `+deb12u15`, the fork must be re-cut as `12.4+deb12u15+athena1` to stay ahead.

The cleaner target is `12.4+athena1` (pristine upstream + our suffix only, the way Devuan and Mint do it). The catch: `+athena1` sorts *below* `+deb12u15` (`a` < `d`), so the collision gate would reject it. Resolving this (tracked as **CONF-14**) means teaching the version-strip logic to treat `+athenaN` as a strippable layer — so a fork and its upstream compare as *equal* at the pristine base, and the fork is simply the record we keep. (A blunter alternative, used by some distros, is to bump the **epoch** — `1:12.4+athena1` outranks any non-epoch upstream version unconditionally — but an epoch can never be removed, so it's a last resort.) Until then, forks keep the upstream suffix.

## Going deeper

- [glossary.md](glossary.md) — definitions of *package*, *fork*, *tunnelled*, etc.
- [strip-nmu-sibling-constraint-idiom.md](strip-nmu-sibling-constraint-idiom.md) — how internal dependency version constraints are rewritten when a version changes.
- [cve-tracking.md](cve-tracking.md) — why pristine versions matter for security scanning.
