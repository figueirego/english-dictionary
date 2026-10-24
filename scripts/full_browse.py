"""Build bounded, human-readable alphabetical navigation for a full edition."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import string
from pathlib import Path

from scripts.entries import _text
from scripts.full_projection import entry_path, display_headword
from scripts.markdown import document


PAGE_SIZE = 1000
INITIALS = (*string.ascii_lowercase, '_')


def build_browse(root: Path, edition: Path) -> tuple[str, dict[str, str]]:
    """Return edition-index navigation and exact-byte hashes of its Markdown pages.

    The database performs sorting on disk. Python retains only one unfinished page
    per initial and a small descriptor per completed page, never all headwords.
    """
    # Imported lazily because the full pipeline calls this module during assembly.
    from scripts.full_pipeline import checked_write, partition_for

    database_path = root / '.cache/full-import/source.sqlite3'
    buffers: dict[str, list[tuple[str, str]]] = {initial: [] for initial in INITIALS}
    pages: dict[str, list[tuple[str, str, str, int]]] = {
        initial: [] for initial in INITIALS
    }
    files: dict[str, str] = {}

    def save(relative: Path, metadata: dict[str, object], body: str) -> None:
        content = document(metadata, body)
        checked_write(edition / relative, content)
        files[relative.as_posix()] = hashlib.sha256(content.encode('utf-8')).hexdigest()

    with sqlite3.connect(database_path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA temp_store=FILE')
        db.execute('PRAGMA cache_size=-16384')
        source_row = db.execute("SELECT value FROM state WHERE key='source'").fetchone()
        source = json.loads(source_row[0]) if source_row else {}
        snapshot_id = source.get('snapshot_id')
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError('Alphabetical navigation requires a source snapshot identity')
        base = {'schema_version': 1, 'language': 'en', 'snapshot_id': snapshot_id,
                'sort_order': 'sqlite-nocase-binary-tiebreak'}

        def flush(initial: str) -> None:
            rows = buffers[initial]
            if not rows:
                return
            filename = f'{len(pages[initial]) + 1:04d}.md'
            first, last = rows[0][0], rows[-1][0]
            label = 'Other initial characters' if initial == '_' else initial.upper()
            lines = [f'# {label} — page {len(pages[initial]) + 1}', '',
                     '[Letter index](INDEX.md) · [Dictionary index](../../INDEX.md)', '',
                     f'{len(rows):,} headwords · {_text(display_headword(first))} — {_text(display_headword(last))}', '',
                     'Original spelling and case are preserved.', '']
            for word, bucket in rows:
                target = f'../../entries/{bucket}/{entry_path(word).name}'
                lines.append(f'- [{_text(display_headword(word))}]({target})')
            lines.extend(['', '[Source snapshot](../../SOURCE.md)'])
            save(Path('browse') / initial / filename,
                 {'kind': 'alphabetical-headword-page', **base, 'initial': initial,
                  'page': len(pages[initial]) + 1, 'headwords': len(rows),
                  'first_headword': first, 'last_headword': last}, '\n'.join(lines))
            pages[initial].append((filename, first, last, len(rows)))
            rows.clear()

        previous_word: str | None = None
        # DISTINCT removes multiple POS/etymology rows without collapsing casing.
        cursor = db.execute('SELECT DISTINCT word, bucket FROM records '
                            'ORDER BY word COLLATE NOCASE, word COLLATE BINARY, bucket')
        for word, bucket in cursor:
            path = entry_path(word)
            if bucket != partition_for(word):
                raise ValueError(f'Headword has an inconsistent source partition: {word!r}')
            if word == previous_word:
                raise ValueError(f'Headword occurs in multiple source partitions: {word!r}')
            previous_word = word
            initial = path.parts[1]
            buffers[initial].append((word, bucket))
            if len(buffers[initial]) == PAGE_SIZE:
                flush(initial)
        for initial in INITIALS:
            flush(initial)

    navigation = ['## Browse by initial', '',
                  'Choose a letter, then a page range. Original spelling and case are preserved.', '',
                  'Accented Latin initials share their plain-letter group; symbols and other initials '
                  'appear under **Other**.', '']
    for initial in INITIALS:
        initial_pages = pages[initial]
        if not initial_pages:
            continue
        label = 'Other' if initial == '_' else initial.upper()
        count = sum(page[3] for page in initial_pages)
        navigation.append(f'- [{label}](browse/{initial}/INDEX.md) — {count:,} headwords')
        body = [f'# {label} headwords', '', '[Dictionary index](../../INDEX.md)', '',
                f'{count:,} headwords across {len(initial_pages):,} pages.', '',
                '| Page | First headword | Last headword | Headwords |',
                '| --- | --- | --- | ---: |']
        for number, (filename, first, last, page_count) in enumerate(initial_pages, 1):
            body.append(f'| [{number}]({filename}) | {_text(display_headword(first))} | {_text(display_headword(last))} | {page_count:,} |')
        body.extend(['', '[Source snapshot](../../SOURCE.md)'])
        save(Path('browse') / initial / 'INDEX.md',
             {'kind': 'alphabetical-letter-index', **base, 'initial': initial,
              'headwords': count, 'pages': len(initial_pages)}, '\n'.join(body))
    if not files:
        navigation.append('No headwords are present in this source inventory.')
    return '\n'.join(navigation) + '\n', files
