# Locked-down execution image for generated code.
#
# Bakes the MNIST dataset and pinned CPU-only dependencies so runtime needs ZERO
# network. Runs as a non-root user (uid 1000). The Runner spawns this image as a
# sibling container with network=none, cap_drop=ALL, read-only rootfs and resource
# limits. Audit metadata (python version, dataset fingerprint) is exposed as
# image labels; the build script populates them in a second cached pass.

ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PYPI_INDEX=https://pypi.org/simple
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
ARG DATASET_FINGERPRINT=""
ARG PYTHON_VERSION=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=8
# 为什么显式写超时与重试：2026-11-23 在这台大陆机房上，torch 本体从
# download.pytorch.org 下载是正常的，但 files.pythonhosted.org 会随机把一次读拖到
# 默认 15s 超时之外，整条 RUN 在跑了 40 分钟后才失败。重试与放宽超时让这种抖动
# 只影响速度，不影响构建结果。

RUN pip install --index-url ${PYPI_INDEX} numpy==1.26.4 pyyaml==6.0.2 \
    && pip install --index-url ${TORCH_INDEX} --extra-index-url ${PYPI_INDEX} \
        torch==2.4.1 torchvision==0.19.1

# Bake MNIST (train + test) into /data/mnist; runtime uses download=False.
RUN python -c "from torchvision import datasets; \
datasets.MNIST('/data/mnist', train=True, download=True); \
datasets.MNIST('/data/mnist', train=False, download=True)"

# Record dataset fingerprint (sha256 over all baked files) and python version.
RUN python -c "import hashlib, pathlib, sys; \
files=sorted(p for p in pathlib.Path('/data/mnist').rglob('*') if p.is_file()); \
h=hashlib.sha256(); \
[h.update(p.read_bytes()) for p in files]; \
pathlib.Path('/data/fingerprint.txt').write_text(h.hexdigest()); \
pathlib.Path('/data/python_version.txt').write_text(sys.version.split()[0])"

RUN groupadd -g 1000 runner && useradd -u 1000 -g 1000 -m runner

USER 1000:1000
WORKDIR /work/code

LABEL reproagent.python_version=${PYTHON_VERSION} \
      reproagent.dataset_fingerprint=${DATASET_FINGERPRINT} \
      reproagent.role="sandbox-exec"

CMD ["python", "train.py"]