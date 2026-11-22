#!/bin/bash
# Stage 8 for one repository, split over several jobs: every array task verifies every n-th group (project
# build and tests included), so a long verification finishes in a fraction of the time.
#
# Once, on a login node (internet):  dsarp prepare-build results/<name> --workdir work
#
# Usage (from the DSARP folder; 8 shards):
#   export DSARP_WORK=/scratch/<project>/<user>/dsarp/work DSARP_MAVEN_REPO=/scratch/<project>/<user>/maven-repository
#   J=$(sbatch --parsable --array=0-7 -A <project> --export=ALL,DSARP_NAME=struts,DSARP_SHARDS=8 slurm/verify_shards.sbatch)
#   sbatch -A <project> --dependency=afterany:$J --export=ALL,DSARP_NAME=struts,DSARP_MERGE=1 slurm/verify_shards.sbatch
# The second job merges the shards into results/<name>/verify/verification.json.
#
# Options: DSARP_BUILD (auto), DSARP_MAX_COMPILES (30), DSARP_HEAP (8g), DSARP_TEST_TIMEOUT (1800 s).

#SBATCH -J dsarp-verify-shard
#SBATCH -p normal
#SBATCH -t 12:00:00
#SBATCH -N 1
#SBATCH -n 1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH -o logs/verify-shard-%A_%a.out

set -euo pipefail

: "${DSARP_NAME:?set DSARP_NAME to the project name (results/<name>)}"
SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
mkdir -p logs

module reset >/dev/null 2>&1 || true
module load tools Apptainer
case ",${APPTAINER_BIND:-}," in
  *",${DSARP_BIND:-/scratch},"*) ;;
  *) export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}" ;;
esac

if [ -n "${DSARP_MERGE:-}" ]; then
    echo "[$(date -Is)] merging the verification shards of $DSARP_NAME"
    apptainer exec "$SIF" dsarp verify "$RESULTS/$DSARP_NAME" --merge
    exit 0
fi

: "${DSARP_WORK:?set DSARP_WORK to the folder with the checkouts}"
: "${DSARP_SHARDS:?set DSARP_SHARDS to the number of array tasks}"
echo "[$(date -Is)] verify $DSARP_NAME shard $SLURM_ARRAY_TASK_ID/$DSARP_SHARDS on $(hostname)"
apptainer exec "$SIF" dsarp verify "$RESULTS/$DSARP_NAME" --workdir "$DSARP_WORK" \
    --shard "$SLURM_ARRAY_TASK_ID/$DSARP_SHARDS" --build "${DSARP_BUILD:-auto}" \
    --max-compiles "${DSARP_MAX_COMPILES:-30}" --memory "${DSARP_HEAP:-8g}"
