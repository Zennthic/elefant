"""
Org domains and slugs: the CNAME verification contract, the role-domain
rule, promoting a primary domain, and turning a name into a slug.

Moved out of main.py by CQ-04 phase 5. The org admin and platform admin
routers use these, and so does main.py's invite flow, so they live here
rather than in any one of them.
"""
import os
import secrets
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from config_db import OrgDomain, Tenant, UserAccount


# ── Domain verification (CNAME-based, multi-domain) ────────────────────────
#
# The contract is: customer adds a CNAME record at elefant-verify.<domain>
# with target value <token>.<ELEFANT_DOMAIN_VERIFY_HOST>.  We do not need
# to host a wildcard A record at that target — we match the CNAME target
# *string* without resolving it.
#
# We deliberately use a leading-letter label (no underscore) because
# RFC 1035 hostnames forbid underscores, and several enterprise DNS
# providers (Cloudflare, Route 53) and corporate proxies will silently
# refuse or rewrite a CNAME at a label like '_elefant-verify'.
#
# The target never resolves, but it must still sit under a domain the
# operator controls: it is what customers put in their public DNS, and it
# reads as a service of whoever owns that domain. So the default is
# verify.<this deployment's own public domain>, never a name nobody here
# owns. An operator can still set ELEFANT_DOMAIN_VERIFY_HOST outright.


def _default_verify_host() -> str:
    """verify.<public domain>, from ELEFANT_PUBLIC_DOMAIN (the k8s base
    passes it) or the host of EDITOR_EXTERNAL_URL (compose and k8s both
    pass that). With neither set, a name under .invalid (RFC 2606): it can
    never resolve and nobody can register it."""
    from urllib.parse import urlsplit
    for name in ("ELEFANT_PUBLIC_DOMAIN", "EDITOR_EXTERNAL_URL"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        host = urlsplit(raw if "//" in raw else f"//{raw}").hostname
        if host:
            return f"verify.{host}"
    return "verify.elefant.invalid"


def _configured_verify_host() -> str:
    return (os.environ.get("ELEFANT_DOMAIN_VERIFY_HOST", "").strip()
            or _default_verify_host()).strip().lower().rstrip(".")


ELEFANT_DOMAIN_VERIFY_HOST = _configured_verify_host()


def _verify_target(token: str) -> str:
    """The full CNAME target the customer must enter (no trailing dot)."""
    return f"{token}.{ELEFANT_DOMAIN_VERIFY_HOST}"


def _verify_host(domain: str) -> str:
    """The host component the customer must add the CNAME under."""
    return f"elefant-verify.{domain}"


def _domain_to_dict(d: OrgDomain) -> dict:
    out = d.to_dict()
    out["verify_host"]   = _verify_host(d.domain)
    out["verify_target"] = _verify_target(d.verify_token)
    return out


def _validate_domain_format(domain: str) -> str:
    """Lowercase, strip, and reject obviously malformed labels.  Returns the cleaned value."""
    d = (domain or "").strip().lower().rstrip(".")
    if not d or "." not in d or len(d) > 253 or " " in d:
        raise HTTPException(status_code=422, detail="Invalid domain")
    # cheap label check: each label 1-63 chars, allowed [a-z0-9-], no leading/trailing -
    import re as _re
    for label in d.split("."):
        if not _re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label):
            raise HTTPException(status_code=422, detail=f"Invalid domain label: {label!r}")
    return d


def validate_role_domain(role: str, domain: str, org: Tenant, session: Session) -> None:
    """
    Enforce the role-domain rule: ``org_admin`` must live on the Org's
    primary domain.  ``team_admin`` and ``user`` may live on any verified
    domain (primary or secondary).  Raises HTTPException(400) on violation.
    Domains that aren't verified at all are also rejected — local-auth users
    cannot sign in via an unverified domain.
    """
    if role == "platform_admin":
        return  # no org affiliation; not our concern here
    if not domain:
        raise HTTPException(status_code=400, detail="Missing domain for non-platform user")
    primary = session.query(OrgDomain).filter_by(org_id=org.id, is_primary=True).first()
    if not primary:
        # Should never happen — every org has a primary row post-backfill
        raise HTTPException(status_code=500, detail="Org has no primary domain")
    if role == "org_admin" and domain != primary.domain:
        raise HTTPException(status_code=400,
            detail=f"org_admin must be on the primary domain ({primary.domain}); got {domain}")
    # team_admin / user: domain must exist as a verified row in this org
    row = session.query(OrgDomain).filter_by(
        org_id=org.id, domain=domain, verified=True
    ).first()
    if not row:
        raise HTTPException(status_code=400,
            detail=f"Domain '{domain}' is not a verified domain for this org")


def set_primary_domain(session: Session, org: Tenant, new_primary_id: str) -> OrgDomain:
    """
    Promote ``new_primary_id`` to primary for ``org``, demoting whoever
    currently holds the flag.  Updates ``Tenant.domain`` cache.  Caller
    is responsible for committing.  Refuses if any current org_admin
    would be left on a non-primary domain.
    """
    target = session.query(OrgDomain).filter_by(id=new_primary_id, org_id=org.id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Domain not found")
    if not target.verified:
        raise HTTPException(status_code=400, detail="Cannot promote an unverified domain")
    if target.is_primary:
        return target  # no-op

    # Block if any current org_admin's full_username domain is neither the
    # current nor the new primary — that admin would become orphaned.
    current_admins = session.query(UserAccount).filter_by(
        org_id=org.id, role="org_admin"
    ).all()
    for admin in current_admins:
        admin_domain = (admin.full_username or "").split("@", 1)[-1]
        if admin_domain != target.domain:
            raise HTTPException(status_code=400, detail=(
                f"Cannot promote: org_admin '{admin.full_username}' is not on the new "
                f"primary domain.  Move or delete them first."
            ))

    # Demote all current primaries (should be exactly one)
    session.query(OrgDomain).filter_by(org_id=org.id, is_primary=True).update(
        {"is_primary": False}
    )
    target.is_primary = True
    org.domain = target.domain
    org.domain_verified = bool(target.verified)
    org.domain_verify_token = target.verify_token
    return target



def _slugify(s: str) -> str:
    import re as _re
    s = (s or "").strip().lower()
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:63] or secrets.token_hex(4)



def _run_cname_check(host: str, expected_target: str) -> tuple[bool, list[str], Optional[str]]:
    """Returns (matched, found_targets, error)."""
    try:
        import dns.resolver
        answers = dns.resolver.resolve(host, "CNAME", lifetime=5.0)
        found = [r.target.to_text().lower().rstrip(".") for r in answers]
    except Exception as exc:
        return False, [], f"DNS lookup failed: {exc}"
    matched = expected_target.lower().rstrip(".") in found
    return matched, found, None
