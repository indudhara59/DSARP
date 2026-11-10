#!/bin/bash
# Stage 9 (research): compare solver, tree search, hybrid, NSGA-II and random
# search (and the language model, with DSARP_LLM=openai) on the top groups of
# each repository, plus the fixed-affinity ablation and weight sensitivity.
#
# Usage:
#   sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/evaluate_array.sbatch
# Options: DSARP_GROUPS (10), DSARP_TIME_LIMIT (120), DSARP_SEARCH_TIME (300), DSARP_BUDGET (2000),
#          DSARP_SEED (0)

#SBATCH -J dsarp-evaluate
#SBATCH -p normal
#SBATCH -t 24:00:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=16
#SBATCH --mem=32G
#SBATCH -o logs/evaluate-%A_%a.out

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
case ",${APPTAINER_BIND:-}," in
  *",${DSARP_BIND:-/scratch},"*) ;;
  *) export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}" ;;
esac

echo "[$(date -Is)] evaluate $NAME on $(hostname)"
apptainer exec "$SIF" dsarp evaluate "$RESULTS/$NAME" --groups "${DSARP_GROUPS:-10}" \
    --time-limit "${DSARP_TIME_LIMIT:-120}" --search-time "${DSARP_SEARCH_TIME:-300}" \
    --budget "${DSARP_BUDGET:-2000}" --seed "${DSARP_SEED:-0}" --ablation
apptainer exec "$SIF" dsarp report "$RESULTS/$NAME"
echo "[$(date -Is)] done"
