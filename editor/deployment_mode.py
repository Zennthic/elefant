"""
How this Elefant is deployed: as one organisation, or as a host for many.

ELEFANT_MODE selects the deployment shape. It is configuration, not a tier:
every feature works in both modes.

  single-org  (default) one organisation. The first user is its org admin.
  multi-org   a platform admin hosts many organisations, each its own tenant,
              with its own domains and wiki spaces, and the platform admin
              console and routes switched on.

Older deployments set ELEFANT_TIER instead. `ELEFANT_TIER=saas` still means
multi-org, with a deprecation warning. Any other ELEFANT_TIER value is ignored:
tiers no longer exist.
"""
import logging
import os

log = logging.getLogger("deployment_mode")

SINGLE_ORG = "single-org"
MULTI_ORG = "multi-org"
MODES = (SINGLE_ORG, MULTI_ORG)


def resolve_mode(environ) -> tuple[str, list[str]]:
    """The mode `environ` selects, and the warnings to log about how it got there.

    ELEFANT_MODE wins when it is set. Without it, the legacy ELEFANT_TIER=saas
    still selects multi-org, so an existing .env keeps working unchanged.
    """
    warnings: list[str] = []
    mode = (environ.get("ELEFANT_MODE") or "").strip().lower()
    tier = (environ.get("ELEFANT_TIER") or "").strip().lower()

    if mode and mode not in MODES:
        warnings.append(
            f"ELEFANT_MODE={mode!r} is not one of {', '.join(MODES)}; "
            f"running as {SINGLE_ORG}."
        )
        mode = SINGLE_ORG

    if tier == "saas":
        if not mode:
            mode = MULTI_ORG
        warnings.append(
            "ELEFANT_TIER=saas is deprecated; set ELEFANT_MODE=multi-org instead."
        )
    elif tier:
        warnings.append(
            f"ELEFANT_TIER={tier} is ignored: Elefant has no tiers, and every "
            "feature is on in every install."
        )

    return mode or SINGLE_ORG, warnings


ELEFANT_MODE, _warnings = resolve_mode(os.environ)
for _w in _warnings:
    log.warning(_w)
log.info("Deployment mode: %s", ELEFANT_MODE)


def is_multi_org() -> bool:
    """True when this deployment hosts many organisations."""
    return ELEFANT_MODE == MULTI_ORG
