"""Publication derivatives preserve meaning and exclude withheld private material."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import full_pipeline, public_export as public
from scripts.full_projection import render
from scripts.markdown import document, parse_document
from test_full_pipeline import capture, lexical


def source_metadata():
    return {'snapshot_id': 'fixture', 'raw_sha256': '0' * 64,
            'editorial_revision': 'corrections-v1'}


def test_etymology_is_removed_from_view_and_projection_without_changing_senses():
    rows = [lexical('example', etymology_text='PRIVATE ETYMOLOGY QUOTATION',
                    etymology_number=1, senses=[{'glosses': ['An unchanged meaning.'],
                                               'examples': [{'type': 'example', 'text': 'An ordinary example.'}]}]),
            lexical('example', pos='verb', etymology_text='SECOND PRIVATE ETYMOLOGY',
                    senses=[{'glosses': ['To illustrate.']}])]
    original_rows = copy.deepcopy(rows)
    original = render('example', rows, source_metadata()).encode()
    result, counts = public.sanitize_entry(original, rows, 'full-parent', None)
    metadata, body = parse_document(result)
    clean, _, _ = public.sanitized_records(rows)
    assert rows == original_rows
    assert counts['etymology_records_withheld'] == counts['etymology_sections_withheld'] == 2
    assert 'PRIVATE ETYMOLOGY' not in result
    assert 'PRIVATE ETYMOLOGY' not in json.dumps(clean)
    assert 'An unchanged meaning.' in body and 'To illustrate.' in body
    assert 'An ordinary example.' in body
    assert clean[0]['etymology_number'] == 1
    assert clean[0]['senses'] == rows[0]['senses']
    assert metadata['headword'] == 'example' and metadata['sense_count'] == 2
    assert metadata['parent_entry_sha256'] == hashlib.sha256(original).hexdigest()
    assert '## Sources' in body


def test_supplemental_definitions_citations_and_licences_survive_verbatim():
    rows = [lexical('example', etymology_text='PRIVATE ETYMOLOGY')]
    metadata, body = parse_document(render('example', rows, source_metadata()))
    supplement = ('**Supplemental definition:** A precisely supported meaning.\n\n'
                  '**Definition sources:** [Primary source](https://example.org/source) — CC BY 4.0\n\n')
    body = body.replace('### Etymology', supplement + '### Etymology')
    metadata.update(definition_ledger_sha256='1' * 64,
                    definition_overlay_counts={'definition_filled_senses': 1})
    result, _ = public.sanitize_entry(document(metadata, body).encode(), rows, 'full-parent', '2' * 64)
    public_meta, public_body = parse_document(result)
    assert supplement in public_body
    assert public_meta['definition_overlay_counts'] == metadata['definition_overlay_counts']
    assert public_meta['definition_ledger_sha256'] == '2' * 64
    assert public_meta['parent_definition_ledger_sha256'] == '1' * 64


def test_only_two_hash_guarded_examples_are_omitted_from_both_views(monkeypatch):
    quotations = {4: 'PRIVATE FIRST QUOTATION', 5: 'PRIVATE SECOND QUOTATION'}
    monkeypatch.setattr(public, 'WATER_EXAMPLES', {
        number: hashlib.sha256(text.encode()).hexdigest() for number, text in quotations.items()})
    senses = [{'glosses': [f'Unchanged meaning {number}.']} for number in range(1, 7)]
    for number, text in quotations.items():
        senses[number - 1]['examples'] = [{'type': 'example', 'text': text},
                                         {'type': 'example', 'text': 'Keep this eligible example.'}]
    rows = [lexical('water', senses=senses, upstream_record_sha256=public.WATER_RAW)]
    original = render('water', rows, source_metadata()).encode()
    result, counts = public.sanitize_entry(original, rows, 'full-parent', None)
    clean, _, _ = public.sanitized_records(rows)
    assert counts['examples_withheld'] == 2
    for number, text in quotations.items():
        assert text not in result and text not in json.dumps(clean)
        assert f'Unchanged meaning {number}.' in result
        assert clean[0]['senses'][number - 1]['examples'] == [
            {'type': 'example', 'text': 'Keep this eligible example.'}]
    changed_guard = copy.deepcopy(rows)
    changed_guard[0]['upstream_record_sha256'] = 'f' * 64
    assert public.sanitized_records(changed_guard)[1]['examples_withheld'] == 0
    changed_source = copy.deepcopy(rows)
    changed_source[0]['senses'][3]['examples'][0]['text'] = 'Unexpected replacement.'
    with pytest.raises(ValueError, match='no longer matches'):
        public.sanitized_records(changed_source)


def test_public_ledger_uses_whitelist_and_keeps_legitimate_users_url(tmp_path):
    row = {'id': '1:1', 'word': 'sample', 'definition': 'A retained definition.',
           'status': 'definition', 'reason': 'Evidence supports this specific use.',
           'raw_record_line': '{"etymology_text":"PRIVATE QUOTATION"}',
           'research': {'headers': {'set-cookie': 'PRIVATE COOKIE'}},
           'sources': [{'title': 'Attributed source', 'url': 'https://example.org/Users/author',
                        'license': 'CC BY 4.0', 'revision': '123', 'capture_sha256': '1' * 64,
                        'capture_path': '.cache/private.json', 'support': 'PRIVATE QUOTED OCR'}]}
    original = document({'decision_count': 1}, '# Ledger\n\n```jsonl\n' + json.dumps(row) + '\n```')
    path = tmp_path / 'DEFINITIONS.md'
    path.write_text(original)
    result = public.public_ledger(path)
    assert 'PRIVATE' not in result and 'capture_path' not in result
    assert 'https://example.org/Users/author' in result
    assert 'A retained definition.' in result and 'CC BY 4.0' in result
    metadata, _ = parse_document(result)
    assert metadata['parent_ledger_sha256'] == hashlib.sha256(original.encode()).hexdigest()


def test_full_derivative_reconciles_audits_and_preserves_parent(tmp_path):
    root = tmp_path / 'private'
    rows = [lexical('example', etymology_text='PRIVATE QUOTATION'),
            lexical('Polish'), lexical('polish'),
            lexical(' ', pos='punct', original_title='Unsupported titles/Space')]
    archive, evidence, _ = capture(root, rows)
    full_pipeline.ingest(root, archive, evidence)
    full_pipeline.build(root, workers=1)
    parent = full_pipeline.active(root)
    before = {path.relative_to(parent): path.read_bytes() for path in parent.rglob('*.md')}
    destination = tmp_path / 'public'
    destination.mkdir()
    (destination / 'README.md').write_text('# Public project\n')
    result = public.export(root, destination, workers=1)
    assert result['headwords'] == 4 and result['senses'] == 4
    assert result['etymology_records_withheld'] == 1
    assert result == public.export(root, destination, workers=1, audit_only=True)
    assert before == {path.relative_to(parent): path.read_bytes() for path in parent.rglob('*.md')}
    for path in (destination / 'dictionary').rglob('*.md'):
        assert 'PRIVATE QUOTATION' not in path.read_text()
    assert (destination / 'README.md').read_text() == '# Public project\n'
    entry = destination / 'dictionary' / full_pipeline.full_entry_path('example')
    entry.write_text(entry.read_text().replace('An illustrative instance.', 'Changed definition.'))
    with pytest.raises(ValueError, match='artifact mismatch'):
        public.export(root, destination, workers=1, audit_only=True)
    assert 'Changed definition.' in entry.read_text()
    assert not (destination / '.public-export.lock').exists()


def test_failed_export_never_promotes_candidate(tmp_path, monkeypatch):
    root = tmp_path / 'private'
    archive, evidence, _ = capture(root, [lexical()])
    full_pipeline.ingest(root, archive, evidence)
    full_pipeline.build(root, workers=1)
    monkeypatch.setattr(public, 'MAX_PUBLIC_FILE', 10)
    destination = tmp_path / 'public'
    with pytest.raises(ValueError, match='file-size limit'):
        public.export(root, destination, workers=1)
    assert not (destination / 'dictionary').exists()
    assert not (destination / '.public-export.lock').exists()


def test_public_destination_cannot_be_an_accepted_edition(tmp_path):
    root = tmp_path / 'private'
    archive, evidence, _ = capture(root, [lexical()])
    full_pipeline.ingest(root, archive, evidence)
    full_pipeline.build(root, workers=1)
    with pytest.raises(ValueError, match='isolated'):
        public.export(root, full_pipeline.active(root), workers=1)
