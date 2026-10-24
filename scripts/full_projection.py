"""Project complete-dump records without turning absent glosses into definitions.

The pilot's immutable renderer remains unchanged. Valid pilot-shaped records use
the same whitelist and reading layout; legitimate source omissions are retained
in the projection and explicitly labelled in the reading view.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import quote

from scripts.entries import _text, entry_path as pilot_entry_path, render_entry
from scripts.markdown import document, parse_document
from scripts.prepare import _objects, _project, _strings


def _identity_digest(headword: str) -> str:
    if not isinstance(headword, str) or not headword or len(headword) > 1024:
        raise ValueError('Headword must contain 1 through 1024 characters')
    return hashlib.sha256(headword.encode('utf-8')).hexdigest()


def _special_spelling(headword: str) -> bool:
    return not headword.strip() or any(ord(char) < 32 or ord(char) == 127 for char in headword)


def display_headword(headword: str) -> str:
    """Return a visible label without changing exact source spelling or identity."""
    _identity_digest(headword)
    if not _special_spelling(headword):
        return headword
    names = {' ': 'Space', '\t': 'Tab', '\n': 'Line feed', '\r': 'Carriage return'}

    def label(char: str) -> str:
        name = names.get(char, unicodedata.name(char, 'Control character').capitalize())
        return f'{name} [U+{ord(char):04X}]'

    if len(headword) == 1:
        return label(headword)
    if not headword.strip():
        return ' + '.join(label(char) for char in headword)
    return ''.join(f'[{label(char)}]' if ord(char) < 32 or ord(char) == 127 else char
                   for char in headword)


def entry_path(headword: str) -> Path:
    """Use bounded safe filenames, including for explicitly sourced characters.

    This pure identity helper cannot validate page provenance; project() requires
    a valid original_title before accepting whitespace or control-character words.
    Ordinary headwords retain the pilot filename and exact-spelling digest.
    """
    fingerprint = _identity_digest(headword)
    if not _special_spelling(headword):
        return pilot_entry_path(headword)
    slug = re.sub(r'[^a-z0-9]+', '-', display_headword(headword).lower()).strip('-')[:64]
    return Path('entries') / '_' / f'{slug}--{fingerprint[:12]}.md'


def _original_title(record: dict[str, object]) -> str | None:
    if 'original_title' not in record:
        return None
    title = record['original_title']
    if (not isinstance(title, str) or not title.strip() or len(title) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise ValueError('original_title must be a non-empty source page title without controls')
    return title


def _gloss_status(sense: dict[str, object]) -> str | None:
    if 'glosses' not in sense:
        return 'absent'
    glosses = _strings(sense['glosses'], 'glosses')
    if not glosses:
        return 'empty'
    if any(not value.strip() for value in glosses):
        return 'partially-blank' if any(value.strip() for value in glosses) else 'blank'
    return None


def _with_context(source: dict[str, object], projected: dict[str, object]) -> dict[str, object]:
    """Keep extractor context verbatim; interpretation belongs to the reading view."""
    for name in ('topics', 'raw_tags', 'tags'):
        if name in source:
            projected[name] = list(_strings(source[name], name))
    for name in ('forms', 'sounds', 'senses', 'synonyms', 'antonyms', 'form_of', 'alt_of'):
        if name not in source:
            continue
        originals = _objects(source[name], name)
        # The projection excludes audio-only records, so align on the same predicate.
        if name == 'sounds':
            originals = [row for row in originals if 'ipa' in row or 'enpr' in row]
        destinations = projected.get(name, [])
        for original, destination in zip(originals, destinations):
            _with_context(original, destination)
    return projected


def project(record: dict[str, object]) -> dict[str, object]:
    """Whitelist English source content while retaining exact context and gaps.

    Context arrays are strict string lists, including at nested relation level.
    They remain extractor/source assertions, not editor-approved usage labels.
    Reprojecting an allowed projection preserves its lexical values and gap status.
    """
    if not isinstance(record, dict) or record.get('lang_code') != 'en':
        raise ValueError('A source record with lang_code en is required')
    word = record.get('word')
    fingerprint = _identity_digest(word)
    title = _original_title(record)
    special = _special_spelling(word)
    if special and title is None:
        raise ValueError('Whitespace or control-character headwords require original_title')
    source_record = dict(record)
    if special:
        source_record['word'] = 'Source character ' + fingerprint
    original_senses = (_objects(record['senses'], 'senses')
                       if 'senses' in record else [])
    statuses = [_gloss_status(sense) for sense in original_senses]
    source_record['senses'] = [
        {**sense, 'glosses': [value if value.strip() else 'Source gap'
                             for value in sense.get('glosses', [])] or ['Source gap']}
        for sense in original_senses
    ] or [{'glosses': ['Source gap']}]
    projected = _project(source_record)
    projected['word'] = word
    if title is not None:
        projected['original_title'] = title
    if not original_senses:
        projected['senses'] = []
        projected['missing_senses'] = True
        prior_status = record.get('senses_status') if record.get('missing_senses') is True else None
        projected['senses_status'] = (prior_status if prior_status in ('absent', 'empty') else
                                      'absent' if 'senses' not in record else 'empty')
    else:
        for source_sense, projected_sense, status in zip(
                original_senses, projected['senses'], statuses):
            if status:
                if 'glosses' in source_sense:
                    projected_sense['glosses'] = list(source_sense['glosses'])
                else:
                    projected_sense.pop('glosses', None)
                projected_sense['missing_gloss'] = True
                projected_sense['gloss_status'] = status
    return _with_context(record, projected)


def _fingerprint(record: dict[str, object]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _source_pages(headword: str, records: list[dict[str, object]]) -> list[dict[str, object]]:
    pages: dict[str, dict[str, object]] = {}
    for number, record in enumerate(records, 1):
        title = record.get('original_title', headword)
        if title not in pages:
            encoded = quote(title, safe='')
            pages[title] = {
                'title': title,
                'source_url': f'https://en.wiktionary.org/wiki/{encoded}#English',
                'history_url': f'https://en.wiktionary.org/w/index.php?title={encoded}&action=history',
                'source_records': [],
            }
        pages[title]['source_records'].append(number)
    return list(pages.values())


def render(headword: str, records: list[dict[str, object]],
           source: dict[str, object], *,
           relation_targets: dict[str, str] | None = None) -> str:
    """Render source gaps as notices, with hashes/counts of the real projections."""
    if not isinstance(records, list) or not records:
        raise ValueError('At least one source record is required')
    projected = []
    for record in records:
        if not isinstance(record, dict) or record.get('word') != headword:
            raise ValueError('Source record headword mismatch')
        projected.append(project(record))

    editorial = None
    if source.get('editorial_revision') == 'corrections-v1':
        from scripts.editorial import EditorialContext
        from scripts.editorial_patches import apply_record

        editorial = EditorialContext(headword, records, relation_targets)
        views = []
        for number, (original, row) in enumerate(zip(records, projected), 1):
            # Guarded adaptations receive raw provenance but never replace the
            # preserved projection or its fingerprint in the resulting document.
            provenance = {key: original[key] for key in
                          ('upstream_record_sha256', 'upstream_line', 'upstream_offset')
                          if key in original}
            view, changes = apply_record(headword, {**row, **provenance}, number)
            views.append(view)
            editorial.patch_changes.extend(changes)
            editorial.changes.update(change['code'] for change in changes)
        projected = views

    # A content-derived marker cannot collide with imported text. It only exists
    # in the temporary rendering input, never in the preserved projection.
    serialized = json.dumps(records, ensure_ascii=False, sort_keys=True, allow_nan=False)
    prefix = 'SourceGap' + hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    while prefix in serialized:
        prefix += 'x'
    replacements: dict[str, str] = {}
    normalized = []
    missing_glosses = 0
    missing_senses = 0
    sense_count = 0
    missing_examples = 0
    special = _special_spelling(headword)
    reading_headword = ('Source character ' + _identity_digest(headword)) if special else headword

    def notice(message: str) -> str:
        marker = prefix + 'N' + str(len(replacements)) + 'End'
        replacements[marker] = '*Source gap: ' + message + '*'
        return marker

    for row in projected:
        view = dict(row)
        view['word'] = reading_headword
        senses = row['senses']
        sense_count += len(senses)
        if not senses:
            missing_senses += 1
            view['senses'] = [{'glosses': [notice('no senses supplied in this source record.')]}]
        else:
            view['senses'] = []
            for sense in senses:
                copied = dict(sense)
                if _gloss_status(sense):
                    missing_glosses += 1
                    copied['glosses'] = [
                        value if value.strip() else notice('a gloss is blank in the source.')
                        for value in sense.get('glosses', [])
                    ] or [notice('this sense has no gloss in the source.')]
                if not sense.get('examples'):
                    missing_examples += 1
                view['senses'].append(copied)
        # Wiktextract emits integer grouping numbers. The pilot display handled
        # only strings; render the explicit number without changing its provenance.
        if type(view.get('etymology_number')) is int:
            view['etymology_number'] = str(view['etymology_number'])
        normalized.append(view)

    metadata, body = parse_document(render_entry(reading_headword, normalized, source,
                                                editorial=editorial))
    metadata['headword'] = headword
    metadata['entry_id'] = 'en-' + _identity_digest(headword)[:20]
    metadata['sense_count'] = sense_count
    metadata['source_record_sha256'] = [_fingerprint(record) for record in records]
    if editorial is not None:
        metadata['editorial_revision'] = 'corrections-v1'
        metadata['editorial_changes'] = dict(sorted(editorial.changes.items()))
        metadata['editorial_change_count'] = sum(editorial.changes.values())
        metadata['editorial_patches'] = editorial.patch_changes
        metadata['review_status'] = 'automated-source-preserving-corrections; full-linguistic-review-pending'
        metadata['relation_link_scope'] = 'exact-headword-entry; not sense-resolved'
    if special:
        display = display_headword(headword)
        body = body.replace('# ' + _text(reading_headword) + '\n', '# ' + _text(display) + '\n', 1)
        body = body.replace('*English*\n', '*English*\n\nThe heading is a visible label for the exact '
                            'source spelling preserved in the metadata.\n', 1)
        metadata['display_headword'] = display
        metadata['source_word_codepoints'] = [f'U+{ord(char):04X}' for char in headword]
    if special or any('original_title' in record for record in projected):
        pages = _source_pages(headword, projected)
        old_links = (f'- [English Wiktionary entry]({metadata["source_url"]})\n'
                     f'- [Wiktionary contributors and revision history]({metadata["history_url"]})')
        links = []
        for page in pages:
            label = (f'Source records {", ".join(map(str, page["source_records"]))}: '
                     if len(pages) > 1 else '')
            links.extend([f'- {label}[English Wiktionary entry]({page["source_url"]})',
                          f'- {label}[Wiktionary contributors and revision history]({page["history_url"]})'])
        body = body.replace(old_links, '\n'.join(links), 1)
        metadata['source_url'] = pages[0]['source_url']
        metadata['history_url'] = pages[0]['history_url']
        metadata['source_pages'] = pages
    for marker, text in replacements.items():
        body = body.replace(_text(marker), text)
    if missing_senses or missing_glosses:
        metadata['missing_sense_records'] = missing_senses
        metadata['missing_gloss_senses'] = missing_glosses
        # The pilot renderer counted temporary reading placeholders as senses.
        # Correct that disclosure to count only actual source sense objects.
        body = re.sub(r'eligible usage examples absent in \d+ of \d+ senses',
                      f'eligible usage examples absent in {missing_examples} of {sense_count} senses',
                      body)
        gaps = (f'{missing_senses} source records without senses; '
                f'{missing_glosses} source senses with absent, empty or blank glosses. '
                'Source-gap notices are editorial labels, not definitions.')
        if '## Source gaps\n\n' in body:
            body = body.replace('## Source gaps\n\n', '## Source gaps\n\n' + gaps + '\n\n', 1)
        else:
            body = body.replace('## Sources\n\n', '## Source gaps\n\n' + gaps
                                + '\n\n## Sources\n\n', 1)
    return document(metadata, body)
