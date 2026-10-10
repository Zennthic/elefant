"""The published embedder image must not carry the proprietary CUDA stack.

From plain PyPI, `pip install torch` on linux/amd64 drags in ~15 NVIDIA wheels
— 2.7GB of GPU runtime under NVIDIA's proprietary license. main.py loads the
model with an explicit device="cpu", so none of it ever runs, but it shipped in
a public image anyway: 5.2GB of site-packages instead of 1.5GB, and an artifact
that is not the wholly-permissive thing an Apache-2.0 project appears to offer.

The Dockerfile installs torch from PyTorch's CPU-only index to avoid that.
These guard the reasoning, because the failure is silent — nothing breaks, the
image just quietly grows a proprietary payload again.
"""
import re
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"
MAIN = Path(__file__).resolve().parent.parent / "main.py"


@pytest.fixture(scope="module")
def dockerfile():
    return DOCKERFILE.read_text()


def test_torch_comes_from_the_cpu_only_index(dockerfile):
    assert "--index-url https://download.pytorch.org/whl/cpu" in dockerfile
    assert re.search(r"--index-url\s+https://download\.pytorch\.org/whl/cpu\s+torch",
                     dockerfile), "the CPU index must be used for the torch install itself"


def test_the_cpu_index_is_not_merely_an_extra_index(dockerfile):
    """With both indexes in play pip is free to pick either torch, and which
    one wins comes down to version-sort rules — not something a published
    image should depend on."""
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" not in dockerfile


def test_torch_is_installed_before_sentence_transformers(dockerfile):
    """Order is the whole mechanism: sentence-transformers must find torch
    already satisfied, or it resolves its own from PyPI and the CUDA wheels
    come back."""
    torch_at = dockerfile.index("download.pytorch.org/whl/cpu")
    local_at = dockerfile.index("-r requirements-local.txt")
    assert torch_at < local_at


def test_build_fails_if_cuda_returns(dockerfile):
    """A torch bump or an index outage that fell back to PyPI would otherwise
    put 2.7GB of proprietary CUDA into a published image unnoticed."""
    assert "endswith('+cpu')" in dockerfile
    assert re.search(r"grep\s+-qiE\s+'\^\(nvidia-\|cuda-\)'", dockerfile), \
        "the build should refuse an image containing nvidia-* / cuda-* packages"


def test_the_slim_variant_still_skips_torch_entirely(dockerfile):
    assert "INCLUDE_LOCAL_EMBEDDINGS" in dockerfile
    assert "Slim build: skipping local embedding backend" in dockerfile


def test_the_code_really_does_pin_cpu():
    """The justification for dropping CUDA is that the model is loaded on the
    CPU explicitly. If that ever changes, this reasoning expires with it."""
    src = MAIN.read_text()
    assert 'device="cpu"' in src
    assert "torch.cuda" not in src, \
        "no GPU code path should exist while the image ships CPU-only torch"
