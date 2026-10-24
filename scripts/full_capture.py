"""Capture the complete raw Wiktextract gzip with pinned, resumable byte ranges."""

import argparse
import gzip
import hashlib
import http.client
import os
import re
import ssl
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .capture import CaptureError, TRANSIENT_HTTP_STATUSES
from .markdown import atomic_write, document, parse_document

CATALOGUE_URL = "https://kaikki.org/dictionary/rawdata.html"
SOURCE_URL = "https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz"
BLOCK_SIZE = 4 * 1024 * 1024
HEADERS = {"Accept-Encoding": "identity", "User-Agent": "EnglishDictionaryArchive/1.0 (source snapshot capture)"}


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def catalogue_evidence(body: bytes) -> dict:
    """Read only links and known evidence fields; never execute source content."""
    text = body.decode("utf-8")
    parser = _Links()
    parser.feed(text)
    links = [urljoin(CATALOGUE_URL, link) for link in parser.links]
    if SOURCE_URL not in links:
        raise CaptureError("The raw catalogue does not link the expected compressed source.")
    dates = re.search(r"structured data extracted on\s+(\d{4}-\d{2}-\d{2}).*?dump dated\s+(\d{4}-\d{2}-\d{2})", text, re.S)
    commits = {}
    for project in ("wiktextract", "wikitextprocessor"):
        pattern = re.compile(rf"https://github\.com/[^/]+/{project}/commit/([0-9a-f]{{40}})")
        match = next((match for link in links if (match := pattern.fullmatch(link))), None)
        if not match:
            raise CaptureError(f"Catalogue lacks a complete {project} commit reference.")
        commits[f"{project}_commit"] = match.group(1)
    if not dates:
        raise CaptureError("Catalogue lacks dump and extraction dates.")
    date.fromisoformat(dates[1])
    date.fromisoformat(dates[2])
    return {"catalogue_url": CATALOGUE_URL, "catalogue_sha256": hashlib.sha256(body).hexdigest(), "extraction_date": dates[1], "dump_date": dates[2], **commits}


def _head(url: str, timeout: float, opener=urlopen) -> dict:
    with opener(Request(url, method="HEAD", headers=HEADERS), timeout=timeout) as response:
        if response.status != 200:
            raise CaptureError(f"Expected HTTP 200 for source HEAD, received {response.status}.")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CaptureError("Source bytes must use identity Content-Encoding.")
        length = response.headers.get("Content-Length", "")
        etag = response.headers.get("ETag", "")
        modified = response.headers.get("Last-Modified", "")
        if not length.isdecimal() or int(length) < 1 or not modified or not etag or etag.startswith("W/"):
            raise CaptureError("Source requires a positive size, strong ETag and Last-Modified for safe resumption.")
        return {"source_url": url, "source_size_bytes": int(length), "etag": etag, "last_modified": modified}


def _status(path: Path, pin: dict, size: int, status: str) -> None:
    total = pin["source_size_bytes"]
    fraction = min(size / total, 1)
    bars = int(fraction * 30)
    metadata = {"kind": "source-download-progress", "status": status, "downloaded_bytes": size, "source_size_bytes": total, "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_write(path, document(metadata, f"# Source download\n\n`{'█' * bars}{'░' * (30 - bars)}` **{fraction:.1%}** — {size:,} / {total:,} compressed bytes\n\nStatus: **{status}**.\n\nThe source contains all Wiktionary languages. English filtering and dictionary import are separate stages.\n\n[Import progress](PROGRESS.md) · [Source catalogue]({CATALOGUE_URL})\n"))


def _download(partial: Path, pin: dict, progress: Path, timeout: float, attempts: int, opener=urlopen) -> None:
    size = partial.stat().st_size if partial.exists() else 0
    total = pin["source_size_bytes"]
    if size > total:
        raise CaptureError("Partial capture exceeds the pinned source size.")
    for attempt in range(attempts):
        if size == total:
            return
        request = Request(pin["source_url"], headers={**HEADERS, "Range": f"bytes={size}-", "If-Range": pin["etag"]})
        try:
            with opener(request, timeout=timeout) as response:
                if response.status != 206:
                    raise CaptureError(f"Expected HTTP 206 for pinned range, received {response.status}; body not read.")
                expected_range = f"bytes {size}-{total - 1}/{total}"
                if response.headers.get("Content-Range") != expected_range:
                    raise CaptureError("Response range differs from the pinned byte interval.")
                if response.headers.get("ETag") != pin["etag"] or response.headers.get("Last-Modified") != pin["last_modified"]:
                    raise CaptureError("The source changed; preserve this cache and select a new snapshot.")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise CaptureError("Unexpected transfer Content-Encoding.")
                if response.headers.get("Content-Length") != str(total - size):
                    raise CaptureError("Response length differs from the pinned range.")
                last_update = 0.0
                with partial.open("ab") as stream:
                    while size < total:
                        block = response.read(min(BLOCK_SIZE, total - size))
                        if not block:
                            raise ConnectionError("Source closed before the pinned byte count.")
                        stream.write(block)
                        size += len(block)
                        now = time.monotonic()
                        if now - last_update >= 5:
                            stream.flush()
                            _status(progress, pin, size, "downloading")
                            last_update = now
                    if response.read(1):
                        raise CaptureError("Source exceeded the advertised byte count.")
                    stream.flush()
                    os.fsync(stream.fileno())
            return
        except HTTPError as error:
            status = error.code
            error.close()
            if status not in TRANSIENT_HTTP_STATUSES or attempt == attempts - 1:
                raise CaptureError(f"Download returned HTTP {status}.") from error
        except (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.IncompleteRead) as error:
            if attempt == attempts - 1:
                raise CaptureError(f"Download interrupted; partial bytes are preserved: {error}") from error
        size = partial.stat().st_size if partial.exists() else 0
        _status(progress, pin, size, "retrying interrupted transfer")
        time.sleep(min(2 ** attempt, 8))


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BLOCK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def capture(cache: Path, progress: Path, timeout: float = 45, attempts: int = 5, opener=urlopen) -> dict:
    if (type(attempts) is not int or not 1 <= attempts <= 10
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 300):
        raise ValueError("Use 1–10 attempts and a timeout in (0, 300] seconds.")
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / ".full-capture.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise CaptureError("Capture is locked; inspect the recorded process before retrying.") from error
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"{os.getpid()}\n")
        pin_path = cache / "PIN.md"
        partial = cache / "raw-wiktextract-data.jsonl.gz.part"
        target = cache / "raw-wiktextract-data.jsonl.gz"
        complete_path = cache / "CAPTURE.md"
        current_pin = _head(SOURCE_URL, timeout, opener)
        if pin_path.exists():
            pin, _ = parse_document(pin_path.read_text(encoding="utf-8"))
            if any(pin.get(key) != value for key, value in current_pin.items()):
                raise CaptureError("Remote source differs from the pinned capture; use a new cache directory.")
            catalogue = (cache / "rawdata.html").read_bytes()
            if hashlib.sha256(catalogue).hexdigest() != pin["catalogue_sha256"]:
                raise CaptureError("Preserved catalogue evidence was modified.")
        else:
            if partial.exists() or target.exists() or complete_path.exists():
                raise CaptureError("Capture bytes exist without a source pin; refusing to infer identity.")
            with opener(Request(CATALOGUE_URL, headers=HEADERS), timeout=timeout) as response:
                if response.status != 200:
                    raise CaptureError("Cannot capture the source catalogue.")
                catalogue = response.read(2 * 1024 * 1024 + 1)
            if len(catalogue) > 2 * 1024 * 1024:
                raise CaptureError("Catalogue exceeds the bounded evidence budget.")
            pin = {"kind": "full-source-pin", **current_pin, **catalogue_evidence(catalogue), "pinned_at": datetime.now(timezone.utc).isoformat()}
            (cache / "rawdata.html").write_bytes(catalogue)
            atomic_write(pin_path, document(pin, "# Pinned complete source\n\nByte ranges must match the strong ETag, Last-Modified and exact compressed size. Catalogue evidence is preserved in `rawdata.html`.\n"))
        if target.exists() and complete_path.exists():
            result, _ = parse_document(complete_path.read_text(encoding="utf-8"))
            if target.stat().st_size != pin["source_size_bytes"] or _digest(target) != result["compressed_sha256"]:
                raise CaptureError("Completed capture has been modified.")
            _status(progress, pin, target.stat().st_size, "complete; verified compressed capture")
            return result
        if target.exists():
            raise CaptureError("Completed bytes lack a manifest; inspect them before recovery.")
        _status(progress, pin, partial.stat().st_size if partial.exists() else 0, "starting or resuming download")
        _download(partial, pin, progress, timeout, attempts, opener)
        _status(progress, pin, pin["source_size_bytes"], "verifying gzip integrity and checksums")
        compressed_sha256 = _digest(partial)
        raw_digest = hashlib.sha256()
        raw_size = 0
        with gzip.open(partial, "rb") as stream:
            for block in iter(lambda: stream.read(BLOCK_SIZE), b""):
                raw_digest.update(block)
                raw_size += len(block)
        if _head(SOURCE_URL, timeout, opener) != current_pin:
            raise CaptureError("Source changed during capture; preserved bytes have not been promoted.")
        result = {**pin, "kind": "full-source-capture", "raw_file": target.name, "compressed_sha256": compressed_sha256, "raw_sha256": raw_digest.hexdigest(), "raw_size_bytes": raw_size, "gzip_integrity": "verified", "completed_at": datetime.now(timezone.utc).isoformat()}
        # The manifest precedes promotion: an interrupted rename can be repeated
        # after the preserved partial is reverified without overwriting evidence.
        atomic_write(complete_path, document(result, "# Complete source capture\n\nThe compressed file was downloaded with pinned byte ranges. Gzip CRC and end-of-stream integrity were checked while hashing all uncompressed bytes. `raw_sha256` covers the uncompressed JSONL; `compressed_sha256` covers the preserved gzip. These local digests identify the captured bytes and are not publisher-signed checksums.\n\nEnglish records must be selected from this multilingual raw source by the import pipeline.\n"))
        os.replace(partial, target)
        _status(progress, pin, pin["source_size_bytes"], "complete; gzip integrity and checksums verified")
        return result
    except Exception:
        if "pin" in locals():
            size = partial.stat().st_size if partial.exists() else (target.stat().st_size if target.exists() else 0)
            _status(progress, pin, size, "stopped; preserved capture requires attention")
        raise
    finally:
        lock.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(".cache/full-20261022"))
    parser.add_argument("--progress", type=Path, default=Path("DOWNLOAD.md"))
    args = parser.parse_args()
    try:
        result = capture(args.cache, args.progress)
    except (CaptureError, ValueError, OSError, EOFError, gzip.BadGzipFile) as error:
        parser.exit(1, f"Full capture failed: {error}\n")
    print(f"Preserved: {args.cache / result['raw_file']}")
    print(f"Compressed bytes: {result['source_size_bytes']}; SHA-256: {result['compressed_sha256']}")
    print(f"Raw bytes: {result['raw_size_bytes']}; SHA-256: {result['raw_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
