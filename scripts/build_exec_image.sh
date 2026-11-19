#!/usr/bin/env bash
# Build the locked-down execution image (baked MNIST + pinned CPU deps + non-root).
#
# Two passes: the first bakes the dataset and writes fingerprint/python-version
# files; the second (fully cached) stamps those values into image labels so the
# Runner can record them in the run audit. Override mirrors via env if needed:
#   PYTHON_IMAGE / PYPI_INDEX / TORCH_INDEX / EXEC_IMAGE
set -euo pipefail
cd "$(dirname "$0")/.."

# Git Bash/MSYS rewrites leading-slash container paths (e.g. /data/...) into Windows
# paths; disable that so `docker run ... cat /data/fingerprint.txt` works.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"

IMAGE="${EXEC_IMAGE:-reproagent-exec:latest}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-slim}"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.org/simple}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cpu}"
DF=infra/docker/exec.Dockerfile
CTX=infra/docker

build() {
  docker build -f "$DF" \
    --build-arg PYTHON_IMAGE="$PYTHON_IMAGE" \
    --build-arg PYPI_INDEX="$PYPI_INDEX" \
    --build-arg TORCH_INDEX="$TORCH_INDEX" \
    "$@" -t "$IMAGE" "$CTX"
}

echo "==> pass 1: bake dataset + pinned dependencies"
build

FP="$(docker run --rm --entrypoint cat "$IMAGE" /data/fingerprint.txt)"
PY="$(docker run --rm --entrypoint cat "$IMAGE" /data/python_version.txt)"
echo "==> dataset fingerprint: $FP"
echo "==> python version:      $PY"

echo "==> pass 2: stamp audit labels (cached)"
build --build-arg DATASET_FINGERPRINT="$FP" --build-arg PYTHON_VERSION="$PY"

echo "==> built $IMAGE"