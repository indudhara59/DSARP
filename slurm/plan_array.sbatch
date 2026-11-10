#!/bin/bash
# Refactoring plans (stages 5a and 6) for many repositories: one array task per
# config. Needs the results of slurm/model_array.sbatch (model/ and triage/).
# CP-SAT (installed in the container) uses all CPUs of the task.
#
# Usage (repository root, Noctua 2):
#   ls configs/*.yaml > configs.list
#   sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/plan_array.sbatch
# Options (environment): DSARP_GROUPS (default 20), DSARP_TIME_LIMIT seconds per
# plan (default 300), DSARP_BACKEND (auto, cpsat, highs).

#SBATCH -J dsarp-plan
#SBATCH -p normal
#SBATCH -t 12:00:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=16
#SBATCH --mem=32G
#SBATCH -o logs/plan-%A_%a.out

set -euo pipefail

SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
LIST=${DSARP_CONFIG_LIST:-configs.list}

CONFIG=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$LIST")
NAME=$(basename "$CONFIG" .yaml)
mkdir -p logs

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}"

echo "[$(date -Is)] plan $NAME on $(hostname) with $SLURM_CPUS_PER_TASK CPUs"
apptainer exec "$SIF" dsarp plan "$RESULTS/$NAME" \
    --groups "${DSARP_GROUPS:-20}" --time-limit "${DSARP_TIME_LIMIT:-300}" --backend "${DSARP_BACKEND:-auto}" --explain
echo "[$(date -Is)] done"
