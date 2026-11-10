#!/bin/bash
# Stages 0-3 (import, program model, triage) for many repositories: one array
# task per config. Set DSARP_CALIBRATE=1 to also compare the in-memory
# detection with the detectors (slower).
# The checkouts must exist already: run `dsarp checkout` on a login node
# (compute nodes may have no internet access).
#
# Usage (repository root, Noctua 2):
#   apptainer exec dsarp.sif dsarp checkout configs/*.yaml --workdir $PC2PFS/<project>/work   # login node
#   ls configs/*.yaml > configs.list
#   sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/model_array.sbatch

#SBATCH -J dsarp-stages0-3
#SBATCH -p normal
#SBATCH -t 02:00:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=4
#SBATCH --mem=24G
#SBATCH -o logs/model-%A_%a.out

set -euo pipefail

: "${DSARP_DATA:?set DSARP_DATA to the detector data folder}"
: "${DSARP_WORK:?set DSARP_WORK to the folder with the checkouts}"
SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
LIST=${DSARP_CONFIG_LIST:-configs.list}
HEAP=${DSARP_HEAP:-16g}

CONFIG=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$LIST")
NAME=$(basename "$CONFIG" .yaml)
mkdir -p logs "$RESULTS/$NAME"

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
case ",${APPTAINER_BIND:-}," in
  *",${DSARP_BIND:-/scratch},"*) ;;
  *) export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}" ;;
esac

echo "[$(date -Is)] model $CONFIG on $(hostname)"
apptainer exec "$SIF" bash -c "
  dsarp ingest '$CONFIG' --data-root '$DSARP_DATA' --out '$RESULTS/$NAME' &&
  dsarp model '$CONFIG' --data-root '$DSARP_DATA' --workdir '$DSARP_WORK' \
        --out '$RESULTS/$NAME/model' --ingest '$RESULTS/$NAME' --memory '$HEAP' &&
  dsarp triage '$RESULTS/$NAME' &&
  if [ '${DSARP_CALIBRATE:-0}' = 1 ]; then dsarp calibrate '$RESULTS/$NAME'; fi"
echo "[$(date -Is)] done"
