#!/usr/bin/env python3
"""Extract the code blocks our install docs promise actually work.

The README's two quick starts are the install path for everybody who arrives
from the release video, and a README is exactly the kind of file that drifts:
nothing fails when the manifests move and the copy-paste block does not. So the
blocks are MARKED, extracted by this script, and run for real in CI
(`.github/workflows/docs-install.yml`).

A marked block looks like this in the markdown:

    <!-- test: compose-quickstart -->
    ```bash
    docker compose up -d
    ```

    <!-- test: k8s-kustomization file=kustomization.yaml -->
    ```yaml
    apiVersion: kustomize.config.k8s.io/v1beta1
    ```

The marker sits immediately before the fence (blank lines between are fine) and
names the block. `file=` says the block is a FILE to write at that path rather
than a script to run; without it the block is commands.

A tag must be unique across every file scanned, because the runner asks for a
block by name and silently getting the wrong one would be worse than an error.

Usage:
    doc_blocks.py list                 # every tag, with its file and kind
    doc_blocks.py get <tag>            # the block's body on stdout
    doc_blocks.py path <tag>           # the `file=` value, or nothing

Paths default to the docs that carry marked blocks (see DEFAULT_DOCS); pass
`--docs a.md,b.md` to scan something else.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Every doc allowed to carry a tested block. A new one has to be added here, so
# that `list` is the whole truth and the CI job cannot miss a file.
DEFAULT_DOCS = (
    "README.md",
    "docs/install/docker-compose.md",
    "docs/install/kubernetes.md",
)

MARKER = re.compile(r"^<!--\s*test:\s*(?P<tag>[a-z0-9][a-z0-9-]*)(?P<attrs>[^>]*?)-->\s*$")
FENCE = re.compile(r"^(?P<fence>`{3,})(?P<lang>[a-zA-Z0-9]*)\s*$")


class DocBlockError(Exception):
    """A marker that does not describe a runnable block."""


class Block:
    __slots__ = ("tag", "lang", "body", "file", "source", "line")

    def __init__(self, tag, lang, body, file, source, line):
        self.tag = tag
        self.lang = lang
        self.body = body
        self.file = file          # the `file=` value, or None for commands
        self.source = source      # repo-relative markdown path
        self.line = line          # 1-based line of the marker

    @property
    def kind(self):
        return f"file:{self.file}" if self.file else "script"

    def __repr__(self):
        return f"<Block {self.tag} {self.kind} from {self.source}:{self.line}>"


def _attrs(raw: str) -> dict:
    out = {}
    for part in raw.split():
        if "=" not in part:
            raise DocBlockError(f"attribute {part!r} is not key=value")
        k, v = part.split("=", 1)
        out[k] = v
    unknown = set(out) - {"file"}
    if unknown:
        raise DocBlockError(f"unknown attribute(s): {sorted(unknown)}")
    return out


def parse(text: str, source: str = "<string>"):
    """Blocks in one markdown document, in order."""
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        m = MARKER.match(lines[i])
        if not m:
            i += 1
            continue
        marker_line = i + 1
        tag = m.group("tag")
        attrs = _attrs(m.group("attrs"))

        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            raise DocBlockError(f"{source}:{marker_line}: `test: {tag}` ends the file")
        fence = FENCE.match(lines[j])
        if not fence:
            raise DocBlockError(
                f"{source}:{marker_line}: `test: {tag}` is not followed by a fenced "
                f"code block (found {lines[j][:40]!r})"
            )
        closing = fence.group("fence")
        k = j + 1
        body = []
        while k < len(lines) and lines[k].rstrip() != closing:
            body.append(lines[k])
            k += 1
        if k >= len(lines):
            raise DocBlockError(f"{source}:{marker_line}: `test: {tag}` block is never closed")

        blocks.append(Block(tag, fence.group("lang"), "\n".join(body) + "\n",
                            attrs.get("file"), source, marker_line))
        i = k + 1
    return blocks


def collect(docs=DEFAULT_DOCS, repo: Path = REPO):
    """Every marked block across `docs`, keyed by tag. Duplicates are an error."""
    found = {}
    for rel in docs:
        path = repo / rel
        if not path.exists():
            raise DocBlockError(f"{rel} does not exist")
        for block in parse(path.read_text(), rel):
            if block.tag in found:
                first = found[block.tag]
                raise DocBlockError(
                    f"tag {block.tag!r} is used twice: {first.source}:{first.line} "
                    f"and {block.source}:{block.line}. The runner asks for blocks by "
                    f"name, so tags have to be unique."
                )
            found[block.tag] = block
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("list", "get", "path"))
    ap.add_argument("tag", nargs="?")
    ap.add_argument("--docs", default=",".join(DEFAULT_DOCS),
                    help="comma-separated markdown paths to scan")
    args = ap.parse_args(argv)

    docs = tuple(d for d in args.docs.split(",") if d)
    try:
        blocks = collect(docs)
    except DocBlockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.action == "list":
        for tag, b in sorted(blocks.items()):
            print(f"{tag}\t{b.lang or '-'}\t{b.kind}\t{b.source}:{b.line}")
        return 0

    if not args.tag:
        print(f"error: `{args.action}` needs a tag", file=sys.stderr)
        return 2
    if args.tag not in blocks:
        print(f"error: no block tagged {args.tag!r}. Known: {sorted(blocks)}",
              file=sys.stderr)
        return 1

    block = blocks[args.tag]
    if args.action == "get":
        sys.stdout.write(block.body)
    else:
        if block.file:
            print(block.file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
