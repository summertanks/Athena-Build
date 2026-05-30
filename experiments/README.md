# `experiments/` — measurement scripts kept for reproducibility

Out-of-band scripts that import the production tree but don't touch it.
Kept under version control so we can re-run them when the underlying
classes change (e.g. after a Cache refactor) without re-deriving the
methodology from scratch.

## `ux04_pickle_perf.py`

Times the UX-04 round-trip on the operator's actual bookworm workload.
Used during UX-04 v2 design to decide whether plain pickle was fast
enough to ship (it was — measured ratio T_resume / T_fresh = 0.433).

```bash
# Run from the repo root:
python3 -u experiments/ux04_pickle_perf.py [--trials N] [--keep-blob]
```

The script:
- Monkey-patches `Cache.__getstate__`/`__setstate__`,
  `DependencyTree.__getstate__`/`__setstate__`, and
  `Package`/`Source.__getstate__`/`__setstate__` at runtime so the
  experiment doesn't depend on the production tree carrying those
  overrides.  This was Phase 1's "don't touch base code" guarantee.
- Builds Cache + DT via `BuildSession.cmd_build_cache` +
  `cmd_parse_dependency` against the operator's `config/`.
- Times pickle save + load + DT `__cache` rewire.
- Prints `T_resume / T_fresh` and a verdict line.

Re-run after any change to:
- The `Cache` / `DependencyTree` / `Package` / `Source` attribute
  surface (the production `__getstate__` / `__setstate__` may need
  matching updates and the perf ratio may shift).
- `python-debian` major version (the multi-valued field wrappers
  evolved in 0.1.50+; if they stop carrying weakrefs Source could
  switch to field-replay like Package).
- The bookworm snapshot pin (very different workload sizes shift the
  ratio non-linearly).

The script is auto-respondent: prompts (`PROMPT_YESNO`, `PROMPT_OPTIONS`)
default to "y" / first option so it runs unattended.  Set
`stdin = /dev/null` is safe.
