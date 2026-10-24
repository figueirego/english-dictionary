"""Prepare a bounded, English-only Markdown input from preserved source bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path

from scripts.markdown import document, parse_document

MAX_RAW_BYTES = 16 * 1024 * 1024
EXTRA_HEADWORDS = frozenset({
    'a-', 'A 1', "Aaron's rod", 'aard-vark', 'abandoned', 'abased',
    'abbreviated', 'abiding', 'able-bodied', 'abler', 'abode',
    'above-mentioned', 'abscissa', 'mouse potato', 'robot',
    'pneumonoultramicroscopicsilicovolcanoconiosis',
})
EVIDENCE_FIELDS = (
    'dump_date', 'extraction_date', 'wiktextract_commit',
    'wikitextprocessor_commit', 'catalogue_url', 'source_url', 'raw_sha256',
)
RELATION_FIELDS = ('alt_of', 'form_of', 'synonyms', 'antonyms')


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}')
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ValueError(f'Non-finite JSON value: {value}')


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f'{label} must be a list of strings')
    return value


def _objects(value: object, label: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f'{label} must be a list of objects')
    return value


def _fields(value: dict[str, object], names: tuple[str, ...]) -> dict[str, object]:
    """Copy only scalar fields; nested unexpected content cannot cross the boundary."""
    result: dict[str, object] = {}
    for name in names:
        if name not in value:
            continue
        item = value[name]
        if name == 'tags':
            result[name] = _strings(item, name)
        elif not isinstance(item, str):
            raise ValueError(f'{name} must be a string')
        else:
            result[name] = item
    return result


def _relations(value: object, label: str) -> list[dict[str, object]]:
    return [_fields(item, ('word', 'tags', 'sense')) for item in _objects(value, label)]


def _project(record: dict[str, object]) -> dict[str, object]:
    result = _fields(record, ('word', 'lang_code', 'pos', 'etymology_text'))
    for name in ('word', 'pos'):
        if not isinstance(result.get(name), str) or not result[name].strip():
            raise ValueError(f'English record requires a non-empty {name}')
    if 'etymology_number' in record:
        number = record['etymology_number']
        if type(number) not in (str, int):
            raise ValueError('etymology_number must be a string or integer')
        result['etymology_number'] = number
    if 'forms' in record:
        result['forms'] = [_fields(item, ('form', 'tags'))
                           for item in _objects(record['forms'], 'forms')]
    if 'sounds' in record:
        sounds = [_fields(item, ('ipa', 'enpr', 'tags'))
                  for item in _objects(record['sounds'], 'sounds')]
        result['sounds'] = [item for item in sounds if 'ipa' in item or 'enpr' in item]
    senses = []
    for sense in _objects(record.get('senses'), 'senses'):
        glosses = _strings(sense.get('glosses'), 'glosses')
        if not glosses or any(not item.strip() for item in glosses):
            raise ValueError('Each sense requires non-empty glosses')
        projected = {'glosses': glosses, **_fields(sense, ('tags', 'qualifier'))}
        if 'examples' in sense:
            examples = []
            for example in _objects(sense['examples'], 'examples'):
                if (example.get('type') != 'example' or 'ref' in example
                        or 'translation' in example or 'translations' in example):
                    continue
                safe = _fields(example, ('type', 'text'))
                if isinstance(safe.get('text'), str) and safe['text'].strip():
                    examples.append(safe)
            if examples:
                projected['examples'] = examples
        for name in RELATION_FIELDS:
            if name in sense:
                projected[name] = _relations(sense[name], name)
        senses.append(projected)
    if not senses:
        raise ValueError('English records require at least one sense')
    result['senses'] = senses
    for name in ('synonyms', 'antonyms'):
        if name in record:
            result[name] = _relations(record[name], name)
    return result


def _save_immutable(path: Path, content: bytes) -> None:
    """Create a complete file atomically, without replacing a concurrent writer."""
    if path.exists():
        if path.is_symlink() or path.read_bytes() != content:
            raise ValueError('Output already exists with different bytes; choose a new path')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.prepare-', delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise ValueError('Output already exists with different bytes; choose a new path')
    finally:
        if temporary is not None:
            os.unlink(temporary)


def prepare(raw_prefix: Path, output: Path, evidence: dict[str, object],
            first_headwords: int = 70) -> dict[str, object]:
    """Preserve every observed English record for the bounded headword selection.

    Upstream line numbers are one-based and byte offsets are zero-based. Record
    hashes cover the original line bytes, including their terminating LF byte.
    A non-LF-terminated tail is excluded even when it happens to be valid JSON.
    """
    if type(first_headwords) is not int or not 0 <= first_headwords <= 10_000:
        raise ValueError('first_headwords must be an integer from 0 through 10000')
    if not isinstance(evidence, dict):
        raise ValueError('Source evidence must be an object')
    for field in EVIDENCE_FIELDS:
        if not isinstance(evidence.get(field), str) or not evidence[field].strip():
            raise ValueError(f'Missing source evidence: {field}')
    try:
        dump_date = date.fromisoformat(evidence['dump_date'])
    except ValueError as error:
        raise ValueError('dump_date must be an explicit ISO date') from error
    if evidence['dump_date'] != dump_date.isoformat():
        raise ValueError('dump_date must use YYYY-MM-DD')
    with raw_prefix.open('rb') as stream:
        raw = stream.read(MAX_RAW_BYTES + 1)
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise ValueError('Raw input must contain 1 through 16777216 bytes')
    raw_sha256 = _sha256(raw)
    if evidence['raw_sha256'] != raw_sha256:
        raise ValueError('Source evidence raw_sha256 does not match the preserved raw input')
    complete_end = raw.rfind(b'\n') + 1
    selected_words: set[str] = set()
    rows: list[tuple[int, int, bytes, dict[str, object]]] = []
    offset = 0
    total_lines = 0
    other_languages = 0
    redirects = 0
    for number, fragment in enumerate(raw[:complete_end].split(b'\n')[:-1], 1):
        line = fragment + b'\n'
        total_lines += 1
        try:
            record = json.loads(line, object_pairs_hook=_object_pairs,
                                parse_constant=_invalid_constant)
        except (ValueError, UnicodeError) as error:
            raise ValueError(f'Invalid JSON at upstream line {number}') from error
        if not isinstance(record, dict):
            raise ValueError(f'Upstream line {number} must contain an object')
        if record.get('pos') == 'hard-redirect':
            if any(not isinstance(record.get(field), str) or not record[field].strip()
                   for field in ('title', 'redirect')):
                raise ValueError(f'Invalid hard-redirect schema at upstream line {number}')
            redirects += 1
            offset += len(line)
            continue
        language = record.get('lang_code')
        if (not isinstance(language, str)
                or not re.fullmatch(r'[a-z]{2,3}(?:-[a-z0-9]{1,12})*', language)):
            raise ValueError(f'Missing or invalid lang_code at upstream line {number}')
        if language == 'en':
            word = record.get('word')
            if not isinstance(word, str) or not word.strip():
                raise ValueError(f'English headword missing at upstream line {number}')
            if len(selected_words) < first_headwords:
                selected_words.add(word)
            rows.append((number, offset, line, record))
        else:
            other_languages += 1
        offset += len(line)
    selected_words.update(EXTRA_HEADWORDS)
    records = []
    for number, offset, line, record in rows:
        if record['word'] in selected_words:
            try:
                projected = _project(record)
            except ValueError as error:
                raise ValueError(f'Invalid English record at upstream line {number}: {error}') from error
            records.append({**projected, 'upstream_record_sha256': _sha256(line),
                            'upstream_line': number, 'upstream_offset': offset})
    if not records:
        raise ValueError('The capture contains no complete selected English records')
    payload = ''.join(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False) + '\n'
                      for record in records)
    capture_counts = {
        'raw_bytes': len(raw), 'complete_line_bytes': complete_end,
        'complete_lines': total_lines, 'incomplete_tail_bytes': len(raw) - complete_end,
        'incomplete_tail_fragments': int(complete_end < len(raw)),
        'english_records': len(rows), 'other_language_records': other_languages,
        'redirect_records': redirects,
        'selected_records': len(records),
        'selected_headwords': len({record['word'] for record in records}),
        'unselected_english_records': len(rows) - len(records),
    }
    metadata: dict[str, object] = {
        'kind': 'source-snapshot',
        'snapshot_id': f'enwiktionary-{dump_date:%Y%m%d}-pilot-{raw_sha256[:12]}',
        'license': 'CC-BY-SA-4.0', 'raw_sha256': raw_sha256,
        'records_sha256': _sha256(payload.encode('utf-8')), 'record_count': len(records),
        **{field: evidence[field] for field in EVIDENCE_FIELDS},
        'capture_counts': capture_counts,
        'selection_first_headwords': first_headwords,
        'selection_additional_headwords': sorted(EXTRA_HEADWORDS),
        'upstream_record_hash_framing': 'Original line bytes including terminating LF',
    }
    body = (
        '# Preserved pilot input\n\n'
        'This is an English-only projection from a bounded source byte prefix. '
        'It is a non-random pilot, not a complete source snapshot or a measure of '
        'global English word coverage. All observed records for selected headwords '
        'are retained; an incomplete trailing line is excluded.\n\n'
        'The original prefix remains in the local raw cache. Its SHA-256 identifies '
        'those bytes and does not independently authenticate the source. Line numbers '
        'are one-based; byte offsets are zero-based; original record hashes include '
        'the terminating LF.\n\n'
        'Projection preserves supported lexical fields and omits translations, '
        'descendants, external quotations, audio, media and source link arrays. '
        'Only usage examples explicitly marked as examples and without a reference '
        'or translation are retained. Missing information has not been generated.\n\n'
        'Text is adapted from English Wiktionary contributors through Kaikki/Wiktextract '
        'under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). '
        'Rendered entries provide the individual source and history links.\n\n'
        '```jsonl\n' + payload + '```\n'
    )
    _save_immutable(output, document(metadata, body).encode('utf-8'))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True,
                        help='Markdown with verified source evidence in front matter')
    parser.add_argument('--output', type=Path, default=Path('sources/pilot.md'))
    parser.add_argument('--first-headwords', type=int, default=70)
    arguments = parser.parse_args()
    try:
        evidence, _ = parse_document(arguments.evidence.read_text(encoding='utf-8'))
        metadata = prepare(arguments.raw, arguments.output, evidence, arguments.first_headwords)
    except (ValueError, OSError) as error:
        parser.exit(1, f'Preparation failed: {error}\n')
    print(f"Prepared {metadata['record_count']} records in {arguments.output}")


if __name__ == '__main__':
    main()
