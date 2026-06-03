# PRD — Vocabulary deck (MVP)

## Goal
From the same video, additionally produce a **vocabulary deck** of unique content
words (nouns/verbs/adjectives/adverbs), each with reading, meaning, the example
sentence it came from, and that sentence's audio. It ships as its **own separate
`<vid>.vocab.apkg`** with its own note type — no interaction with the sentence
deck.

## Decisions (locked, MVP)
- **Output:** separate file `<vid>.vocab.apkg`, deck named
  `youtube::<title>::vocab`. New `VOCAB_DECK_ID` / `VOCAB_MODEL_ID`. The existing
  `<vid>.apkg` is unchanged. (No multi-deck packaging; simpler than one combined
  file. Sentence clips are bundled into both files — accepted disk cost.)
- **Trigger:** `--vocab` = build the sentence deck as today **plus** the vocab
  apkg. `--vocab-only` = build **only** `<vid>.vocab.apkg`, skip writing
  `<vid>.apkg`. (No runtime saving — upstream stages all still run, clips
  included — but the sentence deck isn't emitted.) Neither flag = today's
  behavior. `--vocab-only` implies `--vocab`.
- **Audio:** reuse the per-cue **sentence clip** of the word's first-occurrence
  example. No word-level audio.
- **Dedup:** by lemma (dictionary form); keep the **first occurrence's** sentence
  (its furigana text + translation + clip).
- **Card direction:** recognition only. Front = word (dictionary form); back =
  reading + meaning + form note + example sentence + audio.
- **Lemma over surface form:** `Word` is always the dictionary/plain form. When
  the word is conjugated/inflected in the sentence, the surface form + a short
  inflection label go in the `Form` field, not in `Word`.
- **Scope:** anthropic backend, all source languages. `Reading` is left empty for
  languages written phonetically (Latin/Cyrillic script); populated for JA (kana)
  and other non-phonetic scripts.

## Cut from the original vision (deferred)
- `KanjiInfo` per-kanji keywords field.
- JLPT tags (`jlpt::N5`…`N1`) + `--vocab-jlpt` build-time filter.
- `{{kanji:Word}}` / `{{furigana:Word}}` Anki field filters (plain fields used;
  reading is a separate field, so a plain `Word` front already hides the reading).
- Combined single-apkg multi-deck packaging.
- Reverse/production cards, `--known-words`, pitch accent.
- IPA in `Reading` for phonetic-script langs (left empty instead for MVP).

## Note type `yt2anki Vocab`
Fields: `Word` (plain dictionary form, e.g. `食べる`) · `Reading` (pronunciation;
empty for phonetic-script langs) · `Meaning` · `Form` (surface form as it appeared
+ inflection label; empty when uninflected) · `Sentence` (example, furigana HTML
for JA, else plain source) · `SentenceMeaning` · `Audio`.

Template (Card 1):
- Front: `{{Word}}`.
- Back: `{{FrontSide}}` + `Reading` + `Meaning` + `Form`, then the example
  `Sentence` + `SentenceMeaning` + `{{Audio}}`.
- Reuses the existing night-mode-safe `_APKG_CSS`; `.note`-style classes for the
  secondary lines (`Form` rendered dim, like the grammar note).

## Extraction (one LLM pass, JA + anthropic)
`anthropic_extract_vocab(sentences, model, source_lang, user_lang)` — sends the
numbered source sentences, returns a JSON array aligned to sentences, each a list
of content-word objects `{word, reading, meaning, form}`.
- `word` = dictionary/plain form; skip particles/copula/aux.
- `reading` = pronunciation, empty for phonetic-script source langs.
- `form` = the surface form as it appeared + a short inflection label (e.g.
  `食べました — polite past`, `mangé — past participle`, `Häusern — dative plural`);
  empty when the word already appears in dictionary form.
- Flatten with sentence index → **dedup by lemma**, first occurrence wins →
  carry that sentence's furigana text + translation + clip + the `form` from the
  occurrence that won.
- Output cached to `<vid>.vocab.tsv` (reuse-gated like other artifacts).

## Pipeline wiring (keeps 5 stages)
- **Stage 4 (translate):** when `--vocab`, also run `anthropic_extract_vocab` and
  write `<vid>.vocab.tsv`. Reuses the furigana/translation already produced.
- **Stage 5 (build deck):**
  - Cut per-cue clips as today.
  - Build the sentence deck `<vid>.apkg` as today, **unless `--vocab-only`**.
  - If `--vocab`: build the vocab deck from `<vid>.vocab.tsv` (example
    `Sentence`/`SentenceMeaning`/`Audio` from the dedup'd first-occurrence cue),
    write `<vid>.vocab.apkg`, bundling only the clips its words reference.
- Generalize `write_apkg` to take a model/deck (or add a `write_vocab_apkg`) so
  the Vocab note type can be emitted without touching the sentence path.

## Scope / limits (v1)
- anthropic backend only (`anthropic_extract_vocab`); other backends → warn +
  skip the vocab deck. All source languages supported.
- `Reading` empty for phonetic-script langs; furigana in `Sentence` only for JA.
- Sentence-level audio only (no word clips).
- No JLPT/CEFR filtering → vocab decks may be large/noisy; mitigated only by
  dedup + skipping particles/copula/aux in extraction.
