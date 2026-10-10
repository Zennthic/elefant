"""Names from the environment Elefant is developed in. None of them may
appear in the public tree.

Each name is assembled from pieces, so this file and the tests that import
it do not match the repo-wide scan in test_no_internal_references.py.
"""
import re

_T = "t" + "sw"

# Host names and addresses of the development environment. The docs, the
# workflows and the Kubernetes base use reserved example names instead
# (example.com, 192.0.2.x).
LAB_HOST_MARKERS = (
    "thesmart" + "workshop",
    "10.90" + ".10",
    _T + "-k3s",
    _T + "-docker",
)

# Where our deployments live. The deploy host's directory, and any HOST under
# our own domain: `<label>.<domain>`. Contact addresses (`security@`,
# `conduct@`, `trademarks@`) and the licence URL sit on the bare domain and stay
# allowed: a host has a label before the domain, they do not. The same
# distinction test_install_docs.py draws for on-camera files.
_DOMAIN = r"zenn" + r"thic\.com"
DEPLOY_MARKERS = (
    "/home/" + "webservice",
)
DEPLOYMENT_HOST = re.compile(r"[a-z0-9-]+\." + _DOMAIN, re.IGNORECASE)

# The maintainer's published address (CODEOWNERS) is public on purpose. The
# first name on its own is still internal voice ("ask ...") and stays banned.
_MAINTAINER = r"(?!@" + _DOMAIN + r")"

# The repo-wide scan. `\b` before the short name keeps `startswith` out.
INTERNAL_NAMES = re.compile(
    r"\b" + _T + r"|smart ?work" + r"shop|\bj" + r"oe\b" + _MAINTAINER + r"|rock" + r"arm"
    r"|1pass" + r"word|authe" + r"lia"
    + "".join("|" + re.escape(m) for m in DEPLOY_MARKERS)
    + r"|" + DEPLOYMENT_HOST.pattern,
    re.IGNORECASE,
)
