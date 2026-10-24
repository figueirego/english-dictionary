"""Evidence-backed, source-hash-guarded reading-view changes.

Imported projections stay immutable. These AI-assisted editorial decisions apply
only to exact raw records; an updated upstream record requires new adjudication.
See sources/CORRECTION-EVIDENCE.md for original coordinates and source evidence.
"""

from __future__ import annotations

from copy import deepcopy


COLOR_NOUN = '5b780430763f8e3a6120a10393b89b261556420b2fd088fd720c573b645490fa'
REPOSITORY_NOUN = 'a029c3714e663cf24ead41318872958ace7ef636c0e5db10e83ccd128fd3aaa2'
REPOSITORY_ADJ = '54ea9618c5d0c5d2eab9d14c7a22f874ac6720da4e044f13fe7ecb8fb154f4b7'
WENT_NOUN = 'e775419ea2a421cce9c77c1edde74d0d00526d5ea71de32fc71afaf74b5823e2'
KICK_BUCKET = 'ab53a2da01770616ef00b05e5dd09d9f5fbd637e95611af28c9ae0610813de52'
HELLO_INTJ = 'b6c3ce3cf710d332dfcedb66f5828355bbb56e13737cad4953952b68dac6bcbe'
LEAD_GUIDE = 'f9c70919a5080957c210835641707d00bdb00bad04448014363887190ae7fdfa'
LEAD_MISSPELLING = 'b198964ccfa7f1a91b6a31c130b916970222815d3d0b259e32190665304eed5b'

COLOR_ORIGINAL = 'To bleed, either through injury or blading. Usually prefaced with "get".'
COLOR_CORRECTED = ('Blood shed during a professional wrestling match, through injury or blading; '
                   'usually in the phrase "get color".')

_TREE_BOUNDARIES = {
    ('dictionary', 1, '1816debd3a51de61ec6ff33263cd1874a1c3f009f7b3a0e9bc5855be6082f254'): '\nFrom Middle English dixionare,',
    ('dictionary', 2, '298d3d42afdb887aa5065a3d56ae45356a04ffedb163ceb33cf62029d3311bec'): '\nFrom Middle English dixionare,',
    ('color', 1, COLOR_NOUN): '\nFrom Middle English colour, color,',
    ('color', 2, '0eedcb78cfdde0d3a0e4cc8c3c1e96bfa83ae64656417ba2264c6e0788363ed8'): '\nFrom Middle English colour, color,',
    ('color', 3, 'f9c4170aacd44d13d13c7fda2dbbeee659537bebddde91d3a81d17413eeca5fa'): '\nFrom Middle English colour, color,',
    ('bank', 1, '17f4efebd973d70fb4ef140ea540b095eb38677ec46dbf5eadc0e0da8e0b6e46'): '\nInherited from Middle English banke,',
    ('bank', 2, '77cf5d724eb8db0838ee197a0450f24b96fdfe0713e991ea5bbd6443c322b6a5'): '\nInherited from Middle English banke,',
}

_PATCH_KEYS = {
    ('color', 1, COLOR_NOUN),
    ('repository', 1, REPOSITORY_NOUN),
    ('repository', 2, REPOSITORY_ADJ),
    ('went', 2, WENT_NOUN),
    ('kick the bucket', 1, KICK_BUCKET),
    ('hello', 1, HELLO_INTJ),
    ('lead', 3, LEAD_GUIDE),
    ('lead', 6, LEAD_MISSPELLING),
}


def apply_record(headword: str, original_record: dict, record_number: int) -> tuple[dict, list[dict]]:
    """Return the original or an annotated display copy plus explicit change rows.

    The raw line digest, exact headword, lexical-record position and vulnerable
    field value must match. The digest is provenance, not authentication; the
    pipeline separately verifies it while reading the pinned source capture.
    """
    key = (headword, record_number, original_record.get('upstream_record_sha256'))
    if (key not in _PATCH_KEYS and key not in _TREE_BOUNDARIES) or original_record.get('word') != headword:
        return original_record, []
    record = deepcopy(original_record)
    changes: list[dict] = []
    senses = record.get('senses', [])

    def annotate(target: dict, code: str, coordinate: str, note: str) -> None:
        target.setdefault('_editorial_notes', []).append(note)
        changes.append({'code': code, 'coordinate': coordinate, 'note': note})

    if key == ('color', 1, COLOR_NOUN) and record.get('pos') == 'noun':
        if len(senses) > 22 and senses[22].get('glosses') == [COLOR_ORIGINAL]:
            senses[22]['glosses'] = [COLOR_CORRECTED]
            annotate(senses[22], 'F04', 'R1.S23',
                     'AI-assisted editorial noun-form correction of the Wiktionary gloss; '
                     'the original uses a verbal definition under Noun. The noun meaning '
                     'is independently corroborated by the PW Torch insider glossary '
                     '(https://www.pwtorch.com/insiderglossary.shtml). Original wording '
                     'and evidence remain in the source projection and correction ledger.')
        if len(senses) > 17:
            old = ('The relative lightness or darkness of a mass of written or printed '
                   'text on a page. (See type color on Wikipedia.Wikipedia )')
            if senses[17].get('glosses') == [old]:
                senses[17]['glosses'] = [old.replace('Wikipedia.Wikipedia )', 'Wikipedia.)')]
                annotate(senses[17], 'F05', 'R1.S18',
                         'Removed the duplicated Wikipedia reference token from the reading '
                         'view; the definition and original source wording are preserved.')

    if key == ('repository', 1, REPOSITORY_NOUN) and record.get('pos') == 'noun' and len(senses) > 3:
        sense = senses[3]
        if (sense.get('glosses', [])[-1:] == ['A person to whom a secret is entrusted.']
                and sense.get('synonyms') == [{'word': 'See storehouse'}]):
            sense.pop('synonyms')
            annotate(sense, 'F07', 'R1.S4',
                     'The inherited storehouse cross-reference is omitted from this person '
                     'sense. It remains with the storage sense and in the source projection.')

    if key == ('repository', 2, REPOSITORY_ADJ) and record.get('pos') == 'adj' and len(senses) == 1:
        if not any(value.strip() for value in senses[0].get('glosses', [])):
            annotate(senses[0], 'F03', 'R2.S1',
                     'Unresolved source gap: Wiktionary still requests a definition for '
                     'this historical military adjective. No definition has been invented. '
                     'This is a reviewed gap, not an approved sense.')

    if key == ('went', 2, WENT_NOUN) and record.get('pos') == 'noun':
        expected = ['gaed', 'eode', 'yead', 'yede', 'yode', 'gone', 'wended']
        if [relation.get('word') for relation in record.get('synonyms', [])] == expected:
            record.pop('synonyms')
            annotate(record, 'F07', 'R2',
                     'Seven verb-form relations were removed from this obsolete noun\'s '
                     'recommended synonyms. Their original positions remain in the source; '
                     'they have not been reassigned to other senses without review.')

    if key == ('kick the bucket', 1, KICK_BUCKET) and record.get('pos') == 'verb':
        if senses:
            old = senses[0].get('synonyms', [])
            if any(relation.get('word') == 'be like the the One' for relation in old):
                senses[0]['synonyms'] = [relation for relation in old
                                        if relation.get('word') != 'be like the the One']
                annotate(senses[0], 'F07', 'R1.S1',
                         'One malformed source relation was omitted from the reading view. '
                         'No guessed replacement was supplied; the original remains in the source.')
        etymology = record.get('etymology_text', '')
        if (etymology.startswith('There are many theories as to where this idiom comes from, '
                                'but the Oxford English Dictionary (OED) suggests the following:')
                and '\nAnother theory is given by Roman Catholic Bishop Abbot Horne:\n' in etymology
                and 'After death, when a body had been laid out,' in etymology):
            record.pop('etymology_text')
            annotate(record, 'F12', 'R1.etymology_text',
                     'The imported etymology is withheld from this reading view because it '
                     'embeds an attributed quotation and unverified claims about the OED. '
                     'The full original remains in the source projection. The origin has '
                     'not been established by this review; no replacement history is supplied.')

    if key == ('hello', 1, HELLO_INTJ) and record.get('pos') == 'intj' and len(senses) > 4:
        sense = senses[4]
        if (sense.get('glosses') == ['An expression of puzzlement or discovery.']
                and len(sense.get('synonyms', [])) == 325):
            sense['_source_relations_only'] = True
            annotate(sense, 'F07', 'R1.S5',
                     'The 325 source relations include mixed registers and unlabelled '
                     'strong expressions. They are shown only as uncurated source '
                     'relations, not interchangeable polite greetings. Individual '
                     'register labels and equivalence remain unverified.')

    if key in {('lead', 3, LEAD_GUIDE), ('lead', 6, LEAD_MISSPELLING)}:
        sense_index = 20 if record_number == 3 else 0
        if (len(senses) > sense_index
                and senses[sense_index].get('glosses') == ['Misspelling of led.']
                and senses[sense_index].get('alt_of') == [{'word': 'led'}]):
            coordinate = f'R{record_number}.S{sense_index + 1}'
            annotate(senses[sense_index], 'F08', coordinate,
                     'This same misspelling reference also occurs at '
                     + ('R6.S1' if record_number == 3 else 'R3.S21')
                     + '. The two source positions are preserved; they do not establish '
                     'two independent meanings. This spelling is nonstandard for the past '
                     'of the guiding verb; the source form is led.')

    boundary = _TREE_BOUNDARIES.get(key)
    etymology = record.get('etymology_text', '')
    if (boundary and etymology.startswith('Etymology tree\n')
            and etymology.count(boundary) == 1):
        record['etymology_text'] = etymology[etymology.index(boundary) + 1:]
        annotate(record, 'F10', f'R{record_number}.etymology_text',
                 'The flattened extraction tree is omitted from this reading view at '
                 'an individually verified boundary. The following source prose is '
                 'unchanged; the complete tree remains in the source projection. '
                 'This display correction does not verify the historical claims.')

    return (record, changes) if changes else (original_record, [])
