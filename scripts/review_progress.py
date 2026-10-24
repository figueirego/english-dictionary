"""Build a Markdown review dashboard and factual SVG figures from review evidence."""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

from scripts.full_pipeline import active, file_hash, read_md
from scripts.markdown import atomic_write, document

BG, PANEL, TEXT, MUTED = '#0d1117', '#161d27', '#f0f4fa', '#a4b1c3'
BLUE, GREEN, AMBER, LINE = '#58a6ff', '#3fb950', '#e8b45e', '#293442'


def evidence(root: Path) -> tuple[Path, dict, dict, dict]:
    edition = active(root)
    review = root / 'reviews' / edition.name
    manifest, _ = read_md(edition / 'MANIFEST.md')
    screening, _ = read_md(review / 'SCREENING.md')
    batch, _ = read_md(review / 'BATCH-001.md')
    if screening['edition'] != edition.name or batch['edition'] != edition.name:
        raise ValueError('Review evidence targets another edition')
    if screening['status'] != 'complete-deterministic-screening':
        raise ValueError('Screening has not completed')
    if screening['master_manifest_sha256'] != file_hash(edition / 'MANIFEST.md'):
        raise ValueError('Screening manifest anchor has changed')
    metrics = screening['metrics']
    for key in ('headwords', 'accepted_records', 'senses'):
        if metrics[key] != manifest[key]:
            raise ValueError(f'Screening denominator mismatch: {key}')
    if metrics['partitions'] != len(manifest['partitions']):
        raise ValueError('Screening partition count mismatch')
    words, records, senses = set(), 0, 0
    for item in batch['entries']:
        path = (root / item['path']).resolve()
        if not path.is_relative_to((edition / 'entries').resolve()):
            raise ValueError('Review entry must belong to the accepted edition')
        if file_hash(path) != item['sha256']:
            raise ValueError('Review entry hash mismatch')
        meta, _ = read_md(path)
        if meta['headword'] != item['headword'] or item['headword'] in words:
            raise ValueError('Review headword identity mismatch or duplicate')
        if (meta['source_records'], meta['sense_count']) != (item['records'], item['senses']):
            raise ValueError('Review entry counts mismatch')
        words.add(item['headword'])
        records += item['records']
        senses += item['senses']
    if (len(words), records, senses) != (batch['headwords_reviewed'], batch['records_reviewed'], batch['senses_examined']):
        raise ValueError('Review batch totals mismatch')
    if len(words) > metrics['headwords'] or senses > metrics['senses']:
        raise ValueError('Review exceeds corpus denominator')
    return review, manifest, screening, batch


class Figure:
    def __init__(self, height: int, title: str, subtitle: str):
        self.height = height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="{height}" viewBox="0 0 1400 {height}" role="img" aria-label="{escape(title, quote=True)}">',
                      f'<rect width="1400" height="{height}" fill="{BG}"/>',
                      '<g font-family="Arial, Helvetica, sans-serif">']
        self.text(56, 55, 'ENGLISH DICTIONARY  /  LOCAL REVIEW', 15, BLUE, bold=True)
        self.text(56, 105, title, 35, TEXT, bold=True)
        self.text(56, 142, subtitle, 19, MUTED)

    def text(self, x: int, y: int, value: str, size: int = 20, color: str = TEXT, bold: bool = False, right: bool = False):
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{700 if bold else 400}" text-anchor="{"end" if right else "start"}">{escape(value)}</text>')

    def rect(self, x, y, width, height, color=PANEL, radius=10):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" fill="{color}"/>')

    def bar(self, y: int, label: str, note: str, done: int, total: int, color: str = BLUE, precision: int = 1):
        if not 0 <= done <= total or total <= 0:
            raise ValueError(f'Invalid chart denominator for {label}')
        self.text(56, y, label, 22, TEXT, bold=True)
        self.text(1344, y, f'{done:,} / {total:,}  ·  {100*done/total:.{precision}f}%', 20, color, right=True)
        self.text(56, y + 30, note, 17, MUTED)
        self.rect(56, y + 48, 1288, 13, LINE, 6)
        if done:
            self.rect(56, y + 48, 1288 * done / total, 13, color, 6)

    def finish(self) -> str:
        return '\n'.join(self.parts + ['</g></svg>'])


def figures(review: Path, manifest: dict, screening: dict, batch: dict) -> None:
    m = screening['metrics']
    fig = Figure(1060, 'What has passed — and what remains', 'Evidence for the pinned Wiktionary snapshot · 22 October 2026 · English Markdown')
    for x, value, label in [(56, f"{m['headwords']:,}", 'headword Markdown files'), (493, f"{m['accepted_records']:,}", 'English lexical records'), (930, f"{m['senses']:,}", 'source senses')]:
        fig.rect(x, 180, 414, 115)
        fig.text(x + 24, 230, value, 36, TEXT, bold=True)
        fig.text(x + 24, 266, label, 18, MUTED)
    fig.bar(345, '01  Source import', 'Every eligible English record in this snapshot was imported.', m['accepted_records'], manifest['accepted_records'], GREEN)
    fig.bar(455, '02  Accepted-edition integrity audit', 'Recorded full readback: entry bytes, rendering, counts and navigation.', m['partitions'], len(manifest['partitions']), GREEN)
    fig.bar(565, '03  Deterministic linguistic screening', 'Field coverage and anomaly rules; this is not a semantic accuracy score.', m['headwords'], manifest['headwords'], BLUE)
    fig.bar(675, '04  AI-assisted editorial examination', f"{batch['records_reviewed']:,} records / {batch['senses_examined']:,} senses examined; purposive sample, findings remain open.", batch['headwords_reviewed'], manifest['headwords'], AMBER, 4)
    fig.rect(56, 790, 1288, 115)
    fig.text(80, 831, 'Human lexicographer approval: not performed', 23, AMBER, bold=True)
    fig.text(80, 868, 'No Cambridge / Oxford certification. No estimate of whole-corpus linguistic accuracy.', 19, MUTED)
    fig.text(56, 957, '100% refers to this source snapshot or automated scan — never to all words in English.', 18, MUTED)
    fig.text(56, 995, f"Edition {manifest['edition']} · Sources: MANIFEST.md, SCREENING.md, BATCH-001.md", 15, MUTED)
    atomic_write(review / 'assets' / 'progress.svg', fig.finish())

    fig = Figure(1120, 'How complete are the imported fields?', 'Measured field presence across the entire accepted source projection; presence does not prove correctness.')
    fig.bar(215, 'Headwords with at least one IPA transcription', 'Other records or senses under the same headword may still lack pronunciation.', m['headwords_with_ipa'], m['headwords'], BLUE)
    fig.bar(325, 'Headwords with some etymology text', 'Historical claims and embedded quotations still require evidence review.', m['headwords_with_etymology'], m['headwords'], BLUE)
    fig.bar(435, 'Senses with an eligible source example', 'Only source-labelled examples without external references or translation fields are counted.', m['senses_with_examples'], m['senses'], BLUE)
    fig.bar(545, 'Senses with a nonblank gloss', 'Includes definitions of forms and variants. Readability and meaning need separate review.', m['senses']-m['senses_without_nonblank_gloss'], m['senses'], BLUE)
    fig.rect(56, 650, 1288, 118)
    fig.text(80, 694, f"{m['records_with_source_gaps']:,} records have missing or incomplete sense/gloss data", 25, AMBER, bold=True)
    fig.text(80, 734, 'All are placed in a traceable Markdown review queue. Missing optional fields are not automatically errors.', 18, MUTED)
    fig.text(56, 825, 'Trust assessment', 24, TEXT, bold=True)
    for y, title, note, color in [
        (869, 'Traceable download', 'Pinned source, extraction versions and local byte hashes.', GREEN),
        (923, 'Conversion has known omissions', 'Observed topic/context labels were lost; extraction residue remains.', AMBER),
        (977, 'Linguistic correctness is not certified', 'Crowdsourced source; a small review batch cannot certify the corpus.', AMBER),
    ]:
        fig.text(56, y, title, 20, color, bold=True)
        fig.text(470, y, note, 18, MUTED)
    fig.text(56, 1063, 'Exact counts and definitions: SCREENING.md · Findings: BATCH-001.md · Source assessment: RELIABILITY.md', 16, MUTED)
    atomic_write(review / 'assets' / 'quality.svg', fig.finish())


def build(root: Path) -> None:
    review, manifest, screening, batch = evidence(root)
    m = screening['metrics']
    prefix = review.relative_to(root).as_posix()
    meta = {'kind': 'linguistic-review-progress', 'edition': manifest['edition'],
            'screened_headwords': m['headwords'], 'headwords_examined_by_ai': batch['headwords_reviewed'],
            'records_examined_by_ai': batch['records_reviewed'], 'senses_examined_by_ai': batch['senses_examined'],
            'human_lexicographic_approval': False, 'master_manifest_sha256': screening['master_manifest_sha256'],
            'screening_report_sha256': file_hash(review/'SCREENING.md'), 'batch_report_sha256': file_hash(review/'BATCH-001.md')}
    body = f'''# Linguistic review and source reliability

**Full automated screening completed. The first AI-assisted editorial batch examined {batch['headwords_reviewed']:,} headwords, {batch['records_reviewed']:,} records and {batch['senses_examined']:,} senses. Human lexicographer approval has not been performed.**

The download is traceable and its local integrity was verified. The review found real
source and projection problems, so the collection is not yet a fully edited learner's
dictionary or an equivalent of Cambridge or the OED. No overall accuracy score is justified.

## Measured progress

| Stage | Verified scope | Interpretation |
| --- | ---: | --- |
| Complete import | {m['headwords']:,} headword Markdown files | All eligible English records in the pinned snapshot |
| Original complete readback audit | {m['partitions']:,} / {len(manifest['partitions']):,} partitions | Recorded before promotion; not rerun for this review |
| New deterministic screening | {m['accepted_records']:,} records / {m['senses']:,} senses | All canonical source projections rehashed and scanned |
| AI-assisted editorial examination | {batch['headwords_reviewed']:,} headwords / {batch['senses_examined']:,} senses | Purposive, non-random sample; not population accuracy |
| Missing/incomplete definition records | {m['records_with_source_gaps']:,} | Tracked as unresolved source gaps |
| Human lexicographer approval | None recorded | Pending |

![Measured import and review progress]({prefix}/assets/progress.svg)

![Field coverage and trust assessment]({prefix}/assets/quality.svg)

PNG copies of both figures are provided in the same assets folder for viewing and sharing.
Figures are generated from actual Markdown evidence, not illustrative mock-ups. SVG/PNG
are supporting visual assets; dictionary entries and review records remain English Markdown.

## Findings that affect readiness

- Some source domain and grammatical-context labels were omitted by the field projection.
- `color` retains extraction residue; `hello` contains partial IPA strings requiring interpretation.
- `repository` has an actual unglossed source sense. No definition has been invented to fill it.
- Quotations embedded in etymology prose are not removed by the sense-example quotation filter.
  Older edition footer wording overstates this exclusion; current documentation corrects it.
- Optional field coverage varies. A missing example or etymology is a gap in presentation,
  not proof that the word or definition is incorrect.

See the hash-anchored ledger for exact locations and dispositions. Review notes and
any editorial derivatives are separate from the immutable accepted edition. A reviewed
sense is an examined unit, not automatically an approved unit. The full batch has open findings.

## Evidence and navigation

- [Cambridge / Oxford inspired editorial rubric]({prefix}/RUBRIC.md)
- [Source reliability and limitations]({prefix}/RELIABILITY.md)
- [Complete screening, counters and review queues]({prefix}/SCREENING.md)
- [Batch 001: exact entries, hashes and findings]({prefix}/BATCH-001.md)
- [Concrete editorial changes and derivative examples]({prefix}/PATCHES.md)
- [Browse the accepted dictionary](CURRENT.md)
- [Import and audit evidence](WORKLOG.md)

## Reproduce and continue

```sh
python3 -m scripts.linguistic_screen --root .
python3 -m scripts.review_progress --root .
```

The dashboard refuses stale edition anchors, mismatched totals or changed reviewed
entry hashes. Its SVG figures use only the Python standard library. PNG previews are
derived presentation assets; the SVG and Markdown remain the reproducible evidence.
Continue by resolving the documented projection/context losses in a separately audited
edition and expanding the review with a predeclared sampling design. Do not infer
universal correctness, missing definitions, CEFR levels or historical dates from this batch.
'''
    figures(review, manifest, screening, batch)
    atomic_write(root/'REVIEW.md', document(meta, body))
    progress = root/'PROGRESS.md'
    text = progress.read_text(encoding='utf-8').split('\n<!-- linguistic-review -->')[0].rstrip()
    text += f'\n\n<!-- linguistic-review -->\n## Linguistic review\n\nFull deterministic screening: {m["headwords"]:,} headwords. AI-assisted examination: {batch["headwords_reviewed"]:,} headwords / {batch["senses_examined"]:,} senses. Human lexicographer approval: pending.\n\n[Review dashboard, figures and findings](REVIEW.md).\n'
    atomic_write(progress, text)
    print(f'Built {root / "REVIEW.md"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    build(parser.parse_args().root.resolve())


if __name__ == '__main__':
    main()
