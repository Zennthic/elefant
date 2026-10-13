"""A member added by an admin should be OAuth-capable by default, matching the
UserAccount.can_oauth column default and the invite/SSO paths (issue #87).
A False default here silently 403'd the OAuth consent flow."""


def test_user_create_request_defaults_can_oauth_true():
    from routers import org_admin
    req = org_admin.UserCreateRequest(local_part="alice", domain="example.com")
    assert req.can_oauth is True
