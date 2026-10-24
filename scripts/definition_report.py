"""Reconcile accepted definition decisions and publish English reports and Portuguese figures."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from html import escape
from pathlib import Path
import re
from urllib.parse import quote

from scripts.definition_overlay import (LEDGER_NAME, REVISION, SUPPORTED_REVISIONS, READING_POS_NAMES,
                                       counter_keys, gap_counts, load_ledger, safe_url)
from scripts.entries import POS_NAMES, _text
from scripts.full_pipeline import active, file_hash, full_entry_path, read_md
from scripts.markdown import atomic_write, document
from scripts.review_progress import Figure, BG, BLUE, GREEN, AMBER, LINE, MUTED, TEXT


def integer(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer of at least {minimum}')
    return value


def checked_file(path: Path, expected: object, root: Path) -> None:
    if (path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve())
            or not isinstance(expected, str) or file_hash(path) != expected):
        raise ValueError(f'Accepted evidence checksum or path mismatch: {path.name}')


def decision_counts(rows: list[dict], revision: str = REVISION) -> Counter:
    counts = Counter({key: 0 for key in counter_keys(revision)})
    counts.update(source_gap_senses=len(rows), ledger_decisions_applied=len(rows))
    for row in rows:
        counts[{'definition': 'definition_filled_senses', 'auxiliary': 'auxiliary_relation_senses',
                'pending': 'unresolved_gloss_senses', 'source-structure': 'source_structure_senses'}[row['status']]] += 1
        if row['status'] == 'auxiliary':
            counts['auxiliary_translation_hub_senses' if row['source_kind'] == 'translation-hub'
                   else 'auxiliary_thesaurus_senses'] += 1
    if revision != 'definitions-v1':
        counts['part_of_speech_corrected_records'] = len({(r['word'], r['record']) for r in rows if 'corrected_pos' in r})
    counts['definition_filled_headwords'] = len({r['word'] for r in rows if r['status'] == 'definition'})
    counts['auxiliary_relation_headwords'] = len({r['word'] for r in rows if r['status'] == 'auxiliary'})
    counts['source_kind_corrected_records'] = len({(r['word'], r['record']) for r in rows
                                                  if r.get('source_kind') == 'thesaurus'})
    return counts


def evidence(root: Path) -> tuple[Path, dict, dict, list[dict], dict]:
    edition = active(root)
    manifest, _ = read_md(edition / 'MANIFEST.md')
    if manifest.get('edition') != edition.name or 'definition_ledger_sha256' not in manifest:
        raise ValueError('Current accepted edition has no matching definition ledger')
    ledger, rows, checksum = load_ledger(edition / LEDGER_NAME, manifest['definition_ledger_sha256'])
    for name in (LEDGER_NAME, 'SOURCE.md', 'PROGRESS.md'):
        checked_file(edition / name, manifest.get('files', {}).get(name), edition)
    source, _ = read_md(edition / 'SOURCE.md')
    progress, _ = read_md(edition / 'PROGRESS.md')
    revision = source.get('definition_overlay_revision')
    counters = counter_keys(revision)
    if (revision not in SUPPORTED_REVISIONS
            or source.get('definition_ledger_sha256') != checksum
            or any(source.get(key) != ledger[key] for key in ('snapshot_id', 'raw_sha256'))):
        raise ValueError('Definition source gate or snapshot identity mismatch')
    previous = ledger['base_edition']
    if not re.fullmatch(r'full-[0-9a-f]{16}', previous) or previous == edition.name:
        raise ValueError('Invalid previous edition identity')
    base = root / 'editions' / previous
    checked_file(base / 'MANIFEST.md', ledger['base_manifest_sha256'], base)
    old_manifest, _ = read_md(base / 'MANIFEST.md')
    screening, _ = read_md(root / 'reviews' / previous / 'SCREENING.md')
    if (screening.get('edition') != previous
            or screening.get('status') != 'complete-deterministic-screening'
            or screening.get('master_manifest_sha256') != ledger['base_manifest_sha256']
            or screening.get('metrics', {}).get('missing_definition_findings') != len(rows)):
        raise ValueError('Original gap queue and ledger denominator mismatch')
    counts = decision_counts(rows, revision)
    for label, values in (('manifest', manifest.get('definition_counts')), ('progress', progress.get('definition_counts'))):
        if not isinstance(values, dict) or set(values) != set(counters):
            raise ValueError(f'{label} definition counter contract mismatch')
        for key, value in values.items():
            integer(value, f'{label} {key}')
    if (not rows or manifest.get('definition_counts') != dict(counts)
            or progress.get('definition_counts') != dict(counts)):
        raise ValueError('Ledger and accepted definition counters do not reconcile')
    for key in ('headwords', 'accepted_records', 'senses'):
        value = integer(manifest.get(key), key, 1)
        if (integer(old_manifest.get(key), f'base {key}', 1) != value
                or integer(progress.get(key), f'progress {key}', 1) != value):
            raise ValueError('Accepted corpus denominator changed')
    partitions = manifest.get('partitions')
    if not isinstance(partitions, dict) or not partitions or integer(progress.get('partitions'), 'progress partitions', 1) != len(partitions):
        raise ValueError('Accepted partition denominator mismatch')
    by_bucket: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_bucket[full_entry_path(row['word']).parts[1]][row['word']].append(row)
    if set(by_bucket) - set(partitions):
        raise ValueError('Ledger has a missing source partition')
    actual_counts = Counter({key: 0 for key in counters})
    partition_counts = Counter({key: 0 for key in counters})
    totals = Counter()
    checked_entries = []
    for bucket, expected in sorted(partitions.items()):
        if not re.fullmatch(r'[0-3][0-9a-f]{2}', bucket):
            raise ValueError('Invalid partition identity')
        partition_path = edition / 'manifests' / f'{bucket}.md'
        checked_file(partition_path, expected, edition)
        part, _ = read_md(partition_path)
        if part.get('partition') != bucket:
            raise ValueError('Partition identity mismatch')
        for key in ('headwords', 'accepted_records', 'senses'):
            totals[key] += integer(part.get(key), f'partition {key}')
        values = part.get('definition_counts')
        if not isinstance(values, dict) or set(values) != set(counters):
            raise ValueError('Partition definition counter contract mismatch')
        for key, value in values.items():
            partition_counts[key] += integer(value, key)
        for word, decisions in sorted(by_bucket.get(bucket, {}).items()):
            relative = full_entry_path(word)
            path = edition / relative
            checked_file(path, part.get('files', {}).get(relative.as_posix()), edition)
            checked_file(base / relative, decisions[0]['base_entry_sha256'], base)
            if any(row['base_entry_sha256'] != decisions[0]['base_entry_sha256'] for row in decisions):
                raise ValueError('Inconsistent base entry anchors')
            meta, body = read_md(path)
            if (meta.get('headword') != word or meta.get('language') != 'en'
                    or meta.get('definition_overlay_revision') != revision
                    or meta.get('definition_ledger_sha256') != checksum):
                raise ValueError('Accepted entry definition identity mismatch')
            expected_decisions = [{'id': row['id'], 'coordinate': f'R{row["record"]}.S{row["sense"]}',
                                   'status': row['status']} for row in
                                  sorted(decisions, key=lambda row: (row['record'], row['sense']))]
            if meta.get('definition_decisions') != expected_decisions:
                raise ValueError('Entry decisions do not match the ledger')
            expected_counts = decision_counts(decisions, revision)
            if gap_counts(meta) != expected_counts:
                raise ValueError('Entry definition counters do not reconcile')
            actual_counts.update(gap_counts(meta))
            for row in decisions:
                anchor = f'<a id="r{row["record"]}-s{row["sense"]}"></a>\n\n**Source sense R{row["record"]}.S{row["sense"]}**\n\n'
                if body.count(anchor) != 1:
                    raise ValueError('Exact source-sense anchor is missing or ambiguous')
                block = body.split(anchor, 1)[1]
                block = re.split(r'\n(?:<a id="r\d|##? |### )', block, maxsplit=1)[0]
                if row.get('corrected_pos'):
                    corrected = READING_POS_NAMES[row['corrected_pos']]
                    record_count = integer(meta.get('source_records'), 'entry source_records', 1)
                    record_anchor = f'<a id="r{row["record"]}"></a>\n\n'
                    if (re.findall(r'^<a id="r(\d+)"></a>$', body, re.MULTILINE)
                            != [str(number) for number in range(1, record_count + 1)]
                            or body.count(record_anchor) != 1):
                        raise ValueError('Accepted part-of-speech correction record is missing or ambiguous')
                    record_block = body.split(record_anchor, 1)[1]
                    record_block = re.split(r'\n<a id="r\d+"></a>\n|\n## Sources\n',
                                            record_block, maxsplit=1)[0]
                    suffix = f' · Record {row["record"]}' if record_count > 1 else ''
                    note = (f'**Part-of-speech correction:** {corrected}. The source labels this section as '
                            f'{POS_NAMES[row["pos"]].lower()};')
                    if (not record_block.startswith(f'## {corrected}{suffix}\n\n' + note)
                            or anchor not in record_block):
                        raise ValueError('Accepted part-of-speech correction is absent')
                if row['status'] == 'definition':
                    if not block.startswith(f'{row["sense"]}. {_text(row["definition"])}\n'):
                        raise ValueError('Accepted definition text does not match its exact sense')
                elif row['status'] == 'auxiliary':
                    label = 'translation hub' if row['source_kind'] == 'translation-hub' else 'thesaurus relations'
                    if f'**Source classification: auxiliary {label},' not in block:
                        raise ValueError('Accepted auxiliary classification is absent')
                elif row['status'] == 'source-structure':
                    if ('**Source-structure correction (no definition added):** ' + _text(row['reason']) not in block
                            or not block.startswith('*Source structure: empty duplicate section; not an independent lexical sense.*')):
                        raise ValueError('Accepted source-structure correction is absent')
                elif '**Definition review:** ' + _text(row['reason']) not in block:
                    raise ValueError('Accepted pending reason is absent')
            checked_entries.append({'headword': word, 'path': relative.as_posix(), 'sha256': file_hash(path)})
    if (dict(partition_counts) != dict(counts) or dict(actual_counts) != dict(counts)
            or any(totals[key] != manifest[key] for key in totals)):
        raise ValueError('Readback totals do not match the accepted manifest')
    split = {'ordinary_definitions': sum(row['status'] == 'definition' and row.get('source_kind') != 'thesaurus' for row in rows),
             'thesaurus_concept_definitions': sum(row['status'] == 'definition' and row.get('source_kind') == 'thesaurus' for row in rows),
             'translation_hub_definitions': sum(row['status'] == 'definition' and row.get('source_kind') == 'translation-hub' for row in rows)}
    return edition, manifest, ledger, rows, {**split, 'counts': dict(counts), 'checked_entries': checked_entries}


def pt(value: int) -> str:
    return f'{value:,}'.replace(',', '.')


class PortugueseFigure(Figure):
    def __init__(self, height: int, title: str, subtitle: str):
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}" viewBox="0 0 1400 {height}" role="img" aria-label="{escape(title, quote=True)}">',
                      f'<rect width="1400" height="{height}" fill="{BG}"/>', '<g font-family="Arial, Helvetica, sans-serif">']
        self.text(56, 55, 'DICIONÁRIO DE INGLÊS  /  REVISÃO DAS DEFINIÇÕES', 15, BLUE, bold=True)
        self.text(56, 105, title, 34, TEXT, bold=True)
        self.text(56, 142, subtitle, 18, MUTED)

    def bar(self, y: int, label: str, note: str, done: int, total: int, color: str = BLUE):
        if not 0 <= done <= total or total <= 0:
            raise ValueError('Invalid figure denominator')
        percent = f'{100 * done / total:.1f}'.replace('.', ',')
        self.text(56, y, label, 22, TEXT, bold=True)
        self.text(1344, y, f'{pt(done)} / {pt(total)}  ·  {percent}%', 20, color, right=True)
        self.text(56, y + 30, note, 17, MUTED)
        self.rect(56, y + 48, 1288, 13, LINE, 6)
        if done:
            self.rect(56, y + 48, 1288 * done / total, 13, color, 6)


def figures(review: Path, manifest: dict, result: dict) -> None:
    c = result['counts']; total = c['source_gap_senses']
    structural = c.get('source_structure_senses', 0)
    corrected_pos = c.get('part_of_speech_corrected_records', 0)
    fig = PortugueseFigure(1200, 'Definições recuperadas e pendências reais',
                           f'Revisão de {pt(total)} lacunas do arquivo original · dados preservados e fontes identificadas')
    for x, value, label, color in ((56, c['definition_filled_senses'], 'definições com fonte', GREEN),
                                   (493, c['auxiliary_relation_senses'], 'registros auxiliares classificados', BLUE),
                                   (930, c['unresolved_gloss_senses'], 'lacunas lexicais pendentes', AMBER)):
        fig.rect(x, 180, 414, 115); fig.text(x + 24, 230, pt(value), 36, color, bold=True)
        fig.text(x + 24, 266, label, 17, MUTED)
    fig.bar(350, 'Definições efetivamente fornecidas', 'Inclui definições lexicais e descrições de conceitos do tesauro.', c['definition_filled_senses'], total, GREEN)
    fig.bar(470, 'Conteúdo auxiliar identificado', 'Classificação de origem; não conta como definição preenchida.', c['auxiliary_relation_senses'], total, BLUE)
    fig.bar(590, 'Seções vazias duplicadas', 'Histórico da fonte comprova o defeito de estrutura; nenhuma definição é inventada.', structural, total, BLUE)
    fig.bar(710, 'Definições ainda pendentes', 'A evidência disponível não sustenta um preenchimento seguro.', c['unresolved_gloss_senses'], total, AMBER)
    fig.rect(56, 805, 1288, 185)
    fig.text(80, 846, f'{pt(result["ordinary_definitions"])} definições lexicais  +  {pt(result["thesaurus_concept_definitions"])} descrições de conceitos do tesauro', 24, TEXT, bold=True)
    fig.text(80, 886, f'Auxiliares: {pt(c["auxiliary_thesaurus_senses"])} grupos do tesauro e {pt(c["auxiliary_translation_hub_senses"])} páginas para traduções.', 19, MUTED)
    fig.text(80, 926, f'{pt(corrected_pos)} correções de classe gramatical já incluídas nas definições; não somar novamente.', 19, MUTED)
    fig.text(80, 966, 'Registros da fonte e verbetes continuam em inglês e Markdown.', 19, MUTED)
    fig.text(56, 1048, 'Revisão assistida por IA; sem certificação humana, Cambridge ou Oxford.', 20, AMBER)
    fig.text(56, 1087, 'Uma definição com fonte não garante cobertura de todos os significados da palavra.', 18, MUTED)
    fig.text(56, 1147, f'Edição: {manifest["edition"]} · Contagens reconciliadas com os verbetes e o registro de decisões.', 15, MUTED)
    atomic_write(review / 'assets' / 'definitions-pt.svg', fig.finish())
    fig = PortugueseFigure(1050, 'Progresso do acervo e da revisão', 'Edição local aceita · arquivos em Markdown · integridade e conteúdo medidos separadamente')
    for x, value, label in ((56, manifest['headwords'], 'verbetes em inglês e Markdown'), (493, manifest['accepted_records'], 'registros em inglês da fonte'), (930, manifest['senses'], 'sentidos preservados da fonte')):
        fig.rect(x, 180, 414, 115); fig.text(x + 24, 230, pt(value), 36, TEXT, bold=True); fig.text(x + 24, 266, label, 17, MUTED)
    fig.bar(350, 'Importação e edição aceitas', 'Total do arquivo de origem selecionado; não representa todas as palavras do inglês.', manifest['headwords'], manifest['headwords'], GREEN)
    fig.bar(470, 'Auditoria antes da aceitação', 'Todas as partições auditadas na promoção; seus manifestos foram reconferidos neste relatório.', len(manifest['partitions']), len(manifest['partitions']), GREEN)
    fig.bar(590, 'Lacunas com decisão registrada', 'Definição, classificação auxiliar, defeito de estrutura ou motivo para continuar pendente.', total, total, BLUE)
    fig.rect(56, 685, 1288, 150)
    fig.text(80, 727, f'{pt(len(result["checked_entries"]))} verbetes afetados reconferidos', 27, BLUE, bold=True)
    fig.text(80, 769, 'Texto, posição do sentido, origem, hashes e contadores conferidos nos arquivos aceitos.', 18, MUTED)
    fig.text(80, 804, f'{pt(c["unresolved_gloss_senses"])} lacunas lexicais continuam pendentes; classificação não é definição.', 19, AMBER)
    fig.text(56, 904, '100% mede o processamento deste conjunto, sem indicar aprovação linguística integral.', 19, MUTED)
    fig.text(56, 943, 'Fontes históricas mantêm sua identificação; revisão editorial humana continua pendente.', 18, MUTED)
    fig.text(56, 1000, f'Edição: {manifest["edition"]} · Evidências: manifesto, decisões e leitura dos verbetes afetados.', 15, MUTED)
    atomic_write(review / 'assets' / 'progress-pt.svg', fig.finish())


def case_body(row: dict, edition: Path) -> str:
    relative = '../../editions/' + edition.name + '/' + full_entry_path(row['word']).as_posix()
    anchor = f'r{row["record"]}-s{row["sense"]}'
    lines = [f'## {_text(row["word"])} — R{row["record"]}.S{row["sense"]}', '',
             f'[Accepted entry and exact sense]({relative}#{anchor})', '',
             '**Decision:** ' + row['status'] + '.', '', '**Reason:** ' + _text(row['reason']), '']
    if row['status'] == 'definition':
        lines += ['**Supplemental definition:** ' + _text(row['definition']), '']
    if row.get('source_kind'):
        lines += ['**Source kind:** ' + row['source_kind'] + '.', '']
    if row.get('corrected_pos'):
        lines += ['**Reading-layer part of speech:** ' + row['corrected_pos']
                  + '. Original source label and metadata remain preserved.', '']
    if row.get('transformation'):
        lines += ['**Editorial transformation:** ' + _text(row['transformation']), '']
    for source in row.get('sources', []):
        lines += ['- [' + _text(source['title']) + '](' + safe_url(source['url']) + ') — '
                  + _text(source['revision']) + '; ' + _text(source['license']) + '.']
    if row.get('source_title'):
        lines += ['', '[Attributed source page](https://en.wiktionary.org/wiki/' + quote(row['source_title'], safe='') + ').']
    lines += ['', f'Case `{_text(row["id"])}` · original record SHA-256 `{row["upstream_record_sha256"]}`.', '']
    return '\n'.join(lines)


def build(root: Path) -> Path:
    edition, manifest, ledger, rows, result = evidence(root)
    review = root / 'reviews' / edition.name
    c = result['counts']
    metadata = {'kind': 'definition-review-report', 'edition': edition.name,
                'base_edition': ledger['base_edition'], 'master_manifest_sha256': file_hash(edition / 'MANIFEST.md'),
                'definition_ledger_sha256': manifest['definition_ledger_sha256'], **result,
                'human_lexicographic_approval': False,
                'method': 'Ledger and all partition-manifest reconciliation; checksum, metadata, exact-anchor and displayed-definition readback for every affected headword. Full-corpus audit recorded before promotion.'}
    for filename, title, status in (('RESOLVED.md', 'Supported supplemental definitions', 'definition'),
                                    ('PENDING.md', 'Unresolved lexical definition gaps', 'pending'),
                                    ('AUXILIARY.md', 'Auxiliary source classifications', 'auxiliary'),
                                    ('STRUCTURE.md', 'Documented empty duplicate sections', 'source-structure')):
        selected = [row for row in rows if row['status'] == status]
        text = f'# {title}\n\n[Review overview](DEFINITION-REVIEW.md) · [Immutable decision ledger](../../editions/{edition.name}/DEFINITIONS.md)\n\n'
        text += f'{len(selected):,} cases. Auxiliary classifications and structural repairs are not definitions. Original source data remain preserved.\n\n'
        text += '\n'.join(case_body(row, edition) for row in selected) or 'No cases in this category.\n'
        atomic_write(review / filename, document({'kind': 'definition-review-cases', 'edition': edition.name,
                     'status': status, 'cases': len(selected), 'definition_ledger_sha256': manifest['definition_ledger_sha256']}, text))
    body = f'''# Definition review and source reliability

**The accepted edition contains {manifest['headwords']:,} English Markdown headword files. The original {c['source_gap_senses']:,} source gaps reconcile as {c['definition_filled_senses']:,} supplied definitions, {c['auxiliary_relation_senses']:,} auxiliary classifications, {c.get('source_structure_senses', 0):,} documented duplicate sections and {c['unresolved_gloss_senses']:,} pending lexical gaps.**

| Measure | Count | Meaning |
| --- | ---: | --- |
| Original source gaps | {c['source_gap_senses']:,} | Fixed denominator from the previous edition screening |
| Supplemental lexical definitions | {result['ordinary_definitions']:,} | Ordinary lexical records, including supported translation-hub concepts |
| Recovered thesaurus concept definitions | {result['thesaurus_concept_definitions']:,} | Explicit concept descriptions with corrected thesaurus provenance |
| All supplied definitions | {c['definition_filled_senses']:,} | Sum of the two definition categories above |
| Auxiliary thesaurus groups | {c['auxiliary_thesaurus_senses']:,} | Corrected classification, no supplied definition |
| Auxiliary translation hubs | {c['auxiliary_translation_hub_senses']:,} | Translation-organizing source pages, no supplied definition |
| Empty duplicate source sections | {c.get('source_structure_senses', 0):,} | Source revision history establishes a structural defect; no definition added |
| Corrected reading-layer parts of speech | {c.get('part_of_speech_corrected_records', 0):,} | Included in supplied definitions; not an additional disjoint category |
| Pending lexical definitions | {c['unresolved_gloss_senses']:,} | No sufficiently supported aligned definition accepted |
| Affected headwords read back | {len(result['checked_entries']):,} | Exact file hashes, sense anchors, decisions and counters |
| Accepted partitions reconciled | {len(manifest['partitions']):,} | All partition manifests rehashed; full corpus audited before promotion |

Definitions are attributed supplements or recovered source descriptions. They do not overwrite the original missing source glosses. A supported meaning does not establish complete coverage of every meaning of its headword. Auxiliary classification and documented structural repair are never counted as filled definitions. Grammatical corrections change the reading heading only, with explicit evidence and unchanged original source metadata and forms.

Sources are assessed per case and captured revision. Historical dictionary material retains historical attribution and source-specific usage qualifications; it is not presented as a present-day usage judgment. The original public-domain-in-the-USA New English Dictionary volumes are identified separately from the modern proprietary OED. Targeted contemporary sources are used for independently worded factual descriptions; private verification captures are not redistributed. Source provenance and byte integrity do not establish universal semantic accuracy. AI-assisted adjudication is not human lexicographer certification. Cambridge and Oxford remain editorial references, without imported proprietary definitions, affiliation or certification.

## Decisions and evidence

- [Supplied definitions, exact entry anchors and sources](RESOLVED.md)
- [Pending lexical gaps and reasons](PENDING.md)
- [Auxiliary classifications and reasons](AUXILIARY.md)
- [Documented empty duplicate sections](STRUCTURE.md)
- [Immutable decision ledger](../../editions/{edition.name}/DEFINITIONS.md)
- [Accepted corpus index](../../editions/{edition.name}/INDEX.md)
- [Accepted integrity manifest](../../editions/{edition.name}/MANIFEST.md)

![Definition progress in Portuguese](assets/definitions-pt.svg)

![Collection progress in Portuguese](assets/progress-pt.svg)

The figures are Portuguese presentation aids. Entries, decisions and canonical reports remain English Markdown. Percentages refer only to this pinned collection or the explicitly measured gap queue, never to all English words or whole-corpus linguistic accuracy.

## Reproduce

```sh
python3 -m scripts.definition_report --root .
```

The report refuses a legacy edition without the definition overlay, changed evidence hashes, stale original-queue anchors, missing entry decisions, wrong-language metadata or unreconciled counts. It rereads all affected entries and every partition manifest; it does not claim to repeat the full corpus render audit performed before acceptance.
'''
    figures(review, manifest, result)
    target = review / 'DEFINITION-REVIEW.md'
    atomic_write(target, document(metadata, body))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(build(args.root.resolve()))


if __name__ == '__main__':
    main()
