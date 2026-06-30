"""Policy knobs — operator-tunable decisions.

These are facts the codebase consults, separated from the data layer
(schema, store) and the audit layer (reconcile) so a future operator
override doesn't touch correctness code.

For v1, every knob is HARDCODED at the value the original
design fixed:
  HASH_CONFLICT_POLICY = 'BLOCK'   — see plan §"Hard constraints" #1
  COORD_HEAD_MAX_AGE_SECONDS = max age of a fetched coord-head (stale +
                               fresh InRelease ⇒ rollback signal)

Mutation point if/when these need to flex: this file, plus a coord
config block.  Don't sprinkle policy decisions inline in reconcile.
"""


# When two builders publish the same (package, built_version) tuple
# with DIFFERENT sha256s, the audit goes red and PUBLISH_HALT lands.
# Other options (deferred; in plan §"Out of scope"):
#   'FIRST_WINS'    — second publisher silently rejected at lock acquire
#   'BOTH_VALID'    — operator picks via `coord conflict resolve`
#   'PER_PKG_LIST'  — small allowlist for known-nondeterministic pkgs
# The user explicitly chose BLOCK during the planning session.
HASH_CONFLICT_POLICY = 'BLOCK'


# Number of days an orphan (owner's local pool no longer has the .deb)
# can persist before audit escalates to WARN.  Manual operator action
# (rebuild + adopt, or retract) clears it.  Below this threshold the
# orphan is INFO-only — covers short-window crashes and snapshot pivots
# where the owner intentionally dropped a build.  Enforced in
# reconcile.audit_local's C-1 orphan branch (claim age from built_at).
ORPHAN_WARN_AFTER_DAYS = 14



# Sentinel file written under repo-coord/ when audit detects a hash
# conflict.  Presence of this file blocks all subsequent publishes
# until operator runs `coord conflict resolve` (P3).
PUBLISH_HALT_FILENAME = 'PUBLISH_HALT'


# Maximum age (seconds) of a fetched coord-head before it's considered
# stale.  A stale coord-head + fresh InRelease is a rollback signal.
# 24h is generous for a daily-publish cadence; tighter for higher
# publish frequencies.
COORD_HEAD_MAX_AGE_SECONDS = 24 * 60 * 60
