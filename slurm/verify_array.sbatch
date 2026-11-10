#!/bin/bash
# Stage 8 for many repositories: apply the recommended plans to a clean copy
# of the checkout, compile, drop breaking steps (delta debugging), detect the
# smells again, write patches. One array task per config.
#
# Building with Maven/Gradle (DSARP_BUILD=auto) runs offline on compute nodes:
# download the dependencies once on a login node, e.g.
#   (cd $DSARP_WORK/tika && mvn -q dependency:go-offline)      # uses ~/.m2 or -Dmaven.repo.local
#
# Usage:
#   sbatch --array=1-$(wc -l < configs.list) -A <project> slurm/verify_array.sbatch
# Options: DSARP_BUILD (none|auto|maven|gradle), DSARP_MAX_COMPILES (30), DSARP_HEAP (8g),
#          DSARP_ARCAN_CMD / DSARP_DESIGNITE_CMD (detector commands with {repo} and {out}).

#SBATCH -J dsarp-verify
#SBATCH -p normal
#SBATCH -t 12:00:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH -o logs/verify-%A_%a.out

set -euo pipefail

: "${DSARP_WORK:?set DSARP_WORK to the folder with the checkouts}"
SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
LIST=${DSARP_CONFIG_LIST:-configs.list}

CONFIG=$(sed -n "${SLURM_ARRAY_TASK_ID}p" "$LIST")
NAME=$(basename "$CONFIG" .yaml)
mkdir -p logs "$HOME/.m2"

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}"

ARGS=(--workdir "$DSARP_WORK" --build "${DSARP_BUILD:-none}" --max-compiles "${DSARP_MAX_COMPILES:-30}"
      --memory "${DSARP_HEAP:-8g}")
[ -n "${DSARP_ARCAN_CMD:-}" ] && ARGS+=(--arcan-cmd "$DSARP_ARCAN_CMD")
[ -n "${DSARP_DESIGNITE_CMD:-}" ] && ARGS+=(--designite-cmd "$DSARP_DESIGNITE_CMD")

echo "[$(date -Is)] verify $NAME on $(hostname)"
apptainer exec --bind "$HOME/.m2" "$SIF" bash -c "
  dsarp verify '$RESULTS/$NAME' ${ARGS[*]@Q} && dsarp report '$RESULTS/$NAME'"
echo "[$(date -Is)] done"
