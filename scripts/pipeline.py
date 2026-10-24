"""Build and audit immutable local Markdown editions from a bounded pilot input."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from scripts.entries import entry_path, render_entry
from scripts.markdown import atomic_write, document, parse_document

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_RECORDS = 10_000


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_text(text: str) -> tuple[dict[str, object], list[dict[str, object]]]:
    if len(text.encode()) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Input exceeds the bounded pilot limit")
    metadata, body = parse_document(text)
    if metadata.get("kind") != "source-snapshot" or metadata.get("license") != "CC-BY-SA-4.0":
        raise ValueError("Unsupported source contract or licence")
    for field in ("snapshot_id", "raw_sha256", "records_sha256", "dump_date",
                  "extraction_date", "wiktextract_commit", "catalogue_url"):
        if not isinstance(metadata.get(field), str) or not metadata[field]:
            raise ValueError(f"Missing source metadata: {field}")
    for field in ("raw_sha256", "records_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(metadata[field])):
            raise ValueError(f"Invalid checksum: {field}")
    opening = "```jsonl\n"
    if body.count(opening) != 1:
        raise ValueError("Source must contain one JSONL block")
    payload_and_end = body.split(opening, 1)[1]
    if not payload_and_end.endswith("```\n"):
        raise ValueError("Source JSONL block must end the document")
    payload = payload_and_end[:-4]
    if sha256(payload.encode()) != metadata["records_sha256"]:
        raise ValueError("Source records checksum mismatch")
    records: list[dict[str, object]] = []
    for number, line in enumerate(payload.splitlines(), 1):
        if number > MAX_RECORDS:
            raise ValueError("Input exceeds pilot record limit")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON at record {number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"Record {number} is not an object")
        records.append(value)
    if not records or type(metadata.get("record_count")) is not int or metadata["record_count"] != len(records):
        raise ValueError("Source record count mismatch or empty source")
    return metadata, records


def read_snapshot(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("Input exceeds the bounded pilot limit")
    return snapshot_text(path.read_bytes().decode("utf-8"))


def group_records(records: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = defaultdict(list)
    for index, record in enumerate(records, 1):
        word = record.get("word")
        if not isinstance(word, str) or not word.strip() or record.get("lang_code") != "en":
            raise ValueError(f"Record {index} lacks an English headword")
        groups[word].append(record)
    return dict(sorted(groups.items(), key=lambda pair: (pair[0].casefold(), pair[0])))


def safe_relative(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
        raise ValueError("Invalid edition file path")
    return relative


def validate_edition(edition: Path, *, reader: Callable[[str], str] | None = None,
                     listed_paths: set[str] | None = None) -> dict[str, int]:
    def read(name: str) -> str:
        relative = safe_relative(name)
        if reader:
            return reader(str(relative))
        target = edition / relative
        if target.is_symlink() or not target.resolve().is_relative_to(edition.resolve()):
            raise ValueError("Edition file must be a local regular file")
        return target.read_bytes().decode("utf-8")

    manifest, _ = parse_document(read("MANIFEST.md"))
    if manifest.get("kind") != "edition-manifest":
        raise ValueError("Invalid edition manifest")
    hashes = manifest.get("file_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("Missing edition file checksums")
    expected_paths = set(hashes) | {"MANIFEST.md"}
    actual_paths = listed_paths if listed_paths is not None else {
        str(path.relative_to(edition)) for path in edition.rglob("*.md")}
    if actual_paths != expected_paths:
        raise ValueError("Edition inventory does not match its manifest")
    for filename, checksum in hashes.items():
        if not isinstance(filename, str) or sha256(read(filename).encode()) != checksum:
            raise ValueError(f"Edition checksum mismatch: {filename}")
    source, records = snapshot_text(read("SOURCE.md"))
    groups = group_records(records)
    source_entry_paths = {str(entry_path(word)) for word in groups}
    actual_entry_paths = {path for path in hashes if path.startswith("entries/")}
    if source_entry_paths != actual_entry_paths:
        raise ValueError("Entry inventory does not match source headwords")
    totals = {"headwords": 0, "source_records": 0, "senses": 0,
              "with_pronunciation": 0, "with_etymology": 0, "multiword": 0}
    identifiers: set[str] = set()
    for word, values in groups.items():
        content = read(str(entry_path(word)))
        if content != render_entry(word, values, source):
            raise ValueError(f"Entry differs from its preserved source projection: {word}")
        metadata, _ = parse_document(content)
        identifier = str(metadata.get("entry_id"))
        if identifier in identifiers:
            raise ValueError("Duplicate entry identity")
        identifiers.add(identifier)
        totals["headwords"] += 1
        for field, target in (("source_records", "source_records"), ("sense_count", "senses")):
            count = metadata.get(field)
            if type(count) is not int or count < 1:
                raise ValueError(f"Invalid count: {field}")
            totals[target] += count
        totals["with_pronunciation"] += int(any(
            sound.get("ipa") or sound.get("enpr") for record in values
            for sound in record.get("sounds", []) if isinstance(sound, dict)))
        totals["with_etymology"] += int(any(record.get("etymology_text") for record in values))
        totals["multiword"] += int(" " in word)
    if manifest.get("counts") != totals:
        raise ValueError("Edition counters do not reconcile with Markdown entries")
    return totals


def active_edition(root: Path, *, staged: bool = False) -> Path:
    text = git_text(root, "CURRENT.md") if staged else (root / "CURRENT.md").read_bytes().decode("utf-8")
    metadata, _ = parse_document(text)
    name = metadata.get("edition")
    if not isinstance(name, str) or not re.fullmatch(r"pilot-[0-9a-f]{16}", name):
        raise ValueError("Invalid current edition pointer")
    return root / "editions" / name


def git_text(root: Path, path: str) -> str:
    try:
        return subprocess.run(["git", "show", f":{path}"], cwd=root, check=True,
                              capture_output=True).stdout.decode("utf-8")
    except subprocess.CalledProcessError as error:
        raise ValueError(f"File absent from Git index: {path}") from error


def audit(root: Path, *, staged: bool = False) -> dict[str, int]:
    edition = active_edition(root, staged=staged)
    if not staged:
        return validate_edition(edition)
    relative = str(edition.relative_to(root))
    output = subprocess.run(["git", "ls-files", "-z", "--", relative], cwd=root, check=True,
                            capture_output=True, text=True).stdout
    paths = {path[len(relative) + 1:] for path in output.split("\0") if path.endswith(".md")}
    return validate_edition(edition, reader=lambda name: git_text(root, f"{relative}/{name}"),
                            listed_paths=paths)


def progress_text(counts: dict[str, int], source: dict[str, object]) -> str:
    headwords = counts["headwords"]
    rows = [("Pilot source records accepted", counts["source_records"], source["record_count"]),
            ("Headwords with pronunciation", counts["with_pronunciation"], headwords),
            ("Headwords with etymology", counts["with_etymology"], headwords)]
    lines = ["# Collection progress", "", "Scope: the preserved pilot sample only.", "",
             "| Measure | Complete | Target | Progress |", "| --- | ---: | ---: | --- |"]
    for title, complete, target in rows:
        ratio = complete / int(target) if target else 0
        bar = "█" * round(ratio * 20) + "░" * (20 - round(ratio * 20))
        lines.append(f"| {title} | {complete} | {target} | {bar} {ratio:.1%} |")
    lines += ["", f"- Distinct headwords: **{headwords}**.",
              f"- Source lexical records: **{counts['source_records']}**.",
              f"- Source senses: **{counts['senses']}**.",
              f"- Multiword headwords: **{counts['multiword']}** (overlaps other categories).",
              "- Editorial review: **not performed**; structural validation is automated.",
              "- Complete upstream English coverage: **no measured target**.",
              "- All-English-language coverage: **not a measurable fixed universe**.", "",
              "A complete pilot import does not mean a complete dictionary. Headwords may have",
              "additional records outside the captured byte range. Pronunciation and etymology",
              "availability are field presence measures, not editorial accuracy scores."]
    return document({"kind": "progress-report", "snapshot_id": source["snapshot_id"]}, "\n".join(lines))


def build(source_path: Path, root: Path, *, stop_after: int | None = None) -> Path:
    if source_path.stat().st_size > MAX_SNAPSHOT_BYTES:
        raise ValueError("Input exceeds the bounded pilot limit")
    source_text = source_path.read_bytes().decode("utf-8")
    code = b"".join(Path(__file__).with_name(name).read_bytes()
                    for name in ("pipeline.py", "entries.py", "markdown.py"))
    edition_id = "pilot-" + sha256(source_text.encode() + code)[:16]
    stage = root / ".cache" / "builds" / edition_id
    destination = root / "editions" / edition_id
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".cache" / "build.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ValueError("Another build owns .cache/build.lock; investigate a stale lock before removal") from error
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    try:
        source, records = snapshot_text(source_text)
        groups = group_records(records)
        if destination.exists():
            validate_edition(destination)
        else:
            counts = {"headwords": 0, "source_records": 0, "senses": 0,
                      "with_pronunciation": 0, "with_etymology": 0, "multiword": 0}
            hashes: dict[str, str] = {}
            index = ["# Alphabetical index", "", "English-only pilot. Source records and senses remain distinct.", "",
                     "| Headword | Source records | Senses |", "| --- | ---: | ---: |"]
            for completed, (word, values) in enumerate(groups.items(), 1):
                rendered = render_entry(word, values, source)
                relative = entry_path(word)
                target = stage / relative
                if target.exists() and target.read_bytes().decode("utf-8") != rendered:
                    raise ValueError(f"Checkpoint was edited or corrupted: {relative}")
                if not target.exists():
                    atomic_write(target, rendered)
                hashes[str(relative)] = sha256(rendered.encode())
                metadata, _ = parse_document(rendered)
                counts["headwords"] += 1
                counts["source_records"] += int(metadata["source_records"])
                counts["senses"] += int(metadata["sense_count"])
                counts["with_pronunciation"] += int(any(
                    sound.get("ipa") or sound.get("enpr") for record in values
                    for sound in record.get("sounds", []) if isinstance(sound, dict)))
                counts["with_etymology"] += int(any(record.get("etymology_text") for record in values))
                counts["multiword"] += int(" " in word)
                # Encode every potentially active Markdown character in a title.
                title = "".join(char if char.isalnum() or char == " " else f"&#{ord(char)};" for char in word)
                index.append(f"| [{title}]({relative.as_posix()}) | {metadata['source_records']} | {metadata['sense_count']} |")
                if stop_after is not None and completed >= stop_after:
                    raise InterruptedError("Simulated interruption; rerun the same build to resume")
            additional = {"INDEX.md": "\n".join(index) + "\n",
                          "SOURCE.md": source_text,
                          "PROGRESS.md": progress_text(counts, source)}
            for name, content in additional.items():
                atomic_write(stage / name, content)
                hashes[name] = sha256(content.encode())
            atomic_write(stage / "MANIFEST.md", document({
                "kind": "edition-manifest", "edition": edition_id,
                "snapshot_id": source["snapshot_id"], "counts": counts,
                "file_sha256": hashes}, "# Edition manifest\n\nChecksums cover exact UTF-8 Markdown bytes.\n"))
            validate_edition(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
        pointer = document({"kind": "current-edition", "edition": edition_id},
                           f"# Current dictionary edition\n\n"
                           f"- [Browse the dictionary](editions/{edition_id}/INDEX.md)\n"
                           f"- [Collection progress](editions/{edition_id}/PROGRESS.md)\n"
                           f"- [Source snapshot](editions/{edition_id}/SOURCE.md)\n"
                           f"- [Integrity manifest](editions/{edition_id}/MANIFEST.md)\n")
        if not (root / "CURRENT.md").exists() or (root / "CURRENT.md").read_bytes().decode("utf-8") != pointer:
            atomic_write(root / "CURRENT.md", pointer)
        return destination
    except (ValueError, OSError) as error:
        if not isinstance(error, InterruptedError):
            atomic_write(root / ".cache" / "failures" / edition_id / "REJECTED.md", document({"kind": "failed-build", "edition": edition_id},
                         "# Build rejected\n\n" + str(error).replace("<", "&lt;") + "\n"))
        raise
    finally:
        lock.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="Build a local edition; network is never used")
    build_parser.add_argument("--source", type=Path, default=Path("sources/pilot.md"))
    build_parser.add_argument("--root", type=Path, default=Path("."))
    build_parser.add_argument("--stop-after", type=int, help="Simulate interruption for recovery verification")
    audit_parser = sub.add_parser("audit", help="Audit Markdown bytes, provenance and counters")
    audit_parser.add_argument("--root", type=Path, default=Path("."))
    audit_parser.add_argument("--staged", action="store_true", help="Read Git index bytes instead of the working tree")
    args = parser.parse_args()
    try:
        if args.command == "build":
            if args.source.stat().st_size > MAX_SNAPSHOT_BYTES:
                raise ValueError("Input exceeds the bounded pilot limit")
            print(build(args.source, args.root, stop_after=args.stop_after))
        else:
            print(json.dumps(audit(args.root, staged=args.staged), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
