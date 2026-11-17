#!/bin/bash
# Serves an open language model with vLLM on one Noctua 2 GPU node, as an OpenAI-compatible
# endpoint for DSARP's design review, design advice, search and explanations.
#
# One-time setup on a login node (compute nodes have no internet):
#
#   module load tools Apptainer
#   cd /scratch/<project>/<user>
#   apptainer pull vllm.sif docker://vllm/vllm-openai:latest
#   export HF_HOME=$PWD/hf
#   apptainer exec vllm.sif hf download openai/gpt-oss-120b       # about 65 GB (OpenAI's open-weight model)
#   (optional) apptainer exec vllm.sif hf download Qwen/Qwen3.6-27B   # about 55 GB, an alternative
#
# Start (from the DSARP folder):
#
#   VLLM_SIF=/scratch/<project>/<user>/vllm.sif HF_HOME=/scratch/<project>/<user>/hf \
#       sbatch -A <project> slurm/llm_server.sbatch
#
# When the model is ready, the job writes ~/.dsarp/llm.env (readable only by you) with
# DSARP_LLM, DSARP_LLM_URL, DSARP_LLM_MODEL and DSARP_LLM_KEY. Load it before starting the
# web interface or a CLI command:
#
#   source ~/.dsarp/llm.env
#
# The key is a random one made for this job only: the server runs on a shared cluster
# network, and the key keeps other users from using your GPUs. Stop with `scancel <jobid>`.
#
# Options (environment variables):
#   DSARP_LLM_MODEL   model id or path            (default openai/gpt-oss-120b; also openai/gpt-oss-20b,
#                                                  Qwen/Qwen3.6-27B)
#   DSARP_LLM_REASONING  reasoning effort of gpt-oss: low, medium, high (default medium)
#   DSARP_LLM_GPUS    GPUs, as tensor parallelism (default 4; must match --gres below)
#   DSARP_LLM_PORT    port                        (default 8000)
#   DSARP_LLM_CTX     maximum context length      (default 32768)
#   VLLM_EXTRA        further vllm serve options  (default: --reasoning-parser qwen3 for Qwen models)
#
# gpt-oss runs on A100 with vLLM's Triton attention and Marlin MXFP4 kernels; tensor parallelism 4
# uses the whole node for many requests at once.

#SBATCH -J dsarp-llm
#SBATCH -p gpu
#SBATCH --gres=gpu:a100:4
#SBATCH -N 1
#SBATCH --cpus-per-task=32
#SBATCH --mem=200G
#SBATCH -t 1-00:00:00
#SBATCH -o logs/llm-server-%j.out

set -euo pipefail

: "${VLLM_SIF:?set VLLM_SIF to the vLLM container image (see the setup above)}"
: "${HF_HOME:?set HF_HOME to the folder the model was downloaded to}"
MODEL=${DSARP_LLM_MODEL:-openai/gpt-oss-120b}
GPUS=${DSARP_LLM_GPUS:-4}
PORT=${DSARP_LLM_PORT:-8000}
CTX=${DSARP_LLM_CTX:-32768}
case "$MODEL" in
  *Qwen3*|*qwen3*) DEFAULT_EXTRA="--reasoning-parser qwen3" ;;
  *) DEFAULT_EXTRA="" ;;
esac
EXTRA=${VLLM_EXTRA-$DEFAULT_EXTRA}
# reasoning models count their reasoning in the token budget: give them room
case "$MODEL" in
  *gpt-oss*) MAX_TOKENS=8000; REASONING_LINE="export DSARP_LLM_REASONING=${DSARP_LLM_REASONING:-medium}" ;;
  *Qwen3*|*qwen3*) MAX_TOKENS=8000; REASONING_LINE="" ;;
  *) MAX_TOKENS=1500; REASONING_LINE="" ;;
esac
mkdir -p logs "$HOME/.dsarp"
chmod 700 "$HOME/.dsarp"

module reset >/dev/null 2>&1 || true
module load tools Apptainer
case ",${APPTAINER_BIND:-}," in
  *",/scratch,"*) ;;
  *) export APPTAINER_BIND="${APPTAINER_BIND:+$APPTAINER_BIND,}/scratch" ;;
esac
export HF_HOME HF_HUB_OFFLINE=1

KEY=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
HOST=$(hostname)
ENV_FILE="$HOME/.dsarp/llm.env"
rm -f "$ENV_FILE"

echo "[$(date -Is)] starting vLLM: $MODEL on $GPUS GPUs at $HOST:$PORT"
# the key is passed through the environment, so it does not show in the process list
VLLM_API_KEY=$KEY apptainer exec --nv "$VLLM_SIF" vllm serve "$MODEL" \
    --host 0.0.0.0 --port "$PORT" --tensor-parallel-size "$GPUS" \
    --max-model-len "$CTX" --gpu-memory-utilization 0.90 --served-model-name "$MODEL" $EXTRA &
VLLM=$!
trap 'kill $VLLM 2>/dev/null || true; rm -f "$ENV_FILE"' EXIT

for _ in $(seq 180); do                      # loading takes a few minutes
    if curl -sf -H "Authorization: Bearer $KEY" "http://localhost:$PORT/v1/models" >/dev/null; then
        umask 077
        cat > "$ENV_FILE" <<EOF
# DSARP language model on $HOST (Slurm job $SLURM_JOB_ID, $MODEL); removed when the job ends
export DSARP_LLM=openai
export DSARP_LLM_URL=http://$HOST:$PORT/v1
export DSARP_LLM_MODEL=$MODEL
export DSARP_LLM_KEY=$KEY
export DSARP_LLM_TIMEOUT=600
export DSARP_LLM_WORKERS=8
export DSARP_LLM_MAX_TOKENS=$MAX_TOKENS
$REASONING_LINE
EOF
        echo "[$(date -Is)] ready: source ~/.dsarp/llm.env"
        break
    fi
    if ! kill -0 $VLLM 2>/dev/null; then
        echo "[$(date -Is)] vLLM stopped while loading; see the messages above"
        exit 1
    fi
    sleep 10
done
[ -f "$ENV_FILE" ] || { echo "[$(date -Is)] the model did not become ready within 30 minutes"; exit 1; }
wait $VLLM
