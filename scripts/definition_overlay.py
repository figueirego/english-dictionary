"""Evidence-pinned supplements to exact missing source senses.

The source renderer and its fingerprints remain unchanged. An immutable Markdown
ledger authorizes narrowly guarded changes to its reading view, never its source.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from scripts.entries import POS_NAMES, _text
from scripts.full_projection import render as source_render, _source_pages
from scripts.markdown import document, parse_document
from scripts.prepare import _object_pairs, _invalid_constant

REVISION = 'definitions-v2'
SUPPORTED_REVISIONS = ('definitions-v1', REVISION)
READING_POS_NAMES = {**POS_NAMES, 'root': 'Bound root'}
LEDGER_NAME = 'DEFINITIONS.md'
V1_COUNTERS = ('source_gap_senses', 'definition_filled_senses',
            'auxiliary_relation_senses', 'unresolved_gloss_senses',
            'definition_filled_headwords', 'auxiliary_relation_headwords',
            'source_kind_corrected_records', 'ledger_decisions_applied',
            'auxiliary_thesaurus_senses', 'auxiliary_translation_hub_senses')
COUNTERS = V1_COUNTERS + ('source_structure_senses', 'part_of_speech_corrected_records')
MAX_LEDGER_BYTES = 64 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}')


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sense_hash(sense: dict) -> str:
    return digest(json.dumps(sense, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode())


def _load(text: str) -> object:
    return json.loads(text, object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a non-empty string')
    return value


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError(f'{label} must be a SHA-256 digest')
    return value


def safe_url(value: object) -> str:
    text = _string(value, 'Evidence URL')
    parsed = urlsplit(text)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username or parsed.password or any(ord(c) < 33 for c in text)):
        raise ValueError('Evidence URL must be an explicit safe HTTP(S) URL')
    return quote(text, safe=':/?#[]@!$&\'*,;=+%-._~')


def _missing(sense: dict) -> bool:
    glosses = sense.get('glosses', [])
    return not glosses or any(not value.strip() for value in glosses)


def validate_decision(row: dict) -> None:
    if not isinstance(row, dict):
        raise ValueError('Definition decision must be an object')
    for key in ('id', 'pos', 'reason'):
        _string(row.get(key), key)
    if not isinstance(row.get('word'), str) or not 1 <= len(row['word']) <= 1024:
        raise ValueError('word must preserve an exact nonempty source identity')
    for key in ('record', 'sense'):
        if type(row.get(key)) is not int or row[key] < 1:
            raise ValueError(f'{key} must be a positive integer')
    for key in ('upstream_record_sha256', 'source_sense_sha256', 'base_entry_sha256'):
        _sha(row.get(key), key)
    if 'transformation' in row:
        _string(row['transformation'], 'transformation')
    status = row.get('status')
    if status not in ('definition', 'auxiliary', 'pending', 'source-structure'):
        raise ValueError('Unsupported definition decision status')
    if status == 'definition':
        definition = _string(row.get('definition'), 'definition')
        if (len(definition) > 8000 or '\n' in definition
                or re.fullmatch(r'(?:tbd|todo|unknown|n/?a|no definition[.!]?|source gap[.!]?)',
                                definition.strip(), re.I)):
            raise ValueError('A definition cannot be a placeholder or multiline document')
    if status in ('definition', 'source-structure'):
        sources = row.get('sources')
        if not isinstance(sources, list) or not sources:
            raise ValueError('A resolved decision requires explicit source evidence')
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError('Evidence source must be an object')
            for key in ('title', 'license', 'revision', 'support'):
                _string(source.get(key), f'evidence {key}')
            safe_url(source.get('url'))
            _sha(source.get('capture_sha256'), 'evidence capture_sha256')
    if status != 'definition' and 'definition' in row:
        raise ValueError('Only approved definition decisions may contain a definition')
    if 'corrected_pos' in row:
        if status != 'definition' or (row['pos'], row['corrected_pos']) not in {
                ('adj', 'noun'), ('noun', 'pron'), ('suffix', 'root'), ('adv', 'adj'),
                ('noun', 'phrase')}:
            raise ValueError('Unsupported part-of-speech correction or missing definition')
        _string(row.get('transformation'), 'part-of-speech transformation')
        proof = row.get('part_of_speech_evidence')
        if not isinstance(proof, dict):
            raise ValueError('Part-of-speech correction requires pinned grammatical evidence')
        for key in ('capture_path', 'support'):
            _string(proof.get(key), 'part-of-speech evidence ' + key)
        _sha(proof.get('capture_sha256'), 'part-of-speech evidence capture_sha256')
    if status == 'source-structure':
        if row.get('proposed_correction') != 'empty-duplicate-part-of-speech-section':
            raise ValueError('Unsupported source-structure correction')
        if 'source_kind' in row:
            raise ValueError('A source-structure correction cannot also reclassify provenance')
        _string(row.get('transformation'), 'source-structure transformation')
        history = row.get('structure_evidence')
        if not isinstance(history, dict):
            raise ValueError('Source-structure correction requires pinned before/after evidence')
        for key in ('before_revision', 'after_revision', 'capture_path'):
            _string(history.get(key), 'structure evidence ' + key)
        _sha(history.get('capture_sha256'), 'structure evidence capture_sha256')
    if status == 'auxiliary' and row.get('source_kind') not in ('thesaurus', 'translation-hub'):
        raise ValueError('Auxiliary reclassification requires proven source provenance')
    if 'source_kind' in row:
        if row['source_kind'] not in ('thesaurus', 'translation-hub'):
            raise ValueError('Unsupported source-kind correction')
    if row.get('source_kind') == 'translation-hub' and 'raw_record_line' in row:
        raw = _string(row['raw_record_line'], 'raw_record_line')
        if digest(raw.encode()) != row['upstream_record_sha256']:
            raise ValueError('Translation-hub raw record checksum mismatch')
        original = _load(raw)
        senses = original.get('senses', [])
        if (original.get('word') != row['word'] or original.get('pos') != row['pos']
                or original.get('lang_code') != 'en' or len(senses) < row['sense']
                or 'translation-hub' not in senses[row['sense'] - 1].get('tags', [])):
            raise ValueError('Raw source does not establish this translation-hub sense')
    if row.get('source_kind') == 'thesaurus':
        title = _string(row.get('source_title'), 'source_title')
        raw = _string(row.get('raw_record_line'), 'raw_record_line')
        if (not title.startswith('Thesaurus:') or '\n' in title
                or digest(raw.encode()) != row['upstream_record_sha256']):
            raise ValueError('Thesaurus correction does not match the preserved raw record')
        original = _load(raw)
        if (original.get('word') != row['word'] or original.get('pos') != row['pos']
                or original.get('lang_code') != 'en' or original.get('source') != 'thesaurus'):
            raise ValueError('Raw source does not establish this English thesaurus record')
        titles = {relation['source'] for sense in original.get('senses', [])
                  for values in sense.values() if isinstance(values, list)
                  for relation in values if isinstance(relation, dict)
                  and isinstance(relation.get('source'), str)
                  and relation['source'].startswith('Thesaurus:')}
        if titles != {title}:
            raise ValueError('Raw relations do not establish an unambiguous thesaurus title')


def load_ledger(path: Path, expected_sha256: str | None = None) -> tuple[dict, list[dict], str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LEDGER_BYTES:
        raise ValueError('Definition ledger must be a bounded canonical Markdown file')
    data = path.read_bytes()
    checksum = digest(data)
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ValueError('Definition ledger checksum mismatch')
    metadata, body = parse_document(data.decode('utf-8'))
    if (metadata.get('kind') != 'definition-ledger' or metadata.get('schema_version') != 1
            or metadata.get('scope') != 'all-source-gloss-gaps'):
        raise ValueError('Unsupported definition ledger contract')
    for key in ('base_edition', 'snapshot_id'):
        _string(metadata.get(key), key)
    for key in ('base_manifest_sha256', 'raw_sha256'):
        _sha(metadata.get(key), key)
    if body.count('```jsonl\n') != 1:
        raise ValueError('Ledger requires exactly one JSONL evidence block')
    payload = body.split('```jsonl\n', 1)[1]
    payload, separator, _ = payload.partition('\n```')
    if not separator:
        raise ValueError('Unclosed ledger evidence block')
    rows = [_load(line) for line in payload.split('\n') if line.strip()]
    identities, ids = set(), set()
    for row in rows:
        validate_decision(row)
        identity = (row['word'], row['record'], row['sense'])
        if identity in identities or row['id'] in ids:
            raise ValueError('Duplicate definition decision identity or ID')
        identities.add(identity)
        ids.add(row['id'])
    if type(metadata.get('decision_count')) is not int or metadata['decision_count'] != len(rows):
        raise ValueError('Definition decision count mismatch')
    return metadata, rows, checksum


def counter_keys(revision: str | None) -> tuple[str, ...]:
    return V1_COUNTERS if revision == 'definitions-v1' else COUNTERS


def gap_counts(metadata: dict) -> Counter:
    values = metadata.get('definition_overlay_counts', {})
    if (not isinstance(values, dict) or any(key not in COUNTERS or type(value) is not int
            or value < 0 for key, value in values.items())):
        raise ValueError('Invalid definition overlay counters')
    if metadata.get('definition_overlay_revision') is None:
        if values:
            raise ValueError('Definition counters require an overlay revision')
        # Unchanged entries have no overlay metadata. Do not introduce newer
        # zero-valued keys when accumulating an immutable v1 partition.
        return Counter()
    return Counter({key: values.get(key, 0) for key in
                    counter_keys(metadata.get('definition_overlay_revision'))})


def render(headword: str, records: list[dict], source: dict, *,
           relation_targets: dict[str, str] | None = None,
           decisions: list[dict] | None = None) -> str:
    base = source_render(headword, records, source, relation_targets=relation_targets)
    revision = source.get('definition_overlay_revision')
    if revision is None:
        if decisions:
            raise ValueError('Definitions require an explicit version gate')
        return base
    if revision not in SUPPORTED_REVISIONS:
        raise ValueError('Unsupported definition overlay revision')
    _sha(source.get('definition_ledger_sha256'), 'definition_ledger_sha256')
    decisions = decisions or []
    original_gaps = {(number, sense_number) for number, row in enumerate(records, 1)
                     for sense_number, sense in enumerate(row.get('senses', []), 1)
                     if _missing(sense)}
    if not original_gaps:
        if decisions:
            raise ValueError('Definition decision targets an already-defined entry')
        return base
    decision_map = {(row['record'], row['sense']): row for row in decisions}
    if len(decision_map) != len(decisions) or set(decision_map) != original_gaps:
        raise ValueError('Definition ledger must cover every source gloss gap exactly once')
    base_sha = digest(base.encode())
    metadata, body = parse_document(base)
    counts = Counter({key: 0 for key in counter_keys(revision)})
    counts['source_gap_senses'] = len(original_gaps)
    corrected_sources: dict[int, str] = {}
    applied = []
    for coordinate, row in sorted(decision_map.items()):
        validate_decision(row)
        if revision == 'definitions-v1' and (row['status'] == 'source-structure' or 'corrected_pos' in row):
            raise ValueError('Source-structure correction requires definitions-v2')
        number, sense_number = coordinate
        record, sense = records[number - 1], records[number - 1]['senses'][sense_number - 1]
        if (row['word'] != headword or row['pos'] != record['pos']
                or row['upstream_record_sha256'] != record.get('upstream_record_sha256')
                or row['source_sense_sha256'] != sense_hash(sense)
                or row['base_entry_sha256'] != base_sha):
            raise ValueError('Definition decision does not match the exact source/parent identity')
        if (row.get('source_kind') == 'translation-hub'
                and 'translation-hub' not in sense.get('tags', [])):
            raise ValueError('Projected sense does not establish translation-hub provenance')
        # Partially blank gloss paths need a separately designed hierarchy repair.
        if any(value.strip() for value in sense.get('glosses', [])):
            if row['status'] != 'pending':
                raise ValueError('Cannot replace a partly defined source sense')
        anchor = f'<a id="r{number}-s{sense_number}"></a>\n\n**Source sense R{number}.S{sense_number}**\n\n'
        if body.count(anchor) != 1:
            raise ValueError('Missing or ambiguous exact source-sense anchor')
        start = body.index(anchor) + len(anchor)
        boundary = re.search(r'\n(?:<a id="r\d|##? |### )', body[start:])
        end = start + boundary.start() if boundary else len(body)
        block = body[start:end]
        status = row['status']
        note = '**Definition review:** ' + _text(row['reason'])
        if 'corrected_pos' in row:
            allows_siblings = (row['pos'], row['corrected_pos']) == ('adv', 'adj')
            if len(record.get('senses', [])) != 1 or (len(records) != 1 and not allows_siblings):
                raise ValueError('Part-of-speech correction requires one empty source section; '
                                 'sibling records are supported only for adverb-to-adjective corrections')
            heading_suffix = f' · Record {number}' if len(records) > 1 else ''
            record_heading = f'<a id="r{number}"></a>\n\n## {POS_NAMES[row["pos"]]}{heading_suffix}\n'
            if body.count(record_heading) != 1:
                raise ValueError('Exact part-of-speech heading is missing or ambiguous')
            # Apply after this sense block is written, so its positions stay valid.
            counts['part_of_speech_corrected_records'] += 1
        if status == 'definition':
            expected_lines = [f'{sense_number}. *Source gap: this sense has no gloss in the source.*']
            if sense.get('glosses'):
                expected_lines = [('   ' * depth) + (str(sense_number) if depth == 0 else '1')
                                  + '. *Source gap: a gloss is blank in the source.*'
                                  for depth, _ in enumerate(sense['glosses'])]
            expected = '\n'.join(expected_lines)
            if not block.startswith(expected + '\n'):
                raise ValueError('Expected missing-definition lines do not match the reading view')
            block = f'{sense_number}. {_text(row["definition"])}' + block[len(expected):]
            note = '**Supplemental definition (evidence reviewed; source gap preserved):** ' + _text(row['reason'])
            citations = ['[' + _text(item['title']) + '](' + safe_url(item['url']) + ')'
                         + ' — ' + _text(item['revision']) + '; ' + _text(item['license'])
                         for item in row['sources']]
            note += '\n\n**Definition sources:** ' + '; '.join(citations)
            if row.get('transformation'):
                note += '\n\n**Editorial transformation:** ' + _text(row['transformation'])
            counts['definition_filled_senses'] += 1
        elif status == 'auxiliary':
            category = ('translation hub' if row['source_kind'] == 'translation-hub'
                        else 'thesaurus relations')
            note = ('**Source classification: auxiliary ' + category + ', not an independently '
                    'defined dictionary sense.** ' + _text(row['reason']))
            counts['auxiliary_relation_senses'] += 1
            counts['auxiliary_translation_hub_senses' if row['source_kind'] == 'translation-hub'
                   else 'auxiliary_thesaurus_senses'] += 1
        elif status == 'source-structure':
            if len(record.get('senses', [])) != 1:
                raise ValueError('Source-structure repair is limited to an entirely empty section')
            expected = f'{sense_number}. *Source gap: this sense has no gloss in the source.*'
            if not block.startswith(expected + '\n'):
                raise ValueError('Source-structure repair requires an absent gloss')
            block = ('*Source structure: empty duplicate section; not an independent lexical sense.*'
                     + block[len(expected):])
            note = '**Source-structure correction (no definition added):** ' + _text(row['reason'])
            note += '\n\n**Editorial transformation:** ' + _text(row['transformation'])
            note += '\n\n**Structure evidence:** ' + '; '.join(
                '[' + _text(item['title']) + '](' + safe_url(item['url']) + ')'
                + ' — ' + _text(item['revision']) for item in row['sources'])
            counts['source_structure_senses'] += 1
        else:
            counts['unresolved_gloss_senses'] += 1
        if row.get('source_kind') == 'thesaurus':
            title = row['source_title']
            if number in corrected_sources and corrected_sources[number] != title:
                raise ValueError('Conflicting source attribution for one record')
            corrected_sources[number] = title
            note += ('\n\n**Recovered source attribution:** [' + _text(title) + ']'
                     '(https://en.wiktionary.org/wiki/' + quote(title, safe='') + '). '
                     'The preserved raw record identifies this as thesaurus content.')
        # Keep prior review notes as dated history, without presenting their pending
        # state as the current adjudication after a definition has been supplied.
        if status in ('definition', 'source-structure') and 'Unresolved source gap:' in block:
            block = block.replace('**Editorial note:** Unresolved source gap:',
                                  '**Earlier source-snapshot review (superseded by this supplement):**')
        block = block.rstrip() + '\n\n' + note + '\n\n'
        body = body[:start] + block + body[end:]
        if 'corrected_pos' in row:
            old_pos = POS_NAMES[row['pos']].lower()
            new_pos = READING_POS_NAMES[row['corrected_pos']]
            correction = (f'**Part-of-speech correction:** {new_pos}. The source labels this section as '
                          f'{old_pos}; captured grammatical evidence supports the {new_pos.lower()} reading. '
                          'Original source metadata and generated source forms remain preserved.\n')
            record_start = body.index(record_heading)
            following_record = re.search(r'\n<a id="r\d+"></a>\n|\n## Sources\n',
                                         body[record_start + len(record_heading):])
            record_end = (record_start + len(record_heading) + following_record.start()
                          if following_record else len(body))
            record_block = body[record_start:record_end]
            record_block = record_block.replace(
                record_heading, f'<a id="r{number}"></a>\n\n## {new_pos}{heading_suffix}\n\n' + correction, 1)
            record_block = record_block.replace('### Forms\n', '### Source forms (uncorrected)\n', 1)
            body = body[:record_start] + record_block + body[record_end:]
        counts['ledger_decisions_applied'] += 1
        applied.append({'id': row['id'], 'coordinate': f'R{number}.S{sense_number}', 'status': status})
    counts['definition_filled_headwords'] = int(counts['definition_filled_senses'] > 0)
    counts['auxiliary_relation_headwords'] = int(counts['auxiliary_relation_senses'] > 0)
    counts['source_kind_corrected_records'] = len(corrected_sources)
    if corrected_sources:
        attributed = [dict(record, **({'original_title': corrected_sources[number]}
                                      if number in corrected_sources else {}))
                      for number, record in enumerate(records, 1)]
        pages = _source_pages(headword, attributed)
        source_start = body.index('## Sources\n\n') + len('## Sources\n\n')
        license_start = body.index('- License:', source_start)
        links = []
        for page in pages:
            label = 'Source records ' + ', '.join(map(str, page['source_records'])) + ': '
            links.extend([f'- {label}[English Wiktionary source]({page["source_url"]})',
                          f'- {label}[Contributors and revision history]({page["history_url"]})'])
        body = body[:source_start] + '\n'.join(links) + '\n' + body[license_start:]
        metadata.update(source_url=pages[0]['source_url'], history_url=pages[0]['history_url'], source_pages=pages)
    metadata['definition_overlay_revision'] = revision
    metadata['definition_ledger_sha256'] = source['definition_ledger_sha256']
    metadata['definition_overlay_counts'] = dict(counts)
    metadata['definition_decisions'] = applied
    metadata['review_status'] = 'evidence-pinned-definition-review; full-linguistic-review-pending'
    note = (f'Pinned source gaps: {counts["source_gap_senses"]}; supported supplemental definitions: '
            f'{counts["definition_filled_senses"]}; auxiliary source classifications: '
            f'{counts["auxiliary_relation_senses"]}; unresolved lexical gloss gaps: '
            f'{counts["unresolved_gloss_senses"]}. Original source-gap counts and fingerprints '
            'remain unchanged. See [the immutable decision ledger](../../DEFINITIONS.md).\n\n')
    if revision == REVISION:
        note += (f'Source-structure defects resolved without adding definitions: '
                 f'{counts["source_structure_senses"]}.\n\n')
    body = body.replace('## Source gaps\n\n', '## Source gaps\n\n' + note, 1)
    return document(metadata, body)
