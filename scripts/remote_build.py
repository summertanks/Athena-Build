#!/usr/bin/env python3
"""Self-contained remote source-package builder.

Runs ON a remote build host (where Docker is local, so the build's bind mounts
just work — no daemon-API copy-in/out). It is GENERIC: it knows nothing
Athena-specific.  Everything it needs is staged into one bundle directory by the
orchestrator (or by hand for the adduser prototype):

    <bundle>/
        Dockerfile          the build image recipe (no COPY/ADD → no context)
        source/             the package's .dsc + *.orig.tar.* + *.debian.tar.*
        patch/              the package's *.patch files (may be empty)
        out/                created here; built .deb/.udeb land here
        build.json          parameters (below)

build.json:
    {
      "package":      "adduser",                       # label only
      "image_tag":    "athenalinux:build-bookworm-<ts>",
      "build_args":   {"RELEASE": "...", "SNAPSHOT_TS": "...", ...},
      "cmd_str":      "set -e; ... dpkg-buildpackage ... cp *.deb /repo/ ...",
      "build_cpus":   7.0,     # optional docker --cpus
      "build_memory": "28g"    # optional docker --memory
    }

It (1) builds the image from the Dockerfile via the `docker` CLI (skipped if the
tag already exists — cached after the first run), (2) runs the container with
source→/source, patch→/patch, out→/repo bind mounts and the given cmd_str,
streaming logs to stdout, then (3) prints a single machine-readable result line:

    __ATHENA_REMOTE_RESULT__ {"exit_code": 0, "outputs": ["adduser_*.deb", ...]}

Dependencies on the remote: python3 (stdlib only) + the `docker` CLI.  No
docker-py, no Athena code.
"""

import argparse
import glob
import json
import os
import subprocess
import sys

RESULT_MARKER = "__ATHENA_REMOTE_RESULT__"


def _log(msg: str) -> None:
    print(f"[remote-build] {msg}", flush=True)


def _image_exists(tag: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", tag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def build_image(bundle: str, tag: str, build_args: dict) -> None:
    """Build <tag> from <bundle>/Dockerfile with an EMPTY context (the Dockerfile
    has no COPY/ADD).  Streamed via stdin so `source/` isn't sent as context."""
    if _image_exists(tag):
        _log(f"image {tag} already present — skipping build")
        return
    _log(f"building image {tag} ...")
    _flags: list = []
    for _k, _v in (build_args or {}).items():
        _flags += ["--build-arg", f"{_k}={_v}"]
    _dockerfile = os.path.join(bundle, "Dockerfile")
    with open(_dockerfile, "rb") as _fh:
        _r = subprocess.run(
            ["docker", "build", "-t", tag, *_flags, "-"], stdin=_fh)
    if _r.returncode != 0:
        raise SystemExit(f"docker build failed (rc={_r.returncode})")
    _log(f"image {tag} built")


def run_container(bundle: str, tag: str, cmd_str: str,
                  cpus: 'float | None', memory: 'str | None') -> int:
    """Run the build container with local bind mounts; stream logs; return the
    container exit code."""
    _src = os.path.join(bundle, "source")
    _patch = os.path.join(bundle, "patch")
    _out = os.path.join(bundle, "out")
    os.makedirs(_out, exist_ok=True)
    # container's `athena` user (uid 1000) writes the .debs into /repo → out/;
    # make it world-writable so the copy never fails on a uid mismatch.
    os.chmod(_out, 0o777)
    if not os.path.isdir(_patch):
        os.makedirs(_patch, exist_ok=True)   # empty patch dir is valid

    _caps: list = []
    if cpus and cpus > 0:
        _caps += ["--cpus", str(cpus)]
    if memory:
        _caps += ["--memory", str(memory)]

    _cmd = [
        "docker", "run", "--rm", *_caps,
        "-v", f"{os.path.abspath(_src)}:/source:rw",
        "-v", f"{os.path.abspath(_patch)}:/patch:rw",
        "-v", f"{os.path.abspath(_out)}:/repo:rw",
        tag, "bash", "-c", cmd_str,
    ]
    _log(f"running container ({tag}) — caps={_caps or 'none'}")
    # inherit stdout/stderr so logs stream live back over SSH
    return subprocess.run(_cmd).returncode


def main(argv: 'list[str] | None' = None) -> int:
    _p = argparse.ArgumentParser(
        prog="remote_build.py",
        description="Build one source package on this host's Docker.")
    _p.add_argument("bundle", nargs="?", default=".",
                    help="bundle directory (default: cwd)")
    _a = _p.parse_args(argv)
    _bundle = os.path.abspath(_a.bundle)

    with open(os.path.join(_bundle, "build.json")) as _fh:
        _params = json.load(_fh)
    _tag = _params["image_tag"]
    _cmd_str = _params["cmd_str"]
    _pkg = _params.get("package", "<unknown>")
    _log(f"package={_pkg} bundle={_bundle}")

    build_image(_bundle, _tag, _params.get("build_args", {}))
    _exit = run_container(
        _bundle, _tag, _cmd_str,
        _params.get("build_cpus"), _params.get("build_memory"))

    _out = os.path.join(_bundle, "out")
    _outputs = sorted(
        os.path.basename(_f)
        for _pat in ("*.deb", "*.udeb")
        for _f in glob.glob(os.path.join(_out, _pat)))
    _log(f"container exit={_exit}; {len(_outputs)} artifact(s): {_outputs}")
    print(f"{RESULT_MARKER} "
          + json.dumps({"package": _pkg, "exit_code": _exit,
                        "outputs": _outputs}), flush=True)
    # Non-zero if the container failed OR produced nothing on a clean exit.
    if _exit != 0:
        return _exit
    return 0 if _outputs else 3


if __name__ == "__main__":
    sys.exit(main())
