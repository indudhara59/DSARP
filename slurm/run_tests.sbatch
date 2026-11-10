#!/bin/bash
# Runs the test suite inside the container on a compute node, including the
# checks against the real detector output when DSARP_DATA is set.
#
# Usage: sbatch -A <project> slurm/run_tests.sbatch

#SBATCH -J dsarp-tests
#SBATCH -p normal
#SBATCH -t 00:20:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=2
#SBATCH --mem-per-cpu=4G
#SBATCH -o logs/tests-%j.out

set -euo pipefail
SIF=${DSARP_SIF:-dsarp.sif}
mkdir -p logs

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}"

BIND=""
if [ -n "${DSARP_DATA:-}" ]; then BIND="--bind $DSARP_DATA"; fi
apptainer exec $BIND "$SIF" python3 -m pytest -q
