#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
EDU_CONDA_EXE="${EDU_CONDA_EXE:-/default-filesys/workspace/ysb/miniconda3/bin/conda}"
export CONDA_PKGS_DIRS="$PWD/.cache/conda"
export PIP_CACHE_DIR="$PWD/.cache/pip"
export TMPDIR="$PWD/.cache/tmp"
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
mkdir -p "$TMPDIR" logs reports models data
for name in harness vllm; do
  edu_python_version=3.12
  if [ "$name" = harness ]; then edu_python_version=3.11; fi
  if [ ! -x ".envs/$name/bin/python" ]; then
    "$EDU_CONDA_EXE" create -y -p "$PWD/.envs/$name" "python=$edu_python_version" pip
  fi
done
.envs/harness/bin/python -m pip install -r requirements-harness.lock
.envs/vllm/bin/python -m pip install -r requirements-vllm.lock
.envs/harness/bin/python -m pip check
.envs/vllm/bin/python -m pip check
