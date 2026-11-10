#!/bin/bash
# Plans with the language model (stages 5b and 7) on one A100 GPU node:
# starts a vLLM server with an open model, then runs `dsarp plan --search always
# --explain` for one repository against it. The model weights must be
# downloaded on a login node first (compute nodes may have no internet):
#
#   module load tools Apptainer
#   apptainer pull $PC2PFS/<project>/vllm.sif docker://vllm/vllm-openai:latest
#   export HF_HOME=$PC2PFS/<project>/hf
#   apptainer exec $PC2PFS/<project>/vllm.sif huggingface-cli download <model>
#
# Usage:
#   DSARP_REPO=tika DSARP_LLM_MODEL=<model> sbatch -A <project> slurm/plan_llm.sbatch

#SBATCH -J dsarp-plan-llm
#SBATCH -p gpu
#SBATCH --gres=gpu:a100:1
#SBATCH -t 06:00:00
#SBATCH -N 1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH -o logs/plan-llm-%j.out

set -euo pipefail

: "${DSARP_REPO:?set DSARP_REPO to the repository name (results/<name>)}"
: "${DSARP_LLM_MODEL:?set DSARP_LLM_MODEL to the model name or path}"
: "${VLLM_SIF:?set VLLM_SIF to the vLLM container image}"
SIF=${DSARP_SIF:-dsarp.sif}
RESULTS=${DSARP_RESULTS:-results}
PORT=${DSARP_LLM_PORT:-8000}
mkdir -p logs

module reset >/dev/null 2>&1 || true
module load tools Apptainer
# make the parallel file system (data, checkouts, results) visible inside the container
export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}${DSARP_BIND:-/scratch}"
export HF_HUB_OFFLINE=1

apptainer exec --nv "$VLLM_SIF" vllm serve "$DSARP_LLM_MODEL" --port "$PORT" --max-model-len 16384 \
    > "logs/vllm-$SLURM_JOB_ID.log" 2>&1 &
VLLM=$!
trap 'kill $VLLM 2>/dev/null || true' EXIT
for _ in $(seq 120); do
    curl -sf "http://localhost:$PORT/v1/models" >/dev/null && break
    sleep 10
done

export DSARP_LLM=openai DSARP_LLM_URL="http://localhost:$PORT/v1"
echo "[$(date -Is)] plan $DSARP_REPO with $DSARP_LLM_MODEL"
apptainer exec "$SIF" dsarp plan "$RESULTS/$DSARP_REPO" --groups "${DSARP_GROUPS:-20}" \
    --time-limit "${DSARP_TIME_LIMIT:-300}" --search always --search-time "${DSARP_SEARCH_TIME:-300}" --explain
echo "[$(date -Is)] done"
