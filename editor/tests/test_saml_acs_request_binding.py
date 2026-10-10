"""The SAML ACS only accepts a response that answers a pending AuthnRequest.

saml_login stores the id of every AuthnRequest it sends. saml_acs must spend
that id: a response whose InResponseTo is missing, unknown, expired or already
used is rejected before python3-saml validates it, and the id is gone even when
validation then fails.

These tests sign real responses with a throwaway IdP key, so the normal
SP-initiated path runs through python3-saml's full strict validation.
"""
import base64
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("onelogin.saml2.auth", reason="python3-saml is not installed")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from onelogin.saml2.utils import OneLogin_Saml2_Utils  # noqa: E402

TENANT_ID = "t-saml-binding"
IDP_ENTITY = "https://idp.example.org/metadata"
SP_ENTITY = "https://test.example.com/editor/saml/sp"


def _idp_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return key_pem, cert_pem


KEY_PEM, CERT_PEM = _idp_keypair()


def SessionLocal():
    # config_db.SessionLocal is only bound once main has run init_db.
    import main  # noqa: F401
    import config_db
    return config_db.SessionLocal()


@pytest.fixture
def provider_id():
    from config_db import SamlProvider, Tenant
    pid = str(uuid.uuid4())
    s = SessionLocal()
    try:
        if not s.query(Tenant).filter_by(id=TENANT_ID).first():
            s.add(Tenant(id=TENANT_ID, name="SAML Binding Org", slug="saml-binding",
                         is_active=True, lifecycle_status="active", domain_verified=False,
                         max_teams=10, setup_complete=True))
        s.add(SamlProvider(id=pid, tenant_id=TENANT_ID, display_name="Test IdP",
                           idp_entity_id=IDP_ENTITY, idp_sso_url="https://idp.example.org/sso",
                           idp_x509_cert=CERT_PEM, sp_entity_id=SP_ENTITY,
                           want_assertions_signed=True, want_response_signed=True))
        s.commit()
    finally:
        s.close()
    return pid


def _ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed_response(provider_id, in_response_to=None, email=None):
    import auth_helpers
    # One user per provider: the tests share a tenant, and a username is unique in it.
    email = email or f"u{provider_id[:8]}@example.org"
    acs = f"{auth_helpers.EDITOR_EXTERNAL_URL}/auth/saml/{provider_id}/acs"
    now = datetime.now(timezone.utc)
    irt = f' InResponseTo="{in_response_to}"' if in_response_to else ""
    assertion = f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_ts(now)}">
<saml:Issuer>{IDP_ENTITY}</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData NotOnOrAfter="{_ts(now + timedelta(minutes=5))}" Recipient="{acs}"{irt}/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{_ts(now - timedelta(minutes=1))}" NotOnOrAfter="{_ts(now + timedelta(minutes=5))}"><saml:AudienceRestriction><saml:Audience>{SP_ENTITY}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AuthnStatement AuthnInstant="{_ts(now)}" SessionIndex="_s1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>
<saml:AttributeStatement><saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion>"""
    signed_assertion = OneLogin_Saml2_Utils.add_sign(assertion, KEY_PEM, CERT_PEM).decode()
    signed_assertion = signed_assertion.replace('<?xml version="1.0"?>', "").strip()
    response = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" Version="2.0" IssueInstant="{_ts(now)}" Destination="{acs}"{irt}>
<saml:Issuer>{IDP_ENTITY}</saml:Issuer>
<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
{signed_assertion}
</samlp:Response>"""
    signed = OneLogin_Saml2_Utils.add_sign(response, KEY_PEM, CERT_PEM)
    return base64.b64encode(signed).decode()


def _start_login(client, provider_id):
    r = client.get(f"/auth/saml/{provider_id}/login", follow_redirects=False)
    assert r.status_code in (302, 307), r.text
    saml_request = parse_qs(urlparse(r.headers["location"]).query)["SAMLRequest"][0]
    xml = zlib.decompress(base64.b64decode(saml_request), -15).decode()
    return xml.split(' ID="', 1)[1].split('"', 1)[0]


def _post(client, provider_id, saml_response):
    return client.post(f"/auth/saml/{provider_id}/acs", data={"SAMLResponse": saml_response})


def _pending(provider_id, request_id):
    from config_db import SamlRequestId
    s = SessionLocal()
    try:
        return s.query(SamlRequestId).filter_by(
            provider_id=provider_id, request_id=request_id).count()
    finally:
        s.close()


def test_sp_initiated_login_succeeds(provider_id):
    import main
    client = TestClient(main.app)
    request_id = _start_login(client, provider_id)
    assert _pending(provider_id, request_id) == 1

    r = _post(client, provider_id, _signed_response(provider_id, request_id))
    assert r.status_code == 200, r.text
    assert _pending(provider_id, request_id) == 0


def test_replayed_response_is_rejected(provider_id):
    import main
    client = TestClient(main.app)
    request_id = _start_login(client, provider_id)
    captured = _signed_response(provider_id, request_id)

    assert _post(client, provider_id, captured).status_code == 200
    replay = _post(client, provider_id, captured)
    assert replay.status_code == 401
    assert "does not match a pending sign-in" in replay.text


def test_request_id_cannot_be_used_twice(provider_id):
    """A second, freshly signed response for the same request is refused too."""
    import main
    client = TestClient(main.app)
    request_id = _start_login(client, provider_id)

    assert _post(client, provider_id, _signed_response(provider_id, request_id)).status_code == 200
    second = _post(client, provider_id, _signed_response(provider_id, request_id, f"b{provider_id[:8]}@example.org"))
    assert second.status_code == 401


def test_unsolicited_response_is_rejected(provider_id):
    import main
    client = TestClient(main.app)
    r = _post(client, provider_id, _signed_response(provider_id, in_response_to=None))
    assert r.status_code == 401
    assert "does not match a pending sign-in" in r.text


def test_unknown_request_id_is_rejected(provider_id):
    import main
    client = TestClient(main.app)
    r = _post(client, provider_id, _signed_response(provider_id, "ONELOGIN_never_issued"))
    assert r.status_code == 401


def test_request_id_for_another_provider_is_rejected(provider_id):
    import main
    from config_db import SamlRequestId
    client = TestClient(main.app)
    other = str(uuid.uuid4())
    s = SessionLocal()
    try:
        s.add(SamlRequestId(provider_id=other, request_id="ONELOGIN_other_provider",
                            expires_at=datetime.utcnow() + timedelta(minutes=5)))
        s.commit()
    finally:
        s.close()
    r = _post(client, provider_id, _signed_response(provider_id, "ONELOGIN_other_provider"))
    assert r.status_code == 401
    assert _pending(other, "ONELOGIN_other_provider") == 1


def test_expired_request_id_is_rejected_and_removed(provider_id):
    import main
    from config_db import SamlRequestId
    client = TestClient(main.app)
    s = SessionLocal()
    try:
        s.add(SamlRequestId(provider_id=provider_id, request_id="ONELOGIN_expired",
                            expires_at=datetime.utcnow() - timedelta(minutes=1)))
        s.commit()
    finally:
        s.close()
    r = _post(client, provider_id, _signed_response(provider_id, "ONELOGIN_expired"))
    assert r.status_code == 401
    assert _pending(provider_id, "ONELOGIN_expired") == 0


def test_request_id_is_spent_even_when_validation_fails(provider_id):
    import main
    client = TestClient(main.app)
    request_id = _start_login(client, provider_id)

    tampered = base64.b64decode(_signed_response(provider_id, request_id)).replace(
        b"@example.org", b"@example.net")
    r = _post(client, provider_id, base64.b64encode(tampered).decode())
    assert r.status_code == 401
    assert _pending(provider_id, request_id) == 0

    # The genuine response for that request can no longer be used.
    assert _post(client, provider_id, _signed_response(provider_id, request_id)).status_code == 401


def test_new_providers_require_signed_responses():
    from config_db import SamlProvider
    s = SessionLocal()
    try:
        p = SamlProvider(tenant_id=TENANT_ID, display_name="Defaults", idp_entity_id=IDP_ENTITY,
                         idp_sso_url="https://idp.example.org/sso", idp_x509_cert=CERT_PEM,
                         sp_entity_id=SP_ENTITY)
        s.add(p)
        s.flush()
        assert p.want_response_signed is True
        s.rollback()
    finally:
        s.close()
