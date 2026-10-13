"""The board's people directory: who can be a card's reporter or participant.

A card's `reporter` and `participants` are RESOLVED identities rather than
free text, and this module is where that resolution lives — the editor owns
identity (user_accounts, api_tokens), while memory-api owns cards. So the
store asks here before it writes, and every read that wants a NAME rather than
a reference asks here too.

A principal reference is `<kind>:<id>` over two identity spaces:

  user:<user_account.id>   a member of the tenant
  agent:<api_token.id>     an agent with its own account on the board

Two spaces because the board is already majority agent-written and the card
that specified this said so plainly: a card raised by a dispatch routine has a
real author that is not a human member. Modelling that author as a person with
no account, or as free text, would put the very first card a routine files
outside the field that exists to say who filed it.

An API token that is LINKED to a user resolves to that user, not to an agent.
That is the case of an agent working for a person: the identity behind the
call is that person's, so the card should name them. A standalone token — a routine's
own credential, linked to nobody — is the agent case, and resolves to itself.

The reference is opaque and stable; the display name is derived at read time
and never stored on the card. That is the whole point of the attribute: a name
copied onto a card rots the moment somebody is renamed, and two spellings of
one person split every filter in two.
"""
import re
from datetime import datetime
from typing import Optional

from config_db import ApiToken, Tenant, UserAccount

# `<kind>:<id>`. Ids are UUIDs today; the character class is deliberately a
# little wider so a future id shape doesn't have to be a migration.
PRINCIPAL_KINDS = ("user", "agent")
_REF_RE = re.compile(r"^(user|agent):([A-Za-z0-9][A-Za-z0-9._-]{0,63})$", re.IGNORECASE)

# How many candidates an "I could not resolve that" error is allowed to name.
# Enough to be useful on a small team, bounded so a big org doesn't return its
# whole staff list in an error string.
_CANDIDATE_LIMIT = 25


def parse_ref(value) -> Optional[tuple]:
    """`("user", "<id>")` for a well-formed principal reference, else None."""
    m = _REF_RE.match((value or "").strip())
    if not m:
        return None
    return (m.group(1).lower(), m.group(2))


def is_ref(value) -> bool:
    return parse_ref(value) is not None


def _user_display(u: UserAccount) -> str:
    """The best human name for a user, in the order a person would expect."""
    if u.display_name and u.display_name.strip():
        return u.display_name.strip()
    both = " ".join(p for p in [(u.firstname or "").strip(), (u.lastname or "").strip()] if p)
    if both:
        return both
    return u.username or u.full_username or u.id


def _user_record(u: UserAccount) -> dict:
    return {
        "ref":     f"user:{u.id}",
        "kind":    "user",
        "display": _user_display(u),
        "handle":  u.username or u.full_username or "",
        "source":  None,
        # `active` is how a card keeps naming somebody who has left. The
        # reference is retained either way — deactivating a member must never
        # silently rewrite the history of who raised what.
        "active":  bool(u.is_active),
        "known":   True,
    }


def _agent_record(t: ApiToken) -> dict:
    return {
        "ref":     f"agent:{t.id}",
        "kind":    "agent",
        "display": t.name or (t.source or "agent"),
        "handle":  t.source or "",
        "source":  t.source or "unknown",
        "active":  True,
        "known":   True,
    }


def _unknown_record(ref: str) -> dict:
    """A reference that no longer resolves.

    Deactivating a member keeps their row, so this is the hard-delete case:
    the card still points at a specific principal and still says a specific
    somebody raised it, but there is nobody left to name. Saying "Former
    member" is the honest rendering — inventing a name from a snapshot the
    card never stored would be worse, and blanking the field would be the
    "unresolve it" behaviour this design deliberately rejected.
    """
    kind = (parse_ref(ref) or ("user", ""))[0]
    return {
        "ref":     ref,
        "kind":    kind,
        "display": "Former member" if kind == "user" else "Retired agent",
        "handle":  "",
        "source":  None,
        "active":  False,
        "known":   False,
    }


def _tenant_for(session, tenant_slug: str) -> Optional[Tenant]:
    return session.query(Tenant).filter_by(slug=(tenant_slug or "").strip()).first()


def directory(session, tenant_slug: str) -> list:
    """Every principal that may be a card's reporter or participant, for this
    tenant: its members, then its agent accounts.

    The picker in the board UI is built from this, and so is the candidate
    list an unresolvable name is answered with. An expired token is left out —
    it can no longer act, so offering it as a NEW participant would be
    offering a dead identity; an existing reference to it still resolves
    through `describe`, which is what retaining history means.
    """
    tenant = _tenant_for(session, tenant_slug)
    if tenant is None:
        return []
    users = (session.query(UserAccount)
             .filter(UserAccount.org_id == tenant.id)
             .order_by(UserAccount.created_at).all())
    tokens = (session.query(ApiToken)
              .filter(ApiToken.tenant_id == tenant.id, ApiToken.user_id.is_(None))
              .order_by(ApiToken.created_at).all())
    now = datetime.utcnow()
    live = [t for t in tokens if not (t.expires_at and t.expires_at < now)]
    return [_user_record(u) for u in users] + [_agent_record(t) for t in live]


def describe(session, tenant_slug: str, refs) -> dict:
    """Resolve principal references to display records, for READING a card.

    Best-effort and total: every ref asked about comes back with a record, so
    a caller can render without branching, and a reference that no longer
    resolves renders as a former member rather than disappearing. Never
    raises — a card must be readable even when identity lookup is having a
    bad day.
    """
    wanted = [r for r in (refs or []) if (r or "").strip()]
    if not wanted:
        return {}
    tenant = _tenant_for(session, tenant_slug)
    out: dict = {}
    user_ids, agent_ids = set(), set()
    for ref in wanted:
        parsed = parse_ref(ref)
        if not parsed:
            out[ref] = _unknown_record(ref)
            continue
        (user_ids if parsed[0] == "user" else agent_ids).add(parsed[1])

    if tenant is not None and user_ids:
        for u in (session.query(UserAccount)
                  .filter(UserAccount.org_id == tenant.id,
                          UserAccount.id.in_(user_ids)).all()):
            out[f"user:{u.id}"] = _user_record(u)
    if tenant is not None and agent_ids:
        for t in (session.query(ApiToken)
                  .filter(ApiToken.tenant_id == tenant.id,
                          ApiToken.id.in_(agent_ids)).all()):
            out[f"agent:{t.id}"] = _agent_record(t)

    for ref in wanted:
        out.setdefault(ref, _unknown_record(ref))
    return out


def _match_candidates(entry: dict, needle: str) -> bool:
    """Does this directory entry answer to `needle`?

    EXACT matching only (case-insensitive), across the handful of names a
    person actually answers to. No prefix or substring matching on purpose:
    resolving "jo" to whichever of Jon and Joanna sorted first would put the
    wrong person on a card, and a card that names the wrong person is worse
    than one the caller has to name more precisely.
    """
    return needle in {v for v in (
        (entry.get("display") or "").strip().lower(),
        (entry.get("handle") or "").strip().lower(),
        (entry.get("email") or "").strip().lower(),
        (entry.get("full_username") or "").strip().lower(),
        # The bare id, so a caller that read `user:<id>` off a card can pass
        # the id back without having to reassemble the reference.
        (entry.get("ref") or "").split(":", 1)[-1].strip().lower(),
    ) if v}


def _resolvable_entries(session, tenant_slug: str) -> list:
    """The directory, plus the extra identifiers only matching cares about."""
    tenant = _tenant_for(session, tenant_slug)
    if tenant is None:
        return []
    entries = []
    for u in (session.query(UserAccount)
              .filter(UserAccount.org_id == tenant.id)
              .order_by(UserAccount.created_at).all()):
        rec = dict(_user_record(u))
        rec["email"] = u.email or ""
        rec["full_username"] = u.full_username or ""
        entries.append(rec)
    for t in (session.query(ApiToken)
              .filter(ApiToken.tenant_id == tenant.id, ApiToken.user_id.is_(None))
              .order_by(ApiToken.created_at).all()):
        entries.append(dict(_agent_record(t)))
    return entries


def resolve(session, tenant_slug: str, values) -> dict:
    """Resolve what a caller TYPED into canonical references, for WRITING.

    Strict, because this is the gate the card's promise rests on: a name that
    resolves to nobody is refused rather than stored, and a name that resolves
    to more than one person is refused rather than guessed. Both refusals name
    what to do instead — an error that says "no" without saying "who, then" is
    how a picker-less agent surface becomes unusable.

    Returns {"resolved": {input: ref}, "errors": {input: message}}.
    """
    wanted = [v for v in (values or []) if (v or "").strip()]
    resolved: dict = {}
    errors: dict = {}
    if not wanted:
        return {"resolved": resolved, "errors": errors}

    entries = _resolvable_entries(session, tenant_slug)
    by_ref = {e["ref"].lower(): e for e in entries}

    for raw in wanted:
        value = (raw or "").strip()
        parsed = parse_ref(value)
        if parsed:
            # A well-formed reference still has to name somebody who exists:
            # the store must never accept a reference it cannot resolve, or
            # the field silently becomes free text with a colon in it.
            canonical = f"{parsed[0]}:{parsed[1]}"
            hit = by_ref.get(canonical.lower())
            if hit:
                resolved[raw] = hit["ref"]
            else:
                errors[raw] = (f"{value!r} does not match anyone on this board. "
                               "It looks like a principal reference, but no "
                               "member or agent account has that id.")
            continue

        needle = value.lower()
        hits = [e for e in entries if _match_candidates(e, needle)]
        if len(hits) == 1:
            resolved[raw] = hits[0]["ref"]
        elif not hits:
            errors[raw] = (f"no member or agent account matches {value!r}. "
                           + _candidates_hint(entries))
        else:
            errors[raw] = (
                f"{value!r} matches {len(hits)} accounts "
                + ", ".join(f"{h['display']} (`{h['ref']}`)" for h in hits[:5])
                + ". Name one by its reference so the card records the right one.")
    return {"resolved": resolved, "errors": errors}


def _candidates_hint(entries: list) -> str:
    if not entries:
        return ("This board has no member or agent accounts to attribute a "
                "card to yet.")
    shown = entries[:_CANDIDATE_LIMIT]
    names = ", ".join(
        f"{e['display']}" + (f" (`{e['handle']}`)" if e.get("handle") else "")
        for e in shown)
    more = "" if len(entries) <= _CANDIDATE_LIMIT else f", and {len(entries) - _CANDIDATE_LIMIT} more"
    return f"Known accounts: {names}{more}."
