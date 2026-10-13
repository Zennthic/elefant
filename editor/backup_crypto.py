"""Passcode-based encryption for backup archives (encryption at rest).

Two on-disk formats are supported:

1. AES-256 password ZIP (current — used for new backups). A standard WinZip
   AES-256 encrypted .zip that desktop tools (Keka, The Unarchiver, 7-Zip,
   Windows) can open with the passcode. The archive members are the backup
   components (manifest.json, db.json, memories.json, wiki.tar.gz) so an
   operator can browse them after unlocking. See write_aes_zip/extract_aes_zip.

2. Legacy single-file AES-256-GCM container (version 1) — kept so older
   backups still restore:

       magic(9) "ELEFANTBK" | version(1)=0x01 | salt(16) | nonce(12) | ct | tag(16)

   Key: AES-256 from the passcode via scrypt(salt, n=2**15, r=8, p=1). The
   16-byte GCM tag is appended so both directions stream in fixed chunks; GCM
   authenticates the whole archive, so a wrong passcode or tampering fails at
   finalize.

Caller contract (decrypt_file): decrypt to a temporary path and only trust the
output AFTER decrypt_file() returns without raising — bytes written before the
final authentication check are unverified.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Tuple, Union

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_MAGIC = b"ELEFANTBK"
_VERSION = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
_HEADER_LEN = len(_MAGIC) + 1 + _SALT_LEN + _NONCE_LEN  # 38
_CHUNK = 1024 * 1024  # 1 MiB
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1

_PathLike = Union[str, "os.PathLike[str]"]


class BackupCryptoError(Exception):
    """Raised for any encrypt/decrypt failure (bad passcode, tamper, bad format)."""


def _derive_key(passcode: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passcode.encode("utf-8"))


def is_encrypted(path: _PathLike) -> bool:
    """True if the file begins with the Elefant backup-encryption magic."""
    try:
        with open(path, "rb") as f:
            return f.read(len(_MAGIC)) == _MAGIC
    except OSError:
        return False


def is_encrypted_bytes(data: bytes) -> bool:
    """True if the in-memory blob begins with the backup-encryption magic."""
    return data[: len(_MAGIC)] == _MAGIC


def encrypt_file(src: _PathLike, dst: _PathLike, passcode: str) -> None:
    """Encrypt `src` to `dst` with `passcode`. Streams in chunks."""
    if not passcode:
        raise BackupCryptoError("a passcode is required to encrypt a backup")
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(passcode, salt)
    enc = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(_MAGIC + bytes([_VERSION]) + salt + nonce)
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(enc.update(chunk))
        fout.write(enc.finalize())
        fout.write(enc.tag)


def decrypt_file(src: _PathLike, dst: _PathLike, passcode: str) -> None:
    """Decrypt `src` to `dst` with `passcode`. Raises BackupCryptoError on a
    wrong passcode, tampering, or an unrecognised format. On failure the
    partially written `dst` must be treated as invalid (and deleted)."""
    if not passcode:
        raise BackupCryptoError("a passcode is required to decrypt a backup")
    with open(src, "rb") as fin:
        header = fin.read(_HEADER_LEN)
        if len(header) < _HEADER_LEN or header[: len(_MAGIC)] != _MAGIC:
            raise BackupCryptoError("not an Elefant-encrypted backup")
        version = header[len(_MAGIC)]
        if version != _VERSION:
            raise BackupCryptoError(f"unsupported backup encryption version {version}")
        off = len(_MAGIC) + 1
        salt = header[off : off + _SALT_LEN]
        nonce = header[off + _SALT_LEN : off + _SALT_LEN + _NONCE_LEN]

        fin.seek(0, os.SEEK_END)
        total = fin.tell()
        body_len = total - _HEADER_LEN - _TAG_LEN
        if body_len < 0:
            raise BackupCryptoError("truncated or corrupted backup")
        fin.seek(total - _TAG_LEN)
        tag = fin.read(_TAG_LEN)

        key = _derive_key(passcode, salt)
        dec = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        fin.seek(_HEADER_LEN)
        remaining = body_len
        with open(dst, "wb") as fout:
            while remaining > 0:
                chunk = fin.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                fout.write(dec.update(chunk))
            try:
                fout.write(dec.finalize())
            except Exception as exc:  # InvalidTag etc.
                raise BackupCryptoError(
                    "decryption failed — wrong passcode or corrupted backup"
                ) from exc


# ── AES-256 password ZIP (desktop-openable) ─────────────────────────────────
# A standard WinZip-AES (.zip) container: openable by Keka, The Unarchiver,
# 7-Zip and Windows with the passcode, while still encrypting contents at rest.

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_ZipItem = Tuple[str, Union[bytes, bytearray, _PathLike]]


def is_zip(path: _PathLike) -> bool:
    """True if the file begins with a ZIP local-file/end-of-central-dir magic."""
    try:
        with open(path, "rb") as f:
            return f.read(4) in _ZIP_MAGICS
    except OSError:
        return False


def is_zip_bytes(data: bytes) -> bool:
    """True if the in-memory blob begins with a ZIP magic."""
    return data[:4] in _ZIP_MAGICS


def write_aes_zip(dst: _PathLike, items: Iterable[_ZipItem], passcode: str) -> None:
    """Write `items` into an AES-256 encrypted .zip at `dst`.

    Each item is (arcname, source) where source is either raw bytes or a path
    to a file on disk. Requires a passcode (encryption at rest is enforced).
    """
    if not passcode:
        raise BackupCryptoError("a passcode is required to encrypt a backup")
    import pyzipper

    pw = passcode.encode("utf-8")
    with pyzipper.AESZipFile(str(dst), "w", compression=pyzipper.ZIP_DEFLATED) as zf:
        zf.setpassword(pw)
        zf.setencryption(pyzipper.WZ_AES, nbits=256)
        for arcname, source in items:
            if isinstance(source, (bytes, bytearray)):
                zf.writestr(arcname, bytes(source))
            else:
                zf.write(str(source), arcname=arcname)


def extract_aes_zip(src: _PathLike, dst_dir: _PathLike, passcode: str) -> None:
    """Extract an AES-256 encrypted .zip to `dst_dir`. Raises BackupCryptoError
    on a wrong passcode or a corrupted archive."""
    if not passcode:
        raise BackupCryptoError("a passcode is required to decrypt a backup")
    import pyzipper

    pw = passcode.encode("utf-8")
    try:
        with pyzipper.AESZipFile(str(src), "r") as zf:
            zf.setpassword(pw)
            zf.extractall(str(dst_dir))
    except (RuntimeError, pyzipper.BadZipFile) as exc:
        raise BackupCryptoError(
            "decryption failed — wrong passcode or corrupted backup"
        ) from exc


def read_aes_zip_member(src: _PathLike, arcname: str, passcode: str) -> bytes:
    """Read a single member from an AES-256 encrypted .zip. Raises
    BackupCryptoError on a wrong passcode, corruption, or a missing member."""
    if not passcode:
        raise BackupCryptoError("a passcode is required to decrypt a backup")
    import pyzipper

    pw = passcode.encode("utf-8")
    try:
        with pyzipper.AESZipFile(str(src), "r") as zf:
            zf.setpassword(pw)
            return zf.read(arcname)
    except KeyError as exc:
        raise BackupCryptoError(f"member {arcname!r} not found in backup") from exc
    except (RuntimeError, pyzipper.BadZipFile) as exc:
        raise BackupCryptoError(
            "decryption failed — wrong passcode or corrupted backup"
        ) from exc
