# Contributing

Contributions should improve a specific, evidenced lexical use while preserving traceability. Keep dictionary entries, decisions and project documentation in English Markdown. Portuguese is reserved for the owner's requested explanatory progress figures.

## Propose a lexical change

1. Identify the exact headword, case, part of speech, source record and sense. Include the current entry path and content hash where available.
2. Provide a reliable source that supports that specific use, with its title, author or institution, URL, revision or version, and applicable reuse terms. A matching spelling alone does not establish a matching sense.
3. Explain the before/after change and its scope. Distinguish a new definition from an auxiliary relation, source-structure repair, spelling variant or unresolved case.
4. Preserve original source values and attribution. Add a guarded editorial decision and rebuild the affected derivative; do not silently rewrite accepted source projections or manufacture missing IPA, examples, etymology, CEFR labels or historical dates.
5. Verify affected entries, local links, manifests and relevant automated checks. Report commands actually run and any remaining uncertainty. Do not call a successful rendering check linguistic approval.

Modern Cambridge/Oxford text and other proprietary wording must not be copied merely because it is accessible online. Historical public-domain evidence must identify its edition and jurisdiction. Independent factual wording still needs precise evidence and must not imply redistribution permission for the cited source. Do not add research captures, credentials, local absolute paths or unreviewed etymology prose to the public package.

For missing definitions, check neighbouring senses, source kind, grammar and domain before proposing a supplement. Ambiguous evidence remains pending. The [definition ledger](dictionary/DEFINITIONS.md), [source overview](SOURCE.md) and [licence notices](LICENSE.md) explain the current provenance.

## Pull requests and owner approval

The repository policy requires every change to `main` to arrive through a pull request and receive approval from **@figueirego**, the code owner for every path. New changes require renewed approval when the configured stale-review rule dismisses earlier reviews. Administrators must follow the same policy; do not bypass it, force-push or delete the protected branch. No automated check or AI review substitutes for owner approval.

GitHub does not allow authors to approve their own pull requests. Because @figueirego is the sole code owner, a PR authored by @figueirego cannot satisfy this owner-approval requirement by self-approval. Leave such a PR unmerged rather than waiving the rule. Contributions authored by others can be reviewed by the owner. See [GitHub's required-review documentation](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews).

The [publication report](PUBLICATION.md) records which branch protections are actually configured and verified. `CODEOWNERS` identifies the reviewer; branch protection enforces the merge requirement. Do not infer active checks or protections from these policy files alone.

This upstream contribution workflow does not impose additional restrictions on downstream reuse allowed by the applicable licences.
