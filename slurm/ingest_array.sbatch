#!/bin/bash
# Stages 0-1 for many repositories at once: one array task per config file.
#
# Usage (from the repository root on Noctua 2):
#   ls configs/*.yaml > configs.list
#   sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/ingest_array.sbatch
#
# Needs: dsarp.sif built from containers/pipeline.def, and DSARP_DATA pointing
# to the folder with the Arcan/Designite output (see README).

#SBATCH -J dsarp-ingest
#SBATCH -p normal
#SBATCH -t 00:30:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=2
#SBATCH --mem-per-cpu=4G
#SBATCH -o logs/ingest-%A_%a.out

set -euo pipefail

: "${DSARP_DATA:?set DSARP_DATA to the detector data folder}"
SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
LIST=${DSARP_CONFIG_LIST:-configs.list}

CONFIG=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$LIST")
NAME=$(basename "$CONFIG" .yaml)
mkdir -p logs "$RESULTS"

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}"

echo "[$(date -Is)] ingest $CONFIG on $(hostname)"
apptainer exec --bind "$DSARP_DATA" "$SIF" \
    dsarp ingest "$CONFIG" --data-root "$DSARP_DATA" --out "$RESULTS/$NAME"
echo "[$(date -Is)] done"
