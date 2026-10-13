"""
Configuration database and encryption utilities.
Stores admin settings with encryption at rest using Fernet.
"""
import hashlib
import os
import json
import secrets
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean, Integer, Float, LargeBinary, text, JSON as SAJson, UniqueConstraint, Index
from sqlalchemy.orm import declarative_base, sessionmaker
from cryptography.fernet import Fernet, InvalidToken

# ── Database setup ─────────────────────────────────────────────────────────

_POSTGRES_DSN = os.environ.get("POSTGRES_EDITOR_DSN", "")

if _POSTGRES_DSN:
    DATABASE_URL = _POSTGRES_DSN
    DB_PATH = None
else:
    DB_PATH = Path(os.environ.get("DB_PATH", "/data/config.db"))
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Warning: Could not create database directory {DB_PATH.parent}: {e}")
    DATABASE_URL = f"sqlite:///{DB_PATH}"

# Lazy initialization - will be created on first use
engine = None
SessionLocal = None
Base = declarative_base()

# ── Reserved default tenant ────────────────────────────────────────────────

DEFAULT_TENANT_ID   = "00000000-0000-0000-0000-000000000001"
DEFAULT_TENANT_SLUG = "default"


def _init_engine():
    """Initialize SQLAlchemy engine (lazy initialization)."""
    global engine, SessionLocal
    if engine is None:
        if _POSTGRES_DSN:
            engine = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)
        else:
            engine = create_engine(
                DATABASE_URL,
                connect_args={"check_same_thread": False},
                echo=False,
            )
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine


# ── Encryption utilities ───────────────────────────────────────────────────

class CryptoManager:
    """Handles encryption/decryption with Fernet (symmetric encryption)."""

    def __init__(self):
        self.cipher = None
        self._initialized = False

    def _load_or_create_key(self) -> Fernet:
        """Load encryption key from env or create a new one."""
        key_path = Path(os.environ.get("CRYPTO_KEY_PATH", "/data/crypto.key"))
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"Warning: Could not create crypto key directory {key_path.parent}: {e}")
            raise

        if key_path.exists():
            with open(key_path, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            with open(key_path, "wb") as f:
                f.write(key)
            # Restrict permissions on key file
            os.chmod(key_path, 0o600)

        return Fernet(key)

    def _ensure_initialized(self):
        """Lazily initialize cipher on first use."""
        if not self._initialized:
            try:
                self.cipher = self._load_or_create_key()
                self._initialized = True
            except Exception as e:
                print(f"Error: Failed to initialize encryption: {e}")
                raise

    def encrypt(self, plaintext: str) -> str:
        """Encrypt plaintext and return as base64 string."""
        if not plaintext:
            return ""
        self._ensure_initialized()
        encrypted = self.cipher.encrypt(plaintext.encode("utf-8"))
        return encrypted.decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt ciphertext; return empty string on failure."""
        if not ciphertext:
            return ""
        try:
            self._ensure_initialized()
            decrypted = self.cipher.decrypt(ciphertext.encode("utf-8"))
            return decrypted.decode("utf-8")
        except (InvalidToken, Exception):
            return ""


# Lazy-initialized crypto manager - created on first use
crypto = CryptoManager()


# ── Database models ───────────────────────────────────────────────────────

CLASSIFICATION_TAGS = {"phi", "pii", "sensitive"}


class Tenant(Base):
    """
    A logical tenant / Organization — isolates wiki docs, memory collections,
    tokens, teams, and groups.  In multi-org mode multiple Orgs live here; in
    self-hosted deployments there is exactly one (the default).
    """

    __tablename__ = "tenants"

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name       = Column(String(255), nullable=False)
    # URL-safe slug — used for Qdrant collection names and /docs subdirectory.
    # "default" is reserved for the original single-tenant data.
    slug       = Column(String(63), nullable=False, unique=True, index=True)
    is_active  = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── Lifecycle status ──────────────────────────────────────────────────
    # "active" | "offboarded" | "recycled" | "archived"
    # is_active stays in sync: True only when lifecycle_status == "active"
    lifecycle_status = Column(String(16), nullable=False, default="active")
    recycled_at      = Column(DateTime, nullable=True)   # set when moved to recycle bin

    # ── Org identity ──────────────────────────────────────────────────────
    display_name         = Column(String(255), nullable=True)
    # domain: "acme.local" (unverified) or "acme.com" (TXT/CNAME verified)
    domain               = Column(String(253), nullable=True, unique=True)
    domain_verified      = Column(Boolean, nullable=False, default=False)
    domain_verify_token  = Column(String(64), nullable=True)  # random token for DNS check

    # ── Org policy ────────────────────────────────────────────────────────
    # "enforced" = all users must have MFA; "optional" = user's choice
    mfa_policy    = Column(String(16), nullable=False, default="enforced")
    max_teams     = Column(Integer, nullable=False, default=5)
    setup_complete = Column(Boolean, nullable=False, default=False)  # org wizard done
    saas_plan     = Column(String(64), nullable=True)  # billing hook

    # "legacy" = shared /docs volume (all single-org installs; multi-org orgs before
    # bucket provisioning). "s3" = per-org S3 bucket (set at org-create time
    # when ELEFANT_S3_ACCESS_KEY/SECRET_KEY are configured).
    storage_backend = Column(String(16), nullable=False, default="legacy")

    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "name":             self.name,
            "slug":             self.slug,
            "display_name":     self.display_name,
            "domain":           self.domain,
            "domain_verified":  self.domain_verified,
            "mfa_policy":       self.mfa_policy,
            "max_teams":        self.max_teams,
            "setup_complete":   self.setup_complete,
            "is_active":        self.is_active,
            "lifecycle_status": self.lifecycle_status,
            "storage_backend":  self.storage_backend or "legacy",
            "recycled_at":      self.recycled_at.isoformat() if self.recycled_at else None,
            "created_at":       self.created_at.isoformat() if self.created_at else None,
        }


class OrgDomain(Base):
    """
    A public DNS domain claimed by an Org.  Multiple secondaries are allowed;
    exactly one row per Org may have ``is_primary=True`` (enforced via
    application code on SQLite, partial unique index on Postgres).

    `Tenant.domain` stays as a denormalized cache of the primary row's
    ``domain`` so existing code paths that read ``tenant.domain`` keep
    working without a join.
    """

    __tablename__ = "org_domains"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id        = Column(String(36), nullable=False, index=True)
    domain        = Column(String(253), nullable=False, unique=True)
    is_primary    = Column(Boolean, nullable=False, default=False, index=True)
    verified      = Column(Boolean, nullable=False, default=False)
    verify_token  = Column(String(64), nullable=False)  # 16-char hex
    verified_at   = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "org_id":      self.org_id,
            "domain":      self.domain,
            "is_primary":  bool(self.is_primary),
            "verified":    bool(self.verified),
            "verify_token": self.verify_token,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "created_at":  self.created_at.isoformat() if self.created_at else None,
        }


class OidcProvider(Base):
    """
    External OIDC / OAuth 2.0 identity provider configuration (per-tenant).
    Set use_userinfo_only=True for non-OIDC OAuth providers (e.g. GitHub)
    that do not issue id_tokens.
    """

    __tablename__ = "oidc_providers"

    id                  = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id           = Column(String(36), nullable=False, index=True)
    display_name        = Column(String(255), nullable=False)
    # OIDC issuer URL — discovery document at {issuer}/.well-known/openid-configuration
    issuer              = Column(String(2048), nullable=False)
    client_id           = Column(String(512), nullable=False)
    # Fernet-encrypted client secret (empty for public clients)
    client_secret_enc   = Column(Text, nullable=True)
    scopes              = Column(String(1024), default="openid email profile")
    # Attribute mapping — which claim contains email / display name / subject
    attr_email          = Column(String(255), default="email")
    attr_name           = Column(String(255), default="name")
    attr_sub            = Column(String(255), default="sub")
    # True = GitHub/non-OIDC: skip id_token validation, use userinfo endpoint
    use_userinfo_only   = Column(Boolean, default=False)
    # Override the userinfo endpoint (auto-discovered if None)
    userinfo_endpoint   = Column(String(2048), nullable=True)
    is_active           = Column(Boolean, default=True)
    # Defaults for tokens issued after login
    default_memory_read  = Column(Boolean, default=True)
    default_memory_write = Column(Boolean, default=False)
    default_wiki_read    = Column(Boolean, default=True)
    default_wiki_write   = Column(Boolean, default=False)
    default_rate_limit_rpm  = Column(Integer, default=60)
    default_rate_limit_rpd  = Column(Integer, default=1000)
    default_token_ttl_hours = Column(Integer, default=8)
    # Cached JWKS document (JSON string) with its fetch timestamp
    jwks_cache          = Column(Text, nullable=True)
    jwks_cached_at      = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)

    def client_secret(self) -> str:
        """Return decrypted client secret."""
        return crypto.decrypt(self.client_secret_enc or "")

    def set_client_secret(self, plaintext: str):
        """Encrypt and store the client secret."""
        self.client_secret_enc = crypto.encrypt(plaintext) if plaintext else None

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "tenant_id":            self.tenant_id,
            "display_name":         self.display_name,
            "issuer":               self.issuer,
            "client_id":            self.client_id,
            "client_secret_set":    bool(self.client_secret_enc),
            "scopes":               self.scopes,
            "attr_email":           self.attr_email,
            "attr_name":            self.attr_name,
            "attr_sub":             self.attr_sub,
            "use_userinfo_only":    self.use_userinfo_only,
            "userinfo_endpoint":    self.userinfo_endpoint,
            "is_active":            self.is_active,
            "default_memory_read":  self.default_memory_read,
            "default_memory_write": self.default_memory_write,
            "default_wiki_read":    self.default_wiki_read,
            "default_wiki_write":   self.default_wiki_write,
            "default_rate_limit_rpm":  self.default_rate_limit_rpm,
            "default_rate_limit_rpd":  self.default_rate_limit_rpd,
            "default_token_ttl_hours": self.default_token_ttl_hours,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
        }


class SamlProvider(Base):
    """SAML 2.0 identity provider configuration (per-tenant)."""

    __tablename__ = "saml_providers"

    id                    = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id             = Column(String(36), nullable=False, index=True)
    display_name          = Column(String(255), nullable=False)
    # IdP metadata
    idp_entity_id         = Column(String(2048), nullable=False)
    idp_sso_url           = Column(String(2048), nullable=False)
    idp_x509_cert         = Column(Text, nullable=False)         # PEM certificate
    # SP identity
    sp_entity_id          = Column(String(2048), nullable=False)
    # Attribute mapping
    attr_email            = Column(String(255), default="email")
    attr_name             = Column(String(255), default="displayName")
    attr_sub              = Column(String(255), default="nameID")
    # SP signing (optional)
    sp_private_key_enc    = Column(Text, nullable=True)  # Fernet-encrypted PEM
    sp_certificate        = Column(Text, nullable=True)  # PEM public cert
    sign_requests         = Column(Boolean, default=False)
    want_assertions_signed = Column(Boolean, default=True)
    # True for new providers; rows stored before this default keep their value.
    want_response_signed  = Column(Boolean, default=True)
    is_active             = Column(Boolean, default=True)
    # Defaults for tokens issued after login
    default_memory_read   = Column(Boolean, default=True)
    default_memory_write  = Column(Boolean, default=False)
    default_wiki_read     = Column(Boolean, default=True)
    default_wiki_write    = Column(Boolean, default=False)
    default_rate_limit_rpm   = Column(Integer, default=60)
    default_rate_limit_rpd   = Column(Integer, default=1000)
    default_token_ttl_hours  = Column(Integer, default=8)
    created_at            = Column(DateTime, default=datetime.utcnow)

    def sp_private_key(self) -> str:
        return crypto.decrypt(self.sp_private_key_enc or "")

    def set_sp_private_key(self, pem: str):
        self.sp_private_key_enc = crypto.encrypt(pem) if pem else None

    def to_dict(self) -> dict:
        return {
            "id":                   self.id,
            "tenant_id":            self.tenant_id,
            "display_name":         self.display_name,
            "idp_entity_id":        self.idp_entity_id,
            "idp_sso_url":          self.idp_sso_url,
            "idp_x509_cert":        self.idp_x509_cert,
            "sp_entity_id":         self.sp_entity_id,
            "attr_email":           self.attr_email,
            "attr_name":            self.attr_name,
            "attr_sub":             self.attr_sub,
            "sp_certificate":       self.sp_certificate,
            "sign_requests":        self.sign_requests,
            "want_assertions_signed": self.want_assertions_signed,
            "want_response_signed": self.want_response_signed,
            "is_active":            self.is_active,
            "default_memory_read":  self.default_memory_read,
            "default_memory_write": self.default_memory_write,
            "default_wiki_read":    self.default_wiki_read,
            "default_wiki_write":   self.default_wiki_write,
            "default_rate_limit_rpm":  self.default_rate_limit_rpm,
            "default_rate_limit_rpd":  self.default_rate_limit_rpd,
            "default_token_ttl_hours": self.default_token_ttl_hours,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
        }


class OidcState(Base):
    """Short-lived CSRF state record for OIDC authorization flows."""

    __tablename__ = "oidc_states"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id  = Column(String(36), nullable=False, index=True)
    state_token  = Column(String(128), nullable=False, unique=True, index=True)
    code_verifier = Column(String(128), nullable=True)   # PKCE S256 verifier
    nonce        = Column(String(128), nullable=True)   # OIDC nonce sent with the request
    redirect_after = Column(String(2048), nullable=True)  # where to send user post-login
    created_at   = Column(DateTime, default=datetime.utcnow)
    expires_at   = Column(DateTime, nullable=False)


class SamlRequestId(Base):
    """Tracks issued SAML AuthnRequest IDs for replay prevention."""

    __tablename__ = "saml_request_ids"

    id          = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_id = Column(String(36), nullable=False, index=True)
    request_id  = Column(String(255), nullable=False, unique=True, index=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    expires_at  = Column(DateTime, nullable=False)


def _slugify_source(s: Optional[str]) -> str:
    """Normalize a free-text identifier (OAuth client_name, etc.) into a
    stable lowercase slug usable as a memory `source` value. Non-alnum
    runs collapse to a single underscore; surrounding underscores are
    trimmed. Empty/None becomes "unknown" so downstream code never sees
    an empty source."""
    import re as _re
    if not s:
        return "unknown"
    slug = _re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")
    return slug or "unknown"


class ApiToken(Base):
    """Bearer tokens for MCP/API access with per-token RBAC."""

    __tablename__ = "api_tokens"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name          = Column(String(255), nullable=False)          # Human name; auto-injected as owner tag on writes
    token_hash    = Column(String(64), nullable=False, unique=True)  # SHA-256 hex of raw token
    tenant_id     = Column(String(36), nullable=True, index=True)    # None → default tenant
    user_id       = Column(String(36), nullable=True)                # Linked user (if from IdP login)

    # Rate limits
    rate_limit_rpm = Column(Integer, nullable=False, default=60)    # Requests per minute
    rate_limit_rpd = Column(Integer, nullable=False, default=1000)  # Requests per day

    # Capability flags (safe defaults: read-only)
    memory_read   = Column(Boolean, nullable=False, default=True)
    memory_write  = Column(Boolean, nullable=False, default=False)
    wiki_read     = Column(Boolean, nullable=False, default=True)
    wiki_write    = Column(Boolean, nullable=False, default=False)
    # Standalone graph visualization: unlocks GET /api/memories/graph through
    # the nginx auth gateway (bearer in lieu of a session cookie) so the
    # rotating memory graph can be embedded outside Elefant's own web UI.
    graph_read    = Column(Boolean, nullable=False, default=False)
    # Board (post-it Kanban): read/list cards and columns, add/move/edit cards.
    # Same read-on / write-off default as memory & wiki.
    board_read    = Column(Boolean, nullable=False, default=True)
    board_write   = Column(Boolean, nullable=False, default=False)

    # Label scope: if non-empty, token can only see content with ≥1 matching tag.
    allowed_labels        = Column(SAJson, nullable=False, default=list)

    # Classification access: subset of ["PHI", "PII", "Sensitive"] allowed.
    classification_access = Column(SAJson, nullable=False, default=list)

    # Source attribution: identifies the host/agent that holds this token,
    # so every memory written via it gets a structural source field. Common
    # values: claude_code, claude_desktop, chatgpt, cursor, open_webui,
    # mcp_direct, automation, editor, other. "unknown" is the transitional
    # default for tokens minted before this column existed.
    source        = Column(String(64), nullable=False, default="unknown")

    created_at    = Column(DateTime, default=datetime.utcnow)
    last_used_at  = Column(DateTime, nullable=True)
    expires_at    = Column(DateTime, nullable=True)  # None = never expires

    @staticmethod
    def generate() -> tuple[str, str]:
        """Return (raw_token, token_hash). Store only the hash."""
        raw   = secrets.token_urlsafe(32)
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return raw, hashed

    def effective_tenant_id(self) -> str:
        return self.tenant_id or DEFAULT_TENANT_ID

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """Serialise to dict. Never includes the token hash."""
        return {
            "id":                   self.id,
            "name":                 self.name,
            "tenant_id":            self.effective_tenant_id(),
            "user_id":              self.user_id,
            "rate_limit_rpm":       self.rate_limit_rpm,
            "rate_limit_rpd":       self.rate_limit_rpd,
            "memory_read":          self.memory_read,
            "memory_write":         self.memory_write,
            "wiki_read":            self.wiki_read,
            "wiki_write":           self.wiki_write,
            "graph_read":           bool(self.graph_read),
            "board_read":           bool(self.board_read),
            "board_write":          bool(self.board_write),
            "allowed_labels":       self.allowed_labels or [],
            "classification_access": self.classification_access or [],
            "source":               self.source or "unknown",
            "created_at":           self.created_at.isoformat() if self.created_at else None,
            "last_used_at":         self.last_used_at.isoformat() if self.last_used_at else None,
            "expires_at":           self.expires_at.isoformat() if self.expires_at else None,
        }


class OAuthClient(Base):
    """
    Dynamically registered OAuth 2.0 client (RFC 7591).
    Public clients only — no client secret, PKCE required.
    """
    __tablename__ = "oauth_clients"

    client_id    = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    client_name  = Column(String(255), nullable=False)
    redirect_uris = Column(SAJson, nullable=False)   # list[str]
    scope        = Column(String(500), default="mcp")
    tenant_id    = Column(String(36), nullable=True, index=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "client_id":    self.client_id,
            "client_name":  self.client_name,
            "redirect_uris": self.redirect_uris or [],
            "grant_types":  ["authorization_code"],
            "response_types": ["code"],
            "scope":        self.scope,
            "token_endpoint_auth_method": "none",
        }


class OAuthCode(Base):
    """Short-lived authorization code produced by the consent step."""
    __tablename__ = "oauth_codes"

    code                  = Column(String(64), primary_key=True)
    client_id             = Column(String(36), nullable=False)
    redirect_uri          = Column(String(500), nullable=False)
    scope                 = Column(String(500), default="mcp")
    code_challenge        = Column(String(128), nullable=False)  # PKCE S256
    code_challenge_method = Column(String(10), default="S256")
    user_id               = Column(String(36), nullable=True, index=True)
    # Permissions approved on the consent page.  wiki_write defaults
    # True because the scope is always AI-folder-confined (the editor's
    # /api/internal/wiki/write boundary enforces this server-side
    # regardless of what the agent or any future bypass attempts) —
    # default-on matches the actual blast radius. The consent page
    # mirrors the same default.
    memory_read           = Column(Boolean, default=True)
    memory_write          = Column(Boolean, default=False)
    wiki_read             = Column(Boolean, default=True)
    wiki_write            = Column(Boolean, default=True)
    expires_at            = Column(DateTime, nullable=False)
    used                  = Column(Boolean, default=False)


class OAuthAccessToken(Base):
    """
    Access token issued after a successful code exchange.
    Validated by the MCP server via /api/internal/tokens/validate.
    """
    __tablename__ = "oauth_access_tokens"

    token_hash   = Column(String(64), primary_key=True)  # SHA-256 of raw token
    client_id    = Column(String(36), nullable=False)
    client_name  = Column(String(255), nullable=False)
    scope        = Column(String(500), default="mcp")
    tenant_id    = Column(String(36), nullable=True, index=True)
    user_id      = Column(String(36), nullable=True, index=True)
    # The authorization code this token was minted from. Lets a replay of
    # an already-redeemed code revoke the whole family of tokens issued
    # from that code (RFC 6819 §5.2.1.1) — a replayed code almost always
    # means the code was intercepted.
    from_code    = Column(String(64), nullable=True, index=True)
    # Permissions inherited from the consent page
    memory_read  = Column(Boolean, default=True)
    memory_write = Column(Boolean, default=False)
    wiki_read    = Column(Boolean, default=True)
    wiki_write   = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    expires_at   = Column(DateTime, nullable=True)
    # Set whenever the resource owner edits this session's scope after
    # issuance (see PATCH /api/me/oauth-sessions). NULL means the scope
    # still matches what the client was granted on the original consent
    # page; a non-NULL value flags an out-of-band owner edit so the
    # divergence from that consent stays auditable.
    scope_modified_at = Column(DateTime, nullable=True)

    def effective_tenant_id(self) -> str:
        return self.tenant_id or DEFAULT_TENANT_ID

    @staticmethod
    def generate() -> tuple[str, str]:
        """Return (raw_token, token_hash). Store only the hash."""
        raw    = secrets.token_urlsafe(32)
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return raw, hashed

    def to_validate_dict(self) -> dict:
        """Shape expected by the MCP server's _validate_token helper."""
        return {
            "valid":          True,
            "id":             self.token_hash[:8],
            "name":           self.client_name,
            "tenant_id":      self.effective_tenant_id(),
            "tenant_slug":    DEFAULT_TENANT_SLUG,  # resolved by validate endpoint
            "user_id":        self.user_id,
            "org_id":         self.effective_tenant_id(),
            # No rate-limit fields, deliberately. An OAuth session has no
            # per-token ceiling to carry: there is no column for one here, and
            # none on OAuthClient either. This used to hand back a hardcoded
            # 60/1000, which read like configuration and was not — it could not
            # be seen in any UI or changed by any means short of a code edit,
            # and it caused a production incident nobody could diagnose. The
            # MCP server applies its own configured default (off unless a
            # deployment sets ELEFANT_MCP_RATE_LIMIT_RPM/_RPD) for any token
            # that does not carry its own.
            "memory_read":    self.memory_read,
            "memory_write":   self.memory_write,
            "wiki_read":      self.wiki_read,
            "wiki_write":     self.wiki_write,
            # The board rides along with the wiki grant: both are "author content in
            # my tenant", and a card (thin, tenant-scoped, never embedded) is
            # strictly less powerful than a wiki write. This makes the board
            # tools work over the claude.ai OAuth connector without a separate
            # consent checkbox; a dedicated board scope on the consent page can
            # split these later if the need arises.
            "board_read":     self.wiki_read,
            "board_write":    self.wiki_write,
            "allowed_labels": [],
            "classification_access": [],
            # Source attribution is derived from client_name rather than a
            # user-set field — OAuth clients are pre-registered by the user
            # who controls naming; deriving here makes the field structurally
            # immutable (the only way to change it is to register a new
            # client). For ApiToken (Bearer) source see ApiToken.source.
            "source":         _slugify_source(self.client_name),
        }


# ── Identity & access-control models ───────────────────────────────────────
# UserAccount unifies local-auth and IdP-auth users in one table.

class UserAccount(Base):
    """Unified user record supporting local, OIDC, and SAML authentication."""

    __tablename__ = "user_accounts"
    __table_args__ = (
        UniqueConstraint("full_username", name="uq_user_accounts_full_username"),
        UniqueConstraint("org_id", "username", name="uq_user_accounts_org_username"),
    )

    id               = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # NULL org_id = platform_admin (no org affiliation, multi-org only)
    org_id           = Column(String(36), nullable=True, index=True)
    # local part of the login name (without @domain)
    username         = Column(String(255), nullable=False)
    # canonical login identity:  username@orgname.local  or  username@orgname.com
    full_username    = Column(String(384), nullable=False)
    display_name     = Column(String(255), nullable=True)
    email            = Column(String(320), nullable=True)
    password_hash    = Column(String(255), nullable=True)    # bcrypt; NULL for IdP-only
    # platform_admin | org_admin | team_admin | user
    role             = Column(String(32),  nullable=False, default="user")
    # local | oidc | saml
    auth_method      = Column(String(16),  nullable=False, default="local")
    external_sub     = Column(String(512), nullable=True)    # IdP subject identifier
    oidc_provider_id = Column(String(36),  nullable=True)
    saml_provider_id = Column(String(36),  nullable=True)
    is_active        = Column(Boolean,     nullable=False, default=True)
    # True until the invite onboarding flow is completed
    invite_pending   = Column(Boolean,     nullable=False, default=False)
    # True when admin generated a one-time password; user must change on next login
    must_change_password = Column(Boolean, nullable=False, default=False)
    can_oauth        = Column(Boolean,     nullable=False, default=True)
    # Extended profile fields
    firstname        = Column(String(255), nullable=True)
    lastname         = Column(String(255), nullable=True)
    title            = Column(String(255), nullable=True)
    department       = Column(String(255), nullable=True)
    phone            = Column(String(64),  nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    last_login_at    = Column(DateTime, nullable=True)
    last_password_reset_at = Column(DateTime, nullable=True)
    # UI colour-scheme preference: "dark" (default, the Zennthic theme),
    # "light", or "auto" (follow the browser/OS prefers-color-scheme). Stored
    # per user so the choice follows them to another browser or device; the SPA
    # also mirrors it into localStorage to apply it before first paint.
    theme            = Column(String(8), nullable=False, default="dark")
    # UI colour palette ("indigo", "forest", …): the second, independent axis
    # beside `theme`. The ids are the ones in static/palettes.json, which is
    # also what the CSS is generated from, and the API validates against it.
    palette          = Column(String(24), nullable=False, default="indigo")
    # Bumped to invalidate ALL of this user's stateless session JWTs at once —
    # on logout, password change, role change, or deactivation. The session
    # token embeds the epoch at mint time; /api/auth/check rejects any token
    # whose epoch no longer matches this row. See SEC-04.
    session_epoch    = Column(Integer, nullable=False, default=0)

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "org_id":        self.org_id,
            "username":      self.username,
            "full_username": self.full_username,
            "display_name":  self.display_name,
            "email":         self.email,
            "role":          self.role,
            "auth_method":   self.auth_method,
            "is_active":     self.is_active,
            "invite_pending": self.invite_pending,
            "must_change_password": bool(self.must_change_password),
            "can_oauth":     bool(self.can_oauth),
            "firstname":     self.firstname,
            "lastname":      self.lastname,
            "title":         self.title,
            "department":    self.department,
            "phone":         self.phone,
            "theme":         self.theme or "dark",
            "palette":       self.palette or "indigo",
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class Team(Base):
    """A Team within an Organization."""

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_teams_org_slug"),
    )

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id     = Column(String(36), nullable=False, index=True)
    name       = Column(String(255), nullable=False)
    slug       = Column(String(63),  nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "org_id":     self.org_id,
            "name":       self.name,
            "slug":       self.slug,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Group(Base):
    """A Group within a Team (sub-team for sharing / ACL purposes)."""

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("team_id", "slug", name="uq_groups_team_slug"),
    )

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id    = Column(String(36), nullable=False, index=True)
    org_id     = Column(String(36), nullable=False, index=True)
    name       = Column(String(255), nullable=False)
    slug       = Column(String(63),  nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "team_id":    self.team_id,
            "org_id":     self.org_id,
            "name":       self.name,
            "slug":       self.slug,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TeamMembership(Base):
    """Many-to-many: UserAccount ↔ Team, with an optional team-admin role."""

    __tablename__ = "team_memberships"

    user_id   = Column(String(36), primary_key=True)
    team_id   = Column(String(36), primary_key=True)
    # "member" | "team_admin"
    role      = Column(String(32), nullable=False, default="member")
    joined_at = Column(DateTime, default=datetime.utcnow)


class GroupMembership(Base):
    """Many-to-many: UserAccount ↔ Group."""

    __tablename__ = "group_memberships"

    user_id   = Column(String(36), primary_key=True)
    group_id  = Column(String(36), primary_key=True)
    joined_at = Column(DateTime, default=datetime.utcnow)


class GroupTeam(Base):
    """
    Many-to-many: Group ↔ Team.  A Group may belong to several Teams so that
    Team Admins from any of those Teams can manage it.  Group.team_id is
    retained as a denormalized 'primary team' cache for legacy reads, and
    every Group has at least one matching row here (backfilled at migrate
    time and on every create_group).
    """

    __tablename__ = "group_teams"

    group_id  = Column(String(36), primary_key=True)
    team_id   = Column(String(36), primary_key=True)
    org_id    = Column(String(36), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class InviteToken(Base):
    """
    One-time invite URL + short code for onboarding new Org Admins or Users.
    The URL embeds the UUID; the user must also type the 6-char code to confirm.
    """

    __tablename__ = "invite_tokens"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    code         = Column(String(12),  nullable=False)       # short code user types
    org_id       = Column(String(36),  nullable=False, index=True)
    target_role  = Column(String(32),  nullable=False)       # role to assign on completion
    target_email = Column(String(320), nullable=True)        # pre-filled email (optional)
    team_id      = Column(String(36),  nullable=True)        # auto-join team on completion
    created_by   = Column(String(36),  nullable=False)       # UserAccount.id
    expires_at   = Column(DateTime,    nullable=False)
    used_at      = Column(DateTime,    nullable=True)         # NULL = not yet used
    used_by      = Column(String(36),  nullable=True)        # UserAccount.id of new user

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "org_id":       self.org_id,
            "target_role":  self.target_role,
            "target_email": self.target_email,
            "team_id":      self.team_id,
            "created_by":   self.created_by,
            "expires_at":   self.expires_at.isoformat() if self.expires_at else None,
            "used":         self.used_at is not None,
        }


class MfaCredential(Base):
    """TOTP MFA credential for a UserAccount."""

    __tablename__ = "mfa_credentials"

    user_id         = Column(String(36), primary_key=True)
    totp_secret_enc = Column(Text, nullable=False)     # Fernet-encrypted TOTP base32 secret
    is_enabled      = Column(Boolean, nullable=False, default=False)
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_used_at    = Column(DateTime, nullable=True)
    last_reset_at   = Column(DateTime, nullable=True)  # for the 1-hour MFA+password reset rule


# ── Access-control models ──────────────────────────────────────────────────

class WikiSpaceACL(Base):
    """
    Grants read or write access to a wiki space (top-level directory) for an
    org, team, group, or individual user.  When no ACL rows exist for a space
    the entire Org can access it (open-by-default, backward-compatible).
    """

    __tablename__ = "wiki_space_acls"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id       = Column(String(36), nullable=False, index=True)
    # Top-level directory name, e.g. "Engineering" (no leading slash)
    space_path   = Column(String(512), nullable=False)
    # org | team | group | user
    grantee_type = Column(String(16),  nullable=False)
    # NULL = entire org; set for team/group/user grants
    grantee_id   = Column(String(36),  nullable=True)
    # reader | writer
    access       = Column(String(16),  nullable=False, default="reader")
    created_at   = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "org_id":       self.org_id,
            "space_path":   self.space_path,
            "grantee_type": self.grantee_type,
            "grantee_id":   self.grantee_id,
            "access":       self.access,
        }


class WikiShare(Base):
    """A shareable link to a wiki page or folder (issue: wiki sharing).

    Phase 1 is public "anyone with the link" sharing: the opaque `token` is
    the only credential and lives in the URL. The share names the owning
    tenant + path server-side, so the public read endpoints never trust a
    caller-supplied tenant and stay strictly scoped to the shared subtree.
    """

    __tablename__ = "wiki_shares"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Opaque, unguessable URL slug (secrets.token_urlsafe). Stored raw so the
    # link stays re-copyable from the UI — it is an "anyone with the link"
    # credential granting read-only access to one subtree, revocable any time.
    token         = Column(String(64), nullable=False, unique=True, index=True)
    org_id        = Column(String(36), nullable=False, index=True)
    created_by    = Column(String(36), nullable=True)          # user_id, best-effort
    resource_type = Column(String(16), nullable=False)         # "page" | "folder"
    path          = Column(String(1024), nullable=False)       # normalized, no leading slash
    include_subfolders = Column(Boolean, nullable=False, default=True)
    audience      = Column(String(16), nullable=False, default="public")  # room for "email" later
    revoked       = Column(Boolean, nullable=False, default=False)
    expires_at    = Column(DateTime, nullable=True)            # None = never
    view_count    = Column(Integer, nullable=False, default=0)
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_viewed_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":                 self.id,
            "token":              self.token,
            "resource_type":      self.resource_type,
            "path":               self.path,
            "include_subfolders": bool(self.include_subfolders),
            "audience":           self.audience,
            "revoked":            bool(self.revoked),
            "expires_at":         self.expires_at.isoformat() if self.expires_at else None,
            "view_count":         self.view_count or 0,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }


class GitSourceACL(Base):
    """
    Intra-org sharing grants for a git source. The source's own org_id
    establishes ownership (only that org's admins can edit/sync/delete it,
    and only its members can see results from it by default). This table
    layers additional restrictions: when grant rows exist for a source,
    only the listed teams / groups / users (within the owning org) can
    see results — otherwise the whole owning org has access.

    Rows from other orgs are ignored — cross-org sharing of sources is
    not supported; if you need that, create the source in each org.
    """

    __tablename__ = "git_source_acls"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id    = Column(String(36), nullable=False, index=True)
    org_id       = Column(String(36), nullable=False, index=True)
    grantee_type = Column(String(16), nullable=False)   # org | team | group | user
    grantee_id   = Column(String(36), nullable=True)    # NULL = entire org
    created_at   = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":           self.id,
            "source_id":    self.source_id,
            "org_id":       self.org_id,
            "grantee_type": self.grantee_type,
            "grantee_id":   self.grantee_id,
        }


class GitSource(Base):
    """
    A synced git repository used as a knowledge source. Owned by exactly
    one org — only that org's admins can list/edit/sync/delete it, and
    only that org's members can search its content (further restrictable
    via GitSourceACL grants within the org).

    PAT/credentials are Fernet-encrypted at rest via token_enc; the
    plaintext is only ever returned to the embedder over the internal
    loopback API and supplied to git via HTTP Basic header at invocation
    time (never embedded in URLs or written to .git/config on disk).
    """

    __tablename__ = "git_sources"

    id                    = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id                = Column(String(36), nullable=False, index=True)
    name                  = Column(String(255), nullable=False)
    repo_url              = Column(String(1024), nullable=False)
    token_enc             = Column(Text, nullable=True)
    branch                = Column(String(255), nullable=False, default="main")
    path_prefix           = Column(String(512), nullable=False, default="")
    sync_interval_minutes = Column(Integer, nullable=False, default=60)
    enabled               = Column(Boolean, nullable=False, default=True)
    # Per-session tool availability: 'on' (available by default), 'off' (off by
    # default but a session can enable it), or 'admin_off' (administratively
    # off — cannot be enabled in a session).
    tool_availability     = Column(String(16), nullable=False, default="on")
    created_at            = Column(DateTime, default=datetime.utcnow)
    updated_at            = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def token(self) -> str:
        return crypto.decrypt(self.token_enc or "") if self.token_enc else ""

    def set_token(self, raw: str) -> None:
        self.token_enc = crypto.encrypt(raw) if raw else None

    def to_dict(self, *, include_token: bool = False, include_org_id: bool = False) -> dict:
        d = {
            "id":                    self.id,
            "name":                  self.name,
            "repo_url":              self.repo_url,
            "branch":                self.branch,
            "path_prefix":           self.path_prefix,
            "sync_interval_minutes": self.sync_interval_minutes,
            "enabled":               self.enabled,
            "tool_availability":     self.tool_availability or "on",
            "created_at":            self.created_at.isoformat() if self.created_at else None,
            "updated_at":            self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_org_id:
            d["org_id"] = self.org_id
        if include_token:
            d["token"] = self.token
        return d


class MemorySharingPolicy(Base):
    """
    Controls whether memories are siloed per-user or shared across a
    team or group.  Without a policy row, members only see their own memories.
    """

    __tablename__ = "memory_sharing_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "scope_type", "scope_id",
                         name="uq_memory_sharing_org_scope"),
    )

    id         = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id     = Column(String(36), nullable=False, index=True)
    scope_type = Column(String(16), nullable=False)   # team | group
    scope_id   = Column(String(36), nullable=False)
    # individual = members see own memories only
    # shared     = members see all memories in the team/group pool
    share_mode = Column(String(16), nullable=False, default="individual")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "org_id":     self.org_id,
            "scope_type": self.scope_type,
            "scope_id":   self.scope_id,
            "share_mode": self.share_mode,
        }


class MemoryEditNotification(Base):
    """
    Notifies the original author of a memory when an admin edits it.
    Shown as a bell-icon alert in the web UI.
    """

    __tablename__ = "memory_edit_notifications"

    id                 = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    memory_id          = Column(String(36), nullable=False, index=True)
    original_author_id = Column(String(36), nullable=False, index=True)
    edited_by_id       = Column(String(36), nullable=False)
    edited_at          = Column(DateTime, default=datetime.utcnow)
    seen               = Column(Boolean, nullable=False, default=False)

    def to_dict(self) -> dict:
        return {
            "id":                 self.id,
            "memory_id":          self.memory_id,
            "original_author_id": self.original_author_id,
            "edited_by_id":       self.edited_by_id,
            "edited_at":          self.edited_at.isoformat() if self.edited_at else None,
            "seen":               self.seen,
        }


class SoftDeletedPage(Base):
    """Tracks soft-deleted wiki pages (recycle bin + archive)."""

    __tablename__ = "soft_deleted_pages"

    id            = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_path = Column(String(1024), nullable=False)
    name          = Column(String(255), nullable=False)
    content       = Column(Text, nullable=False)
    deleted_at    = Column(DateTime, default=datetime.utcnow)
    status        = Column(String(16), nullable=False, default="recycled")
    tenant_id     = Column(String(36), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "original_path": self.original_path,
            "name":          self.name,
            "deleted_at":    self.deleted_at.isoformat() if self.deleted_at else None,
            "status":        self.status,
        }


class WikiRevision(Base):
    """Append-only edit history for wiki pages — powers the History view
    (list · diff · revert).

    One row per saved version, capturing the RESULTING content, the parsed
    frontmatter (tags/title), who made the change, and which fields moved vs
    the previous tracked version. The sibling of memory_revisions on the wiki
    side, and of SoftDeletedPage (recycle bin) in this module.

    List-valued columns (tags, changed_fields) are JSON-encoded TEXT so the
    schema stays portable across SQLite and PostgreSQL — the editor config DB
    runs on either. tenant_id holds the Tenant.id (same convention as
    SoftDeletedPage), so history is scoped per org.
    """

    __tablename__ = "wiki_revisions"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id      = Column(String(36), nullable=True)
    path           = Column(String(1024), nullable=False)
    content        = Column(Text, nullable=False)
    tags           = Column(Text, nullable=True)        # JSON-encoded list[str]
    title          = Column(String(512), nullable=True)
    edited_by_id   = Column(String(64), nullable=True)
    edited_by_name = Column(String(255), nullable=True)
    source         = Column(String(64), nullable=True)  # editor | mcp | <token source> | revert
    message        = Column(String(512), nullable=True)
    changed_fields = Column(Text, nullable=True)        # JSON-encoded list[str]
    created        = Column(Boolean, nullable=False, default=False)
    edited_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("wiki_revisions_tenant_path_idx", "tenant_id", "path", "edited_at"),
    )

    def to_dict(self, include_content: bool = True) -> dict:
        d = {
            "id":             self.id,
            "path":           self.path,
            "tags":           json.loads(self.tags) if self.tags else [],
            "title":          self.title,
            "edited_by_id":   self.edited_by_id,
            "edited_by_name": self.edited_by_name,
            "source":         self.source,
            "message":        self.message,
            "changed_fields": json.loads(self.changed_fields) if self.changed_fields else [],
            "created":        bool(self.created),
            "edited_at":      self.edited_at.isoformat() if self.edited_at else None,
            "size":           len(self.content or ""),
        }
        if include_content:
            d["content"] = self.content
        return d


class WikiPageLock(Base):
    """A human-applied write lock on a single wiki page.

    A row's existence IS the lock — unlocking deletes it. While a page is
    locked every write is refused for everybody: the person who locked it, any
    other user, and any AI agent on the token-authed MCP path. Only a
    session-authed human can lock or unlock, which is the whole point: it lets
    an `AI/` page be frozen in place instead of moved out of the tree the
    agents are allowed to write to.

    This is purely a write lock. Nothing here touches indexing, retrieval or
    the confidence the search layer assigns an `AI/` page.

    tenant_id holds the Tenant.id, matching WikiRevision and SoftDeletedPage,
    so a lock is scoped to one org's wiki.
    """

    __tablename__ = "wiki_page_locks"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id      = Column(String(36), nullable=True)
    path           = Column(String(1024), nullable=False)
    locked_by_id   = Column(String(64), nullable=True)
    locked_by_name = Column(String(255), nullable=True)
    reason         = Column(String(512), nullable=True)
    locked_at      = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "path", name="wiki_page_locks_tenant_path_uq"),
    )

    def to_dict(self) -> dict:
        return {
            "locked":         True,
            "path":           self.path,
            "locked_by_id":   self.locked_by_id,
            "locked_by_name": self.locked_by_name,
            "reason":         self.reason,
            "locked_at":      self.locked_at.isoformat() if self.locked_at else None,
        }


class ConfigEntry(Base):
    """Stores configuration key-value pairs with encryption."""

    __tablename__ = "config_entries"

    key = Column(String(255), primary_key=True, index=True)
    encrypted_value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_value(self) -> Optional[Dict]:
        decrypted = crypto.decrypt(self.encrypted_value)
        if not decrypted:
            return None
        try:
            return json.loads(decrypted)
        except json.JSONDecodeError:
            return None

    def set_value(self, value: Dict) -> None:
        json_str = json.dumps(value)
        self.encrypted_value = crypto.encrypt(json_str)


class MemoryBackupConfig(Base):
    """Configuration for automatic memory backups."""

    __tablename__ = "memory_backup_config"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(255), default=DEFAULT_TENANT_SLUG, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    interval_hours = Column(Integer, default=24)
    retention_days = Column(Integer, default=30)
    last_backup_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WikiBackupConfig(Base):
    """Configuration for automatic wiki backups (mirrors MemoryBackupConfig)."""

    __tablename__ = "wiki_backup_config"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(String(255), default=DEFAULT_TENANT_SLUG, nullable=False, index=True)
    enabled = Column(Boolean, default=True)
    interval_hours = Column(Integer, default=24)
    retention_days = Column(Integer, default=30)
    last_backup_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TenantSettings(Base):
    """Per-tenant programmatic settings — real stored values, NOT semantic memory.

    Read once at session start by the MCP `get_user_settings` tool (and the
    `kb://settings` resource), and consumed by the memory-api compactor for
    per-tenant hot-tier thresholds. Keyed by tenant *slug* — the same space the
    hot tier and compactor already use (cf. MemoryBackupConfig).

    A missing row, or any NULL field, means "inherit the system default". The
    defaults themselves are never persisted here, so changing a global default
    later still applies wherever a tenant has not set an explicit override.
    """

    __tablename__ = "tenant_settings"

    SETTABLE = (
        "second_brain_mode",
        "hot_tier_promote_score",
        "hot_tier_drop_score",
        "hot_tier_drop_age_days",
        "hot_tier_cluster_threshold",
        "board_nudge_chars",
    )

    tenant_slug                = Column(String(255), primary_key=True, default=DEFAULT_TENANT_SLUG)
    second_brain_mode          = Column(Boolean, nullable=True)
    hot_tier_promote_score     = Column(Float, nullable=True)
    hot_tier_drop_score        = Column(Float, nullable=True)
    hot_tier_drop_age_days     = Column(Float, nullable=True)
    hot_tier_cluster_threshold = Column(Float, nullable=True)
    # Board post-it nudge threshold: card_add / card_update warn (never block)
    # once a card body passes this many characters — the signal to write the
    # idea up as a wiki page and keep the sticky thin. NULL = global default
    # (memory-api BOARD_NUDGE_DEFAULT = 280). Agent-settable, low-risk.
    board_nudge_chars          = Column(Integer, nullable=True)
    # Per-org backup passcode (Fernet-encrypted at rest). Deliberately NOT
    # exposed via to_dict()/get_user_settings — it's a secret, not an
    # agent-readable setting.
    backup_passcode_enc        = Column(Text, nullable=True)
    # Top-level wiki folder AI agents are allowed to write into (via the MCP
    # wiki_write tool). NULL = use the global default ("AI"). Admin-configured
    # only — never settable through the agent-facing settings path, since an
    # agent must not be able to move its own write boundary.
    ai_wiki_folder             = Column(String(128), nullable=True)
    created_at                 = Column(DateTime, default=datetime.utcnow)
    updated_at                 = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "second_brain_mode":          self.second_brain_mode,
            "hot_tier_promote_score":     self.hot_tier_promote_score,
            "hot_tier_drop_score":        self.hot_tier_drop_score,
            "hot_tier_drop_age_days":     self.hot_tier_drop_age_days,
            "hot_tier_cluster_threshold": self.hot_tier_cluster_threshold,
            "board_nudge_chars":          self.board_nudge_chars,
            "ai_wiki_folder":             self.ai_wiki_folder,
            "updated_at":                 self.updated_at.isoformat() if self.updated_at else None,
        }

    @staticmethod
    def empty_dict() -> dict:
        """Payload for a tenant with no row yet — every field at its default."""
        return {
            "second_brain_mode":          None,
            "hot_tier_promote_score":     None,
            "hot_tier_drop_score":        None,
            "hot_tier_drop_age_days":     None,
            "hot_tier_cluster_threshold": None,
            "board_nudge_chars":          None,
            "ai_wiki_folder":             None,
            "updated_at":                 None,
        }


class OrgBackupS3Config(Base):
    """Platform-level S3-compatible storage config for org backups (singleton row)."""

    __tablename__ = "org_backup_s3_config"

    id             = Column(Integer, primary_key=True)
    enabled        = Column(Boolean, default=False, nullable=False)
    endpoint_url   = Column(Text, nullable=True)   # MinIO / custom endpoint; NULL = AWS
    access_key_enc = Column(Text, nullable=True)   # Fernet-encrypted
    secret_key_enc = Column(Text, nullable=True)   # Fernet-encrypted
    bucket         = Column(String(255), nullable=True)
    region         = Column(String(64),  nullable=True, default="us-east-1")
    prefix         = Column(String(255), nullable=True, default="org-backups/")
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, *, masked: bool = True) -> dict:
        return {
            "enabled":      self.enabled,
            "endpoint_url": self.endpoint_url,
            "access_key":   "***" if masked and self.access_key_enc else (
                crypto.decrypt(self.access_key_enc) if self.access_key_enc else ""),
            "secret_key":   "***" if masked and self.secret_key_enc else (
                crypto.decrypt(self.secret_key_enc) if self.secret_key_enc else ""),
            "bucket":       self.bucket,
            "region":       self.region or "us-east-1",
            "prefix":       self.prefix or "org-backups/",
        }


class OrgBackupSSHTarget(Base):
    """Platform-level SSH/SFTP backup target for org snapshots (singleton)."""

    __tablename__ = "org_backup_ssh_target"

    id              = Column(Integer, primary_key=True)
    enabled         = Column(Boolean, default=False, nullable=False)
    host            = Column(String(255), nullable=True)
    port            = Column(Integer, default=22)
    username        = Column(String(255), nullable=True)
    remote_path     = Column(String(512), nullable=True)   # base remote directory
    password_enc    = Column(Text, nullable=True)           # Fernet-encrypted
    private_key_enc = Column(Text, nullable=True)           # Fernet-encrypted PEM
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, *, masked: bool = True) -> dict:
        return {
            "enabled":      self.enabled,
            "host":         self.host,
            "port":         self.port or 22,
            "username":     self.username,
            "remote_path":  self.remote_path,
            "auth_method":  "key" if self.private_key_enc else ("password" if self.password_enc else "none"),
            "password":     "***" if masked and self.password_enc else (
                crypto.decrypt(self.password_enc) if self.password_enc else ""),
            "private_key":  "***" if masked and self.private_key_enc else (
                crypto.decrypt(self.private_key_enc) if self.private_key_enc else ""),
        }


class OrgBackupSchedule(Base):
    """Per-org automatic backup schedule and target selection."""

    __tablename__ = "org_backup_schedules"

    id             = Column(Integer, primary_key=True)
    org_id         = Column(String(36), nullable=False, unique=True, index=True)
    enabled        = Column(Boolean, default=True,  nullable=False)
    interval_hours = Column(Integer,  default=24,   nullable=False)
    retention_days = Column(Integer,  default=30,   nullable=False)
    # Which targets to write each backup to (any combination)
    use_local      = Column(Boolean, default=True,  nullable=False)
    use_s3         = Column(Boolean, default=False, nullable=False)
    use_ssh        = Column(Boolean, default=False, nullable=False)
    # Per-org override for the platform backup passcode (Fernet-encrypted).
    # NULL => fall back to the platform-global passcode.
    passcode_enc   = Column(Text, nullable=True)
    last_backup_at = Column(DateTime, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "org_id":         self.org_id,
            "enabled":        self.enabled,
            "interval_hours": self.interval_hours,
            "retention_days": self.retention_days,
            "use_local":      self.use_local,
            "use_s3":         self.use_s3,
            "use_ssh":        self.use_ssh,
            "last_backup_at": self.last_backup_at.isoformat() if self.last_backup_at else None,
        }


class OrgRestoreJob(Base):
    """A platform org restore, as a durable record rather than a thread.

    A restore runs for minutes and the operator who started it is not obliged
    to keep the tab open, so the job's state cannot live only in the process
    that happens to be running it. Progress is mirrored here often enough that
    a browser coming back — on a refresh, from another machine, an hour later
    — can find the job by org instead of by an id it no longer has, and read
    what actually happened.

    `status` is the life cycle: staged (previewed, awaiting the operator's
    go-ahead) → queued → running ⇄ paused → done | cancelled | error.
    `phases_done` is the honest part: a cancel after the memories phase has
    started leaves an org partly restored, and this says how far it got.
    """

    __tablename__ = "org_restore_jobs"

    id            = Column(String(32), primary_key=True)
    org_id        = Column(String(36), nullable=False, index=True)
    org_slug      = Column(String(255), nullable=True)
    # What the operator picked: a stored backup id, or an uploaded filename.
    source_label  = Column(Text, nullable=True)
    # Staged bundle awaiting confirmation; removed once the job finishes.
    staged_path   = Column(Text, nullable=True)
    status        = Column(String(16), default="staged", nullable=False, index=True)
    phase         = Column(String(32), nullable=True)
    phase_detail  = Column(Text, nullable=True)
    processed     = Column(Integer, default=0, nullable=False)
    total         = Column(Integer, default=0, nullable=False)
    percent       = Column(Float,   default=0.0, nullable=False)
    eta_seconds   = Column(Integer, nullable=True)
    phases_done   = Column(Text, nullable=True)   # JSON array of phase names
    # Which areas were restored, in which mode, and whether embedding was
    # deferred. A restore is destructive and partial restores are the norm now,
    # so "what was actually asked for" has to outlive the browser that asked.
    plan_json     = Column(Text, nullable=True)
    preview_json  = Column(Text, nullable=True)   # what the operator approved
    result_json   = Column(Text, nullable=True)
    error         = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at    = Column(DateTime, nullable=True)
    finished_at   = Column(DateTime, nullable=True)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # A restore is destructive, so a finished job is kept as evidence rather
    # than deleted; ACTIVE is what the UI reattaches to.
    ACTIVE = ("staged", "queued", "running", "paused", "pausing", "cancelling")

    def to_dict(self) -> dict:
        return {
            "job_id":       self.id,
            "org_id":       self.org_id,
            "org_slug":     self.org_slug,
            "source_label": self.source_label,
            "status":       self.status,
            "phase":        self.phase,
            "phase_detail": self.phase_detail,
            "processed":    self.processed or 0,
            "total":        self.total or 0,
            "percent":      round(self.percent or 0.0, 1),
            "eta_seconds":  self.eta_seconds,
            "phases_done":  json.loads(self.phases_done) if self.phases_done else [],
            "plan":         json.loads(self.plan_json) if self.plan_json else None,
            "preview":      json.loads(self.preview_json) if self.preview_json else None,
            "result":       json.loads(self.result_json) if self.result_json else None,
            "error":        self.error,
            "created_at":   self.created_at.isoformat() + "Z" if self.created_at else None,
            "started_at":   self.started_at.isoformat() + "Z" if self.started_at else None,
            "finished_at":  self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }


# ── Tenant-routing helpers ─────────────────────────────────────────────────

def tenant_wiki_collection(slug: str) -> str:
    """Return the Qdrant collection name for the wiki corpus of this tenant."""
    return "knowledge_base" if slug == DEFAULT_TENANT_SLUG else f"{slug}_wiki"


def tenant_mem_collection(slug: str) -> str:
    """Return the Qdrant collection name for the memories of this tenant."""
    return "memories" if slug == DEFAULT_TENANT_SLUG else f"{slug}_memories"


def tenant_docs_root(slug: str) -> Path:
    """Return the /docs subdirectory for this tenant."""
    base = Path(os.environ.get("DOCS_DIR", "/docs")).resolve()
    return base if slug == DEFAULT_TENANT_SLUG else base / slug


# ── Database initialization ────────────────────────────────────────────────

def _add_column_if_missing(conn, table: str, column: str, column_def: str, dialect: str = "sqlite"):
    """ALTER TABLE ADD COLUMN — no-op if column already exists. Works with SQLite and PostgreSQL."""
    if dialect == "postgresql":
        result = conn.execute(
            text("SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
            {"t": table, "c": column},
        )
        exists = result.fetchone() is not None
    else:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        exists = any(r[1] == column for r in rows)
    if not exists:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}"))


def _coerce_int_column_to_boolean(conn, table: str, column: str, dialect: str):
    """Postgres-only: if `column` was added as INTEGER but the model declares it
    as Boolean, convert it in place. SQLite is permissive — no-op there."""
    if dialect != "postgresql":
        return
    row = conn.execute(
        text("SELECT data_type FROM information_schema.columns "
             "WHERE table_name=:t AND column_name=:c"),
        {"t": table, "c": column},
    ).fetchone()
    if row and row[0] == "integer":
        conn.execute(text(
            f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT, "
            f"ALTER COLUMN {column} TYPE BOOLEAN USING ({column} <> 0), "
            f"ALTER COLUMN {column} SET DEFAULT FALSE"
        ))


def _run_migrations(eng):
    """
    Idempotent schema migrations for columns added after initial release.
    Works with both SQLite (ADD COLUMN) and PostgreSQL.
    """
    dialect = eng.dialect.name   # "sqlite" | "postgresql"
    with eng.connect() as conn:
        # ApiToken — multi-tenant columns
        _add_column_if_missing(conn, "api_tokens", "tenant_id", "TEXT", dialect)
        _add_column_if_missing(conn, "api_tokens", "user_id",   "TEXT", dialect)
        # ApiToken — source attribution (host/agent the token belongs to)
        _add_column_if_missing(conn, "api_tokens", "source",    "TEXT", dialect)
        # Tag pre-existing tokens as 'legacy' so memories written through them
        # carry an honest source attribution instead of NULL/unknown.
        conn.execute(text("UPDATE api_tokens SET source = 'legacy' WHERE source IS NULL"))
        # OAuthClient — tenant scope
        _add_column_if_missing(conn, "oauth_clients",       "tenant_id", "TEXT", dialect)
        # OAuthAccessToken — tenant scope and authorizing user
        _add_column_if_missing(conn, "oauth_access_tokens", "tenant_id", "TEXT", dialect)
        _add_column_if_missing(conn, "oauth_access_tokens", "user_id",   "TEXT", dialect)
        # OAuthCode — authorizing user (carried into the access token)
        _add_column_if_missing(conn, "oauth_codes",         "user_id",   "TEXT", dialect)
        # OAuthAccessToken — link back to the code it was minted from, so a
        # replay of that code can revoke the whole token family.
        _add_column_if_missing(conn, "oauth_access_tokens", "from_code", "TEXT", dialect)
        # OAuthAccessToken — audit stamp for owner-initiated scope edits made
        # after issuance (narrow OR widen via PATCH /api/me/oauth-sessions).
        _add_column_if_missing(conn, "oauth_access_tokens", "scope_modified_at", "TIMESTAMP", dialect)
        # SoftDeletedPage — tenant isolation
        _add_column_if_missing(conn, "soft_deleted_pages", "tenant_id", "TEXT", dialect)
        # Tenant — org identity and policy fields (Phase 1)
        _add_column_if_missing(conn, "tenants", "display_name",        "TEXT", dialect)
        _add_column_if_missing(conn, "tenants", "domain",              "TEXT", dialect)
        _bool_def = "BOOLEAN NOT NULL DEFAULT FALSE" if dialect == "postgresql" else "INTEGER NOT NULL DEFAULT 0"
        _add_column_if_missing(conn, "tenants", "domain_verified",     _bool_def, dialect)
        _add_column_if_missing(conn, "tenants", "domain_verify_token", "TEXT", dialect)
        _add_column_if_missing(conn, "tenants", "mfa_policy",          "TEXT NOT NULL DEFAULT 'enforced'", dialect)
        _add_column_if_missing(conn, "tenants", "max_teams",           "INTEGER NOT NULL DEFAULT 5", dialect)
        _add_column_if_missing(conn, "tenants", "setup_complete",      _bool_def, dialect)
        # Heal columns that prior versions of this migration created as
        # INTEGER on Postgres (the model has always been Boolean).
        _coerce_int_column_to_boolean(conn, "tenants", "domain_verified", dialect)
        _coerce_int_column_to_boolean(conn, "tenants", "setup_complete",  dialect)
        _add_column_if_missing(conn, "tenants", "saas_plan",           "TEXT", dialect)

        # ApiToken — standalone graph-visualization scope
        _add_column_if_missing(conn, "api_tokens", "graph_read", _bool_def, dialect)
        # ApiToken — Board scopes. board_read defaults TRUE (reading a tenant's
        # own board is the same low-risk class as memory_read/wiki_read, so
        # pre-existing tokens can view boards immediately); board_write is
        # opt-in like every other write scope.
        _bool_true_def = "BOOLEAN NOT NULL DEFAULT TRUE" if dialect == "postgresql" else "INTEGER NOT NULL DEFAULT 1"
        _add_column_if_missing(conn, "api_tokens", "board_read",  _bool_true_def, dialect)
        _add_column_if_missing(conn, "api_tokens", "board_write", _bool_def, dialect)

        # OidcState — nonce bound into the id_token (SEC-10)
        _add_column_if_missing(conn, "oidc_states", "nonce", "TEXT", dialect)

        # UserAccount — extended profile fields
        _add_column_if_missing(conn, "user_accounts", "firstname",   "TEXT", dialect)
        _add_column_if_missing(conn, "user_accounts", "lastname",    "TEXT", dialect)
        _add_column_if_missing(conn, "user_accounts", "title",       "TEXT", dialect)
        _add_column_if_missing(conn, "user_accounts", "department",  "TEXT", dialect)
        _add_column_if_missing(conn, "user_accounts", "phone",       "TEXT", dialect)
        _add_column_if_missing(conn, "user_accounts", "session_epoch",
                               "INTEGER NOT NULL DEFAULT 0", dialect)
        _add_column_if_missing(conn, "user_accounts", "theme",
                               "TEXT NOT NULL DEFAULT 'dark'", dialect)
        _add_column_if_missing(conn, "user_accounts", "palette",
                               "TEXT NOT NULL DEFAULT 'indigo'", dialect)
        _add_column_if_missing(conn, "user_accounts", "must_change_password",
                               _bool_def, dialect)
        _coerce_int_column_to_boolean(conn, "user_accounts", "must_change_password", dialect)

        # Tenant — lifecycle status columns
        _add_column_if_missing(conn, "tenants", "lifecycle_status", "TEXT NOT NULL DEFAULT 'active'", dialect)
        _add_column_if_missing(conn, "tenants", "recycled_at",      "TIMESTAMP", dialect)

        # Tenant — per-org wiki content storage backend (PR 3: SaaS Data Boundaries)
        _add_column_if_missing(conn, "tenants", "storage_backend",  "TEXT NOT NULL DEFAULT 'legacy'", dialect)

        # GitSource — per-session tool availability ('on' | 'off' | 'admin_off').
        _add_column_if_missing(conn, "git_sources", "tool_availability",
                               "TEXT NOT NULL DEFAULT 'on'", dialect)

        # Backup encryption passcodes (Fernet-encrypted at rest).
        _add_column_if_missing(conn, "org_backup_schedules", "passcode_enc", "TEXT", dialect)
        _add_column_if_missing(conn, "tenant_settings", "backup_passcode_enc", "TEXT", dialect)
        # Per-tenant AI-writable wiki folder (admin-configured; NULL = "AI").
        _add_column_if_missing(conn, "tenant_settings", "ai_wiki_folder", "TEXT", dialect)
        # Per-tenant Board post-it nudge threshold (NULL = default 280).
        _add_column_if_missing(conn, "tenant_settings", "board_nudge_chars", "INTEGER", dialect)

        # OrgDomain — partial unique index to enforce one primary per org
        # (Postgres only; SQLite enforces this in application code).
        if dialect == "postgresql":
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uniq_org_primary_domain "
                "ON org_domains (org_id) WHERE is_primary = TRUE"
            ))
        conn.commit()


def _backfill_org_domains(eng):
    """
    For every Tenant with a non-null ``domain`` but no matching OrgDomain
    row, create a primary OrgDomain row carrying the existing verification
    state.  Idempotent — safe to run on every boot.
    """
    import secrets as _secrets
    with sessionmaker(bind=eng)() as session:
        tenants = session.query(Tenant).filter(Tenant.domain.isnot(None)).all()
        for t in tenants:
            existing = session.query(OrgDomain).filter_by(org_id=t.id).first()
            if existing:
                continue
            session.add(OrgDomain(
                org_id=t.id,
                domain=t.domain,
                is_primary=True,
                verified=bool(t.domain_verified),
                verify_token=t.domain_verify_token or _secrets.token_hex(8),
                verified_at=datetime.utcnow() if t.domain_verified else None,
            ))
        session.commit()


def _backfill_lifecycle_status(eng):
    """
    One-time backfill: existing rows with is_active=False and lifecycle_status
    still set to the default 'active' should be treated as 'offboarded'.
    Idempotent — safe to run on every boot.
    """
    with sessionmaker(bind=eng)() as session:
        stale = session.query(Tenant).filter(
            Tenant.is_active == False,
            Tenant.lifecycle_status == "active",
        ).all()
        for t in stale:
            t.lifecycle_status = "offboarded"
        if stale:
            session.commit()


def _ensure_default_tenant(eng):
    """Create the default tenant / org record if it doesn't exist."""
    with sessionmaker(bind=eng)() as session:
        existing = session.query(Tenant).filter_by(id=DEFAULT_TENANT_ID).first()
        if not existing:
            session.add(Tenant(
                id=DEFAULT_TENANT_ID,
                name="Default",
                slug=DEFAULT_TENANT_SLUG,
                display_name="Default Organization",
                domain="default.local",
                domain_verified=False,
                mfa_policy="enforced",
                max_teams=5,
                setup_complete=True,  # self-hosted default org skips wizard
                is_active=True,
            ))
            session.commit()


def _backfill_group_teams(eng):
    """
    For every Group with a non-null ``team_id``, ensure a matching row exists
    in ``group_teams``.  Idempotent — only inserts rows that don't already
    exist.  Run on every boot so freshly migrated DBs and pre-multi-team
    deployments self-heal.
    """
    with sessionmaker(bind=eng)() as session:
        groups = session.query(Group).filter(Group.team_id.isnot(None)).all()
        for g in groups:
            existing = session.query(GroupTeam).filter_by(
                group_id=g.id, team_id=g.team_id
            ).first()
            if existing:
                continue
            session.add(GroupTeam(
                group_id=g.id,
                team_id=g.team_id,
                org_id=g.org_id,
            ))
        session.commit()


def init_db():
    """Create all tables and run migrations."""
    try:
        eng = _init_engine()
        Base.metadata.create_all(bind=eng)
        _run_migrations(eng)
        _ensure_default_tenant(eng)
        _backfill_org_domains(eng)
        _backfill_group_teams(eng)
        _backfill_lifecycle_status(eng)
        _migrate_git_sources_blob_to_rows(eng)
    except Exception as e:
        print(f"Error: Failed to initialize database: {e}")
        raise


def _migrate_git_sources_blob_to_rows(eng) -> None:
    """One-shot migration: move git sources out of the global
    config_entries["git_sources"] JSON blob into per-org GitSource rows.

    Existing sources are assigned to the org indicated by their first
    GitSourceACL row if any exist, otherwise to the default tenant. The
    operator can move them between orgs afterwards via direct DB edit.
    Once migrated, the blob row is deleted so the new code path is the
    only source of truth.
    """
    Session = sessionmaker(bind=eng)
    session = Session()
    try:
        blob_entry = session.query(ConfigEntry).filter_by(key="git_sources").first()
        if not blob_entry:
            return  # nothing to migrate
        try:
            data = blob_entry.get_value() or {}
        except Exception as exc:
            print(f"git_sources blob migration: could not decode blob ({exc!r}) — leaving in place")
            return
        sources = (data or {}).get("sources") or []
        if not sources:
            session.delete(blob_entry)
            session.commit()
            return

        # Pick a fallback org for sources with no ACL rows — the default
        # tenant if it exists, else the oldest tenant. In single-org CE
        # deployments this is the only org and the choice is trivial.
        fallback = (
            session.query(Tenant).filter_by(id=DEFAULT_TENANT_ID).first()
            or session.query(Tenant).order_by(Tenant.created_at).first()
        )
        if not fallback:
            print("git_sources blob migration: no tenants exist — skipping")
            return

        migrated = 0
        for s in sources:
            sid = s.get("id")
            if not sid:
                continue
            if session.query(GitSource).filter_by(id=sid).first():
                continue  # already migrated on a prior boot
            acl_row = session.query(GitSourceACL).filter_by(source_id=sid).order_by(GitSourceACL.created_at).first()
            org_id  = acl_row.org_id if acl_row else fallback.id
            gs = GitSource(
                id                    = sid,
                org_id                = org_id,
                name                  = (s.get("name") or s.get("repo_url") or sid)[:255],
                repo_url              = (s.get("repo_url") or "")[:1024],
                branch                = (s.get("branch") or "main")[:255],
                path_prefix           = (s.get("path_prefix") or "")[:512],
                sync_interval_minutes = int(s.get("sync_interval_minutes") or 60),
                enabled               = bool(s.get("enabled", True)),
            )
            gs.set_token(s.get("token") or "")
            session.add(gs)
            migrated += 1

        session.delete(blob_entry)
        session.commit()
        print(f"git_sources blob migration: migrated {migrated} source(s) into per-org rows")
    finally:
        session.close()


def get_db_session():
    """Dependency for FastAPI to get a database session."""
    try:
        if SessionLocal is None:
            _init_engine()
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()
    except Exception as e:
        print(f"Error: Failed to get database session: {e}")
        raise


# ── Configuration helpers ──────────────────────────────────────────────────

class ConfigManager:
    """High-level config get/set with validation."""

    SENSITIVE_FIELDS = {"api_key", "secret", "password", "token"}

    SCHEMAS = {
        "storage_backend": {
            "type": "object",
            "properties": {
                # S3 is the only supported backend — bundled MinIO (the
                # "local" option for self-hosted deployments) or any
                # managed AWS-compatible endpoint. GitHub/GitLab as wiki
                # content backends were dropped; inbound git-source sync
                # for memories is a separate feature still supported.
                "backend": {"type": "string", "enum": ["s3"]},
                "s3_bucket": {"type": "string"},
                "s3_region": {"type": "string"},
                "s3_access_key": {"type": "string"},
                "s3_secret_key": {"type": "string"},
            },
        },
        "embedding_config": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["local", "openai"]},
                "model": {"type": "string"},
                "openai_api_key": {"type": "string"},
                "openai_org_id": {"type": "string"},
                "local_model_path": {"type": "string"},
            },
        },
        "general_settings": {
            "type": "object",
            "properties": {
                "kb_name": {"type": "string"},
                "kb_description": {"type": "string"},
                "is_public": {"type": "boolean"},
            },
        },
    }

    @staticmethod
    def get_config(session, key: str) -> Optional[Dict]:
        entry = session.query(ConfigEntry).filter_by(key=key).first()
        if not entry:
            return None
        return entry.get_value()

    @staticmethod
    def set_config(session, key: str, value: Dict) -> None:
        entry = session.query(ConfigEntry).filter_by(key=key).first()
        if not entry:
            entry = ConfigEntry(key=key)
            session.add(entry)
        entry.set_value(value)
        session.commit()

    @staticmethod
    def mask_sensitive(data: dict, schema_key: str = None) -> dict:
        if not data:
            return data
        masked = data.copy()
        for field, value in masked.items():
            if any(sensitive in field.lower() for sensitive in ConfigManager.SENSITIVE_FIELDS):
                if isinstance(value, str) and len(value) > 4:
                    masked[field] = value[:4] + "*" * (len(value) - 4)
                elif isinstance(value, str):
                    masked[field] = "*" * len(value)
        return masked

    @staticmethod
    def get_raw_config(session, key: str) -> Optional[Dict]:
        entry = session.query(ConfigEntry).filter_by(key=key).first()
        if not entry:
            return None
        return entry.get_value()

    @staticmethod
    def get_all_configs(session) -> dict:
        entries = session.query(ConfigEntry).all()
        result = {}
        for entry in entries:
            value = entry.get_value()
            if value:
                result[entry.key] = ConfigManager.mask_sensitive(value, entry.key)
        return result
