"""Publish evidence-anchored correction reports and Portuguese progress figures."""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import re

from scripts.full_pipeline import active, file_hash, full_entry_path, read_md
from scripts.markdown import atomic_write, document
from scripts.review_progress import Figure, BG, BLUE, GREEN, AMBER, LINE, MUTED, TEXT

EDITION_ID = re.compile(r'full-[0-9a-f]{16}')


def integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer of at least {minimum}')
    return value


def checked_entries(root: Path, edition: Path, entries: object) -> tuple[set[str], int, int]:
    if not isinstance(entries, list) or not entries:
        raise ValueError('Post-correction entries must be a nonempty list')
    words, records, senses = set(), 0, 0
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get('headword'), str):
            raise ValueError('Invalid post-correction entry identity')
        word = item['headword']
        if word in words:
            raise ValueError('Duplicate post-correction headword')
        relative = item.get('path')
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError('Post-correction path must be relative to the repository')
        path = root / relative
        canonical = edition / full_entry_path(word)
        if path.is_symlink() or path.resolve() != canonical.resolve() or not path.is_file():
            raise ValueError('Post-correction entry must use its canonical accepted-edition path')
        if file_hash(path) != item.get('sha256'):
            raise ValueError('Post-correction entry hash mismatch')
        meta, _ = read_md(path)
        if meta.get('headword') != word or meta.get('language') != 'en':
            raise ValueError('Post-correction entry metadata identity mismatch')
        rec = integer(item.get('records'), 'entry records', minimum=1)
        sen = integer(item.get('senses'), 'entry senses')
        if (integer(meta.get('source_records'), 'metadata records', minimum=1),
                integer(meta.get('sense_count'), 'metadata senses')) != (rec, sen):
            raise ValueError('Post-correction entry counts mismatch')
        words.add(word)
        records += rec
        senses += sen
    return words, records, senses


def evidence(root: Path) -> tuple[Path, dict, dict, dict]:
    edition = active(root)
    review = root / 'reviews' / edition.name
    manifest, _ = read_md(edition / 'MANIFEST.md')
    screening, _ = read_md(review / 'SCREENING.md')
    post, _ = read_md(review / 'POST-CORRECTION-CHECK.md')
    if any(item.get('edition') != edition.name for item in (manifest, screening, post)):
        raise ValueError('Correction evidence targets another edition')
    master_hash = file_hash(edition / 'MANIFEST.md')
    if screening.get('status') != 'complete-deterministic-screening':
        raise ValueError('Deterministic screening has not completed')
    if screening.get('master_manifest_sha256') != master_hash:
        raise ValueError('Screening master manifest anchor has changed')
    if 'master_manifest_sha256' in post and post['master_manifest_sha256'] != master_hash:
        raise ValueError('Post-correction master manifest anchor has changed')
    if not isinstance(post.get('method'), str) or not post['method'].strip():
        raise ValueError('Post-correction method must be explicit')
    metrics = screening.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('Screening metrics must be an object')
    for key in ('headwords', 'accepted_records', 'senses'):
        total = integer(manifest.get(key), key, minimum=1)
        if integer(metrics.get(key), f'screened {key}', minimum=1) != total:
            raise ValueError(f'Screening denominator mismatch: {key}')
    partitions = manifest.get('partitions')
    if (not isinstance(partitions, dict) or not partitions or
            integer(metrics.get('partitions'), 'screened partitions', minimum=1) != len(partitions)):
        raise ValueError('Screening partition denominator mismatch')
    for key, denominator in (('senses_without_nonblank_gloss', 'senses'),
                             ('records_with_source_gaps', 'accepted_records')):
        if integer(metrics.get(key), key) > manifest[denominator]:
            raise ValueError(f'Screening gap denominator mismatch: {key}')
    changed = integer(manifest.get('corrected_headwords'), 'corrected_headwords')
    counts = manifest.get('correction_counts')
    if not isinstance(counts, dict) or any(not isinstance(key, str) or not key for key in counts):
        raise ValueError('Correction counters must be named integer counts')
    for key, value in counts.items():
        integer(value, f'correction counter {key}')
    if (changed > manifest['headwords'] or changed > sum(counts.values()) or
            bool(changed) != bool(sum(counts.values()))):
        raise ValueError('Corrected-headword denominator mismatch')
    source, _ = read_md(edition / 'SOURCE.md')
    if source.get('editorial_revision') != 'corrections-v1':
        raise ValueError('Current edition does not contain the correction renderer')
    if manifest.get('files', {}).get('SOURCE.md') != file_hash(edition / 'SOURCE.md'):
        raise ValueError('Source manifest anchor has changed')
    previous = post.get('previous_edition')
    if not isinstance(previous, str) or not EDITION_ID.fullmatch(previous) or previous == edition.name:
        raise ValueError('Previous edition identity is invalid')
    previous_dir = root / 'editions' / previous
    if file_hash(previous_dir / 'MANIFEST.md') != post.get('previous_manifest_sha256'):
        raise ValueError('Previous manifest anchor has changed')
    old_manifest, _ = read_md(previous_dir / 'MANIFEST.md')
    for key in ('headwords', 'accepted_records', 'senses'):
        if old_manifest.get(key) != manifest[key]:
            raise ValueError(f'Correction edition changed a source denominator: {key}')
    if (not source.get('raw_sha256') or source['raw_sha256'] !=
            old_manifest.get('inventory', {}).get('raw_sha256')):
        raise ValueError('Correction edition does not use the same source bytes')
    words, records, senses = checked_entries(root, edition, post.get('entries'))
    expected = tuple(integer(post.get(key), key, minimum=1) for key in
                     ('checked_headwords', 'checked_records', 'checked_senses'))
    if (len(words), records, senses) != expected:
        raise ValueError('Post-correction checked totals mismatch')
    if any(actual > manifest[key] for actual, key in
           zip(expected, ('headwords', 'accepted_records', 'senses'))):
        raise ValueError('Post-correction sample exceeds corpus denominator')
    old_batch, _ = read_md(root / 'reviews' / previous / 'BATCH-001.md')
    if old_batch.get('edition') != previous:
        raise ValueError('Historical review batch edition mismatch')
    old_words, old_records, old_senses = checked_entries(root, previous_dir, old_batch.get('entries'))
    if (words, records, senses) != (old_words, old_records, old_senses):
        raise ValueError('Post-correction sample differs from the original reviewed entries')
    for actual, key in zip((len(old_words), old_records, old_senses),
                           ('headwords_reviewed', 'records_reviewed', 'senses_examined')):
        if integer(old_batch.get(key), key, minimum=1) != actual:
            raise ValueError('Historical review batch totals mismatch')
    return review, manifest, screening, post


def pt(value: int) -> str:
    return f'{value:,}'.replace(',', '.')


class PortugueseFigure(Figure):
    def __init__(self, height: int, title: str, subtitle: str):
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}" '
                      f'viewBox="0 0 1400 {height}" role="img" aria-label="{escape(title, quote=True)}">',
                      f'<rect width="1400" height="{height}" fill="{BG}"/>',
                      '<g font-family="Arial, Helvetica, sans-serif">']
        self.text(56, 55, 'DICIONÁRIO DE INGLÊS  /  CORREÇÕES LOCAIS', 15, BLUE, bold=True)
        self.text(56, 105, title, 35, TEXT, bold=True)
        self.text(56, 142, subtitle, 19, MUTED)

    def complete(self, y: int, label: str, note: str, done: int, total: int) -> None:
        if type(done) is not int or type(total) is not int or total <= 0 or done != total:
            raise ValueError('A completed stage requires matching positive denominators')
        self.text(56, y, label, 22, TEXT, bold=True)
        self.text(1344, y, f'{pt(done)} / {pt(total)}  ·  100%', 20, GREEN, right=True)
        self.text(56, y + 30, note, 17, MUTED)
        self.rect(56, y + 48, 1288, 13, GREEN, 6)


def figures(review: Path, manifest: dict, screening: dict, post: dict) -> None:
    m, c = screening['metrics'], manifest['correction_counts']
    fig = PortugueseFigure(1120, 'O que foi corrigido e verificado',
                           'Mesma fonte preservada · nova edição em Markdown · números conferidos nos relatórios')
    for x, value, label in ((56, m['headwords'], 'verbetes em inglês e Markdown'),
                            (493, m['accepted_records'], 'registros lexicais da fonte'),
                            (930, m['senses'], 'sentidos registrados na fonte')):
        fig.rect(x, 180, 414, 115)
        fig.text(x + 24, 230, pt(value), 36, TEXT, bold=True)
        fig.text(x + 24, 266, label, 18, MUTED)
    fig.complete(345, '01  Nova edição gerada', 'Os verbetes foram reconstruídos com correções rastreáveis.', m['headwords'], manifest['headwords'])
    fig.complete(455, '02  Auditoria integral da edição', 'Arquivos, reprodução, contagens, links e navegação conferidos antes da promoção.', m['partitions'], len(manifest['partitions']))
    fig.complete(565, '03  Triagem automática atualizada', 'Todas as projeções da fonte verificadas; isso não mede a precisão dos significados.', m['headwords'], manifest['headwords'])
    fig.rect(56, 660, 1288, 125)
    fig.text(80, 703, f'{pt(manifest["corrected_headwords"])} verbetes com mudanças contabilizadas', 27, BLUE, bold=True)
    fig.text(80, 743, 'Inclui contexto, rótulos, links e correções pontuais; não equivale a aprovação linguística.', 19, MUTED)
    fig.rect(56, 810, 1288, 145)
    fig.text(80, 855, f'{pt(post["checked_headwords"])} verbetes da amostra reconferidos', 27, GREEN, bold=True)
    fig.text(80, 894, f'{pt(post["checked_records"])} registros / {pt(post["checked_senses"])} sentidos · amostra dirigida, sem estimativa de precisão geral.', 19, MUTED)
    fig.text(80, 928, 'Revisão por lexicógrafo humano e certificação Cambridge/Oxford: não realizadas.', 18, AMBER)
    fig.text(56, 1020, '100% refere-se à edição e às verificações automáticas, nunca a todas as palavras do inglês.', 18, MUTED)
    fig.text(56, 1070, f'Edição: {manifest["edition"]} · Evidências: manifesto, triagem e conferência após correções', 15, MUTED)
    atomic_write(review / 'assets' / 'progress-pt.svg', fig.finish())

    fig = PortugueseFigure(1190, 'Correções aplicadas, limites preservados',
                           'Contagens de ações na exibição · uma ocorrência pode aparecer em mais de um registro')
    rows = [
        ('Rótulos de assunto recuperados', c.get('source_topic_labels_restored', 0), 'Hierarquia original do extrator; não são classificações editoriais aprovadas.'),
        ('Contextos originais recuperados', c.get('raw_context_labels_restored', 0), 'Expressões e restrições preservadas literalmente, sem completar dados por suposição.'),
        ('Artefatos retirados das listas de formas', c.get('nonlexical_forms_suppressed', 0), 'Controles de tabela e casos pontuais comprovados; o original continua preservado.'),
        ('Transcrições parciais identificadas', c.get('partial_pronunciations_labelled', 0), 'Fragmentos de pronúncia rotulados; nenhum som ausente foi inventado.'),
        ('Links locais entre verbetes', c.get('local_relation_links', 0), 'Destinos existentes e exatos; ainda não apontam automaticamente ao sentido correto.'),
        ('Definições reformuladas com evidência', c.get('F04', 0), 'Correção pontual de substantivo em color; adaptação identificada e rastreável.'),
    ]
    for number, (title, value, note) in enumerate(rows):
        y = 190 + number * 112
        fig.rect(56, y, 1288, 96)
        fig.text(80, y + 34, title, 22, TEXT, bold=True)
        fig.text(1320, y + 37, pt(value), 28, BLUE, bold=True, right=True)
        fig.text(80, y + 72, note, 17, MUTED)
    fig.rect(56, 887, 1288, 151)
    fig.text(80, 933, f'{pt(m["senses_without_nonblank_gloss"])} sentidos continuam sem definição utilizável', 27, AMBER, bold=True)
    fig.text(80, 974, 'Lacunas foram mantidas e sinalizadas. Avisos e correções de formato não resolvem o conteúdo.', 18, MUTED)
    fig.text(80, 1007, 'História das palavras, registros de uso e equivalência entre sinônimos ainda exigem revisão.', 18, MUTED)
    fig.text(56, 1090, 'Origem rastreável + integridade verificada. A qualidade linguística global permanece não certificada.', 18, MUTED)
    fig.text(56, 1143, 'As filas analisam a fonte preservada: um alerta antigo pode permanecer mesmo após a correção da exibição.', 17, MUTED)
    atomic_write(review / 'assets' / 'corrections-pt.svg', fig.finish())


def build(root: Path) -> None:
    review, manifest, screening, post = evidence(root)
    m, c = screening['metrics'], manifest['correction_counts']
    old = post['previous_edition']
    prefix = review.relative_to(root).as_posix()
    meta = {'kind': 'editorial-correction-report', 'edition': manifest['edition'],
            'previous_edition': old, 'master_manifest_sha256': file_hash(active(root) / 'MANIFEST.md'),
            'previous_manifest_sha256': post['previous_manifest_sha256'],
            'screening_report_sha256': file_hash(review / 'SCREENING.md'),
            'post_correction_report_sha256': file_hash(review / 'POST-CORRECTION-CHECK.md'),
            'corrected_headwords': manifest['corrected_headwords'], 'correction_counts': c,
            'checked_headwords': post['checked_headwords'], 'checked_records': post['checked_records'],
            'checked_senses': post['checked_senses'], 'human_lexicographic_approval': False}
    rows = '\n'.join(f'| `{key}` | {value:,} |' for key, value in sorted(c.items()))
    body = f'''# Source-preserving dictionary corrections

**A new accepted edition contains {m['headwords']:,} English Markdown entries. It applies counted reading-view changes to {manifest['corrected_headwords']:,} headwords. The same {m['accepted_records']:,} lexical records and {m['senses']:,} source senses remain represented.**

The complete build and readback audit preceded promotion to CURRENT.md. A new deterministic screening covers every source partition. The post-correction check rereads the original review sample: **{post['checked_headwords']:,} headwords, {post['checked_records']:,} records and {post['checked_senses']:,} sense positions**. This is a targeted verification of the corrections, not a new random sample or a whole-corpus semantic approval.

The previous accepted edition remains available. Expanded source projections recover context from the same pinned raw bytes; original values remain in those projections. Reading-view adaptations are identified in entry metadata and notes. No missing definitions, pronunciation strings or historical dates were generated. Cambridge and Oxford supply editorial principles, not imported definitions, affiliation or certification.

## What the numbers mean

| Measure | Count | Interpretation |
| --- | ---: | --- |
| Accepted Markdown headwords | {m['headwords']:,} | Exact spellings in this snapshot |
| Headwords with counted display changes | {manifest['corrected_headwords']:,} | At least one action below; not linguistically certified entries |
| Source-topic label occurrences restored | {c.get('source_topic_labels_restored', 0):,} | Includes extractor ancestors; not curated domain assignments |
| Original contextual label occurrences restored | {c.get('raw_context_labels_restored', 0):,} | Verbatim source restrictions/context |
| Non-lexical form rows suppressed | {c.get('nonlexical_forms_suppressed', 0):,} | Reading view only; raw evidence retained |
| Partial pronunciation occurrences labelled | {c.get('partial_pronunciations_labelled', 0):,} | Shorthand disclosed, never completed by guessing |
| Existing local relation links | {c.get('local_relation_links', 0):,} | Exact headword navigation, not target-sense alignment |
| Evidence-backed noun-form recastings | {c.get('F04', 0):,} | Explicit AI-assisted adaptation of the `color` source gloss |
| Source senses without a nonblank gloss | {m['senses_without_nonblank_gloss']:,} | Still unresolved; no missing definition supplied |

Actions count occurrences; several may affect one entry. Metadata/notice changes do not establish semantic correctness. Source-field availability measures preserved input: withholding a reading-view etymology does not make the original field absent. Screening queues deliberately inspect that preserved input, so their older flags can remain after a reading-view fix.

## Dispositions of the original findings

| Finding | Current disposition | Remaining work |
| --- | --- | --- |
| F01 — lost contexts | Implemented: strict source topics/raw contexts restored across the new projection and labelled as source/extractor context | Curate domain hierarchies and ambiguous source labels; `heading` is a diagnostic, not a domain |
| F02 — non-lexical forms | Implemented for table-control tags and the hash-guarded `color`, `lead` and Space cases | Other suspected forms require evidence; genuine symbols remain intact |
| F03 — missing adjective definition | Unresolved: diagnostics separated and the `repository` gap explicitly reviewed | Obtain and review a licensed definition; the annotation is not a filled gap |
| F04 — noun/verb conflict | Corrected for the exact `color` noun sense with a labelled evidence-backed recasting | Human specialist approval remains pending |
| F05 — residue / punctuation heading | Corrected: guarded repeated reference token and punctuation label expansion | Other residue candidates remain a separate queue |
| F06 — pronunciation shorthand | Implemented: partial strings disclosed, original context preserved, unspecified labels distinguished | Verify pronunciation validity, variety and learner presentation; no missing sounds inferred |
| F07 — relation contamination | Partial: selected misplaced/malformed relations removed; `hello` list exposed only as uncurated source relations | Individual register and interchangeability checks remain |
| F08 — duplicate misspelling | Explained at both exact `lead` coordinates; source sense counts preserved | No automatic merge or claim of two independent meanings |
| F09 — repeated parent glosses | Partial: source coordinates and hierarchy explanation added | A genuinely deduplicated editorial sense hierarchy is still pending |
| F10 — flattened etymology trees | Corrected at {c.get('F10', 0):,} individually verified boundaries in selected headwords | Other trees and all historical claims need further review |
| F11 — unusable relations | Partial: existing exact headwords now linked locally | POS/sense-specific target alignment remains pending |
| F12 — quotation/filter mismatch | Filter disclosure corrected; one guarded idiom etymology withheld with an explanation | Other embedded quotations, source-labelled examples and history claims need review |

`F03` counters record an unresolved-gap annotation; `F08` counters record overlap explanations. `F07` is scoped curation, not universal synonym approval. These are actions and dispositions, not a total of solved linguistic defects.

## Portuguese progress figures

The dictionary and this report remain English Markdown. Portuguese appears only in the requested explanatory figures. SVG is generated from the validated evidence; PNG may be derived for viewing.

![Progress after corrections](assets/progress-pt.svg)

![Measured correction actions and remaining gaps](assets/corrections-pt.svg)

## Complete action ledger

| Renderer action | Occurrences |
| --- | ---: |
{rows}

## Evidence and continuation

- [New complete source screening](SCREENING.md)
- [Post-correction sample and exact hashes](POST-CORRECTION-CHECK.md)
- [Correction evidence and guarded decisions](../../sources/CORRECTION-EVIDENCE.md)
- [Original findings and sample](../{old}/BATCH-001.md)
- [Original editorial rubric](../{old}/RUBRIC.md)
- [Source reliability assessment](../{old}/RELIABILITY.md)
- [Historical demonstration derivatives](../{old}/PATCHES.md)
- [Accepted dictionary](../../CURRENT.md)

Reproduce the figures and report after accepted-edition screening and the post-correction check:

```sh
python3 -m scripts.correction_report --root .
```

This command refuses stale manifest/edition anchors, impossible counters, different source totals, changed sample bytes or a sample that does not match the original reviewed headwords. It does not rerun the full audit or perform an independent linguistic certification.
'''
    figures(review, manifest, screening, post)
    atomic_write(review / 'CORRECTIONS.md', document(meta, body))
    dashboard = f'''# Dictionary corrections and review progress

**The corrected edition has {m['headwords']:,} English Markdown entries; {manifest['corrected_headwords']:,} have counted reading-view changes. Full readback verification and source screening are complete.**

The original review sample was checked again: **{post['checked_headwords']:,} headwords, {post['checked_records']:,} records and {post['checked_senses']:,} sense positions**. No whole-corpus linguistic approval is claimed. **{m['senses_without_nonblank_gloss']:,} source senses still lack a usable definition.**

![Updated progress, in Portuguese]({prefix}/assets/progress-pt.svg)

![Applied corrections and remaining gaps, in Portuguese]({prefix}/assets/corrections-pt.svg)

The figures are the user's requested Portuguese exception. Entries, indexes, evidence and reports remain English Markdown. Counts describe source-preserving corrections and navigation, not Cambridge/Oxford certification.

- [Complete correction report and F01–F12 dispositions]({prefix}/CORRECTIONS.md)
- [Current screening and unresolved-definition queue]({prefix}/SCREENING.md)
- [Post-correction check: exact entry hashes]({prefix}/POST-CORRECTION-CHECK.md)
- [Source evidence for guarded editorial changes](sources/CORRECTION-EVIDENCE.md)
- [Original review findings](reviews/{old}/BATCH-001.md)
- [Cambridge/Oxford inspired rubric](reviews/{old}/RUBRIC.md)
- [Source reliability and limitations](reviews/{old}/RELIABILITY.md)
- [Browse the accepted dictionary](CURRENT.md)

Source topics and raw context have been recovered; table-control forms, selected source mistakes and misleading presentation have been corrected. Local links open existing exact headwords. Missing definitions, sense-aware link alignment, wider relation curation and historical/linguistic verification remain pending.

Run `python3 -m scripts.correction_report --root .` to reproduce this dashboard and its SVG figures from the accepted evidence. PNG copies are supporting presentation assets when generated; they are not a separate data source.
'''
    atomic_write(root / 'REVIEW.md', document({**meta, 'kind': 'linguistic-review-progress'}, dashboard))
    print(f'Built {review / "CORRECTIONS.md"}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    build(parser.parse_args().root.resolve())


if __name__ == '__main__':
    main()
