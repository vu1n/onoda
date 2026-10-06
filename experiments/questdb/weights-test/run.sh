#!/usr/bin/env bash
# Which state-weight syntax does ORCA 6.1.1 accept in %casscf? (gate 2, #36)
#
# Runs one small SA-CASSCF(6,5)/def2-SVP on formaldehyde (QUESTDB geometry, the
# fixture formaldehyde_nevpt2_np4) per candidate, serially on one core, and
# prints whether each input parsed, whether ORCA terminated normally, and the
# block and root weights it used. Blocks are mult 1 (3 roots) and mult 3
# (2 roots); equal per-root weights are 0.2 each. No NEVPT2, no model, no broker.
# Usage: experiments/questdb/weights-test/run.sh [WORK]  (default /tmp/qwt)
set -euo pipefail
ORCA_HOME="${ORCA_HOME:-$HOME/code/onoda/orca-6.1.1}"
[ -x "$ORCA_HOME/orca" ] || { echo "no ORCA at $ORCA_HOME/orca" >&2; exit 2; }
WORK="${1:-/tmp/qwt}"
mkdir -p "$WORK"

declare -a NAMES=(none bweight_eq bweight_sp bweight_idx weights_idx)
declare -a LINES=(
  ""
  "  bweight = 3,2"
  "  bweight 3,2"
  "  bweight[0] = 3,2"
  "  weights[0] = 0.333333,0.333333,0.333333"
)

for i in "${!NAMES[@]}"; do
  name="${NAMES[$i]}"; dir="$WORK/$name"; mkdir -p "$dir"
  {
    echo "! def2-SVP TightSCF"
    echo "%maxcore 2000"
    echo "%casscf"
    echo "  nel 6"
    echo "  norb 5"
    echo "  mult 1,3"
    echo "  nroots 3,2"
    [ -n "${LINES[$i]}" ] && echo "${LINES[$i]}"
    echo "  maxiter 150"
    echo "end"
    echo "* xyz 0 1"
    echo "C 0.00000000 0.00000000 -0.60298484"
    echo "O 0.00000000 0.00000000 0.60539374"
    echo "H 0.00000000 0.93467276 -1.18217429"
    echo "H 0.00000000 -0.93467276 -1.18217429"
    echo "*"
  } > "$dir/job.inp"
  (cd "$dir" && "$ORCA_HOME/orca" job.inp > job.out 2>&1) || true
  echo "== $name: ${LINES[$i]:-(no weight line)}"
  grep -m3 -i "Last token\|Unknown identifier\|INPUT ERROR\|aborting" "$dir/job.out" || true
  grep -m1 "ORCA TERMINATED NORMALLY" "$dir/job.out" || echo "  (no normal termination)"
  grep -m7 -E "^BLOCK +[0-9]+ WEIGHT=|^ +ROOT=[0-9]+ WEIGHT=" "$dir/job.out" | sed 's/^/  /' || true
done
