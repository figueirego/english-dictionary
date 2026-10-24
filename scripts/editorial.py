"""Evidence-bounded reading improvements; source projections remain unchanged."""

from __future__ import annotations

from collections import Counter
import re

from scripts.entries import _objects, _strings, _text, RELATION_NAMES

REVISION = 'corrections-v1'
CONTROL_TAGS = frozenset({'table-tags', 'inflection-template'})
DIAGNOSTIC_TAGS = frozenset({'no-gloss', 'empty-gloss', 'error-unknown-tag',
                            'error-unrecognized-form', 'error-missing-gloss'})
COLOR_HASHES = frozenset({
    '5b780430763f8e3a6120a10393b89b261556420b2fd088fd720c573b645490fa',
    '0eedcb78cfdde0d3a0e4cc8c3c1e96bfa83ae64656417ba2264c6e0788363ed8',
    'f9c4170aacd44d13d13c7fda2dbbeee659537bebddde91d3a81d17413eeca5fa',
})
LEAD_TABLE_HASH = 'f9c70919a5080957c210835641707d00bdb00bad04448014363887190ae7fdfa'
SPACE_HASH = 'c42fd8028e8ce780029c10abf1bf7639ade47a3ea934549d7aa86b8cabbed5a8'
SAFE_TARGET = re.compile(r'\.\./[0-9a-f]{3}/[a-z0-9-]+--[0-9a-f]{12}\.md')


def diagnostic_tag(label: str) -> bool:
    return label in DIAGNOSTIC_TAGS or label.startswith('error-')


def partial_transcription(text: str) -> bool:
    """Recognize explicit boundary dashes without expanding any sound sequence."""
    if len(text) < 3 or (text[0], text[-1]) not in {('/', '/'), ('[', ']')}:
        return False
    inner = text[1:-1].strip()
    return bool(inner) and (inner.startswith('-') or inner.endswith('-'))


class EditorialContext:
    def __init__(self, headword: str, records: list[dict[str, object]],
                 relation_targets: dict[str, str] | None = None):
        self.headword = headword
        self.records = records
        self.targets = relation_targets or {}
        if any(not isinstance(word, str) or not isinstance(target, str)
               or not SAFE_TARGET.fullmatch(target) for word, target in self.targets.items()):
            raise ValueError('Editorial relation targets must be safe local entry paths')
        self.changes: Counter[str] = Counter()
        self.patch_changes: list[dict[str, object]] = []

    def labels(self, value: dict[str, object]) -> str:
        labels = [label for label in _strings(value.get('tags')) if not diagnostic_tag(label)]
        qualifier = value.get('qualifier')
        if isinstance(qualifier, str) and qualifier.strip():
            labels.append(qualifier)
        return '; '.join(_text(label) for label in labels)

    def context(self, value: dict[str, object]) -> list[str]:
        result = []
        topics = _strings(value.get('topics'))
        visible_topics = [topic for topic in topics if topic != 'heading']
        if visible_topics:
            result.append('**Source topics (extractor hierarchy; not curated):** '
                          + '; '.join(_text(topic) for topic in visible_topics))
            self.changes['source_topic_labels_restored'] += len(visible_topics)
        raw_tags = _strings(value.get('raw_tags'))
        if raw_tags:
            result.append('**Source context (verbatim):** '
                          + '; '.join(_text(tag) for tag in raw_tags))
            self.changes['raw_context_labels_restored'] += len(raw_tags)
        diagnostics = [label for label in _strings(value.get('tags')) if diagnostic_tag(label)]
        if 'heading' in topics:
            diagnostics.append('topic marker: heading')
        if diagnostics:
            result.append('**Extractor diagnostics (not usage labels):** '
                          + '; '.join(_text(tag) for tag in diagnostics))
            self.changes['diagnostic_labels_separated'] += len(diagnostics)
        result.extend('**Editorial note:** ' + _text(note)
                      for note in _strings(value.get('_editorial_notes')))
        return result

    def pronunciations(self, record: dict[str, object]) -> list[str]:
        result = []
        for sound in _objects(record.get('sounds')):
            for field, system in (('ipa', 'IPA'), ('enpr', 'enPR')):
                text = sound.get(field)
                if not isinstance(text, str) or not text.strip():
                    continue
                item = f'{system}: {_text(text)}'
                labels = self.labels(sound)
                if labels:
                    item += ' — source labels: ' + labels
                elif _strings(sound.get('raw_tags')):
                    item += ' — no normalized variety label; see source context'
                else:
                    item += ' — variety unspecified in the source labels'
                    self.changes['unspecified_pronunciations_labelled'] += 1
                if field == 'ipa' and partial_transcription(text):
                    item += ' — **partial source transcription; not a complete pronunciation**'
                    self.changes['partial_pronunciations_labelled'] += 1
                details = self.context(sound)
                if details:
                    item += '; ' + '; '.join(details)
                result.append(item)
        return result

    def forms(self, record: dict[str, object], number: int) -> tuple[list[str], list[str]]:
        result = []
        notes = []
        source_hash = self.records[number - 1].get('upstream_record_sha256')
        for form in _objects(record.get('forms')):
            text = form.get('form')
            if not isinstance(text, str) or not text.strip():
                continue
            tags = set(_strings(form.get('tags')))
            reason = None
            if tags & CONTROL_TAGS:
                reason = 'an extraction table control'
            elif (self.headword == 'color' and source_hash in COLOR_HASHES
                  and text == 'usage notes' and tags == {'alternative'}):
                reason = 'a source section label, not a spelling variant'
            elif (self.headword == 'lead' and source_hash == LEAD_TABLE_HASH
                  and text == '-' and tags == {'imperative', 'past'}):
                reason = 'a source table placeholder for an unavailable form'
            if reason:
                notes.append(f'Source form {_text(text)} is {reason}; retained in the source projection.')
                self.changes['nonlexical_forms_suppressed'] += 1
                continue
            if (self.headword == ' ' and source_hash == SPACE_HASH
                    and text == '] [' and tags == {'canonical'}):
                notes.append('Source display representation `] [` is retained as notation for '
                             'the space character; it is not a spoken inflection.')
                self.changes['source_display_forms_explained'] += 1
                continue
            labels = self.labels(form)
            item = _text(text) + (' — ' + labels if labels else '')
            details = self.context(form)
            if details:
                item += '; ' + '; '.join(details)
            result.append(item)
        return result, notes

    def relations(self, value: dict[str, object]) -> list[str]:
        result = []
        for key, title in RELATION_NAMES.items():
            terms = []
            for relation in _objects(value.get(key)):
                word = relation.get('word')
                if not isinstance(word, str) or not word.strip():
                    continue
                term = _text(word)
                if word in self.targets:
                    term = f'[{term}]({self.targets[word]})'
                    self.changes['local_relation_links'] += 1
                labels = self.labels(relation)
                if labels:
                    term += f' ({labels})'
                sense = relation.get('sense')
                if isinstance(sense, str) and sense.strip():
                    term += f' — {_text(sense)}'
                details = self.context(relation)
                if details:
                    term += '; ' + '; '.join(details)
                terms.append(term)
            if terms:
                label = title if key in ('form_of', 'alt_of') else 'Source ' + title.lower()
                result.append(f'**{label}:** ' + '; '.join(terms))
                if key in ('synonyms', 'antonyms'):
                    self.changes['source_relation_groups_labelled'] += 1
        if result and value.get('_source_relations_only'):
            result = ['<details>', '<summary>Uncurated source relations — register and '
                      'interchangeability are not verified</summary>', '', *result, '', '</details>']
        return result
