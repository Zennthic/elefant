"""The editor's route table survives CQ-04's split of main.py into routers.

CQ-04 moves handlers out of main.py without changing a single URL. These
tests hold it to that: `route_table_cq04.json` is the table as it was before
the first router was carved out, and every path + method in it must still be
served. A route that is removed on purpose has to be removed from the
snapshot too, in the same change, so the removal is a decision a reviewer
sees rather than a side effect of a move.

Routes added since the snapshot are fine; this only checks nothing went
missing.
"""
import json
import re
from pathlib import Path

import pytest

from served_routes import served_routes

_SNAPSHOT = Path(__file__).parent / "route_table_cq04.json"


def _table(app):
    return {(r.path, m) for r in served_routes(app) for m in (getattr(r, "methods", None) or [""])}


def _snapshot():
    return {(row["path"], m) for row in json.loads(_SNAPSHOT.read_text())
            for m in (row["methods"] or [""])}


def test_every_route_from_before_the_split_is_still_served():
    import main
    missing = sorted(_snapshot() - _table(main.app))
    assert not missing, f"routes no longer served: {missing}"


# What CQ-04 phase 1 moved, and where it went.
OAUTH_ROUTES = [
    ("/.well-known/oauth-authorization-server", "GET"),
    ("/oauth/register", "POST"),
    ("/oauth/authorize", "GET"),
    ("/oauth/authorize", "POST"),
    ("/oauth/token", "POST"),
    ("/oauth/revoke", "POST"),
]
SSO_ROUTES = [
    ("/api/admin/identity-providers", "GET"),
    ("/api/admin/identity-providers/oidc", "POST"),
    ("/api/admin/identity-providers/oidc/{provider_id}", "PATCH"),
    ("/api/admin/identity-providers/oidc/{provider_id}", "DELETE"),
    ("/api/admin/identity-providers/oidc/{provider_id}/test", "POST"),
    ("/api/admin/identity-providers/saml", "POST"),
    ("/api/admin/identity-providers/saml/{provider_id}", "PATCH"),
    ("/api/admin/identity-providers/saml/{provider_id}", "DELETE"),
    ("/auth/{tenant_slug}/providers", "GET"),
    ("/auth/oidc/{provider_id}/login", "GET"),
    ("/auth/oidc/{provider_id}/callback", "GET"),
    ("/auth/saml/{provider_id}/login", "GET"),
    ("/auth/saml/{provider_id}/acs", "POST"),
    ("/auth/saml/{provider_id}/metadata", "GET"),
]

# What CQ-04 phase 2 moved: the wiki's session-authed web API.
WIKI_ROUTES = [
    ("/api/wiki/share", "POST"),
    ("/api/wiki/shares", "GET"),
    ("/api/wiki/share/{share_id}", "DELETE"),
    ("/api/public/share/{token}", "GET"),
    ("/api/public/share/{token}/file", "GET"),
    ("/api/public/share/{token}/image", "GET"),
    ("/api/tree", "GET"),
    ("/api/file", "GET"),
    ("/api/file", "PUT"),
    ("/api/file", "POST"),
    ("/api/file", "DELETE"),
    ("/api/wiki/history", "GET"),
    ("/api/wiki/revision", "GET"),
    ("/api/wiki/revert", "POST"),
    ("/api/wiki/lock", "GET"),
    ("/api/wiki/lock", "POST"),
    ("/api/mkdir", "POST"),
    ("/api/recycle", "POST"),
    ("/api/recycle-bin", "GET"),
    ("/api/recycle-bin/{item_id}/restore", "POST"),
    ("/api/recycle-bin/{item_id}/archive", "POST"),
    ("/api/recycle-bin/{item_id}", "DELETE"),
    ("/api/move-file", "POST"),
    ("/api/rename-dir", "POST"),
    ("/api/directory", "DELETE"),
    ("/api/image", "GET"),
    ("/api/upload-image", "POST"),
    ("/api/upload-file", "POST"),
    ("/api/attachment", "GET"),
]

# What CQ-04 phase 3 moved: the board's web API, its live stream, and the
# peer-service board endpoints the MCP server calls.
BOARD_ROUTES = [
    ("/api/board/stream", "GET"),
    ("/api/board/columns", "GET"),
    ("/api/board/columns", "POST"),
    ("/api/board/columns/{column_id}", "PATCH"),
    ("/api/board/columns/{column_id}/archive", "POST"),
    ("/api/board/people", "GET"),
    ("/api/internal/board/people", "POST"),
    ("/api/board/labels", "GET"),
    ("/api/board/cards", "GET"),
    ("/api/board/cards", "POST"),
    ("/api/board/cards/{card_slug}", "GET"),
    ("/api/board/cards/{card_slug}/comments", "GET"),
    ("/api/board/cards/{card_slug}/comments", "POST"),
    ("/api/board/cards/{card_slug}/comments/{comment_id}", "PATCH"),
    ("/api/board/cards/{card_slug}/comments/{comment_id}", "DELETE"),
    ("/api/internal/board/wiki-pages", "POST"),
    ("/api/board/wiki-pages", "GET"),
    ("/api/board/cards/{card_slug}/status", "GET"),
    ("/api/board/wiki-page-cards", "GET"),
    ("/api/internal/board/page-cards", "POST"),
    ("/api/board/cards/{card_slug}/move", "PATCH"),
    ("/api/board/cards/{card_slug}", "PATCH"),
    ("/api/board/cards/{card_slug}/link", "POST"),
    ("/api/board/next-card", "GET"),
    ("/api/board/cards/{card_slug}/archive", "POST"),
    ("/api/board/cards/{card_slug}/unarchive", "POST"),
    ("/api/board/cards/{card_slug}/wiki", "POST"),
]

# What CQ-04 phase 4 moved: import/export and the memory and wiki backups
# (the org-admin surface), then org snapshots, restore and the supervised
# restore jobs (the platform surface). Both routers live in routers.backup.
BACKUP_ROUTES = [
    ("/api/admin/import", "POST"),
    ("/api/export/page", "GET"),
    ("/api/export/kb", "GET"),
    ("/api/admin/memories/export", "GET"),
    ("/api/admin/memories/auto-backup", "GET"),
    ("/api/admin/memories/auto-backup", "POST"),
    ("/api/admin/memories/backup-now", "POST"),
    ("/api/admin/backup-passcode", "GET"),
    ("/api/admin/backup-passcode", "POST"),
    ("/api/admin/backup-status", "GET"),
    ("/api/admin/memories/backups", "GET"),
    ("/api/admin/memories/backups/{backup_id}/download", "GET"),
    ("/api/admin/memories/backups/upload", "POST"),
    ("/api/admin/restore-jobs/{job_id}", "GET"),
    ("/api/admin/memories/restore", "POST"),
    ("/api/admin/wiki/auto-backup", "GET"),
    ("/api/admin/wiki/auto-backup", "POST"),
    ("/api/admin/wiki/backup-now", "POST"),
    ("/api/admin/wiki/backups", "GET"),
    ("/api/admin/wiki/export", "GET"),
    ("/api/admin/wiki/restore", "POST"),
]
PLATFORM_BACKUP_ROUTES = [
    ("/api/platform/backup-config", "GET"),
    ("/api/platform/backup-config", "POST"),
    ("/api/platform/backup-passcode", "GET"),
    ("/api/platform/backup-passcode", "POST"),
    ("/api/platform/backup-status", "GET"),
    ("/api/platform/backup-config/test", "POST"),
    ("/api/platform/ssh-target", "GET"),
    ("/api/platform/ssh-target", "POST"),
    ("/api/platform/ssh-target/test", "POST"),
    ("/api/platform/orgs/{org_id}/backup-schedule", "GET"),
    ("/api/platform/orgs/{org_id}/backup-schedule", "POST"),
    ("/api/platform/orgs/{org_id}/backup", "POST"),
    ("/api/platform/orgs/{org_id}/backups", "GET"),
    ("/api/platform/orgs/{org_id}/backups/{backup_id}/download", "GET"),
    ("/api/platform/orgs/restore-from-upload", "POST"),
    ("/api/platform/restore-jobs", "GET"),
    ("/api/platform/restore-jobs/{job_id}", "GET"),
    ("/api/platform/orgs/{org_id}/pending-embeddings", "GET"),
    ("/api/platform/orgs/{org_id}/pending-embeddings", "POST"),
    ("/api/platform/restore-jobs/{job_id}/pause", "POST"),
    ("/api/platform/restore-jobs/{job_id}/resume", "POST"),
    ("/api/platform/restore-jobs/{job_id}/cancel", "POST"),
    ("/api/platform/orgs/{org_id}/login-debug", "GET"),
    ("/api/platform/orgs/{org_id}/restore-preview", "POST"),
    ("/api/platform/orgs/import-preview", "POST"),
    ("/api/platform/restore-jobs/{job_id}/start", "POST"),
    ("/api/platform/orgs/{org_id}/restore-from-backup", "POST"),
]


# What CQ-04 phase 5 moved to routers.org_admin: the org admin console
# (users, teams, groups, memberships, memory sharing, wiki and git ACLs) on
# `router`, then tenant settings, memory edit notifications and the org
# setup wizard on `settings_router`.
ORG_ADMIN_ROUTES = [
    ("/api/admin/users", "GET"),
    ("/api/admin/users", "POST"),
    ("/api/admin/users/{user_id}", "PATCH"),
    ("/api/admin/users/{user_id}/resend-invite", "POST"),
    ("/api/admin/users/{user_id}", "DELETE"),
    ("/api/admin/teams", "GET"),
    ("/api/admin/teams", "POST"),
    ("/api/admin/teams/{team_id}", "PATCH"),
    ("/api/admin/teams/{team_id}", "DELETE"),
    ("/api/admin/teams/{team_id}/members", "GET"),
    ("/api/admin/teams/{team_id}/members", "POST"),
    ("/api/admin/teams/{team_id}/members/{user_id}", "DELETE"),
    ("/api/admin/groups", "GET"),
    ("/api/admin/groups", "POST"),
    ("/api/admin/groups/{group_id}", "PATCH"),
    ("/api/admin/teams/{team_id}/groups", "GET"),
    ("/api/admin/teams/{team_id}/groups", "POST"),
    ("/api/admin/groups/{group_id}", "DELETE"),
    ("/api/admin/users/{user_id}/groups", "GET"),
    ("/api/admin/users/{user_id}/groups", "PUT"),
    ("/api/admin/groups/{group_id}/members", "GET"),
    ("/api/admin/groups/{group_id}/members", "POST"),
    ("/api/admin/groups/{group_id}/members/{user_id}", "DELETE"),
    ("/api/admin/memory-sharing", "GET"),
    ("/api/admin/memory-sharing", "POST"),
    ("/api/admin/wiki-spaces", "GET"),
    ("/api/admin/wiki-spaces/acl", "POST"),
    ("/api/admin/wiki-spaces/acl/{acl_id}", "DELETE"),
    ("/api/admin/git-sources/{source_id}/acls", "GET"),
    ("/api/admin/git-sources/{source_id}/acls", "POST"),
    ("/api/admin/git-sources/acl/{acl_id}", "DELETE"),
]
ORG_SETTINGS_ROUTES = [
    ("/api/internal/tenant-settings", "GET"),
    ("/api/internal/tenant-settings", "POST"),
    ("/api/admin/tenant-settings", "GET"),
    ("/api/admin/tenant-settings", "PUT"),
    ("/api/notifications", "GET"),
    ("/api/notifications/{notification_id}/seen", "POST"),
    ("/api/internal/memory-edit-notify", "POST"),
    ("/setup-org", "GET"),
    ("/api/org", "GET"),
    ("/api/org", "PATCH"),
]

# What CQ-04 phase 5 moved to routers.admin_platform: the legacy tenants 410,
# the platform admin console, and the org-scoped domain routes.
LEGACY_TENANT_ROUTES = [
    ("/api/admin/tenants{rest:path}", method)
    for method in ("GET", "POST", "PATCH", "DELETE", "PUT")
]
PLATFORM_ADMIN_ROUTES = [
    ("/platform/admin", "GET"),
    ("/api/platform/orgs", "GET"),
    ("/api/platform/orgs", "POST"),
    ("/api/platform/orgs/{org_id}", "PATCH"),
    ("/api/platform/orgs/{org_id}", "DELETE"),
    ("/api/platform/orgs/{org_id}/recycle", "POST"),
    ("/api/platform/orgs/{org_id}/archive", "POST"),
    ("/api/platform/orgs/{org_id}/restore", "POST"),
    ("/api/platform/orgs/{org_id}/reactivate", "POST"),
    ("/api/platform/orgs/{org_id}/purge", "DELETE"),
    ("/api/platform/orgs/{org_id}/invite-admin", "POST"),
    ("/api/platform/orgs/{org_id}/domains", "GET"),
    ("/api/platform/orgs/{org_id}/domains", "POST"),
    ("/api/platform/orgs/{org_id}/domains/{domain_id}/verify", "POST"),
    ("/api/platform/orgs/{org_id}/domains/{domain_id}/promote", "POST"),
    ("/api/platform/orgs/{org_id}/domains/{domain_id}", "DELETE"),
    ("/api/platform/admins", "GET"),
    ("/api/platform/admins", "POST"),
    ("/api/platform/admins/{user_id}", "DELETE"),
    ("/api/platform/users/{user_id}/mfa-override", "PATCH"),
]
ORG_DOMAIN_ROUTES = [
    ("/api/admin/org/domains", "GET"),
    ("/api/admin/org/domains", "POST"),
    ("/api/admin/org/domains/{domain_id}/verify", "POST"),
    ("/api/admin/org/domains/{domain_id}/promote", "POST"),
    ("/api/admin/org/domains/{domain_id}", "DELETE"),
    ("/api/admin/org/domain-verify-instructions", "GET"),
    ("/api/admin/org/verify-domain", "POST"),
]

# What CQ-04 phase 6 moved to routers.git_sources: the org admin's git
# knowledge source routes on `router`, then the embedder's git source ACL and
# listing peer endpoints on `internal_router`.
GIT_SOURCE_ROUTES = [
    ("/api/admin/git-sources", "GET"),
    ("/api/admin/git-sources", "POST"),
    ("/api/admin/git-sources/{source_id}", "PUT"),
    ("/api/admin/git-sources/{source_id}", "DELETE"),
    ("/api/admin/git-sources/{source_id}/sync", "POST"),
    ("/api/admin/git-sources/{source_id}/reindex", "POST"),
    ("/api/admin/git-sources/status", "GET"),
]
INTERNAL_GIT_SOURCE_ROUTES = [
    ("/api/internal/git-source-access", "GET"),
    ("/api/internal/git-sources", "GET"),
]

# What CQ-04 phase 7 moved to routers.tokens: the org admin's API token routes
# and the MCP server's token validation on `router`, then a user's own tokens
# and OAuth sessions on `me_router`.
TOKEN_ROUTES = [
    ("/api/admin/tokens", "GET"),
    ("/api/admin/tokens", "POST"),
    ("/api/admin/tokens/{token_id}", "PATCH"),
    ("/api/admin/tokens/{token_id}", "DELETE"),
    ("/api/internal/tokens/validate", "POST"),
]
MY_TOKEN_ROUTES = [
    ("/api/me/tokens", "GET"),
    ("/api/me/tokens", "POST"),
    ("/api/me/tokens/{token_id}", "PATCH"),
    ("/api/me/tokens/{token_id}", "DELETE"),
    ("/api/me/oauth-sessions", "GET"),
    ("/api/me/oauth-sessions/{token_id_prefix}", "PATCH"),
    ("/api/me/oauth-sessions/{token_id_prefix}", "DELETE"),
]

# What CQ-04 phase 7 moved to routers.local_auth: first-run setup, login and
# logout, the forced password change, MFA, the invite flow and the admin MFA
# reset.
LOCAL_AUTH_ROUTES = [
    ("/setup", "GET"),
    ("/setup", "POST"),
    ("/login-redirect", "GET"),
    ("/login", "GET"),
    ("/login", "POST"),
    ("/logout", "GET"),
    ("/change-password", "GET"),
    ("/change-password", "POST"),
    ("/mfa", "GET"),
    ("/mfa", "POST"),
    ("/setup-mfa", "GET"),
    ("/setup-mfa", "POST"),
    ("/api/admin/invites", "POST"),
    ("/api/admin/invites", "GET"),
    ("/api/admin/invites/{invite_id}", "DELETE"),
    ("/invite/{invite_id}", "GET"),
    ("/invite/{invite_id}", "POST"),
    ("/api/admin/users/{user_id}/mfa-reset", "POST"),
]


@pytest.mark.parametrize("module,routes", [
    ("routers.oauth", OAUTH_ROUTES),
    ("routers.sso", SSO_ROUTES),
    ("routers.wiki", WIKI_ROUTES),
    ("routers.boards", BOARD_ROUTES),
    ("routers.backup", BACKUP_ROUTES),
    ("routers.backup", PLATFORM_BACKUP_ROUTES),
    ("routers.org_admin", ORG_ADMIN_ROUTES),
    ("routers.org_admin", ORG_SETTINGS_ROUTES),
    ("routers.admin_platform", LEGACY_TENANT_ROUTES),
    ("routers.admin_platform", PLATFORM_ADMIN_ROUTES),
    ("routers.admin_platform", ORG_DOMAIN_ROUTES),
    ("routers.git_sources", GIT_SOURCE_ROUTES),
    ("routers.git_sources", INTERNAL_GIT_SOURCE_ROUTES),
    ("routers.tokens", TOKEN_ROUTES),
    ("routers.tokens", MY_TOKEN_ROUTES),
    ("routers.local_auth", LOCAL_AUTH_ROUTES),
])
def test_moved_routes_are_served_by_their_router_and_only_once(module, routes):
    import main
    for path, method in routes:
        handlers = [r.endpoint for r in served_routes(main.app)
                    if r.path == path and method in (getattr(r, "methods", None) or ())]
        assert len(handlers) == 1, f"{method} {path}: {len(handlers)} handlers"
        assert handlers[0].__module__ == module, (
            f"{method} {path} is served from {handlers[0].__module__}, not {module}")


def test_main_no_longer_defines_moved_handlers():
    # main.py still names some of these paths (the CSRF exemption list), so
    # look for route decorators rather than the bare strings.
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text()
    declared = set(re.findall(r'@app\.\w+\(\s*"([^"]+)"', src))
    moved = {path for path, _ in OAUTH_ROUTES + SSO_ROUTES + WIKI_ROUTES + BOARD_ROUTES
             + BACKUP_ROUTES + PLATFORM_BACKUP_ROUTES
             + ORG_ADMIN_ROUTES + ORG_SETTINGS_ROUTES
             + LEGACY_TENANT_ROUTES + PLATFORM_ADMIN_ROUTES + ORG_DOMAIN_ROUTES
             + GIT_SOURCE_ROUTES + INTERNAL_GIT_SOURCE_ROUTES
             + TOKEN_ROUTES + MY_TOKEN_ROUTES + LOCAL_AUTH_ROUTES}
    assert not declared & moved, f"main.py still declares {sorted(declared & moved)}"


def test_backup_startup_hooks_still_run_once_each():
    # The scheduler threads and the restore-job reaper stay registered on the
    # app itself, once, in the order they had before phase 4. A router that
    # also registered them would start every scheduler loop twice.
    import main
    hooks = [f.__name__ for f in main.app.router.on_startup]
    assert hooks.count("start_auto_backup_scheduler") == 1, hooks
    assert hooks.count("settle_interrupted_restores") == 1, hooks
    assert hooks.index("settle_interrupted_restores") < hooks.index("start_auto_backup_scheduler")


def test_platform_admin_startup_hooks_move_with_their_router_and_keep_their_place():
    # Phase 5 moved bootstrap_platform_admin and _auto_purge_expired_recycle_bin
    # onto routers.admin_platform's router. Including it adds them to the app's
    # startup hooks at the point it is included, which is where they were
    # declared before, so they still run once each and in the same order.
    import main
    hooks = [f for f in main.app.router.on_startup]
    names = [f.__name__ for f in hooks]
    for name in ("bootstrap_platform_admin", "_auto_purge_expired_recycle_bin"):
        assert names.count(name) == 1, names
        assert hooks[names.index(name)].__module__ == "routers.admin_platform"
    assert (names.index("purge_old_recycle_bin")
            < names.index("bootstrap_platform_admin")
            < names.index("_auto_purge_expired_recycle_bin")
            < names.index("purge_expired_auth_states"))


def test_no_router_carries_its_own_startup_or_shutdown_hooks():
    # FastAPI 0.142 runs an included router's own on_startup handlers twice:
    # include_router copies them onto the app, and also merges the router's
    # default lifespan, which runs them again. A hook therefore belongs on the
    # app (main.py registers each one beside the include_router call), never
    # on the router.
    import importlib
    import pkgutil

    import routers
    from fastapi import APIRouter

    for info in pkgutil.iter_modules(routers.__path__):
        module = importlib.import_module(f"routers.{info.name}")
        for name, obj in vars(module).items():
            if isinstance(obj, APIRouter):
                assert not obj.on_startup, f"routers.{info.name}.{name} has startup hooks"
                assert not obj.on_shutdown, f"routers.{info.name}.{name} has shutdown hooks"
