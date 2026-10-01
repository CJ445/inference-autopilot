#!/usr/bin/env bash
# Clean-machine check: what a first-time user gets from a NON-editable install on a machine with no
# configuration, no state, no workload and no GPU/Docker/Kubernetes. Needs python3 and cargo.
# Usage: scripts/clean_machine_check.sh        (exits non-zero on the first thing that is wrong)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export HOME="$work/home" XDG_CONFIG_HOME="$work/home/.config" XDG_STATE_HOME="$work/home/.state"
mkdir -p "$HOME" "$work/cwd"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAILED: %s\n' "$*" >&2; exit 1; }

step "1. install into a fresh virtualenv (not editable) and build the operator UI"
python3 -m venv "$work/venv"
"$work/venv/bin/pip" -q install "$root" >/dev/null
rm -rf "$root/build"
cargo build --release --locked --manifest-path "$root/tui/Cargo.toml" >/dev/null 2>&1
aiops="$work/venv/bin/aiops"
cd "$work/cwd"

step "2. every module is importable from the installed package"
"$work/venv/bin/python" - <<'PY'
import importlib, pkgutil, aiops
bad = []
for m in pkgutil.walk_packages(aiops.__path__, "aiops."):
    try:
        importlib.import_module(m.name)
    except Exception as e:                       # a missing subpackage once crashed the install
        bad.append(f"{m.name}: {e}")
assert not bad, bad
print("all aiops modules import")
PY

step "3. the SIMULATION golden scenario needs no configuration and touches nothing real"
"$aiops" demo --approve | tail -3 | grep -q "SIMULATION complete: incident inc_001 RESOLVED" \
  || fail "aiops demo --approve did not resolve"
"$aiops" demo --reject | grep -q "SIMULATION complete" || fail "aiops demo --reject"

step "4. no configuration: a clear message and a non-zero exit, not a traceback"
for cmd in doctor status stop; do
  set +e; out="$("$aiops" "$cmd" 2>&1)"; rc=$?; set -e
  [ "$rc" -ne 0 ] || fail "aiops $cmd succeeded with no configuration"
  echo "$out" | grep -q "no configuration found" || fail "aiops $cmd: unclear message: $out"
  echo "$out" | grep -q "Traceback" && fail "aiops $cmd printed a traceback"
done

step "5. the operator UI reports an unreachable control plane honestly (exit 3)"
set +e
"$root/tui/target/release/aiops-tui" --once --url http://127.0.0.1:9 >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 3 ] || fail "aiops-tui --once exited $rc, expected 3"

step "6. nothing was left behind: no state, PID, socket, simulation or fault-lease file"
left="$(find "$HOME" "$work/cwd" -type f \( -name '*.state.json*' -o -name '*.pid' -o -name '*.sock' \
        -o -name '*.fault.json' -o -name '*.db' -o -name 'aiops.toml' \) 2>/dev/null || true)"
[ -z "$left" ] || fail "left behind: $left"
# only processes of THIS run (their command line names the throwaway directory): the machine's
# owner may have an aiops of their own running
pgrep -f "$work" >/dev/null 2>&1 && fail "a process of this run is still running" || true

printf '\nclean-machine check: OK\n'
