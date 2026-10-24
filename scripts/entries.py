"""Render source-attributed English dictionary entries as safe Markdown."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from scripts.markdown import document

if TYPE_CHECKING:
    from scripts.editorial import EditorialContext


POS_NAMES = {
    'adj': 'Adjective', 'adv': 'Adverb', 'intj': 'Interjection', 'num': 'Numeral',
    'name': 'Proper noun', 'prep': 'Preposition', 'pron': 'Pronoun',
    'conj': 'Conjunction', 'det': 'Determiner', 'article': 'Article',
    'noun': 'Noun', 'verb': 'Verb', 'prefix': 'Prefix', 'suffix': 'Suffix',
    'character': 'Character', 'symbol': 'Symbol', 'phrase': 'Phrase',
    'proverb': 'Proverb', 'abbrev': 'Abbreviation', 'particle': 'Particle',
}
RELATION_NAMES = {
    'synonyms': 'Synonyms', 'antonyms': 'Antonyms',
    'form_of': 'Form of', 'alt_of': 'Alternative form of',
}


def _headword_digest(headword: str) -> str:
    if not isinstance(headword, str) or not headword.strip():
        raise ValueError('Headword must be a non-empty string')
    if len(headword) > 1024 or any(ord(char) < 32 for char in headword):
        raise ValueError('Headword is too long or contains control characters')
    return hashlib.sha256(headword.encode('utf-8')).hexdigest()


def entry_path(headword: str) -> Path:
    """Use exact spelling for identity, and a bounded ASCII label for filenames."""
    digest = _headword_digest(headword)
    folded = unicodedata.normalize('NFKD', headword).encode('ascii', 'ignore').decode()
    slug = re.sub(r'[^a-z0-9]+', '-', folded.lower()).strip('-')[:64].rstrip('-')
    slug = slug or 'word'
    first = folded[:1].lower()
    initial = first if first.isascii() and first.isalpha() else '_'
    return Path('entries') / initial / f'{slug}--{digest[:12]}.md'


def _text(value: str) -> str:
    """Keep source strings inert even in renderers that autolink bare URLs."""
    escaped = html.escape(' '.join(value.split()), quote=False)
    escaped = re.sub(r'([\\`*_{}\[\]#|])', r'\\\1', escaped)
    escaped = re.sub(r'^([+>-])(?=\s)', r'\\\1', escaped)
    escaped = re.sub(r'^(\d+)([.)])(?=\s)', r'\1\\\2', escaped)
    return escaped.replace(':', '&#58;')


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _labels(value: dict[str, object]) -> str:
    labels = _strings(value.get('tags'))
    qualifier = value.get('qualifier')
    if isinstance(qualifier, str) and qualifier.strip():
        labels = [*labels, qualifier]
    return '; '.join(_text(label) for label in labels)


def _relations(value: dict[str, object]) -> list[str]:
    result = []
    for key, title in RELATION_NAMES.items():
        terms = []
        for relation in _objects(value.get(key)):
            word = relation.get('word')
            if not isinstance(word, str) or not word.strip():
                continue
            term = _text(word)
            labels = _labels(relation)
            if labels:
                term += f' ({labels})'
            sense = relation.get('sense')
            if isinstance(sense, str) and sense.strip():
                term += f' — {_text(sense)}'
            terms.append(term)
        if terms:
            result.append(f'**{title}:** ' + '; '.join(terms))
    return result


def _pronunciations(record: dict[str, object]) -> list[str]:
    pronunciations = []
    for sound in _objects(record.get('sounds')):
        for field, system in (('ipa', 'IPA'), ('enpr', 'enPR')):
            pronunciation = sound.get(field)
            if not isinstance(pronunciation, str) or not pronunciation.strip():
                continue
            item = f'{system}: {_text(pronunciation)}'
            labels = _labels(sound)
            if labels:
                item += f' — {labels}'
            if item not in pronunciations:
                pronunciations.append(item)
    return pronunciations


def _validate_records(headword: str, records: list[dict[str, object]]) -> None:
    if not isinstance(records, list) or not records:
        raise ValueError('At least one source record is required')
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f'Record {index}: expected an object')
        if record.get('lang_code') != 'en':
            raise ValueError(f'Record {index}: expected English language code')
        if record.get('word') != headword:
            raise ValueError(f'Record {index}: headword mismatch')
        pos = record.get('pos')
        if not isinstance(pos, str) or not pos.strip():
            raise ValueError(f'Record {index}: part of speech is required')
        senses = record.get('senses')
        if not isinstance(senses, list) or not senses:
            raise ValueError(f'Record {index}: senses must be a non-empty list')
        for sense in senses:
            if not isinstance(sense, dict):
                raise ValueError(f'Record {index}: each sense must be an object')
            glosses = sense.get('glosses')
            if (not isinstance(glosses, list) or not glosses
                    or any(not isinstance(gloss, str) or not gloss.strip() for gloss in glosses)):
                raise ValueError(f'Record {index}: glosses must contain non-empty strings')


def _render_senses(record: dict[str, object], record_number: int = 1,
                   editorial: EditorialContext | None = None) -> list[str]:
    lines = ['### Senses', '']
    for number, sense in enumerate(_objects(record['senses']), 1):
        glosses = _strings(sense['glosses'])
        if editorial is not None:
            lines.extend([f'<a id="r{record_number}-s{number}"></a>', '',
                          f'**Source sense R{record_number}.S{number}**', ''])
        for depth, gloss in enumerate(glosses):
            marker = str(number) if depth == 0 else '1'
            lines.append('   ' * depth + f'{marker}. {_text(gloss)}')
        # Details belong to the leaf meaning, not to a shared parent gloss.
        indentation = '   ' * len(glosses)
        details = []
        labels = editorial.labels(sense) if editorial is not None else _labels(sense)
        if labels:
            details.append(f'**Usage:** {labels}')
        for example in _objects(sense.get('examples')):
            text = example.get('text')
            if (example.get('type') == 'example' and 'ref' not in example
                    and isinstance(text, str) and text.strip()):
                details.append(f'**Example:** {_text(text)}')
        if editorial is not None:
            details.extend(editorial.context(sense))
            details.extend(editorial.relations(sense))
        else:
            details.extend(_relations(sense))
        for detail in details:
            lines.extend(['', indentation + detail])
        lines.append('')
    return lines


def _source_urls(headword: str) -> tuple[str, str]:
    title = quote(headword, safe='')
    return (f'https://en.wiktionary.org/wiki/{title}#English',
            f'https://en.wiktionary.org/w/index.php?title={title}&action=history')


def render_entry(headword: str, records: list[dict[str, object]],
                 source: dict[str, object], *,
                 editorial: EditorialContext | None = None) -> str:
    """Render projected records without inferring absent lexical information.

    The hashes identify the supplied projections, including fields intentionally
    omitted from the reading view. Corpus projection is the caller's responsibility.
    """
    digest = _headword_digest(headword)
    _validate_records(headword, records)
    for key in ('snapshot_id', 'raw_sha256'):
        if not isinstance(source.get(key), str) or not source[key]:
            raise ValueError(f'Source {key} is required')
    source_url, history_url = _source_urls(headword)
    fingerprints = [hashlib.sha256(json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
    ).encode('utf-8')).hexdigest() for row in records]
    metadata: dict[str, object] = {
        'kind': 'dictionary-entry',
        'schema_version': 1,
        'entry_id': f'en-{digest[:20]}',
        'headword': headword,
        'language': 'en',
        'source_records': len(records),
        'sense_count': sum(len(_objects(record['senses'])) for record in records),
        'source_id': 'enwiktionary',
        'snapshot_id': source['snapshot_id'],
        'source_url': source_url,
        'history_url': history_url,
        'license': 'CC-BY-SA-4.0',
        'raw_sha256': source['raw_sha256'],
        'source_record_sha256': fingerprints,
        'review_status': 'imported-unreviewed',
    }
    lines = [f'# {_text(headword)}', '', '*English*', '']
    if editorial is not None:
        lines.extend(['This reading view applies documented source-preserving corrections. '
                      'Source senses and context are not a completed linguistic review. '
                      'Repeated parent glosses remain source paths, not additional meanings. '
                      'Related-word links open exact local entries; they do not select a target '
                      'sense or certify interchangeable usage.', ''])
    missing_ipa = 0
    missing_etymology = 0
    missing_examples = 0
    for index, record in enumerate(records, 1):
        pos = str(record['pos'])
        heading = POS_NAMES.get(pos, pos.capitalize())
        if editorial is not None and pos == 'punct':
            heading = 'Punctuation'
            editorial.changes['punctuation_headings_expanded'] += 1
        etymology_number = record.get('etymology_number')
        if isinstance(etymology_number, str) and etymology_number.strip():
            heading += f' · Etymology {etymology_number}'
        # Separate source records remain separate, even when labels coincide.
        if len(records) > 1:
            heading += f' · Record {index}'
        if editorial is not None:
            lines.extend([f'<a id="r{index}"></a>', ''])
        lines.extend([f'## {_text(heading)}', ''])
        if editorial is not None:
            for context in editorial.context(record):
                lines.extend([context, ''])
        pronunciations = editorial.pronunciations(record) if editorial is not None else _pronunciations(record)
        if pronunciations:
            lines.extend(['### Pronunciation', ''])
            lines.extend(f'- {item}' for item in pronunciations)
            lines.append('')
        if not any(isinstance(sound.get('ipa'), str) and sound['ipa'].strip()
                   for sound in _objects(record.get('sounds'))):
            missing_ipa += 1

        forms = []
        form_notes = []
        if editorial is not None:
            forms, form_notes = editorial.forms(record, index)
        else:
            for form in _objects(record.get('forms')):
                text = form.get('form')
                if not isinstance(text, str) or not text.strip():
                    continue
                labels = _labels(form)
                forms.append(_text(text) + (f' — {labels}' if labels else ''))
        if forms:
            lines.extend(['### Forms', ''])
            lines.extend('- ' + form for form in forms)
            lines.append('')

        for note in form_notes:
            lines.extend(['**Editorial note:** ' + note, ''])
        lines.extend(_render_senses(record, index, editorial))
        record_relations = editorial.relations(record) if editorial is not None else _relations(record)
        if record_relations:
            lines.extend(['### Related words', ''])
            for relation in record_relations:
                lines.extend([relation, ''])
        etymology = record.get('etymology_text')
        if isinstance(etymology, str) and etymology.strip():
            lines.extend(['### Etymology', ''])
            if editorial is not None:
                lines.extend(['Source etymology is reproduced as an attributed claim. '
                              'Historical assertions, tree notation and any embedded quotations '
                              'have not been independently verified.', ''])
            collapsible = len(etymology.splitlines()) > 12
            if collapsible:
                lines.extend(['<details>', '<summary>Source etymology</summary>', ''])
            for paragraph in re.split(r'\n\s*\n', etymology):
                if paragraph.strip():
                    lines.extend(['  \n'.join(_text(line) for line in paragraph.splitlines()), ''])
            if collapsible:
                lines.extend(['</details>', ''])
        else:
            original_etymology = (editorial.records[index - 1].get('etymology_text')
                                  if editorial is not None else None)
            if not isinstance(original_etymology, str) or not original_etymology.strip():
                missing_etymology += 1
        for sense in _objects(record['senses']):
            if not any(example.get('type') == 'example' and 'ref' not in example
                       and isinstance(example.get('text'), str) and example['text'].strip()
                       for example in _objects(sense.get('examples'))):
                missing_examples += 1

    gaps = []
    if missing_ipa:
        gaps.append(f'IPA pronunciation absent in {missing_ipa} of {len(records)} source records')
    if missing_etymology:
        gaps.append(f'etymology absent in {missing_etymology} of {len(records)} source records')
    if missing_examples:
        gaps.append(f'eligible usage examples absent in {missing_examples} of {metadata["sense_count"]} senses')
    if gaps:
        lines.extend(['## Source gaps', '', '; '.join(gaps) + '.', '',
                      'Missing fields have not been generated or inferred.', ''])
    lines.extend([
        '## Sources', '',
        f'- [English Wiktionary entry]({source_url})',
        f'- [Wiktionary contributors and revision history]({history_url})',
        '- License: [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).',
        f'- Snapshot: {_text(str(source["snapshot_id"]))}.',
        ('- Adaptation: structured extraction and Markdown formatting; translations and media omitted. '
         'The example filter excludes referenced sense examples and quotation-typed examples; '
         'it does not establish authorship of unreferenced examples or remove every quotation '
         'embedded in free-form etymology.' if editorial is not None else
         '- Adaptation: structured extraction and Markdown formatting; translations, '
         'external quotations and media omitted.'),
        ('- Review status: automated source-preserving corrections applied; '
         'complete semantic and human review pending.' if editorial is not None else
         '- Review status: imported automatically; editorial review pending.'),
    ])
    return document(metadata, '\n'.join(lines))
