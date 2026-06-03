"""COORD-01: multi-builder repo coordination.

Modules:

  schema     — claim record, snapshot.json, coord-head dataclasses;
               canonical-JSON serialization.
  identity   — Ed25519 keypair generation, sign, verify, keyring load.
               Backed by openssl pkeyutl (subprocess; no Python crypto
               dep required on the host).
  store      — per-builder JSONL append-only writer + atomic-replace;
               fold-over-all-files reader; seqno bookkeeping.
  reconcile  — three-way audits (local, cross).  Local-only in P1;
               `repo` audit lands in P3 with the publish path.
  policy     — operator-tunable hash-conflict / orphan policy knobs.

  transport  — SSH/rsync fetch + push of repo-coord/ tree; remote flock
               helper.  Pull side lands in P2, push side in P3.
  publish    — 11-step publish transaction state machine (P3).

The on-disk sidecar lives at config.dir_coord (defaults to
<working_dir>/repo-coord/) — see utils.BuildConfig.__init__ for the
auto-create + writability gate (memory `feedback_dir_ensure_on_config_load`).
The sidecar tree is NEVER served over HTTP; it's reached over SSH only.
"""
