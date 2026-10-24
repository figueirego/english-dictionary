"""A small, deterministic subset of YAML front matter, with JSON values."""

import json
import os
import re
import tempfile
from pathlib import Path


def document(metadata: dict[str, object], body: str) -> str:
    lines = ["---"]
    for key, value in metadata.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise ValueError(f"Invalid metadata key: {key}")
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines) + "\n---\n\n" + body.rstrip() + "\n"


def parse_document(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("Missing Markdown front matter")
    header, body = text[4:].split("\n---\n", 1)
    metadata: dict[str, object] = {}
    for line in header.split('\n'):
        key, separator, value = line.partition(": ")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in metadata:
            raise ValueError("Invalid or duplicate metadata field")
        metadata[key] = json.loads(value)
    return metadata, body.lstrip("\n")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".write-", delete=False, newline="\n") as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
