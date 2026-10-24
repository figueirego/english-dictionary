"""Screen immutable Markdown source projections without certifying their language."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import itertools
from pathlib import Path
import re
import tempfile
import time

from scripts.entries import _text
from scripts.full_pipeline import (
    active, encode, file_hash, full_entry_path, partition_for, partition_rows, read_md,
)
from scripts.full_projection import display_headword
from scripts.markdown import atomic_write, document

VERSION = 1
PAGE_SIZE = 100
SAMPLE_LIMIT = 100
MARKUP = re.compile(r'\{\{[^{}\n]{1,200}\}\}|\[\[[^\[\]\n]{1,200}\]\]')
ERROR_TEXT = re.compile(r'\b(?:Lua error|Script error|Module error|Template loop detected)\b', re.I)
FRAGMENT_IPA = re.compile(r'[-–][/\]]$')
METRIC_KEYS = (
    'partitions', 'headwords', 'accepted_records', 'senses',
    'records_with_source_gaps', 'headwords_with_source_gaps',
    'records_without_senses', 'senses_with_gloss_gaps', 'senses_without_nonblank_gloss',
    'records_with_ipa', 'headwords_with_ipa', 'records_with_etymology',
    'headwords_with_etymology', 'records_with_examples', 'headwords_with_examples',
    'senses_with_examples', 'eligible_examples', 'linked_senses',
    'unlinked_gloss_senses', 'unlinked_glossless_senses', 'all_linked_records',
    'records_with_unlinked_gloss_senses', 'missing_definition_findings',
    'template_residue_findings', 'extractor_error_findings', 'duplicate_sense_findings',
    'display_artifact_findings', 'pronunciation_fragment_findings', 'etymology_excerpt_findings',
)


def fingerprint(record: dict) -> str:
    """Match the edition's projected-record hash, including upstream provenance."""
    return hashlib.sha256(encode(record).encode()).hexdigest()


def gloss_gap(sense: dict) -> str | None:
    if 'glosses' not in sense:
        return 'absent'
    glosses = sense['glosses']
    if not glosses:
        return 'empty'
    if any(not value.strip() for value in glosses):
        return 'partially-blank' if any(value.strip() for value in glosses) else 'blank'
    return None


def related_sense(sense: dict) -> bool:
    return bool(sense.get('form_of') or sense.get('alt_of')
                or set(sense.get('tags', ())) & {'form-of', 'alt-of'})


def eligible_examples(sense: dict) -> list[dict]:
    """Keep the same explicit eligibility boundary as the source projection."""
    return [example for example in sense.get('examples', ())
            if example.get('type') == 'example' and example.get('text', '').strip()
            and not any(key in example for key in ('ref', 'translation', 'translations'))]


def screen_record(record: dict) -> tuple[Counter, list[dict]]:
    """Return measured availability and review candidates; no semantic verdicts."""
    counts = Counter(accepted_records=1)
    findings = []
    senses = record.get('senses', [])
    counts['senses'] = len(senses)
    counts['records_with_ipa'] = int(any(sound.get('ipa', '').strip()
                                         for sound in record.get('sounds', ())))
    counts['records_with_etymology'] = int(bool(record.get('etymology_text', '').strip()))
    for index, form in enumerate(record.get('forms', ()), 1):
        if form.get('form', '').casefold() in {'usage notes', 'further reading'} and 'alternative' in form.get('tags', ()):
            findings.append({'check': 'display-artifact', 'sense_index': None,
                             'field': f'forms[{index}]', 'reason': 'section-heading-as-alternative-form',
                             'excerpt': form['form']})
    for index, sound in enumerate(record.get('sounds', ()), 1):
        if FRAGMENT_IPA.search(sound.get('ipa', '')):
            findings.append({'check': 'pronunciation-fragment', 'sense_index': None,
                             'field': f'sounds[{index}].ipa', 'reason': 'trailing-hyphen-before-closing-delimiter',
                             'excerpt': sound['ipa']})
    etymology = record.get('etymology_text', '')
    if len(etymology) >= 100 and ('[…]' in etymology or '[...]' in etymology):
        findings.append({'check': 'etymology-excerpt', 'sense_index': None,
                         'field': 'etymology_text', 'reason': 'bracketed-ellipsis-in-long-etymology',
                         'excerpt': 'Inspect the preserved etymology for an embedded attributed passage.'})
    if not senses:
        counts['records_without_senses'] = 1
        findings.append({'check': 'missing-definition', 'sense_index': None,
                         'field': 'senses', 'reason': record.get('senses_status', 'empty')})
    seen_senses: dict[str, int] = {}
    for number, sense in enumerate(senses, 1):
        gap = gloss_gap(sense)
        if gap:
            counts['senses_with_gloss_gaps'] += 1
            findings.append({'check': 'missing-definition', 'sense_index': number,
                             'field': 'glosses', 'reason': gap})
        nonblank = any(text.strip() for text in sense.get('glosses', ()))
        counts['senses_without_nonblank_gloss'] += not nonblank
        if related_sense(sense):
            counts['linked_senses'] += 1
        elif nonblank:
            counts['unlinked_gloss_senses'] += 1
        else:
            counts['unlinked_glossless_senses'] += 1
        examples = eligible_examples(sense)
        counts['eligible_examples'] += len(examples)
        counts['senses_with_examples'] += bool(examples)
        # Inspect the leaf gloss, not its contextual parent glosses repeated by
        # the extractor. Example and etymology text may intentionally quote syntax.
        leaf = next((text for text in reversed(sense.get('glosses', ())) if text.strip()), '')
        if MARKUP.search(leaf):
            findings.append({'check': 'template-residue', 'sense_index': number,
                             'field': 'leaf_gloss', 'reason': 'template-or-wikilink-delimiters',
                             'excerpt': leaf[:240]})
        if 'Wikipedia.Wikipedia' in leaf:
            findings.append({'check': 'display-artifact', 'sense_index': number,
                             'field': 'leaf_gloss', 'reason': 'duplicated-wikipedia-link-label',
                             'excerpt': leaf[:240]})
        tags = [tag for tag in sense.get('tags', ()) if tag.startswith('error-')]
        if tags or ERROR_TEXT.search(leaf):
            findings.append({'check': 'extractor-error', 'sense_index': number,
                             'field': 'tags-or-leaf-gloss',
                             'reason': ', '.join(tags) if tags else 'literal-error-marker',
                             'excerpt': leaf[:240]})
        # Exact entire sense objects only. Shared glosses with distinct labels,
        # examples or links must not be mistaken for duplicate senses.
        sense_key = encode(sense)
        if sense_key in seen_senses:
            findings.append({'check': 'duplicate-sense', 'sense_index': number,
                             'field': 'sense', 'reason': f'exact-match-to-sense-{seen_senses[sense_key]}'})
        else:
            seen_senses[sense_key] = number
    counts['records_with_source_gaps'] = int(bool(counts['records_without_senses']
                                                or counts['senses_with_gloss_gaps']))
    counts['records_with_examples'] = int(bool(counts['senses_with_examples']))
    counts['all_linked_records'] = int(bool(senses) and counts['linked_senses'] == len(senses))
    counts['records_with_unlinked_gloss_senses'] = int(bool(counts['unlinked_gloss_senses']))
    for finding in findings:
        counts[finding['check'].replace('-', '_') + '_findings'] += 1
    return counts, findings


def verified_partition(edition: Path, bucket: str, expected_hash: str) -> tuple[dict, list[dict]]:
    if not re.fullmatch(r'[0-3][0-9a-f]{2}', bucket):
        raise ValueError('Invalid source partition identity')
    manifest_path = edition / 'manifests' / f'{bucket}.md'
    if file_hash(manifest_path) != expected_hash:
        raise ValueError('Partition manifest checksum mismatch')
    manifest, _ = read_md(manifest_path)
    source_path = edition / 'sources' / f'{bucket}.md'
    if file_hash(source_path) != manifest['source_sha256']:
        raise ValueError('Source partition file changed')
    metadata, rows = partition_rows(source_path)
    if metadata['partition'] != bucket or manifest['partition'] != bucket:
        raise ValueError('Source partition identity mismatch')
    return manifest, rows


class QueueWriter:
    """Spool complete missing-definition findings in bounded Markdown pages."""

    def __init__(self, folder: Path, edition_id: str):
        self.folder = folder
        self.edition_id = edition_id
        self.pending: list[dict] = []
        self.pages: list[tuple[str, int]] = []

    def add(self, finding: dict) -> None:
        self.pending.append(finding)
        if len(self.pending) == PAGE_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        name = f'{len(self.pages) + 1:04d}.md'
        metadata = {'kind': 'missing-definition-review-queue', 'edition': self.edition_id,
                    'screening_version': VERSION, 'findings': self.pending}
        body = ['# Missing-definition review queue', '', '[Queue index](INDEX.md)', '',
                'Every item is a source gap requiring inspection, not a generated correction.', '',
                'Record and sense indexes are one-based within the exact headword and source record. '
                '`null` sense index means that the record has no senses.', '']
        for finding in self.pending:
            relative = '../../../editions/' + self.edition_id + '/' + finding['entry_path']
            body += [f"## {_text(display_headword(finding['headword']))}", '',
                     f'[Accepted entry]({relative}) · Record {finding["record_index"]} · '
                     f'Sense {finding["sense_index"] or "not supplied"}', '',
                     f'Observed: {finding["field"]} — {finding["reason"]}.', '',
                     f'Projected record SHA-256: `{finding["projected_record_sha256"]}`.', '',
                     f'Upstream line: {finding["upstream_line"]}; '
                     f'upstream record SHA-256: `{finding["upstream_record_sha256"]}`.', '']
        atomic_write(self.folder / name, document(metadata, '\n'.join(body)))
        self.pages.append((name, len(self.pending)))
        self.pending = []

    def finish(self) -> None:
        self.flush()
        total = sum(count for _, count in self.pages)
        rows = ['# Missing-definition review queue', '', '[Screening report](../SCREENING.md)', '',
                f'{total:,} findings. No definitions have been filled in automatically.', '',
                '| Page | Findings |', '| --- | ---: |']
        rows += [f'| [Page {index}]({name}) | {count} |'
                 for index, (name, count) in enumerate(self.pages, 1)]
        atomic_write(self.folder / 'INDEX.md', document(
            {'kind': 'missing-definition-queue-index', 'edition': self.edition_id,
             'findings': total, 'pages': len(self.pages)}, '\n'.join(rows)))


def scoped_finding(finding: dict, record: dict, record_index: int, entry_hash: str) -> dict:
    return {**finding, 'headword': record['word'], 'record_index': record_index,
            'entry_path': str(full_entry_path(record['word'])),
            'accepted_entry_sha256': entry_hash, 'projected_record_sha256': fingerprint(record),
            'upstream_record_sha256': record['upstream_record_sha256'],
            'upstream_line': record['upstream_line']}


def rate(value: int, total: int) -> str:
    return f'{value:,} / {total:,} ({value / total * 100:.2f}%)' if total else 'No eligible units'


def report_body(metrics: dict) -> str:
    records, words, senses = (metrics[name] for name in ('accepted_records', 'headwords', 'senses'))
    rows = ['# Full-corpus linguistic screening', '',
            '**Complete deterministic screening; semantic accuracy is not certified.**', '',
            f'The scan traversed {metrics["partitions"]:,} accepted Markdown source partitions, '
            f'{records:,} lexical records, {words:,} exact headwords and {senses:,} source senses. '
            'All four counts reconcile with the accepted edition manifest.', '',
            'This run rechecked the master-to-partition-manifest, source-file and embedded-payload '
            'hash chains. It did not repeat the earlier full rendering audit of every entry file. '
            'Queue entry hashes are those recorded by that accepted manifest.', '',
            '## Rubric and denominators', '',
            'The criteria were fixed before the scan: preserve source identity; expose absent '
            'definitions; measure pronunciation, etymology and examples separately; distinguish '
            'inflection/variant relations; send suspicious syntax to a review queue. '
            'The ECC evaluation approach separates these code-based checks from the model-assisted '
            'review and any future human lexicographic review.', '',
            'A lexical record is one source POS/etymology grouping. A headword is an exact spelling '
            'and may contain several records. A sense is a source sense object; parent glosses '
            'are not counted as extra senses. Optional field availability is not a quality score.', '',
            '## Field availability', '',
            '| Field | Lexical records with field | Headwords with field in at least one record |',
            '| --- | ---: | ---: |']
    for label, key in [('IPA transcription', 'ipa'), ('Etymology text', 'etymology'),
                       ('Eligible source example', 'examples')]:
        rows.append(f'| {label} | {rate(metrics[f"records_with_{key}"], records)} | '
                    f'{rate(metrics[f"headwords_with_{key}"], words)} |')
    rows += ['', f'Senses with eligible examples: {rate(metrics["senses_with_examples"], senses)}. '
             f'Eligible example objects: {metrics["eligible_examples"]:,}.', '',
             'Presence means nonblank supplied text. IPA validity, accent assignment, etymological '
             'accuracy and example naturalness have not been certified. Examples must have '
             '`type=example`, nonblank text, no external reference and no translation fields. '
             'This filter establishes field eligibility, not verified authorship or provenance '
             'of every sentence. Omitted referenced quotations are outside this measurement.', '',
             '## Source gaps requiring review', '',
             f'- Affected lexical records: {rate(metrics["records_with_source_gaps"], records)}.',
             f'- Affected exact headwords: {rate(metrics["headwords_with_source_gaps"], words)}.',
             f'- Records without senses: {metrics["records_without_senses"]:,}.',
             f'- Senses with absent, empty or partly/wholly blank gloss arrays: '
             f'{metrics["senses_with_gloss_gaps"]:,}.',
             f'- Senses with no nonblank gloss at all: {metrics["senses_without_nonblank_gloss"]:,}.',
             f'- [Exhaustive missing-definition queue](missing-definitions/INDEX.md): '
             f'{metrics["missing_definition_findings"]:,} findings.', '',
             'A partly blank gloss can coexist with usable text. A source gap can originate in '
             'the Wiktionary page, extraction or field projection; this scan alone cannot assign '
             'its cause. No missing definition, IPA or example was invented or patched.', '',
             '## Forms and variants', '',
             '| Mutually exclusive source-sense category | Senses |', '| --- | ---: |',
             f'| Supplied form/variant relation or explicit form-of/alt-of tag | {metrics["linked_senses"]:,} |',
             f'| Nonblank gloss without the above relation/tag | {metrics["unlinked_gloss_senses"]:,} |',
             f'| No relation/tag and no nonblank gloss | {metrics["unlinked_glossless_senses"]:,} |', '',
             f'{metrics["all_linked_records"]:,} records have at least one sense and every sense '
             f'linked as a form or variant. {metrics["records_with_unlinked_gloss_senses"]:,} records '
             'have at least one nonblank gloss without those links. These record categories can '
             'leave gap-only records outside either category; they are not counts of unique lemmas.', '',
             'Form-only records are valid lexical material. Missing relation metadata does not '
             'prove that a word is a lemma, and a linked sense can contain additional information.', '',
             '## Lower-priority heuristic candidates', '',
             '| Check | Total findings | Interpretation |', '| --- | ---: | --- |',
             f'| Template/wikilink delimiters in leaf gloss | {metrics["template_residue_findings"]:,} | Literal syntax may be intentional. |',
             f'| error-* sense tags or explicit Lua/Script/Module error text | {metrics["extractor_error_findings"]:,} | Investigate source and extraction; not proof of a bad definition. |',
             f'| Identical complete sense objects within one record | {metrics["duplicate_sense_findings"]:,} | Possible duplicate; no automatic merge. |',
             f'| Duplicated Wikipedia label or section heading marked as an alternative form | {metrics["display_artifact_findings"]:,} | Possible extraction/display residue. |',
             f'| IPA ending in a hyphen before its closing delimiter | {metrics["pronunciation_fragment_findings"]:,} | Possibly intentional abbreviated notation; not automatically invalid IPA. |',
             f'| Etymology of at least 100 characters containing a bracketed ellipsis | {metrics["etymology_excerpt_findings"]:,} | Inspect possible embedded excerpts and attribution; no copyright conclusion. |', '',
             f'[Heuristic samples](HEURISTIC-SAMPLES.md) keep the first {SAMPLE_LIMIT} candidates '
             'per check in deterministic partition, headword, record and sense order. Their totals '
             'are exhaustive; the displayed samples are bounded and are not random.', '',
             '## What remains unverified', '',
             '- Semantic accuracy, sense completeness, POS correctness and historical claims.',
             '- Actual language of every gloss/example. `lang_code=en` is the source label, '
             'not a language-detection certificate; etymologies can legitimately cite other languages.',
             '- Pronunciation validity, dialect/usage labels, natural examples and learner suitability.',
             '- Equivalence to Cambridge or Oxford editorial review. Their principles inform '
             'the review rubric; their proprietary entries were not imported.', '',
             'No overall language coverage or linguistic approval percentage is derived from '
             'field presence, hash matches or automated flags. Imported entries remain unchanged.', '',
             '## Reproduce', '',
             '```sh', 'python3 -m scripts.linguistic_screen --root .', '```', '',
             'The standard-library scan holds one source partition and its manifest at a time, '
             f'spools missing-definition findings in pages of {PAGE_SIZE}, and caps heuristic '
             f'samples at {SAMPLE_LIMIT} per check. It reads canonical Markdown, not the SQLite '
             'ingestion cache. It makes no network/model calls.']
    return '\n'.join(rows)


def screen(root: Path) -> dict:
    root = root.resolve()
    edition = active(root)
    master_path = edition / 'MANIFEST.md'
    master_hash = file_hash(master_path)
    master, _ = read_md(master_path)
    if master.get('edition') != edition.name:
        raise ValueError('Accepted edition identity mismatch')
    expected = set(master['partitions'])
    for folder in ('sources', 'manifests'):
        if {path.name for path in (edition / folder).iterdir()} != {key + '.md' for key in expected}:
            raise ValueError('Unexpected or missing source partitions')
    counts = Counter({key: 0 for key in METRIC_KEYS})
    samples: dict[str, list[dict]] = {
        'template-residue': [], 'extractor-error': [], 'duplicate-sense': [],
        'display-artifact': [], 'pronunciation-fragment': [], 'etymology-excerpt': [],
    }
    cache = root / '.cache'
    cache.mkdir(exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='linguistic-screen-', dir=cache) as temporary:
        stage = Path(temporary)
        queue = QueueWriter(stage / 'missing-definitions', edition.name)
        for bucket in sorted(expected):
            manifest, rows = verified_partition(edition, bucket, master['partitions'][bucket])
            local = Counter()
            seen: set[str] = set()
            for word, group in itertools.groupby(rows, key=lambda record: record['word']):
                if word in seen or partition_for(word) != bucket:
                    raise ValueError('Repeated or misplaced headword group')
                seen.add(word)
                local['headwords'] += 1
                flags = Counter()
                entry_hash = manifest['files'].get(str(full_entry_path(word)))
                if entry_hash is None:
                    raise ValueError('Source headword missing from accepted entry manifest')
                for index, record in enumerate(group, 1):
                    if record.get('lang_code') != 'en':
                        raise ValueError('Unexpected source language')
                    stats, findings = screen_record(record)
                    local.update(stats)
                    flags.update({key: bool(stats['records_with_' + key])
                                  for key in ('ipa', 'etymology', 'examples', 'source_gaps')})
                    for finding in findings:
                        if finding['check'] == 'missing-definition':
                            queue.add(scoped_finding(finding, record, index, entry_hash))
                        elif len(samples[finding['check']]) < SAMPLE_LIMIT:
                            samples[finding['check']].append(scoped_finding(finding, record, index, entry_hash))
                local.update({'headwords_with_' + key: int(bool(value)) for key, value in flags.items()})
            for key in ('headwords', 'accepted_records', 'senses'):
                if local[key] != manifest[key]:
                    raise ValueError('Screening partition counts do not reconcile')
            counts.update(local)
            counts['partitions'] += 1
            if counts['partitions'] % 128 == 0:
                print(encode({'phase': 'linguistic-screening', 'partitions': counts['partitions'],
                              'records': counts['accepted_records']}), flush=True)
        if file_hash(master_path) != master_hash:
            raise ValueError('Accepted master manifest changed during screening')
        for key in ('headwords', 'accepted_records', 'senses'):
            if counts[key] != master[key] or counts[key] != master['inventory'][key]:
                raise ValueError('Screening corpus counts do not reconcile')
        if counts['records_with_source_gaps'] != master['inventory']['records_with_source_gaps']:
            raise ValueError('Source gap count differs from accepted inventory')
        if counts['linked_senses'] + counts['unlinked_gloss_senses'] + counts['unlinked_glossless_senses'] != counts['senses']:
            raise ValueError('Sense classification does not reconcile')
        queue.finish()
        sample_rows = ['# Heuristic review candidates', '', '[Screening report](SCREENING.md)', '',
                       'These are unconfirmed candidates. No entry was changed or automatically rejected. '
                       'The metadata preserves edition-scoped record/sense identities and accepted entry hashes.', '']
        for check, findings in samples.items():
            sample_rows += [f'## {check}', '', f'{len(findings)} displayed candidates.', '']
            for finding in findings:
                link = '../../editions/' + edition.name + '/' + finding['entry_path']
                sample_rows += [f'- [{_text(display_headword(finding["headword"]))}]({link}), '
                                f'record {finding["record_index"]}, sense {finding["sense_index"]}: '
                                f'{_text(finding["reason"])}.']
                if finding.get('excerpt'):
                    sample_rows += ['  ' + _text(finding['excerpt'])]
            sample_rows += ['']
        atomic_write(stage / 'HEURISTIC-SAMPLES.md', document(
            {'kind': 'linguistic-heuristic-samples', 'edition': edition.name,
             'screening_version': VERSION, 'samples_per_check_limit': SAMPLE_LIMIT,
             'samples': samples}, '\n'.join(sample_rows)))
        metadata = {'kind': 'linguistic-screening-report', 'edition': edition.name,
                    'screening_version': VERSION, 'status': 'complete-deterministic-screening',
                    'master_manifest_sha256': master_hash, 'script_sha256': file_hash(Path(__file__)),
                    'completed_at_utc': datetime.now(timezone.utc).isoformat(),
                    'elapsed_seconds': round(time.monotonic() - started, 3),
                    'metrics': dict(counts), 'human_lexicographic_approval': False,
                    'language_detection_performed': False, 'entries_modified': 0}
        atomic_write(stage / 'SCREENING.md', document(metadata, report_body(dict(counts))))
        destination = root / 'reviews' / edition.name
        # Publish generated queue files before its final completion report. Other
        # editorial review files in this shared directory are not touched.
        for path in sorted(stage.rglob('*.md')):
            if path.name != 'SCREENING.md':
                atomic_write(destination / path.relative_to(stage), path.read_text(encoding='utf-8'))
        atomic_write(destination / 'SCREENING.md', (stage / 'SCREENING.md').read_text(encoding='utf-8'))
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(encode(screen(args.root)))


if __name__ == '__main__':
    main()
