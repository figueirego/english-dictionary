"""Complete snapshot ingestion, immutable resumption and offline Markdown audit."""

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from scripts import full_pipeline as pipeline
from scripts.full_projection import render
from scripts.markdown import document, parse_document


def lexical(word='example', **changes):
    result = {'word': word, 'lang_code': 'en', 'pos': 'noun',
              'senses': [{'glosses': ['An illustrative instance.']}]}
    result.update(changes)
    return result


def capture(root, records=None, *, payload=None):
    if payload is None:
        payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                          for row in records).encode()
    archive = root / 'input.jsonl.gz'
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(gzip.compress(payload, mtime=0))
    metadata = {
        'dump_date': '2026-10-01', 'extraction_date': '2026-10-21',
        'wiktextract_commit': '1' * 40, 'wikitextprocessor_commit': '2' * 40,
        'compressed_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    evidence = root / 'EVIDENCE.md'
    evidence.write_text(document(metadata, '# Source evidence'), encoding='utf-8')
    return archive, evidence, payload


@pytest.fixture
def source_rows():
    return [
        lexical('example'),
        lexical('example', pos='verb', senses=[{'glosses': ['To illustrate.']}]),
        lexical('gap', senses=[{'tags': ['no-gloss']}]),
        lexical('empty', senses=[]),
        lexical('foreign', lang_code='fr'),
        {'pos': 'hard-redirect', 'title': 'alternate', 'redirect': 'example'},
    ]


def test_complete_import_reconciles_snapshot_and_reproduces_markdown(tmp_path, source_rows):
    archive, evidence, payload = capture(tmp_path, source_rows)
    inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert inventory['raw_records'] == 6
    assert inventory['english_records'] == inventory['accepted_records'] == 4
    assert inventory['other_language_records'] == inventory['redirect_records'] == 1
    assert inventory.get('rejected_records', 0) == 0
    assert inventory['headwords'] == 3
    assert inventory['senses'] == 3
    assert inventory['records_with_source_gaps'] == 2
    assert inventory['raw_sha256'] == hashlib.sha256(payload).hexdigest()
    assert inventory['raw_bytes'] == len(payload)

    result = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    assert result['headwords'] == 3
    assert result['accepted_records'] == 4
    assert result['senses'] == 3
    assert pipeline.audit_edition(edition, workers=1) == result
    progress, _ = pipeline.read_md(tmp_path / 'PROGRESS.md')
    assert progress['phase'] == 'Complete and verified'
    for source_file in (edition / 'sources').glob('*.md'):
        source, records = pipeline.partition_rows(source_file)
        grouped = {}
        for row in records:
            grouped.setdefault(row['word'], []).append(row)
        for word, group in grouped.items():
            assert (edition / pipeline.full_entry_path(word)).read_bytes() == render(word, group, source).encode()
    # The accepted edition no longer needs working-database contents for audit.
    for path in (tmp_path / '.cache/full-import').glob('source.sqlite3*'):
        path.unlink()
    assert pipeline.audit_edition(edition, workers=1) == result


def test_idempotent_ingest_and_build_preserve_identical_edition(tmp_path, source_rows):
    archive, evidence, _ = capture(tmp_path, source_rows)
    first_inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert pipeline.ingest(tmp_path, archive, evidence) == first_inventory
    first = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    before = {str(path.relative_to(edition)): path.read_bytes() for path in edition.rglob('*.md')}
    assert pipeline.build(tmp_path, workers=1) == first
    assert pipeline.active(tmp_path) == edition
    assert before == {str(path.relative_to(edition)): path.read_bytes() for path in edition.rglob('*.md')}


def test_partial_build_resumes_without_promoting_or_duplicating(tmp_path, source_rows):
    archive, evidence, _ = capture(tmp_path, source_rows)
    pipeline.ingest(tmp_path, archive, evidence)
    current = document({'kind': 'current-edition', 'edition': 'pilot-example'}, '# Last good edition')
    (tmp_path / 'CURRENT.md').write_text(current, encoding='utf-8')
    partial = pipeline.build(tmp_path, workers=1, max_partitions=1)
    assert partial['completed_partitions'] == 1
    assert (tmp_path / 'CURRENT.md').read_text() == current
    stage = next((tmp_path / '.cache/full-import/builds').iterdir())
    checkpoint_files = {path: path.read_bytes() for path in stage.rglob('*.md')}
    complete = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    assert complete['headwords'] == 3
    for path, original in checkpoint_files.items():
        assert (edition / path.relative_to(stage)).read_bytes() == original


def test_changed_checkpoint_is_preserved_and_blocks_promotion(tmp_path, source_rows):
    archive, evidence, _ = capture(tmp_path, source_rows)
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1, max_partitions=1)
    stage = next((tmp_path / '.cache/full-import/builds').iterdir())
    entry = next((stage / 'entries').rglob('*.md'))
    changed = entry.read_bytes() + b'\nManual correction.\n'
    entry.write_bytes(changed)
    with pytest.raises(ValueError, match='checksum'):
        pipeline.build(tmp_path, workers=1)
    assert entry.read_bytes() == changed
    assert not (tmp_path / 'CURRENT.md').exists()


def test_changed_published_entry_is_preserved_and_fails_rebuild(tmp_path):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    entry = edition / pipeline.full_entry_path('example')
    changed = entry.read_bytes().replace(b'An illustrative instance.', b'A manual correction.')
    entry.write_bytes(changed)
    pointer = (tmp_path / 'CURRENT.md').read_bytes()
    with pytest.raises(ValueError, match='checksum'):
        pipeline.build(tmp_path, workers=1)
    assert entry.read_bytes() == changed
    assert (tmp_path / 'CURRENT.md').read_bytes() == pointer


@pytest.mark.parametrize('extra', ['entries/000/unexpected.md', 'indexes/unexpected.md', 'UNEXPECTED.md'])
def test_extra_files_are_detected(tmp_path, extra):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    if extra.startswith('entries/'):
        extra = str(pipeline.full_entry_path('example').parent / 'unexpected.md')
    path = edition / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('# Unexpected', encoding='utf-8')
    with pytest.raises(ValueError, match='[Uu]nexpected'):
        pipeline.audit_edition(edition, workers=1)


@pytest.mark.parametrize('damage', ['truncated', 'crc'])
def test_corrupt_gzip_never_marks_inventory_complete(tmp_path, damage):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    data = archive.read_bytes()
    if damage == 'truncated':
        data = data[:-8]
    else:
        data = data[:-8] + bytes([data[-8] ^ 1]) + data[-7:]
    archive.write_bytes(data)
    meta, body = pipeline.read_md(evidence)
    meta['compressed_sha256'] = hashlib.sha256(data).hexdigest()
    evidence.write_text(document(meta, body), encoding='utf-8')
    with pytest.raises((EOFError, gzip.BadGzipFile)):
        pipeline.ingest(tmp_path, archive, evidence)
    with pipeline.database(tmp_path) as db:
        assert not pipeline.state_get(db, 'complete')
    assert not (tmp_path / '.cache/full-import/operation.lock').exists()


def test_checksum_mismatch_stops_before_ingestion(tmp_path):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    meta, body = pipeline.read_md(evidence)
    meta['compressed_sha256'] = '0' * 64
    evidence.write_text(document(meta, body), encoding='utf-8')
    with pytest.raises(ValueError, match='capture evidence'):
        pipeline.ingest(tmp_path, archive, evidence)
    assert not (tmp_path / '.cache/full-import/source.sqlite3').exists()


def test_malformed_rows_are_preserved_in_rejections_and_block_build(tmp_path):
    payload = (json.dumps(lexical()).encode() + b'\n{broken}\n' +
               json.dumps(lexical('bad', senses='wrong')).encode() + b'\n')
    archive, evidence, _ = capture(tmp_path, payload=payload)
    inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert inventory['raw_records'] == 3
    assert inventory['accepted_records'] == 1
    assert inventory['rejected_records'] == 2
    assert inventory['english_records'] == 2
    with pipeline.database(tmp_path) as db:
        rejected = db.execute('SELECT ordinal,reason,raw_line FROM rejected ORDER BY ordinal').fetchall()
    assert rejected[0][0] == 2
    assert rejected[0][2] == b'{broken}\n'
    assert rejected[1][0] == 3
    with pytest.raises(ValueError, match='Unresolved'):
        pipeline.build(tmp_path, workers=1)
    assert not (tmp_path / 'CURRENT.md').exists()


def test_unicode_line_separators_do_not_split_json_records(tmp_path):
    word = 'word\u2028part'
    archive, evidence, _ = capture(tmp_path, [lexical(word, senses=[{'glosses': ['A\u2028definition\u0085with\u2029separators.']}])])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    assert pipeline.audit_edition(edition, workers=1)['headwords'] == 1
    metadata, _ = pipeline.read_md(edition / pipeline.full_entry_path(word))
    assert metadata['headword'] == word


def test_changed_source_snapshot_cannot_reuse_work_cache(tmp_path):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    pipeline.ingest(tmp_path, archive, evidence)
    archive, evidence, _ = capture(tmp_path, [lexical('different')])
    with pytest.raises(ValueError, match='another snapshot'):
        pipeline.ingest(tmp_path, archive, evidence)


def test_duplicate_json_keys_are_rejected_explicitly(tmp_path):
    payload = b'{"word":"example","lang_code":"en","lang_code":"fr","pos":"noun","senses":[]}\n'
    archive, evidence, _ = capture(tmp_path, payload=payload)
    inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert inventory['rejected_records'] == 1
    assert inventory.get('accepted_records', 0) == 0
    with pipeline.database(tmp_path) as db:
        reason = db.execute('SELECT reason FROM rejected').fetchone()[0]
    assert 'Duplicate JSON key' in reason


def test_ingestion_replays_from_committed_checkpoint_after_failure(tmp_path, monkeypatch):
    rows = [lexical() for _ in range(5000)]
    rows.append(lexical('long', senses=[{'glosses': ['A' * 2000]}]))
    archive, evidence, payload = capture(tmp_path, rows)
    original_limit = pipeline.MAX_LINE
    monkeypatch.setattr(pipeline, 'MAX_LINE', 1024)
    with pytest.raises(ValueError, match='bounded line limit'):
        pipeline.ingest(tmp_path, archive, evidence)
    with pipeline.database(tmp_path) as db:
        assert pipeline.state_get(db, 'counts')['raw_records'] == 5000
        assert not pipeline.state_get(db, 'complete')
        assert db.execute('SELECT count(*) FROM records').fetchone()[0] == 5000
    monkeypatch.setattr(pipeline, 'MAX_LINE', original_limit)
    inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert inventory['raw_records'] == inventory['accepted_records'] == 5001
    assert inventory['headwords'] == 2
    assert inventory['raw_sha256'] == hashlib.sha256(payload).hexdigest()
    with pipeline.database(tmp_path) as db:
        assert db.execute('SELECT count(*) FROM records').fetchone()[0] == 5001
        last = json.loads(db.execute('SELECT payload FROM records WHERE ordinal=5001').fetchone()[0])
    assert last['upstream_line'] == 5001
    assert payload[last['upstream_offset']:].endswith(b'\n')
    assert hashlib.sha256(payload[last['upstream_offset']:]).hexdigest() == last['upstream_record_sha256']


def test_audit_reconstructs_source_even_if_entry_checksums_are_recomputed(tmp_path):
    archive, evidence, _ = capture(tmp_path, [lexical()])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    relative = pipeline.full_entry_path('example')
    entry = edition / relative
    entry.write_bytes(entry.read_bytes().replace(b'An illustrative instance.', b'An invented definition.'))
    bucket = pipeline.partition_for('example')
    partition_manifest = edition / 'manifests' / f'{bucket}.md'
    metadata, body = pipeline.read_md(partition_manifest)
    metadata['files'][str(relative)] = pipeline.file_hash(entry)
    partition_manifest.write_text(document(metadata, body), encoding='utf-8')
    manifest = edition / 'MANIFEST.md'
    metadata, body = pipeline.read_md(manifest)
    metadata['partitions'][bucket] = pipeline.file_hash(partition_manifest)
    manifest.write_text(document(metadata, body), encoding='utf-8')
    with pytest.raises(ValueError, match='does not reproduce'):
        pipeline.audit_edition(edition, workers=1)


def test_partition_size_limit_is_checked_before_materializing_rows(tmp_path, monkeypatch):
    archive, evidence, _ = capture(tmp_path, [lexical(senses=[{'glosses': ['A' * 2000]}])])
    source = pipeline.ingest(tmp_path, archive, evidence)
    monkeypatch.setattr(pipeline, 'MAX_PARTITION', 1024)
    stage = tmp_path / 'isolated-stage'
    with pytest.raises(ValueError, match='before allocation'):
        pipeline.build_partition(str(tmp_path), str(stage), pipeline.partition_for('example'), source)
    assert not stage.exists()


def test_special_source_titles_and_exact_space_identity_survive_full_import(tmp_path):
    records = [lexical(' ', pos='punct', original_title='Unsupported titles/Space'),
               lexical('C`num`', original_title='Unsupported titles/C`num`')]
    archive, evidence, _ = capture(tmp_path, records)
    inventory = pipeline.ingest(tmp_path, archive, evidence)
    assert inventory['accepted_records'] == 2
    assert inventory.get('rejected_records', 0) == 0
    result = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    assert result['headwords'] == 2
    assert pipeline.audit_edition(edition, workers=1)['headwords'] == 2
    for word in [' ', 'C`num`']:
        metadata, body = pipeline.read_md(edition / pipeline.full_entry_path(word))
        assert metadata['headword'] == word
        assert metadata['source_url'].startswith('https://en.wiktionary.org/wiki/Unsupported%20titles%2F')
        assert metadata['source_url'] in body
    bucket = pipeline.partition_for(' ')
    index_text = (edition / 'indexes' / f'{bucket}.md').read_text()
    assert 'Space' in index_text and 'U+0020' in index_text


def test_relation_targets_match_exact_existing_headwords_and_audit_without_cache(tmp_path):
    rows = [
        lexical('variant', senses=[{'glosses': ['An alternative spelling.'], 'alt_of': [
            {'word': 'Polish'}, {'word': 'POLISH'}, {'word': 'missing'},
            {'word': ' '}, {'word': 'résumé'}]}]),
        lexical('Polish'), lexical('polish'), lexical('résumé'),
        lexical(' ', pos='punct', original_title='Unsupported titles/Space'),
    ]
    archive, evidence, _ = capture(tmp_path, rows)
    pipeline.ingest(tmp_path, archive, evidence)
    result = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    manifest, _ = pipeline.read_md(edition / 'manifests' / f'{pipeline.partition_for("variant")}.md')
    expected = {word: pipeline.relation_url(word) for word in ['Polish', ' ', 'résumé']}
    assert manifest['relation_targets'] == expected
    source, _ = pipeline.partition_rows(edition / 'sources' / f'{pipeline.partition_for("variant")}.md')
    assert source['editorial_revision'] == 'corrections-v1'
    body = (edition / pipeline.full_entry_path('variant')).read_text()
    assert pipeline.relation_url('Polish') in body
    assert pipeline.relation_url('résumé') in body
    assert pipeline.relation_url('POLISH') not in body
    assert pipeline.relation_url('missing') not in body
    for path in (tmp_path / '.cache/full-import').glob('source.sqlite3*'):
        path.unlink()
    assert pipeline.audit_edition(edition, workers=1) == result


def test_target_inventory_uses_bounded_binary_database_queries(tmp_path):
    with pipeline.database(tmp_path) as db:
        for ordinal, word in enumerate(['Polish', 'polish', 'résumé', ' '], 1):
            db.execute('INSERT INTO records VALUES (?,?,?,?)',
                       (ordinal, pipeline.partition_for(word), word, '{}'))
        targets = pipeline.database_relation_targets(db, {'Polish', 'POLISH', 'résumé', ' ', 'absent'})
    assert targets == {word: pipeline.relation_url(word) for word in ['Polish', 'résumé', ' ']}


def test_target_lookup_batches_many_different_partitions_without_per_word_queries(tmp_path):
    words = {f'word-{number}' for number in range(901)}
    with pipeline.database(tmp_path) as db:
        db.executemany('INSERT INTO records VALUES (?,?,?,?)', [
            (ordinal, pipeline.partition_for(word), word, '{}')
            for ordinal, word in enumerate(sorted(words))])
        db.execute('CREATE INDEX records_bucket_word ON records(bucket,word,ordinal)')
        queries = []
        db.set_trace_callback(queries.append)
        targets = pipeline.database_relation_targets(db, words)
        db.set_trace_callback(None)
    assert set(targets) == words
    assert len(queries) == 3
    assert all(query.startswith('WITH requested') for query in queries)


def test_target_spelling_is_checked_inside_canonical_markdown(tmp_path):
    word = 'target'
    path = tmp_path / pipeline.full_entry_path(word)
    path.parent.mkdir(parents=True)
    path.write_text(document({'headword': 'Target', 'language': 'en'}, '# Wrong identity'), encoding='utf-8')
    with pytest.raises(ValueError, match='target identity'):
        pipeline.edition_relation_targets(tmp_path, {word})
    path.write_text(document({'headword': word, 'language': 'fr'}, '# Wrong language'), encoding='utf-8')
    with pytest.raises(ValueError, match='target identity'):
        pipeline.edition_relation_targets(tmp_path, {word})


def test_symlink_is_not_accepted_as_a_relation_target(tmp_path):
    target = tmp_path / 'elsewhere.md'
    target.write_text(document({'headword': 'target', 'language': 'en'}, '# Target'), encoding='utf-8')
    path = tmp_path / pipeline.full_entry_path('target')
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    with pytest.raises(ValueError, match='not a canonical'):
        pipeline.edition_relation_targets(tmp_path, {'target'})


def test_relation_target_manifest_tampering_fails_even_if_its_checksum_is_recomputed(tmp_path):
    archive, evidence, _ = capture(tmp_path, [
        lexical('variant', senses=[{'glosses': ['An alternative spelling.'], 'alt_of': [{'word': 'base'}]}]),
        lexical('base'),
    ])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    bucket = pipeline.partition_for('variant')
    manifest_path = edition / 'manifests' / f'{bucket}.md'
    metadata, body = pipeline.read_md(manifest_path)
    metadata['relation_targets']['base'] = '../000/nonexistent.md'
    manifest_path.write_text(document(metadata, body), encoding='utf-8')
    master_path = edition / 'MANIFEST.md'
    master, body = pipeline.read_md(master_path)
    master['partitions'][bucket] = pipeline.file_hash(manifest_path)
    master_path.write_text(document(master, body), encoding='utf-8')
    with pytest.raises(ValueError, match='relation targets'):
        pipeline.audit_edition(edition, workers=1)


def test_editorial_counters_reconcile_with_rendered_entries(tmp_path):
    archive, evidence, _ = capture(tmp_path, [lexical('context', senses=[{
        'glosses': ['A specialist use.'], 'topics': ['computing'], 'raw_tags': ['with a noun'],
    }])])
    pipeline.ingest(tmp_path, archive, evidence)
    result = pipeline.build(tmp_path, workers=1)
    edition = pipeline.active(tmp_path)
    metadata, _ = pipeline.read_md(edition / pipeline.full_entry_path('context'))
    assert metadata['editorial_changes']['source_topic_labels_restored'] > 0
    assert result['correction_counts'] == metadata['editorial_changes']
    assert result['corrected_headwords'] == 1
    master, body = pipeline.read_md(edition / 'MANIFEST.md')
    master['corrected_headwords'] = 0
    (edition / 'MANIFEST.md').write_text(document(master, body), encoding='utf-8')
    with pytest.raises(ValueError, match='correction counters'):
        pipeline.audit_edition(edition, workers=1)


def test_partial_relation_build_resumes_before_other_target_partitions_exist(tmp_path):
    headwords = sorted(['base', 'variant'], key=pipeline.partition_for)
    first, later = headwords
    archive, evidence, _ = capture(tmp_path, [
        lexical(first, senses=[{'glosses': ['An alternative spelling.'], 'alt_of': [{'word': later}]}]),
        lexical(later),
    ])
    pipeline.ingest(tmp_path, archive, evidence)
    pipeline.build(tmp_path, workers=1, max_partitions=1)
    stage = next((tmp_path / '.cache/full-import/builds').iterdir())
    assert not (stage / pipeline.full_entry_path(later)).exists()
    result = pipeline.build(tmp_path, workers=1)
    assert result['headwords'] == 2
    edition = pipeline.active(tmp_path)
    assert pipeline.relation_url(later) in (edition / pipeline.full_entry_path(first)).read_text()


def test_relation_collection_retains_exact_words_and_ignores_unrenderable_targets():
    rows = [{'form_of': [{'word': 'Polish'}], 'senses': [
        {'alt_of': [{'word': 'polish'}, {'word': ' '}, {'word': 'x' * 1025}, {'word': ''}]},
        {'form_of': [{'word': 'Polish'}]},
    ]}]
    assert pipeline.relation_words(rows) == {'Polish', 'polish', ' '}
