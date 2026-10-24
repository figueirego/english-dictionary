"""Opt-in, bounded capture of the raw Kaikki Wiktextract source."""

import argparse
import hashlib
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .markdown import atomic_write, document

SOURCE_URL = "https://kaikki.org/dictionary/raw-wiktextract-data.jsonl"
CATALOGUE_URL = "https://kaikki.org/dictionary/rawdata.html"
MAX_CAPTURE_BYTES = 100 * 1024 * 1024
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class CaptureError(RuntimeError):
    """The source or local cache failed an explicit capture constraint."""


def _read_prefix(url: str, max_bytes: int, timeout: float) -> tuple[bytes, dict]:
    request = Request(url, headers={
        "Range": f"bytes=0-{max_bytes - 1}",
        "Accept-Encoding": "identity",
        "User-Agent": "EnglishDictionaryPilot/0.1 (bounded source capture)",
    })
    with urlopen(request, timeout=timeout) as response:
        if response.status != 206:
            raise CaptureError(f"Expected HTTP 206; received {response.status}. Body not read.")
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+)", content_range)
        if not match:
            raise CaptureError("Invalid Content-Range: expected a byte range starting at zero.")
        end, total = map(int, match.groups())
        expected_bytes = min(max_bytes, total)
        if total < 1 or end + 1 != expected_bytes:
            raise CaptureError("Unexpected byte range: response does not match the requested prefix.")
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding != "identity":
            raise CaptureError("Unexpected Content-Encoding; byte range must contain identity bytes.")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            if not content_length.isdecimal() or int(content_length) != expected_bytes:
                raise CaptureError("Content-Length does not match the advertised range length.")
        # One extra byte detects a server sending beyond the advertised range.
        body = response.read(expected_bytes + 1)
        if len(body) != expected_bytes:
            raise CaptureError(f"Body length mismatch: expected {expected_bytes}, received {len(body)}.")
        headers = {
            name.lower(): response.headers.get(name)
            for name in ("Content-Range", "Content-Length", "Content-Type", "Date", "ETag", "Last-Modified")
            if response.headers.get(name) is not None
        }
    metadata = {
        "source_url": url,
        "catalogue_url": CATALOGUE_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "request_range": f"bytes=0-{max_bytes - 1}",
        "response_status": 206,
        "response_headers": headers,
        "source_size_bytes": total,
        "captured_size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "dump_date": None,
        "extraction_date": None,
        "extractor_commit": None,
    }
    return body, metadata


def fetch_prefix(url: str, max_bytes: int, timeout: float = 30,
                 attempts: int = 3) -> tuple[bytes, dict]:
    """Fetch at most the requested prefix, rejecting unbounded or invalid responses.

    The byte budget applies to each attempt. A failed partial transport may consume
    bytes before a retry. Protocol/integrity failures are never retried silently.
    """
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_CAPTURE_BYTES:
        raise ValueError("max_bytes must be an integer from 1 through 104857600.")
    if type(attempts) is not int or not 1 <= attempts <= 10:
        raise ValueError("attempts must be an integer from 1 through 10.")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
        raise ValueError("timeout must be greater than zero and at most 300 seconds.")
    for attempt in range(attempts):
        try:
            return _read_prefix(url, max_bytes, timeout)
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in TRANSIENT_HTTP_STATUSES or attempt == attempts - 1:
                raise CaptureError(f"Source returned HTTP {status} after {attempt + 1} attempt(s).") from error
        except (URLError, TimeoutError, ConnectionError, OSError) as error:
            if attempt == attempts - 1:
                raise CaptureError(f"Source transport failed after {attempt + 1} attempt(s): {error}") from error
        time.sleep(min(2 ** attempt, 8))
    raise AssertionError("Attempt loop ended without a result")


def _atomic_write_bytes(path: Path, body: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".capture-", delete=False) as stream:
            temporary = stream.name
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def save_capture(cache: Path, body: bytes, metadata: dict) -> None:
    """Preserve an original capture; changing source bytes requires a new cache."""
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / ".capture.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise CaptureError("Cache is locked; inspect the existing capture before retrying.") from error
    try:
        os.close(descriptor)
        raw_path = cache / "raw-prefix.bin"
        manifest_path = cache / "capture.md"
        if raw_path.exists():
            if raw_path.read_bytes() != body:
                raise CaptureError("Source bytes changed; choose a new cache directory to preserve the original capture.")
            if manifest_path.exists():
                return
        elif manifest_path.exists():
            raise CaptureError("A manifest exists without raw bytes; choose a new cache directory.")
        else:
            _atomic_write_bytes(raw_path, body)
        complete_metadata = {
            **metadata,
            "raw_file": raw_path.name,
            "captured_size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        atomic_write(manifest_path, document(complete_metadata, """# Source capture

This is an opt-in, bounded byte-prefix capture, not a complete dictionary snapshot.
The original bytes are preserved in `raw-prefix.bin`; the last line may be incomplete.
Only complete JSONL records may enter the import pipeline.

The SHA-256 digest identifies the downloaded bytes locally. It does not independently
authenticate their origin. HTTP dates are transport metadata, not dump dates.
Dump date, extraction date and extractor revision remain unknown until verified
against the source catalogue for this capture; they must not be inferred.

See the [raw-source catalogue](https://kaikki.org/dictionary/rawdata.html).
This command does not select records, publish an edition or schedule further downloads.
"""))
    finally:
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".cache/capture"))
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    arguments = parser.parse_args()
    try:
        body, metadata = fetch_prefix(SOURCE_URL, arguments.max_bytes)
        save_capture(arguments.cache, body, metadata)
    except (CaptureError, ValueError, OSError) as error:
        parser.exit(1, f"Capture failed: {error}\n")
    print(f"Preserved {len(body)} bytes in {arguments.cache / 'raw-prefix.bin'}")
    print(f"SHA-256: {metadata['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
