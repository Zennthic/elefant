FROM python:3.11.17-slim-trixie

# INCLUDE_LOCAL_EMBEDDINGS=true (default) → "full" image with the on-box
# embedding backend (sentence-transformers + CPU-only torch, ~1.5GB of
# site-packages). Build with --build-arg INCLUDE_LOCAL_EMBEDDINGS=false for the
# "slim" image used by deployments that point at an external/custom embedding
# API — it drops ~1.2GB of torch that such deployments never import. The
# service's own API (search / memory / reindex) is identical in both variants;
# only the local embedding code path (and its clear runtime error, see main.py)
# differs.
ARG INCLUDE_LOCAL_EMBEDDINGS=true

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Run as a fixed, non-root UID/GID (10001). The number matters more than the
# name: the fix-perms step in docker-compose.yml and the k8s init containers
# chown existing (root-owned) volumes to exactly this, and k8s' runAsUser and
# fsGroup name it too. Change all of them together or not at all.
RUN groupadd --system --gid 10001 elefant \
 && useradd --system --uid 10001 --gid 10001 --home-dir /home/elefant --create-home --shell /usr/sbin/nologin elefant

# See editor/Dockerfile: existing volumes may still be root-owned for one boot.
RUN git config --system --add safe.directory '*'

# The model cache lives under the non-root user's home, not /root (which the
# user cannot traverse). Compose and k8s mount the hf_cache volume here.
ENV HF_HOME=/home/elefant/.cache/huggingface

WORKDIR /app

COPY requirements.txt requirements-local.txt ./
# torch comes from PyTorch's CPU-ONLY index, deliberately, and must be installed
# BEFORE sentence-transformers so that pulls it in already satisfied.
#
# From plain PyPI, `pip install torch` on linux/amd64 drags in ~15 NVIDIA CUDA
# wheels — 2.7GB of GPU runtime under NVIDIA's proprietary license, in an image
# we publish publicly. main.py loads the model with an explicit device="cpu"
# (see _build_backend), so not one byte of it is ever executed. Dropping it
# takes the full image from ~5.2GB to ~1.5GB of site-packages and leaves the
# published artifact wholly permissively licensed.
#
# --index-url, not --extra-index-url: with both indexes in play pip is free to
# pick either torch, and "which one won" is not something a build should leave
# to version-sort rules. Two explicit installs, one index each.
#
# torch is pinned here rather than in requirements-local.txt because it comes
# from a different index. Dependabot cannot see this line, so bump it by hand.
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt \
    && if [ "$INCLUDE_LOCAL_EMBEDDINGS" = "true" ]; then \
         pip install --no-cache-dir --timeout 120 --retries 5 \
             --index-url https://download.pytorch.org/whl/cpu torch==2.14.0 ; \
         pip install --no-cache-dir --timeout 120 --retries 5 -r requirements-local.txt ; \
       else \
         echo "Slim build: skipping local embedding backend (sentence-transformers/torch)"; \
       fi

# Fail the build rather than ship the GPU stack by accident: a torch version
# bump or an index outage that silently fell back to PyPI would otherwise put
# 2.7GB of proprietary CUDA back into a published image without anyone noticing.
RUN if [ "$INCLUDE_LOCAL_EMBEDDINGS" = "true" ]; then \
      python -c "import torch, sys; \
v = torch.__version__; \
print('torch', v); \
sys.exit(0 if v.endswith('+cpu') else 1)" \
      || { echo 'ERROR: torch is not the +cpu build — the CUDA wheels are back.'; exit 1; }; \
      ! pip list 2>/dev/null | grep -qiE '^(nvidia-|cuda-)' \
      || { echo 'ERROR: NVIDIA CUDA packages present in the image.'; pip list | grep -iE '^(nvidia-|cuda-)'; exit 1; }; \
    fi

COPY main.py .

RUN mkdir -p /docs /backend_sync /git-sources "$HF_HOME" \
 && chown -R elefant:elefant /docs /backend_sync /git-sources /home/elefant/.cache

USER 10001:10001

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]


