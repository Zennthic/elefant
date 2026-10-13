#!/usr/bin/env python3
"""Decrypt an Elefant-encrypted org/wiki/memory backup with its passcode.

Elefant backups are AES-256-GCM encrypted (not plain gzip), so macOS / `tar`
can't open them directly and there is no password prompt. This standalone
tool decrypts one back into a normal archive you can open with `tar`/Finder —
it needs only Python 3 and the `cryptography` package, NOT a running Elefant.

    pip install cryptography
    python3 decrypt-elefant-backup.py  org-backup-YYYYMMDD-HHMMSS.tar.gz  [out.tar.gz]

It prompts for the passcode (the platform or per-org backup passcode that was
in effect when the backup was taken), writes the decrypted archive, then:

    tar xzf out.tar.gz

Format (version 1): magic "ELEFANTBK" | version(1) | salt(16) | nonce(12) |
ciphertext | GCM tag(16). Key = scrypt(passcode, salt, n=2**15, r=8, p=1).
"""
import getpass
import os
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_MAGIC = b"ELEFANTBK"
_VERSION = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
_HEADER_LEN = len(_MAGIC) + 1 + _SALT_LEN + _NONCE_LEN
_CHUNK = 1024 * 1024


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src + ".decrypted.tar.gz"

    with open(src, "rb") as f:
        header = f.read(_HEADER_LEN)
        if len(header) < _HEADER_LEN or header[: len(_MAGIC)] != _MAGIC:
            print("This file is NOT an Elefant-encrypted backup (no magic header).")
            print("If it's already a plain archive, just run:  tar xzf", src)
            return 2
        version = header[len(_MAGIC)]
        if version != _VERSION:
            print(f"Unsupported backup encryption version: {version}")
            return 2
        off = len(_MAGIC) + 1
        salt = header[off : off + _SALT_LEN]
        nonce = header[off + _SALT_LEN : off + _SALT_LEN + _NONCE_LEN]

        f.seek(0, os.SEEK_END)
        total = f.tell()
        body_len = total - _HEADER_LEN - _TAG_LEN
        if body_len < 0:
            print("File is truncated or corrupted.")
            return 2
        f.seek(total - _TAG_LEN)
        tag = f.read(_TAG_LEN)

        passcode = getpass.getpass("Backup passcode: ")
        key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passcode.encode("utf-8"))
        dec = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()

        f.seek(_HEADER_LEN)
        remaining = body_len
        with open(dst, "wb") as out:
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                out.write(dec.update(chunk))
            try:
                out.write(dec.finalize())
            except Exception:
                out.close()
                os.remove(dst)
                print("Decryption FAILED — wrong passcode or the file is corrupted.")
                return 3

    print(f"Decrypted -> {dst}")
    print(f"Now run:  tar xzf {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
