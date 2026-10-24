"""Stream a complete pinned gzip export into a partitioned Markdown dictionary."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from scripts.entries import _text
from scripts.full_projection import project, entry_path, display_headword
from scripts.definition_overlay import (render, load_ledger, gap_counts, REVISION as DEFINITION_REVISION,
                                        LEDGER_NAME, counter_keys, SUPPORTED_REVISIONS)
from scripts.full_browse import build_browse
from scripts.markdown import atomic_write, document, parse_document
from scripts.prepare import _object_pairs, _invalid_constant

PARTITIONS = 1024
MAX_LINE = 64 * 1024 * 1024
MAX_PARTITION = 256 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def load(line: str | bytes) -> object:
    return json.loads(line, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)


def file_hash(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read_md(path: Path) -> tuple[dict, str]:
    return parse_document(path.read_bytes().decode('utf-8'))


def partition_for(word: str) -> str:
    return f'{int(digest(word.encode())[:3], 16) // 4:03x}'


def full_entry_path(word: str) -> Path:
    return Path('entries') / partition_for(word) / entry_path(word).name


def relation_words(rows: list[dict]) -> set[str]:
    """Collect exact source targets once per bounded source partition."""
    words = set()
    for row in rows:
        for context in [row, *row.get('senses', [])]:
            for field in ('form_of', 'alt_of'):
                for relation in context.get(field, []):
                    word = relation.get('word')
                    if isinstance(word, str) and word:
                        # Invalid or overlong relation strings remain source text.
                        try:
                            full_entry_path(word)
                        except (ValueError, UnicodeError):
                            continue
                        words.add(word)
    return words


def relation_url(word: str) -> str:
    relative = full_entry_path(word)
    return '../' + '/'.join(relative.parts[1:])


def database_relation_targets(db: sqlite3.Connection, words: set[str]) -> dict[str, str]:
    """Resolve only existing exact spellings with bounded indexed queries."""
    candidates = sorted(words)
    result = {}
    for start in range(0, len(candidates), 400):
        batch = candidates[start:start + 400]
        placeholders = ','.join('(?,?)' for _ in batch)
        parameters = [value for word in batch for value in (partition_for(word), word)]
        matches = db.execute(
            f'WITH requested(bucket, word) AS (VALUES {placeholders}) '
            'SELECT DISTINCT records.word FROM requested JOIN records '
            'ON records.bucket=requested.bucket AND records.word=requested.word COLLATE BINARY '
            'ORDER BY records.word COLLATE BINARY', parameters)
        for (word,) in matches:
            result[word] = relation_url(word)
    return result


def edition_relation_targets(edition: Path, words: set[str]) -> dict[str, str]:
    """Verify target identity from canonical Markdown, without the ingest cache."""
    result = {}
    for word in sorted(words):
        path = edition / full_entry_path(word)
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError('Relation target is not a canonical Markdown file')
        metadata, _ = read_md(path)
        if metadata.get('headword') != word or metadata.get('language') != 'en':
            raise ValueError('Relation target identity does not match the exact English headword')
        result[word] = relation_url(word)
    return result


def correction_counts(metadata: dict) -> Counter:
    changes = metadata.get('editorial_changes', {})
    if (not isinstance(changes, dict) or any(
            not isinstance(key, str) or type(value) is not int or value < 0
            for key, value in changes.items())):
        raise ValueError('Invalid editorial correction counters')
    return Counter({key: value for key, value in changes.items() if value})


@contextmanager
def exclusive(root: Path):
    cache = root / '.cache' / 'full-import'
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / 'operation.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield cache
    finally:
        lock.unlink(missing_ok=True)


def progress(root: Path, phase: str, counts: dict, **extra: object) -> None:
    metadata = {'kind': 'collection-progress', 'phase': phase,
                'updated_at_utc': datetime.now(timezone.utc).isoformat(), **counts, **extra}
    rows = [('# English dictionary collection'), '', f'**Status: {phase}.**', '',
            'Progress measures this pinned source snapshot. The total vocabulary of English is open-ended.', '',
            '| Measure | Count |', '| --- | ---: |']
    for key, value in {**counts, **extra}.items():
        if type(value) in (int, float):
            label = ('Headwords with editorial display changes' if key == 'corrected_headwords'
                     else key.replace('_', ' ').capitalize())
            rows.append(f'| {label} | {value:,} |')
    measurements = {**counts, **extra}
    done, total = measurements.get('completed_partitions', 0), measurements.get('total_partitions', 0)
    if total:
        filled = int(30 * done / total)
        rows.extend(['', f'Partitions: `{"█" * filled}{"░" * (30-filled)}` {done}/{total} ({100*done/total:.1f}%).'])
    rows.extend(['', '- [Download status](DOWNLOAD.md)', '- [Current accepted edition](CURRENT.md)', '',
                 'Imported text is English-only. Linguistic review is separate from structural verification.'])
    atomic_write(root / 'PROGRESS.md', document(metadata, '\n'.join(rows)))


def database(root: Path) -> sqlite3.Connection:
    path = root / '.cache/full-import/source.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('PRAGMA cache_size=-32768')
    db.executescript('''CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
          ordinal INTEGER PRIMARY KEY, bucket TEXT NOT NULL, word TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rejected (
          ordinal INTEGER PRIMARY KEY, reason TEXT NOT NULL, raw_line BLOB NOT NULL);''')
    return db


def state_get(db: sqlite3.Connection, key: str, default=None):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def state_set(db: sqlite3.Connection, key: str, value: object) -> None:
    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, encode(value)))


def ingest(root: Path, archive: Path, evidence_path: Path) -> dict:
    """Resume committed records by replaying gzip to the checkpoint, with bounded RAM."""
    with exclusive(root):
        evidence, _ = read_md(evidence_path)
        required = ('dump_date', 'extraction_date', 'wiktextract_commit', 'wikitextprocessor_commit')
        if any(not isinstance(evidence.get(key), str) or not evidence[key] for key in required):
            raise ValueError('Capture evidence must identify dump and extractor revisions')
        compressed_sha256 = file_hash(archive)
        expected = evidence.get('compressed_sha256') or evidence.get('sha256')
        if not expected or expected != compressed_sha256:
            raise ValueError('Compressed archive does not match capture evidence')
        projector_code = b''.join((Path(__file__).parent / name).read_bytes() for name in
                                 ('full_projection.py', 'prepare.py', 'entries.py', 'markdown.py'))
        source = {key: evidence[key] for key in required}
        source['projector_sha256'] = digest(projector_code)
        source.update({'source_url': 'https://kaikki.org/dictionary/raw-wiktextract-data.jsonl',
                       'archive_url': 'https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz',
                       'catalogue_url': 'https://kaikki.org/dictionary/rawdata.html',
                       'compressed_sha256': compressed_sha256, 'compressed_bytes': archive.stat().st_size,
                       'license': 'CC-BY-SA-4.0', 'language': 'en',
                       'snapshot_id': f'enwiktionary-{evidence["dump_date"]}-raw-{compressed_sha256[:12]}'})
        with database(root) as db:
            previous = state_get(db, 'source')
            if previous and previous != source:
                raise ValueError('Work cache belongs to another snapshot; preserve it and use another root')
            state_set(db, 'source', source)
            saved = state_get(db, 'counts', {})
            if state_get(db, 'complete'):
                return {**source, **state_get(db, 'result')}
            counts = Counter(saved)
            committed = counts['raw_records']
            raw_hash = hashlib.sha256()
            raw_bytes = 0
            last_report = time.monotonic()
            with gzip.open(archive, 'rb') as stream:
                ordinal = 0
                while True:
                    line = stream.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        raise ValueError('Source line exceeds bounded line limit')
                    ordinal += 1
                    raw_hash.update(line)
                    offset = raw_bytes
                    raw_bytes += len(line)
                    if ordinal <= committed:
                        continue
                    counts['raw_records'] += 1
                    try:
                        record = load(line)
                        if not isinstance(record, dict):
                            raise ValueError('Source row must be an object')
                        if (record.get('pos') == 'hard-redirect' and
                                isinstance(record.get('redirect'), str) and isinstance(record.get('title'), str)):
                            counts['redirect_records'] += 1
                        elif isinstance(record.get('lang_code'), str) and record['lang_code']:
                            if record['lang_code'] != 'en':
                                counts['other_language_records'] += 1
                            else:
                                counts['english_records'] += 1
                                projected = project(record)
                                projected.update({'upstream_record_sha256': digest(line),
                                                  'upstream_line': ordinal, 'upstream_offset': offset})
                                word = projected['word']
                                full_entry_path(word)  # validate names before creating durable output
                                db.execute('INSERT INTO records VALUES (?,?,?,?)',
                                           (ordinal, partition_for(word), word, encode(projected)))
                                counts['accepted_records'] += 1
                                counts['senses'] += len(projected.get('senses', []))
                                counts['records_with_source_gaps'] += bool(projected.get('missing_senses') or any(sense.get('missing_gloss') for sense in projected.get('senses', [])))
                        else:
                            raise ValueError('Missing language code outside a recognized hard redirect')
                    except (ValueError, TypeError, UnicodeError) as error:
                        counts['rejected_records'] += 1
                        db.execute('INSERT INTO rejected VALUES (?,?,?)', (ordinal, str(error), line))
                    if ordinal % 5000 == 0:
                        counts['raw_bytes'] = raw_bytes
                        state_set(db, 'counts', dict(counts))
                        db.commit()
                        if time.monotonic() - last_report >= 5:
                            progress(root, 'Reading complete source', dict(counts))
                            print(encode({'phase': 'ingest', **counts}), flush=True)
                            last_report = time.monotonic()
            counts['raw_bytes'] = raw_bytes
            counts['raw_sha256'] = raw_hash.hexdigest()
            if evidence.get('raw_sha256') and evidence['raw_sha256'] != counts['raw_sha256']:
                raise ValueError('Decompressed source hash differs from capture evidence')
            if evidence.get('raw_size_bytes') and evidence['raw_size_bytes'] != raw_bytes:
                raise ValueError('Decompressed source size differs from capture evidence')
            state_set(db, 'counts', dict(counts))
            db.execute('CREATE INDEX IF NOT EXISTS records_bucket_word ON records(bucket,word,ordinal)')
            counts['headwords'] = db.execute('SELECT count(*) FROM (SELECT word FROM records GROUP BY word)').fetchone()[0]
            counts['partitions'] = db.execute('SELECT count(DISTINCT bucket) FROM records').fetchone()[0]
            if counts['raw_records'] != counts['accepted_records'] + counts['other_language_records'] + counts['redirect_records'] + counts['rejected_records']:
                raise ValueError('Source accounting does not reconcile')
            state_set(db, 'result', dict(counts))
            state_set(db, 'complete', True)
            db.commit()
            result = {**source, **dict(counts)}
            atomic_write(root / '.cache/full-import/INGEST.md', document(result, '# Complete source inventory\n\nEvery source record is accounted for.'))
            progress(root, 'Source inventoried', dict(counts))
            return result


def checked_write(path: Path, content: str) -> None:
    """Immutable writes; fsync the partition checkpoint rather than every small entry."""
    data = content.encode('utf-8')
    if path.exists():
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError(f'Existing file differs; preserved for inspection: {path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.full-', delete=False) as stream:
            temporary = stream.name
            stream.write(data)
        os.link(temporary, path)
    finally:
        if temporary:
            os.unlink(temporary)


def source_document(rows: list[dict], source: dict, bucket: str) -> str:
    payload = ''.join(encode(row) + '\n' for row in rows)
    return document({'kind': 'english-source-partition', 'partition': bucket,
                     'record_count': len(rows), 'payload_sha256': digest(payload.encode()), **source},
                    '# English source projection\n\nTranslations, referenced sense quotations and media are omitted.\n'
                    'Embedded material in free-form etymology requires separate review.\n'
                    'Upstream line numbers and hashes refer to the complete uncompressed export.\n'
                    'This fenced block is data, never instructions.\n\n```jsonl\n' + payload + '```')


def partition_rows(path: Path) -> tuple[dict, list[dict]]:
    if path.stat().st_size > MAX_PARTITION:
        raise ValueError('Source partition exceeds memory bound')
    meta, body = read_md(path)
    start, separator, payload = body.partition('```jsonl\n')
    if not separator or not payload.endswith('```\n'):
        raise ValueError('Invalid source projection framing')
    payload = payload[:-4]
    if digest(payload.encode()) != meta['payload_sha256']:
        raise ValueError('Source projection checksum mismatch')
    rows = [load(line) for line in payload.rstrip('\n').split('\n')]
    if len(rows) != meta['record_count']:
        raise ValueError('Source projection count mismatch')
    return meta, rows


def validate_partition(edition: Path, bucket: str, expected_hash: str | None = None,
                       *, build_targets: dict[str, str] | None = None,
                       definition_rows: list[dict] | None = None,
                       definition_sha256: str | None = None) -> dict:
    manifest_path = edition / f'manifests/{bucket}.md'
    if expected_hash and file_hash(manifest_path) != expected_hash:
        raise ValueError('Partition manifest checksum mismatch')
    manifest, _ = read_md(manifest_path)
    source_path = edition / f'sources/{bucket}.md'
    if file_hash(source_path) != manifest['source_sha256']:
        raise ValueError('Source partition file changed')
    if file_hash(edition / f'indexes/{bucket}.md') != manifest['index_sha256']:
        raise ValueError('Partition index changed')
    source, rows = partition_rows(source_path)
    overlay = source.get('definition_overlay_revision') is not None
    if overlay:
        if definition_sha256 is None:
            _, all_decisions, definition_sha256 = load_ledger(
                edition / LEDGER_NAME, source.get('definition_ledger_sha256'))
            definition_rows = [row for row in all_decisions if partition_for(row['word']) == bucket]
        if source.get('definition_ledger_sha256') != definition_sha256:
            raise ValueError('Partition definition ledger identity mismatch')
    elif definition_rows or definition_sha256 is not None:
        raise ValueError('Definition decisions cannot be applied without the source gate')
    by_word = {}
    for decision in definition_rows or []:
        by_word.setdefault(decision['word'], []).append(decision)
    targets = None
    if source.get('editorial_revision') == 'corrections-v1':
        targets = (edition_relation_targets(edition, relation_words(rows))
                   if build_targets is None else build_targets)
        if manifest.get('relation_targets') != targets:
            raise ValueError('Partition relation targets do not match the exact source inventory')
    expected_names = set()
    counts = Counter()
    corrections = Counter()
    definitions = Counter({key: 0 for key in counter_keys(source.get('definition_overlay_revision'))})
    for word, group in itertools.groupby(rows, key=lambda row: row['word']):
        records = list(group)
        relative = full_entry_path(word)
        if relative.parts[1] != bucket:
            raise ValueError('Record in wrong partition')
        actual = (edition / relative).read_bytes()
        if digest(actual) != manifest['files'].get(str(relative)):
            raise ValueError(f'Entry checksum mismatch: {relative}')
        if actual != render(word, records, source, relation_targets=targets,
                            decisions=by_word.pop(word, [])).encode():
            raise ValueError(f'Entry does not reproduce source projection: {relative}')
        if targets is not None:
            entry_metadata, _ = parse_document(actual.decode('utf-8'))
            changes = correction_counts(entry_metadata)
            corrections.update(changes)
            counts['corrected_headwords'] += bool(changes)
            if overlay:
                definitions.update(gap_counts(entry_metadata))
        if relative.name in expected_names:
            raise ValueError('Headword repeated outside its source group')
        expected_names.add(relative.name)
        counts['headwords'] += 1
        counts['accepted_records'] += len(records)
        counts['senses'] += sum(len(row.get('senses', [])) for row in records)
    actual_names = {path.name for path in (edition / 'entries' / bucket).iterdir()}
    if actual_names != expected_names or len(manifest['files']) != len(expected_names):
        raise ValueError('Unexpected or missing files in partition')
    if any(manifest[key] != counts[key] for key in counts):
        raise ValueError('Partition counters do not reconcile')
    if targets is not None and manifest.get('correction_counts') != dict(corrections):
        raise ValueError('Partition correction counters do not reconcile')
    if by_word:
        raise ValueError('Definition ledger contains an unmatched headword')
    if overlay and manifest.get('definition_counts') != dict(definitions):
        raise ValueError('Partition definition counters do not reconcile')
    return {**dict(counts), **({'correction_counts': dict(corrections)} if targets is not None else {}),
            **({'definition_counts': dict(definitions)} if overlay else {}),
            'manifest_sha256': file_hash(manifest_path), 'partition': bucket}


def build_partition(root_string: str, stage_string: str, bucket: str, source: dict,
                    definition_rows: list[dict] | None = None) -> dict:
    root, stage = Path(root_string), Path(stage_string)
    manifest_path = stage / f'manifests/{bucket}.md'
    with sqlite3.connect(root / '.cache/full-import/source.sqlite3') as db:
        partition_bytes = db.execute(
            'SELECT coalesce(sum(length(CAST(payload AS BLOB))),0) FROM records WHERE bucket=?', (bucket,)).fetchone()[0]
        if partition_bytes > MAX_PARTITION:
            raise ValueError('Partition exceeds memory bound before allocation')
        rows = [json.loads(row[0]) for row in db.execute(
            'SELECT payload FROM records WHERE bucket=? ORDER BY word COLLATE BINARY,ordinal', (bucket,))]
        targets = database_relation_targets(db, relation_words(rows))
    if manifest_path.exists():
        return validate_partition(stage, bucket, build_targets=targets,
                                  definition_rows=definition_rows,
                                  definition_sha256=source.get('definition_ledger_sha256'))
    source_text = source_document(rows, source, bucket)
    if len(source_text.encode()) > MAX_PARTITION:
        raise ValueError('Partition too large; increase partitioning before import')
    checked_write(stage / f'sources/{bucket}.md', source_text)
    counts = Counter()
    corrections = Counter()
    definitions = Counter({key: 0 for key in counter_keys(source.get('definition_overlay_revision'))})
    by_word = {}
    for decision in definition_rows or []:
        by_word.setdefault(decision['word'], []).append(decision)
    files = {}
    index_lines = [f'# Dictionary partition {bucket}', '', '[Dictionary index](../INDEX.md)', '',
                   '| Headword | Records | Senses |', '| --- | ---: | ---: |']
    for word, group in itertools.groupby(rows, key=lambda row: row['word']):
        records = list(group)
        relative = full_entry_path(word)
        text = render(word, records, source, relation_targets=targets,
                      decisions=by_word.pop(word, []))
        entry_metadata, _ = parse_document(text)
        changes = correction_counts(entry_metadata)
        corrections.update(changes)
        counts['corrected_headwords'] += bool(changes)
        definitions.update(gap_counts(entry_metadata))
        if str(relative) in files:
            raise ValueError('Filename collision')
        checked_write(stage / relative, text)
        files[str(relative)] = digest(text.encode())
        senses = sum(len(row.get('senses', [])) for row in records)
        counts['headwords'] += 1
        counts['accepted_records'] += len(records)
        counts['senses'] += senses
        index_lines.append(f'| [{_text(display_headword(word))}](../{relative.as_posix()}) | {len(records)} | {senses} |')
    if by_word:
        raise ValueError('Definition ledger contains an unmatched headword')
    index_text = document({'kind': 'dictionary-partition-index', 'partition': bucket, **counts}, '\n'.join(index_lines))
    checked_write(stage / f'indexes/{bucket}.md', index_text)
    metadata = {'kind': 'partition-manifest', 'partition': bucket, **counts,
                'source_sha256': digest(source_text.encode()), 'index_sha256': digest(index_text.encode()),
                'relation_targets': targets, 'correction_counts': dict(corrections), 'files': files}
    if source.get('definition_overlay_revision'):
        metadata['definition_counts'] = dict(definitions)
    # fsync the checkpoint only after all target files can be read back correctly.
    for name, expected in files.items():
        if file_hash(stage / name) != expected:
            raise ValueError(f'Entry readback mismatch: {name}')
    atomic_write(manifest_path, document(metadata, f'# Partition {bucket} integrity manifest\n\nSHA-256 covers exact UTF-8 Markdown bytes.'))
    return {**dict(counts), 'correction_counts': dict(corrections),
            **({'definition_counts': dict(definitions)} if source.get('definition_overlay_revision') else {}),
            'partition': bucket, 'manifest_sha256': file_hash(manifest_path)}


def edition_summary(root: Path, stage: Path, source: dict, inventory: dict, partitions: dict) -> None:
    totals = {key: sum(value[key] for value in partitions.values()) for key in ('headwords', 'accepted_records', 'senses')}
    if any(totals[key] != inventory[key] for key in totals):
        raise ValueError('Rendered corpus does not reconcile with source inventory')
    totals['corrected_headwords'] = sum(value['corrected_headwords'] for value in partitions.values())
    corrections = Counter()
    definitions = Counter({key: 0 for key in counter_keys(source.get('definition_overlay_revision'))})
    for value in partitions.values():
        corrections.update(value['correction_counts'])
        definitions.update(value.get('definition_counts', {}))
    navigation, browse_files = build_browse(root, stage)
    index = ['# English dictionary', '', f'{totals["headwords"]:,} headwords · {totals["accepted_records"]:,} lexical records · {totals["senses"]:,} senses.', '',
             '[Progress](PROGRESS.md) · [Source](SOURCE.md) · [Integrity](MANIFEST.md)', '', navigation, '', '## Source partitions', '',
             'Words retain exact source spelling and case. Partitions bound directory and index sizes.', '',
             'To locate a word locally: `python3 -m scripts.full_pipeline find --word dictionary` from the project root.', '',
             '| Partition | Headwords | Records |', '| --- | ---: | ---: |']
    for bucket, stats in sorted(partitions.items()):
        index.append(f'| [{bucket}](indexes/{bucket}.md) | {stats["headwords"]:,} | {stats["accepted_records"]:,} |')
    checked_write(stage / 'INDEX.md', document({'kind': 'dictionary-index', **totals}, '\n'.join(index)))
    source_body = '# Complete source snapshot\n\n' + '\n'.join([
        '- Source: [Kaikki raw English Wiktionary export](https://kaikki.org/dictionary/rawdata.html).',
        '- Text by English Wiktionary contributors, licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).',
        '- Per-entry source and contributor-history links accompany every headword.',
        '- All English lexical records in this pinned export are represented; other languages and non-lexical hard redirects are counted separately.',
        '- Translations, referenced sense quotations, media and unused extraction fields are omitted. Embedded material in free-form etymology requires separate review.',
        '- Form and spelling-variant relations link only to exact English headwords present in this edition. These are entry-level links, not independently aligned sense links.',
        '- Automatic editorial display corrections are counted separately from independent linguistic review. They do not certify every definition or pronunciation.',
        '- The records_with_source_gaps counter identifies missing senses or glosses; pronunciation and etymology gaps are disclosed separately in entries.',
        '- No Portuguese translations or explanations are added. English etymologies may mention words from other languages.',
        '- Cambridge and modern Oxford references inform editorial organization and targeted factual verification; their dictionary text is not imported and no affiliation is claimed. Historical first-edition NED evidence is separately attributed in the definition ledger.',
        '- Raw SHA-256 covers every uncompressed source byte. Compressed SHA-256 covers the retained gzip archive.',
        '- The source projections in sources/*.md permit offline reproduction of every reading view.'])
    if source.get('definition_overlay_revision'):
        source_body += ('\n\n## Evidence-pinned definition supplements\n\n'
                        'The imported source projections and their original gaps remain unchanged. '
                        'The versioned [decision ledger](DEFINITIONS.md) supplies separately attributed '
                        'definitions and verified thesaurus source classifications. Every target is '
                        'guarded by its exact source and parent-entry hashes; pending cases remain '
                        'undefined. Source-gap counts do not measure remaining reading-view gaps.\n')
    checked_write(stage / 'SOURCE.md', document({'kind': 'complete-source-snapshot', **source, 'inventory': inventory}, source_body))
    progress_lines = ['# English dictionary progress', '', '**Pinned source import: complete.**', '',
        '| Stage | Completed | Total | Progress |', '| --- | ---: | ---: | ---: |',
        f'| Source records processed | {inventory["raw_records"]:,} | {inventory["raw_records"]:,} | 100% |',
        f'| English lexical records | {totals["accepted_records"]:,} | {inventory["english_records"]:,} | 100% |',
        f'| Headwords in Markdown | {totals["headwords"]:,} | {inventory["headwords"]:,} | 100% |',
        f'| Partitions verified | {len(partitions):,} | {len(partitions):,} | 100% |',
        '', f'Other-language records: {inventory.get("other_language_records",0):,}. Non-lexical redirects: {inventory.get("redirect_records",0):,}. Rejected records: {inventory.get("rejected_records",0):,}.', '',
        f'Headwords with automatic editorial display changes: **{totals["corrected_headwords"]:,}**. '
        'This is a count of changed reading views, not linguistically certified entries.', '',
        '**Coverage of all English: unknown. Editorial review: pending.**', '',
        'These percentages measure a dated source export, not the entire English language.',
        'Source omissions remain explicit; definitions, pronunciation and historical dates are not invented.']
    progress_metadata = {'kind': 'edition-progress', **totals, 'partitions': len(partitions), 'editorial_review': 'pending'}
    if source.get('definition_overlay_revision'):
        progress_metadata['definition_counts'] = dict(definitions)
        progress_lines.extend(['', '## Definition-gap decisions', '',
                               '| Measure | Count |', '| --- | ---: |'])
        for key, value in definitions.items():
            progress_lines.append(f'| {key.replace("_", " ").capitalize()} | {value:,} |')
        progress_lines.extend(['', 'Auxiliary classifications are not filled definitions. '
                               'The pinned source retains its original gaps. '
                               '[Immutable evidence and decisions](DEFINITIONS.md).'])
    checked_write(stage / 'PROGRESS.md', document(progress_metadata, '\n'.join(progress_lines)))
    metadata = {'kind': 'full-edition-manifest', 'edition': stage.name, **totals,
                'correction_counts': dict(corrections),
                'inventory': inventory, 'partitions': {key: value['manifest_sha256'] for key, value in sorted(partitions.items())},
                'files': {**{name: file_hash(stage / name) for name in ('INDEX.md', 'SOURCE.md', 'PROGRESS.md')}, **browse_files}}
    if source.get('definition_overlay_revision'):
        metadata.update(definition_counts=dict(definitions),
                        definition_ledger_sha256=source['definition_ledger_sha256'])
        metadata['files'][LEDGER_NAME] = file_hash(stage / LEDGER_NAME)
    atomic_write(stage / 'MANIFEST.md', document(metadata, '# Complete edition integrity manifest\n\nPartition manifests cover each source projection, index and dictionary entry.'))


def audit_edition(edition: Path, root: Path | None = None, workers: int = 4) -> dict:
    manifest, _ = read_md(edition / 'MANIFEST.md')
    expected_root = {'INDEX.md', 'SOURCE.md', 'PROGRESS.md', 'MANIFEST.md', 'entries', 'indexes', 'sources', 'manifests', 'browse'}
    decisions_by_bucket = {}
    ledger_metadata = None
    source_metadata = {}
    if 'definition_ledger_sha256' in manifest:
        expected_root.add(LEDGER_NAME)
        ledger_metadata, decisions, checksum = load_ledger(edition / LEDGER_NAME,
                                                          manifest['definition_ledger_sha256'])
        source_metadata, _ = read_md(edition / 'SOURCE.md')
        if (source_metadata.get('definition_overlay_revision') not in SUPPORTED_REVISIONS
                or source_metadata.get('definition_ledger_sha256') != checksum
                or source_metadata.get('raw_sha256') != ledger_metadata['raw_sha256']
                or source_metadata.get('snapshot_id') != ledger_metadata['snapshot_id']):
            raise ValueError('Edition source and definition ledger disagree')
        for decision in decisions:
            decisions_by_bucket.setdefault(partition_for(decision['word']), []).append(decision)
        if set(decisions_by_bucket) - set(manifest['partitions']):
            raise ValueError('Definition decision has no source partition')
    if {item.name for item in edition.iterdir()} != expected_root:
        raise ValueError('Unexpected files in edition root')
    for name, expected in manifest['files'].items():
        if file_hash(edition / name) != expected:
            raise ValueError(f'Edition metadata checksum mismatch: {name}')
    browse_paths = {name for name in manifest['files'] if name.startswith('browse/')}
    if {str(path.relative_to(edition)) for path in (edition / 'browse').rglob('*') if path.is_file()} != browse_paths:
        raise ValueError('Unexpected or missing alphabetical navigation files')
    browse_total = sum(read_md(edition / name)[0]['headwords'] for name in browse_paths if not name.endswith('/INDEX.md'))
    if browse_total != manifest['headwords']:
        raise ValueError('Alphabetical navigation does not reconcile with the corpus')
    totals = Counter()
    corrections = Counter()
    definitions = Counter({key: 0 for key in counter_keys(source_metadata.get('definition_overlay_revision'))})
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(validate_partition, edition, bucket, checksum,
                               definition_rows=decisions_by_bucket.get(bucket),
                               definition_sha256=manifest.get('definition_ledger_sha256')): bucket
                   for bucket, checksum in manifest['partitions'].items()}
        for future in as_completed(futures):
            stats = future.result()
            totals.update({key: stats[key] for key in ('headwords', 'accepted_records', 'senses')})
            if 'corrected_headwords' in stats:
                totals['corrected_headwords'] += stats['corrected_headwords']
                corrections.update(stats['correction_counts'])
                definitions.update(stats.get('definition_counts', {}))
            totals['completed_partitions'] += 1
            if root and totals['completed_partitions'] % 16 == 0:
                progress(root, 'Verifying Markdown against source projections', dict(totals), total_partitions=len(futures))
    for folder in ('entries', 'indexes', 'sources', 'manifests'):
        expected_names = {bucket + ('' if folder == 'entries' else '.md') for bucket in manifest['partitions']}
        if {item.name for item in (edition / folder).iterdir()} != expected_names:
            raise ValueError(f'Unexpected files or partitions in {folder}')
    for key in ('headwords', 'accepted_records', 'senses'):
        if totals[key] != manifest[key] or totals[key] != manifest['inventory'][key]:
            raise ValueError('Edition counters do not reconcile')
    if 'correction_counts' in manifest:
        if (dict(corrections) != manifest['correction_counts'] or
                totals['corrected_headwords'] != manifest['corrected_headwords']):
            raise ValueError('Edition correction counters do not reconcile')
        if ledger_metadata is not None:
            if (dict(definitions) != manifest.get('definition_counts')
                    or definitions['ledger_decisions_applied'] != ledger_metadata['decision_count']
                    or definitions['source_gap_senses'] != sum(definitions[key] for key in
                        ('definition_filled_senses', 'auxiliary_relation_senses', 'unresolved_gloss_senses',
                         'source_structure_senses'))):
                raise ValueError('Edition definition counters do not reconcile')
        return {**dict(totals), 'correction_counts': dict(corrections),
                **({'definition_counts': dict(definitions)} if ledger_metadata is not None else {})}
    return dict(totals)


def inherited_definition_ledger(root: Path) -> Path | None:
    """Keep accepted supplements when the normal build command is repeated."""
    pointer = root / 'CURRENT.md'
    if not pointer.exists():
        return None
    current, _ = read_md(pointer)
    if not str(current.get('edition', '')).startswith('full-'):
        return None
    edition = active(root)
    manifest, _ = read_md(edition / 'MANIFEST.md')
    if manifest.get('kind') != 'full-edition-manifest' or manifest.get('edition') != edition.name:
        raise ValueError('Invalid current edition manifest')
    source_path = edition / 'SOURCE.md'
    if file_hash(source_path) != manifest.get('files', {}).get('SOURCE.md'):
        raise ValueError('Current edition source checksum mismatch')
    source, _ = read_md(source_path)
    ledger = edition / LEDGER_NAME
    checksum = manifest.get('definition_ledger_sha256')
    if checksum is None and not ledger.exists() and not source.get('definition_overlay_revision'):
        return None
    if (not checksum or manifest.get('files', {}).get(LEDGER_NAME) != checksum
            or source.get('definition_overlay_revision') not in SUPPORTED_REVISIONS
            or source.get('definition_ledger_sha256') != checksum):
        raise ValueError('Current edition definition ledger metadata mismatch')
    load_ledger(ledger, checksum)
    return ledger


def build(root: Path, workers: int = 4, max_partitions: int | None = None,
          definition_ledger: Path | None = None) -> dict:
    with exclusive(root) as cache:
        if definition_ledger is None:
            definition_ledger = inherited_definition_ledger(root)
        with database(root) as db:
            if not state_get(db, 'complete'):
                raise ValueError('Complete source ingestion is required before building')
            inventory = state_get(db, 'result')
            if inventory.get('rejected_records', 0):
                reasons = db.execute('SELECT reason,count(*) FROM rejected GROUP BY reason').fetchall()
                raise ValueError(f'Unresolved source records prevent complete promotion: {reasons}')
            source = state_get(db, 'source')
            projector_code = b''.join((Path(__file__).parent / name).read_bytes() for name in
                                     ('full_projection.py', 'prepare.py', 'entries.py', 'markdown.py'))
            if source['projector_sha256'] != digest(projector_code):
                raise ValueError('Projection code changed; rebuild ingestion before rendering')
            source['raw_sha256'] = inventory['raw_sha256']
            source['editorial_revision'] = 'corrections-v1'
            buckets = [row[0] for row in db.execute('SELECT DISTINCT bucket FROM records ORDER BY bucket')]
        if not buckets:
            raise ValueError('The source inventory contains no English headwords')
        if max_partitions is not None and max_partitions < 1:
            raise ValueError('max_partitions must be positive')
        ledger_text = None
        decisions_by_bucket = {}
        if definition_ledger is not None:
            ledger_metadata, decisions, ledger_sha = load_ledger(definition_ledger)
            base_name = ledger_metadata['base_edition']
            if not re.fullmatch(r'full-[0-9a-f]{16}', base_name):
                raise ValueError('Invalid base edition identity')
            if file_hash(root / 'editions' / base_name / 'MANIFEST.md') != ledger_metadata['base_manifest_sha256']:
                raise ValueError('Definition ledger base manifest mismatch')
            if (ledger_metadata['snapshot_id'] != source['snapshot_id']
                    or ledger_metadata['raw_sha256'] != source['raw_sha256']):
                raise ValueError('Definition ledger belongs to another source snapshot')
            for decision in decisions:
                decisions_by_bucket.setdefault(partition_for(decision['word']), []).append(decision)
            if set(decisions_by_bucket) - set(buckets):
                raise ValueError('Definition ledger contains unknown source partitions')
            ledger_text = definition_ledger.read_text(encoding='utf-8')
            if digest(ledger_text.encode()) != ledger_sha:
                raise ValueError('Definition ledger changed while preparing the build')
            source.update(definition_overlay_revision=DEFINITION_REVISION,
                          definition_ledger_sha256=ledger_sha)
        code = b''.join((Path(__file__).parent / name).read_bytes() for name in
                        ('full_pipeline.py', 'full_projection.py', 'full_browse.py', 'entries.py',
                         'editorial.py', 'editorial_patches.py', 'markdown.py', 'prepare.py',
                         'definition_overlay.py'))
        source['renderer_sha256'] = digest(code)
        edition_id = 'full-' + digest((source['raw_sha256'] + source['renderer_sha256']
                                       + source.get('definition_ledger_sha256', '')).encode())[:16]
        stage = cache / 'builds' / edition_id
        published = root / 'editions' / edition_id
        if published.exists():
            result = audit_edition(published, root, workers)
            promote_pointer(root, edition_id, result)
            return result
        stage.mkdir(parents=True, exist_ok=True)
        if ledger_text is not None:
            checked_write(stage / LEDGER_NAME, ledger_text)
        selected = buckets[:max_partitions] if max_partitions is not None else buckets
        results = {}
        totals = Counter()
        start = time.monotonic()
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(build_partition, str(root), str(stage), bucket, source,
                                       decisions_by_bucket.get(bucket)): bucket for bucket in selected}
                for future in as_completed(futures):
                    stats = future.result()
                    results[stats['partition']] = stats
                    totals.update({key: stats[key] for key in ('headwords', 'accepted_records', 'senses')})
                    totals['corrected_headwords'] += stats['corrected_headwords']
                    totals['completed_partitions'] += 1
                    progress(root, 'Writing English Markdown', dict(totals), total_partitions=len(buckets),
                             elapsed_seconds=round(time.monotonic()-start, 1))
                    if totals['completed_partitions'] % 16 == 0:
                        print(encode({'phase': 'build', **totals}), flush=True)
            if len(selected) != len(buckets):
                return dict(totals)
            edition_summary(root, stage, source, inventory, results)
            verified = audit_edition(stage, root, workers)
            published.parent.mkdir(parents=True, exist_ok=True)
            os.rename(stage, published)
            promote_pointer(root, edition_id, verified)
            return verified
        except BaseException as error:
            progress(root, 'Stopped; previous accepted edition preserved', dict(totals), error=str(error))
            raise


def promote_pointer(root: Path, edition_id: str, totals: dict) -> None:
    atomic_write(root / 'CURRENT.md', document({'kind': 'current-edition', 'edition': edition_id},
        '# Current English dictionary\n\n' + '\n'.join(
            f'- [{label}](editions/{edition_id}/{name}.md)' for label, name in
            [('Browse the dictionary', 'INDEX'), ('Collection progress', 'PROGRESS'),
             ('Source snapshot', 'SOURCE'), ('Integrity manifest', 'MANIFEST')])) )
    progress(root, 'Complete and verified', totals, total_partitions=totals['completed_partitions'])


def active(root: Path) -> Path:
    current, _ = read_md(root / 'CURRENT.md')
    edition_id = current.get('edition', '')
    if not isinstance(edition_id, str) or not re.fullmatch(r'full-[0-9a-f]{16}', edition_id):
        raise ValueError('Current edition is not a complete-import edition')
    return root / 'editions' / edition_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('ingest', 'build', 'audit', 'find'):
        command = sub.add_parser(name)
        command.add_argument('--root', type=Path, default=Path('.'))
        if name == 'ingest':
            command.add_argument('--archive', type=Path, required=True)
            command.add_argument('--evidence', type=Path, required=True)
        if name in ('build', 'audit'):
            command.add_argument('--workers', type=int, choices=range(1, 9), default=4)
        if name == 'build':
            command.add_argument('--max-partitions', type=int)
            command.add_argument('--definition-ledger', type=Path)
        if name == 'find':
            command.add_argument('--word', required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'ingest':
        result = ingest(root, args.archive.resolve(), args.evidence.resolve())
    elif args.command == 'build':
        result = build(root, args.workers, args.max_partitions,
                       args.definition_ledger.resolve() if args.definition_ledger else None)
    elif args.command == 'audit':
        with exclusive(root):
            result = audit_edition(active(root), workers=args.workers)
    else:
        path = active(root) / full_entry_path(args.word)
        if not path.is_file():
            parser.exit(1, 'Exact headword not present in this source snapshot.\n')
        result = {'path': str(path)}
    print(encode(result))


if __name__ == '__main__':
    main()
