"""_build_saml_settings hands python3-saml bare base64 bodies.

python3-saml wants sp.privateKey, sp.x509cert and idp.x509cert without PEM
armour or line breaks. The keys and certificate are generated here at run
time, so this file holds no PEM text for a secret scanner to flag.
"""
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from routers.sso import _build_saml_settings, _pem_body


@pytest.fixture(scope="module")
def key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-sp")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return key, cert


def _b64(der: bytes) -> str:
    return base64.b64encode(der).decode()


@pytest.mark.parametrize("fmt", [serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.PrivateFormat.PKCS8])
def test_pem_body_strips_private_key_armour(key_and_cert, fmt):
    """Both the PKCS#1 ("RSA PRIVATE KEY") and PKCS#8 ("PRIVATE KEY") forms."""
    key, _ = key_and_cert
    pem = key.private_bytes(serialization.Encoding.PEM, fmt,
                            serialization.NoEncryption()).decode()
    der = key.private_bytes(serialization.Encoding.DER, fmt,
                            serialization.NoEncryption())
    assert _pem_body(pem) == _b64(der)


def test_pem_body_strips_certificate_armour_and_crlf(key_and_cert):
    _, cert = key_and_cert
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    der = cert.public_bytes(serialization.Encoding.DER)
    assert _pem_body(pem) == _b64(der)
    assert _pem_body(pem.replace("\n", "\r\n")) == _b64(der)
    assert _pem_body(_b64(der)) == _b64(der), "a bare body passes through"


def test_build_saml_settings_uses_bare_bodies(key_and_cert):
    key, cert = key_and_cert
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    provider = SimpleNamespace(
        idp_x509_cert=cert_pem, sp_entity_id="https://sp.example.com",
        idp_entity_id="https://idp.example.com", idp_sso_url="https://idp.example.com/sso",
        want_assertions_signed=True, want_response_signed=True, sign_requests=True,
        sp_private_key=lambda: key_pem, sp_certificate=cert_pem,
    )
    settings = _build_saml_settings(provider, "https://sp.example.com/acs")
    cert_b64 = _b64(cert.public_bytes(serialization.Encoding.DER))
    assert settings["idp"]["x509cert"] == cert_b64
    assert settings["sp"]["x509cert"] == cert_b64
    assert settings["sp"]["privateKey"] == _b64(key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    for value in (settings["idp"]["x509cert"], settings["sp"]["x509cert"],
                  settings["sp"]["privateKey"]):
        assert "-----" not in value and "\n" not in value
