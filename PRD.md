# PRD — Vocabulary subdeck

## Goal
From the same video, additionally produce a **vocabulary deck** of unique content
words (nouns/verbs/adjectives/adverbs), each with reading, meaning, the example
sentence it came from, and that sentence's audio. It is a **separate subdeck**
inside the same `.apkg`, with its own note type — no interaction with the
sentence cards.

## Decisions (locked)
- **Output:** subdeck `youtube::<title>::vocab` in the existing `<vid>.apkg`
  (genanki `Package` accepts multiple decks; a new `VOCAB_DECK_ID` /
  `VOCAB_MODEL_ID`).
- **Trigger:** `--vocab` (build sentence deck **and** vocab subdeck) and
  `--vocab-only` (build only the vocab subdeck). Neither flag = current behavior.
- **Audio:** reuse the per-cue **sentence clip** of the word's example sentence.
  Word-level audio is a later enhancement.
- **Noise control:** each note tagged `jlpt::N5`…`jlpt::N1` (filter/suspend in
  Anki, no rerun) + `yt2anki::vocab`. Optional `--vocab-jlpt` pre-filters at
  build time (e.g. `N4-N1` / `N5,N4`); default keeps all, untagged words kept.
- **Card direction:** recognition — front = word, back = reading + meaning +
  example sentence + audio. Reverse is a possible later flag.
- **Dedup:** by lemma (dictionary form); keep the **first occurrence's** sentence.
- Pitch accent deferred. `KanjiMeaning` = LLM keywords, optional field.

## Note type `yt2anki Vocab`
Fields: `Word` (Anki ruby notation `食[た]べる`) · `Reading` (kana) · `Meaning` ·
`KanjiInfo` (per-kanji keywords, may be empty) · `Sentence` (example, furigana
HTML) · `SentenceMeaning` · `Audio`.

Template (Card 1):
- Front: `{{kanji:Word}}` (kanji only — tests recall).
- Back: `{{furigana:Word}}` + `Reading` + `Meaning` + (optional) `KanjiInfo`,
  then the example `Sentence` + `SentenceMeaning` + `{{Audio}}`.
- Reuses the night-mode-safe CSS; `.note`-style classes for the secondary lines.

## Extraction (one LLM pass, JA + anthropic)
`anthropic_extract_vocab(sentences, model, source_lang, user_lang)` — sends the
numbered source sentences, returns a JSON array aligned to sentences, each a list
of content-word objects:
`{lemma_ruby, reading, meaning, jlpt, kanji_info}`.
- Lemma in dictionary form, ruby notation; skip particles/copula/aux.
- Flatten with sentence index → **dedup by lemma**, first occurrence wins →
  carry that sentence's furigana text + translation + clip.
- Output cached to `<vid>.vocab.tsv` (reuse-gated like other artifacts).

## Pipeline wiring (keeps 5 stages)
- **Stage 4 (translate):** when `--vocab`/`--vocab-only`, also run
  `anthropic_extract_vocab` and write `<vid>.vocab.tsv`. (Reuses the
  furigana/translation already produced.)
- **Stage 5 (build deck):**
  - Cut per-cue clips as today (needed for example audio either way).
  - Build the sentence deck unless `--vocab-only`.
  - If vocab requested, build the vocab subdeck from `<vid>.vocab.tsv`
    (example `Sentence`/`SentenceMeaning`/`Audio` come from the dedup'd
    first-occurrence cue), apply `--vocab-jlpt` filter, tag notes.
  - Package all decks + combined media into one `<vid>.apkg`.
- `write_apkg` refactored to accept multiple `(deck, media)` builds.

## Scope / limits (v1)
- JA + anthropic only (matches furigana/grammar scope). Non-JA: no vocab deck
  (warn + skip) — future work.
- Sentence-level audio only (no word clips yet).
- JLPT levels are LLM-estimated, not authoritative — hence tags, so the user
  filters rather than trusting hard cutoffs.

## Open / future
- Word-level audio via Whisper word timestamps.
- `--known-words FILE` to skip lemmas already learned across videos.
- Reverse (production) vocab cards.
- Non-JA vocab (no furigana/JLPT; CEFR tag instead).
