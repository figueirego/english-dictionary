# English Dictionary

An English-only dictionary in Markdown, with exact source attribution, explicit editorial changes and auditable counts.

**[Browse the dictionary](INDEX.md)** · [Publication and verification](PUBLICATION.md) · [Sources](SOURCE.md) · [Licensing](LICENSE.md)

The collection contains **1,396,111 exact headwords**, **1,499,161 English lexical records** and **1,795,048 source sense positions** from a pinned English Wiktionary extraction. These are snapshot counts, not a claim to contain every English word or every independent meaning.

Of 1,335 originally unglossed source positions, reviewed decisions supply 1,101 English definitions or concept descriptions, identify 219 auxiliary relation positions and 5 proven source-structure defects, and leave **10 lexical gaps unresolved**. Auxiliary classifications are not supplied definitions. Each supplement identifies its sources, exact target and transformation; see the [public definition ledger](dictionary/DEFINITIONS.md).

## Reading the collection

Start at [INDEX.md](INDEX.md) for alphabetical browsing. Each headword keeps its exact spelling and case; part-of-speech records and source senses remain distinguishable. Entries retain source-page and contributor-history links. Related-word links open existing exact headwords, without claiming that every target sense or synonym is interchangeable.

The public derivative omits unreviewed etymology prose from both entry displays and source projections. Definitions, sense identities and attribution remain available. Private research captures and historical local editions are not part of this repository. The [publication report](PUBLICATION.md) records the exported edition, transformations, counts and verification evidence.

## Scope and quality

The base source is English Wiktionary, extracted by Kaikki/Wiktextract from the 2026-10-01 dump on 2026-10-21. Source context, traceable corrections and separately attributed supplements improve the reading view. Missing evidence stays visible.

Automated integrity checks establish consistency with the selected inputs; they do not certify every definition, pronunciation, register or historical claim. The remaining 10 lexical gaps and broader editorial review are still pending. Cambridge and Oxford inform presentation principles; their proprietary dictionary entries were not imported, and neither institution endorses this project. Excluding etymology prose is not a claim that every remaining source string has received independent rights review.

## Contributions

Changes to `main` require a pull request and approval by **[@figueirego](https://github.com/figueirego)**. Follow [CONTRIBUTING.md](CONTRIBUTING.md), preserve per-sense evidence and source-specific terms, and keep entries in English Markdown. The [publication report](PUBLICATION.md) records the actual repository protection configuration and verification state.

This review policy governs contributions to this repository. It does not restrict copying, redistribution, adaptation or forks permitted by the applicable [content licences](LICENSE.md).

## Tools and verification

Python runtime tools are included under `scripts/`; runtime dependencies are from the
standard library. The included synthetic tests cover complete ingestion and public
export boundaries. Install `requirements-dev.txt` in a virtual environment and run
`python -m pytest -q`. The release preparation ran all 35 included tests successfully.

The public export is reproduced with
`python -m scripts.public_export build --root /path/to/original-local-project --destination /path/to/public-project`.
Use `audit` instead of `build` to compare the derivative against that same parent.
These commands require the pinned, accepted local parent edition; the public derivative
does not contain withheld text and cannot reconstruct it. Runtime import tooling is
included for maintainers, but no scheduled collection or automatic publication is enabled.
