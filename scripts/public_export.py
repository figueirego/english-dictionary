"""Create and audit a publication derivative without changing accepted editions."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import time

from scripts.entries import _text
from scripts.full_pipeline import (active, checked_write, encode, file_hash,
                                   full_entry_path, partition_rows, read_md)
from scripts.markdown import atomic_write, document, parse_document

POLICY = 'public-withheld-etymology-v1'
MAX_PUBLIC_FILE = 100 * 1024 * 1024
WATER_RAW = '71847bc19adf345ed82afaf4db1a00f5f1ee917805a4f9abe32826fce35717d2'
WATER_EXAMPLES = {
    4: '0ac8938ba30b8400b373f5c0c81a0a4622b24c2b6715ffe006f8b9a82477f368',
    5: '47afd14e20ac2a18276c603b4dbaef8426e71b5a3cb8cd7281c1021cba0a52bd',
}
ETYMOLOGY_NOTICE = ('Etymology prose is withheld from this public derivative. '
                    'Original material is retained only in the local parent edition.')
EXAMPLE_NOTICE = ('One source-labelled example is withheld because its quotation provenance '
                  'is unresolved. The source sense and other eligible examples are preserved.')
DECISION_FIELDS = {
    'id', 'word', 'record', 'sense', 'pos', 'status', 'definition', 'reason', 'transformation',
    'source_kind', 'source_title', 'corrected_pos', 'original_gap_type', 'review_method',
    'source_sense_sha256', 'upstream_record_sha256', 'base_entry_sha256', 'scope_limit',
}
SOURCE_FIELDS = {'title', 'url', 'revision', 'license', 'capture_sha256'}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def safe_path(base: Path, relative: str) -> Path:
    path = Path(relative)
    require(not path.is_absolute() and '..' not in path.parts and path.suffix == '.md',
            'Unsafe Markdown path in source manifest')
    result = base / path
    for candidate in [result, *result.parents]:
        if candidate == base:
            break
        require(not candidate.is_symlink(), 'Symlink in canonical artifact path')
    return result


def sanitized_records(records: list[dict]) -> tuple[list[dict], Counter, list[tuple[int, int, str]]]:
    result = deepcopy(records)
    counts = Counter(etymology_records_withheld=0, etymology_utf8_bytes_withheld=0,
                     examples_withheld=0)
    examples = []
    for number, row in enumerate(result, 1):
        if 'etymology_text' in row:
            prose = row.pop('etymology_text')
            require(isinstance(prose, str), 'Etymology text must be a string')
            counts['etymology_records_withheld'] += 1
            counts['etymology_utf8_bytes_withheld'] += len(prose.encode())
        if row.get('word') == 'water' and row.get('upstream_record_sha256') == WATER_RAW:
            require(row.get('pos') == 'noun', 'Unexpected guarded water part of speech')
            for sense_number, expected in WATER_EXAMPLES.items():
                sense = row['senses'][sense_number - 1]
                matching = [example for example in sense.get('examples', [])
                            if digest(example.get('text', '').encode()) == expected]
                require(len(matching) == 1, 'Guarded water example no longer matches')
                examples.append((number, sense_number, matching[0]['text']))
                sense['examples'] = [example for example in sense['examples'] if example not in matching]
                counts['examples_withheld'] += 1
    return result, counts, examples


def sanitize_entry(original: bytes, records: list[dict], parent_name: str,
                   public_ledger_sha: str | None) -> tuple[str, Counter]:
    metadata, body = parse_document(original.decode('utf-8'))
    clean, counts, removed_examples = sanitized_records(records)
    sections = re.compile(r'(?ms)^### Etymology\n.*?(?=^#{1,3} |^<a id="r\d+"></a>|\Z)')
    body, count = sections.subn('### Etymology\n\n' + ETYMOLOGY_NOTICE + '\n\n', body)
    counts['etymology_sections_withheld'] = count
    for patch in metadata.get('editorial_patches', []):
        if str(patch.get('coordinate', '')).endswith('.etymology_text'):
            body = body.replace(_text(patch['note']), ETYMOLOGY_NOTICE)
            patch['parent_note_sha256'] = digest(patch['note'].encode())
            patch['note'] = ETYMOLOGY_NOTICE
    for number, sense_number, text in removed_examples:
        anchor = f'<a id="r{number}-s{sense_number}"></a>'
        require(body.count(anchor) == 1, 'Missing guarded example source-sense anchor')
        start = body.index(anchor)
        boundary = re.search(r'\n(?:<a id=|#{1,3} )', body[start + len(anchor):])
        end = start + len(anchor) + boundary.start() if boundary else len(body)
        block = body[start:end]
        pattern = re.compile(r'(?m)^ *\*\*Example:\*\* ' + re.escape(_text(text)) + r'\n')
        block, removed = pattern.subn('', block)
        require(removed == 1, 'Guarded quotation is not an exact rendered example')
        block = block.rstrip() + '\n\n   **Public export note:** ' + EXAMPLE_NOTICE + '\n\n'
        body = body[:start] + block + body[end:]
    if removed_examples:
        senses = [sense for row in clean for sense in row.get('senses', [])]
        missing = sum(not sense.get('examples') for sense in senses)
        body = re.sub(r'eligible usage examples absent in \d+ of \d+ senses',
                      f'eligible usage examples absent in {missing} of {len(senses)} senses', body)
    metadata['parent_entry_sha256'] = digest(original)
    metadata['parent_edition'] = parent_name
    metadata['public_export_policy'] = POLICY
    metadata['public_omissions'] = dict(counts)
    metadata['parent_source_record_sha256'] = metadata['source_record_sha256']
    metadata['source_record_sha256'] = [digest(encode(row).encode()) for row in clean]
    if 'definition_ledger_sha256' in metadata:
        require(public_ledger_sha is not None, 'Public definition ledger is required')
        metadata['parent_definition_ledger_sha256'] = metadata['definition_ledger_sha256']
        metadata['definition_ledger_sha256'] = public_ledger_sha
    body = body.replace('Embedded material in free-form etymology requires separate review.',
                        'Unreviewed etymology prose is withheld from both public views and projections.')
    body = re.sub(r'(?m)^- Adaptation:.*$',
                  '- Adaptation: structured extraction and Markdown formatting; translations and media '
                  'omitted. The original example filter excludes referenced sense examples and '
                  'quotation-typed examples. This public derivative additionally withholds all '
                  'unreviewed etymology prose and two individually identified water examples. '
                  'Authorship of every remaining unreferenced example is not independently verified.', body)
    body = body.replace('## Sources\n\n',
                        '## Sources\n\n- [Content licences and source-specific notices](../../../LICENSE.md)\n', 1)
    if counts['etymology_records_withheld'] or counts['etymology_sections_withheld']:
        heading, separator, rest = body.partition('\n\n')
        body = heading + separator + '**Public export:** ' + ETYMOLOGY_NOTICE + '\n\n' + rest
    return document(metadata, body), counts


def public_ledger(path: Path) -> str:
    metadata, body = read_md(path)
    _, separator, payload = body.partition('```jsonl\n')
    require(bool(separator) and payload.endswith('```\n'), 'Invalid definition ledger framing')
    rows = [json.loads(line) for line in payload[:-4].rstrip('\n').split('\n')]
    require(len(rows) == metadata['decision_count'], 'Definition ledger count mismatch')
    clean = []
    for row in rows:
        selected = {key: deepcopy(value) for key, value in row.items() if key in DECISION_FIELDS}
        if 'sources' in row:
            selected['sources'] = [{key: deepcopy(value) for key, value in source.items()
                                    if key in SOURCE_FIELDS} for source in row['sources']]
        clean.append(selected)
    public_metadata = {key: value for key, value in metadata.items()
                       if key in {'schema_version', 'snapshot_id', 'raw_sha256', 'scope', 'decision_count'}}
    public_metadata.update(kind='public-definition-decisions', public_export_policy=POLICY,
                           parent_ledger_sha256=file_hash(path))
    text = document(public_metadata,
        '# Public definition decisions\n\nDefinitions, decision coordinates, editorial reasons, '
        'source URLs, licences and provenance digests are retained. Private raw records, captures, '
        'HTTP metadata, local paths, quoted support excerpts and research history are excluded. The parent ledger is retained '
        'locally; this public derivative has its own checksum.\n\n```jsonl\n'
        + ''.join(encode(row) + '\n' for row in clean) + '```')
    require('.cache/' not in text and not re.search(r'(?<![A-Za-z0-9./])/(?:Users|home)/', text)
            and 'raw_record_line' not in text,
            'Private capture material remains in the public ledger')
    return text


def source_text(rows: list[dict], original_metadata: dict, parent_sha: str,
                ledger_sha: str | None) -> str:
    clean, counts, _ = sanitized_records(rows)
    payload = ''.join(encode(row) + '\n' for row in clean)
    metadata = {**original_metadata, 'kind': 'public-english-source-partition',
                'parent_source_sha256': parent_sha, 'public_export_policy': POLICY,
                'payload_sha256': digest(payload.encode()), 'public_omissions': dict(counts)}
    if 'definition_ledger_sha256' in metadata:
        metadata['parent_definition_ledger_sha256'] = metadata['definition_ledger_sha256']
        metadata['definition_ledger_sha256'] = ledger_sha
    return document(metadata, '# Public English source projection\n\n'
        'All unreviewed etymology_text fields and two individually guarded water examples are '
        'omitted under the public export policy. Definitions, sense coordinates, attribution, '
        'other eligible examples and original raw-line digests remain. Raw-line digests identify '
        'the complete upstream records, not these sanitized projections.\n\n```jsonl\n' + payload + '```')


def checked_file(path: Path, content: str, audit: bool, stats: Counter) -> str:
    data = content.encode()
    require(len(data) < MAX_PUBLIC_FILE, f'Public Markdown exceeds file-size limit: {path.name}')
    if not audit:
        checked_write(path, content)
    require(not path.is_symlink() and path.read_bytes() == data, f'Public artifact mismatch: {path}')
    stats['markdown_files'] += 1
    stats['markdown_bytes'] += len(data)
    stats['largest_file_bytes'] = max(stats['largest_file_bytes'], len(data))
    return digest(data)


def process_partition(parent: Path, destination: Path, bucket: str, expected: str,
                      ledger_sha: str | None, audit: bool = False) -> dict:
    parent_manifest_path = safe_path(parent, f'manifests/{bucket}.md')
    require(file_hash(parent_manifest_path) == expected, 'Parent partition manifest changed')
    parent_manifest, _ = read_md(parent_manifest_path)
    parent_source = safe_path(parent, f'sources/{bucket}.md')
    require(file_hash(parent_source) == parent_manifest['source_sha256'], 'Parent source changed')
    source, rows = partition_rows(parent_source)
    stats = Counter(headwords=0, accepted_records=0, senses=0)
    omissions = Counter()
    definitions = Counter()
    files = {}
    for word, group in itertools.groupby(rows, key=lambda row: row['word']):
        records = list(group)
        relative = full_entry_path(word)
        require(relative.parts[1] == bucket and all(row['lang_code'] == 'en' for row in records),
                'Invalid parent English partition identity')
        original = safe_path(parent, relative.as_posix()).read_bytes()
        require(digest(original) == parent_manifest['files'].get(relative.as_posix()), 'Parent entry changed')
        text, removed = sanitize_entry(original, records, parent.name, ledger_sha)
        files[relative.as_posix()] = checked_file(destination / relative, text, audit, stats)
        metadata, _ = parse_document(text)
        definitions.update(metadata.get('definition_overlay_counts', {}))
        omissions.update(removed)
        stats.update(headwords=1, accepted_records=len(records),
                     senses=sum(len(row.get('senses', [])) for row in records))
    require(len(files) == len(parent_manifest['files']), 'Parent entry inventory does not reconcile')
    for key in ('headwords', 'accepted_records', 'senses'):
        require(stats[key] == parent_manifest[key], f'Parent partition counter mismatch: {key}')
    index_path = safe_path(parent, f'indexes/{bucket}.md')
    require(file_hash(index_path) == parent_manifest['index_sha256'], 'Parent index changed')
    index_sha = checked_file(destination / f'indexes/{bucket}.md', index_path.read_text(), audit, stats)
    public_source = source_text(rows, source, parent_manifest['source_sha256'], ledger_sha)
    source_sha = checked_file(destination / f'sources/{bucket}.md', public_source, audit, stats)
    metadata = {'kind': 'public-partition-manifest', 'partition': bucket, 'public_export_policy': POLICY,
                'parent_manifest_sha256': expected, **{key: stats[key] for key in
                    ('headwords', 'accepted_records', 'senses')}, 'public_omissions': dict(omissions),
                'definition_counts': dict(definitions), 'source_sha256': source_sha,
                'index_sha256': index_sha, 'files': files}
    manifest_sha = checked_file(destination / f'manifests/{bucket}.md', document(metadata,
        '# Public partition integrity\n\nHashes cover exact public Markdown bytes. '
        'The parent manifest identifies the immutable local evidence.'), audit, stats)
    actual = {path.name for path in (destination / 'entries' / bucket).iterdir()}
    require(actual == {Path(name).name for name in files}, 'Unexpected public partition files')
    return {**dict(stats), 'public_omissions': dict(omissions), 'definition_counts': dict(definitions),
            'partition': bucket, 'manifest_sha256': manifest_sha}


def publication_progress(destination: Path, phase: str, completed: int, total: int,
                         counts: dict | None = None) -> None:
    metadata = {'kind': 'public-export-progress', 'phase': phase,
                'completed_partitions': completed, 'total_partitions': total, **(counts or {})}
    lines = ['# Public dictionary export', '', f'**{phase}.** {completed:,}/{total:,} partitions.', '',
             'The public derivative withholds all unreviewed etymology prose and two individually '
             'identified water examples from both entries and source projections. The original '
             'accepted editions remain local. This is not a complete linguistic certification.', '',
             '[Dictionary](INDEX.md) · [Source](SOURCE.md)']
    if counts:
        lines.extend(['', '| Measure | Count |', '| --- | ---: |'])
        for key, value in counts.items():
            if type(value) is int:
                lines.append(f'| {key.replace("_", " ")} | {value:,} |')
    atomic_write(destination / 'PUBLICATION.md', document(metadata, '\n'.join(lines)))


def export(root: Path, destination: Path, workers: int = 4, *, audit_only: bool = False) -> dict:
    root, destination = root.resolve(), destination.resolve()
    parent = active(root)
    require(not destination.is_relative_to(parent) and destination != root,
            'Public destination must be isolated from the accepted project')
    require(not destination.is_symlink(), 'Public destination cannot be a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    lock = destination / '.public-export.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        return _export(parent, destination, workers, audit_only)
    except Exception:
        publication_progress(destination, 'Public export/audit failed; existing files preserved', 0, 0)
        raise
    finally:
        lock.unlink(missing_ok=True)


def _export(parent: Path, destination: Path, workers: int, audit_only: bool) -> dict:
    parent_master, _ = read_md(parent / 'MANIFEST.md')
    parent_sha = file_hash(parent / 'MANIFEST.md')
    tool_sha = digest(Path(__file__).read_bytes() + Path(__file__).with_name('markdown.py').read_bytes())
    identity = 'public-' + digest((parent_sha + POLICY + tool_sha).encode())[:16]
    canonical = destination / 'dictionary'
    stage = destination / '.public-export-work' / identity
    audit = audit_only or canonical.exists()
    target = canonical if audit else stage
    require(not audit or canonical.is_dir(), 'No promoted public dictionary to audit')
    if audit:
        existing, _ = read_md(canonical / 'MANIFEST.md')
        require(existing.get('edition') == identity, 'Public output belongs to another source or export version')
    else:
        stage.mkdir(parents=True, exist_ok=True)
    ledger_text = None
    ledger_sha = None
    if 'definition_ledger_sha256' in parent_master:
        ledger_path = parent / 'DEFINITIONS.md'
        require(file_hash(ledger_path) == parent_master['definition_ledger_sha256'], 'Parent ledger changed')
        ledger_text = public_ledger(ledger_path)
        ledger_sha = digest(ledger_text.encode())
    totals = Counter()
    omissions = Counter()
    definitions = Counter()
    results = {}
    start = time.monotonic()
    phase = 'Auditing public Markdown' if audit else 'Exporting public Markdown'
    partitions = parent_master['partitions']
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_partition, parent, target, bucket, checksum, ledger_sha, audit): bucket
                   for bucket, checksum in partitions.items()}
        for future in as_completed(futures):
            result = future.result()
            results[result['partition']] = result['manifest_sha256']
            totals.update({key: result[key] for key in ('headwords', 'accepted_records', 'senses',
                                                       'markdown_files', 'markdown_bytes')})
            totals['largest_file_bytes'] = max(totals['largest_file_bytes'], result['largest_file_bytes'])
            omissions.update(result['public_omissions'])
            definitions.update(result['definition_counts'])
            if len(results) % 16 == 0 or len(results) == len(partitions):
                counts = {**dict(totals), 'elapsed_seconds': round(time.monotonic() - start)}
                publication_progress(destination, phase, len(results), len(partitions), counts)
                print(encode({'phase': phase, 'partitions': len(results), **counts}), flush=True)
    for key in ('headwords', 'accepted_records', 'senses'):
        require(totals[key] == parent_master[key], f'Public corpus counter mismatch: {key}')
    require(dict(definitions) == parent_master.get('definition_counts', {}), 'Supplemental definitions changed')
    top_stats = Counter()
    files = {}
    for name, expected in sorted(parent_master['files'].items()):
        if not (name.startswith('browse/') or name in ('INDEX.md', 'PROGRESS.md')):
            continue
        path = safe_path(parent, name)
        require(file_hash(path) == expected, f'Parent navigation changed: {name}')
        text = path.read_text()
        if name == 'INDEX.md':
            text = re.sub(r'^To locate a word locally:.*\n', '', text, flags=re.M)
        files[name] = checked_file(target / name, text, audit, top_stats)
    if ledger_text is not None:
        files['DEFINITIONS.md'] = checked_file(target / 'DEFINITIONS.md', ledger_text, audit, top_stats)
    source, _ = read_md(parent / 'SOURCE.md')
    require(file_hash(parent / 'SOURCE.md') == parent_master['files']['SOURCE.md'], 'Parent source report changed')
    source.update(kind='public-source-snapshot', public_export_policy=POLICY,
                  parent_edition=parent.name, parent_manifest_sha256=parent_sha,
                  public_omissions=dict(omissions))
    if ledger_sha:
        source['parent_definition_ledger_sha256'] = source['definition_ledger_sha256']
        source['definition_ledger_sha256'] = ledger_sha
    files['SOURCE.md'] = checked_file(target / 'SOURCE.md', document(source,
        '# Public source snapshot\n\nText derives from the pinned English Wiktionary extraction, '
        'with per-entry contributor and revision-history attribution under CC BY-SA 4.0. '
        'Supplemental definitions retain their individually documented sources and licences.\n\n'
        'All unreviewed etymology prose is withheld from entries and source projections. Two '
        'individually identified water examples with unresolved quotation provenance are also '
        'withheld. Other eligible usage examples are retained under the original field filter; '
        'this does not claim every remaining sentence has independent authorship verification.\n\n'
        'Raw-line and full-capture digests refer to original upstream bytes, which are retained '
        'only locally. Public projection and entry hashes refer to the actual sanitized files. '
        'The public definition ledger excludes raw captures and private research material.\n\n'
        'Cambridge and modern Oxford are editorial references, not imported corpora or affiliations.'),
        audit, top_stats)
    metadata = {'kind': 'public-edition-manifest', 'edition': identity, 'public_export_policy': POLICY,
                'exporter_sha256': tool_sha, 'parent_edition': parent.name, 'parent_manifest_sha256': parent_sha,
                **{key: totals[key] for key in ('headwords', 'accepted_records', 'senses')},
                'public_omissions': dict(omissions), 'definition_counts': dict(definitions),
                'partitions': dict(sorted(results.items())), 'files': files}
    checked_file(target / 'MANIFEST.md', document(metadata,
        '# Public edition integrity\n\nEvery public entry and source projection is reproducible '
        'from the hash-verified immutable local parent through the documented export policy.'), audit, top_stats)
    expected_root = {'entries', 'sources', 'indexes', 'manifests', 'browse', 'INDEX.md',
                     'PROGRESS.md', 'SOURCE.md', 'MANIFEST.md'} | ({'DEFINITIONS.md'} if ledger_text else set())
    require({path.name for path in target.iterdir()} == expected_root, 'Unexpected public root files')
    for folder in ('entries', 'sources', 'indexes', 'manifests'):
        expected_names = {bucket + ('' if folder == 'entries' else '.md') for bucket in partitions}
        require({path.name for path in (target / folder).iterdir()} == expected_names,
                f'Unexpected public {folder} inventory')
    expected_browse = {name for name in files if name.startswith('browse/')}
    require({path.relative_to(target).as_posix() for path in (target / 'browse').rglob('*') if path.is_file()}
            == expected_browse, 'Unexpected public browse inventory')
    totals['markdown_files'] += top_stats['markdown_files']
    totals['markdown_bytes'] += top_stats['markdown_bytes']
    totals['largest_file_bytes'] = max(totals['largest_file_bytes'], top_stats['largest_file_bytes'])
    if not audit:
        # Every file was read back and compared before its checkpoint was accepted.
        os.rename(stage, canonical)
    checked_write(destination / 'INDEX.md', '# Public English dictionary\n\n[Browse all entries](dictionary/INDEX.md).\n')
    checked_write(destination / 'SOURCE.md', '# Public dictionary sources\n\n[Source snapshot and publication policy](dictionary/SOURCE.md).\n')
    final = {**dict(totals), **dict(omissions), 'edition': identity,
             'parent_edition': parent.name, 'parent_manifest_sha256': parent_sha,
             'manifest_sha256': file_hash(canonical / 'MANIFEST.md'), 'definition_counts': dict(definitions)}
    publication_progress(destination, 'Public derivative complete and verified', len(partitions), len(partitions), final)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'audit'])
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 9), default=4)
    arguments = parser.parse_args()
    print(encode(export(arguments.root, arguments.destination, arguments.workers,
                        audit_only=arguments.command == 'audit')))


if __name__ == '__main__':
    main()
