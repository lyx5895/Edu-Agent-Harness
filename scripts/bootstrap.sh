#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export CONDA_PKGS_DIRS="$PWD/.cache/conda"
export PIP_CACHE_DIR="$PWD/.cache/pip"
mkdir -p logs reports models data .cache
for name in inference harness; do
  if [ ! -x ".envs/$name/bin/python" ]; then
    conda create -y -p "$PWD/.envs/$name" python=3.11 pip
  fi
done
.envs/inference/bin/pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
.envs/inference/bin/pip install -r requirements-inference.lock --extra-index-url https://download.pytorch.org/whl/cu128
.envs/harness/bin/pip install -r requirements-harness.lock
.envs/inference/bin/pip check
.envs/harness/bin/pip check
