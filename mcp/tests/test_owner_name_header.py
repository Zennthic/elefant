"""The MCP forwards the token's display name so the embedder can match
memories owned only by an `owner:<name>` tag (issue #85)."""


def test_auth_headers_include_owner_name_when_present():
    import main
    hdrs = main._auth_headers({
        "user_id": "u-uuid", "org_id": "o", "tenant_slug": "acme", "name": "Claude",
    })
    assert hdrs["X-Auth-Owner-Name"] == "Claude"
    # The existing identity headers are still present.
    assert hdrs["X-Auth-User-Id"] == "u-uuid"
    assert hdrs["X-Auth-Org-Slug"] == "acme"


def test_auth_headers_omit_owner_name_when_absent():
    import main
    hdrs = main._auth_headers({"user_id": "u-uuid", "tenant_slug": "acme"})
    assert "X-Auth-Owner-Name" not in hdrs
