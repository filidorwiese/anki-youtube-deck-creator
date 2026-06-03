# PRD — Vocabulary deck (MVP, shipped)

## Goal
From the same video, additionally produce a **vocabulary deck** of unique content
words (nouns/verbs/adjectives/adverbs), each with its part of speech, reading,
meaning, a per-kanji breakdown, and the example sentence it came from with that
sentence's audio. It ships as its **own separate `<vid>.vocab.apkg`** with its own
note type — no interaction with the sentence deck.

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
- **Card direction:** recognition only. Front = word (dictionary form, furigana
  ruby above the kanji for JA); back = part of speech + reading + meaning +
  per-kanji breakdown + example sentence + translation + audio.
- **Lemma over surface form:** `Word` is always the dictionary/plain form. (The
  earlier `Form` inflection note was dropped; part of speech is kept instead.)
- **Furigana on the word:** extraction returns `word_ruby` (Anki ruby notation
  `食[た]べる`, rendered by `ruby_to_html`); the front shows the reading as ruby
  above each kanji run. Non-JA / all-kana words fall back to the plain `Word`.
- **Scope:** anthropic backend, all source languages. `Reading` is left empty for
  languages written phonetically (Latin/Cyrillic script); populated for JA (kana)
  and other non-phonetic scripts.

## Note type `yt2anki Vocab`
Fields: `Word` (dictionary form; furigana ruby HTML for JA, else plain) · `Pos`
(part of speech; for JA, verb class u-/ru- and adjective class i-/na-) · `Reading`
(pronunciation; empty for phonetic-script langs) · `Meaning` · `KanjiInfo`
(per-kanji `kanji = keyword` breakdown, JA; empty otherwise) · `Sentence`
(example, furigana HTML for JA, else plain source) · `SentenceMeaning` · `Audio`.

Template (Card 1):
- Front: `{{Word}}`.
- Back: `{{FrontSide}}` + `Pos` + `Reading` + `Meaning` + `KanjiInfo`, then the
  example `Sentence` + `SentenceMeaning` + `{{Audio}}`. Audio stays on the back:
  it's the whole-sentence clip, so on the front it would give away the answer.
- Reuses the night-mode-safe `_APKG_CSS` plus `.pos` / `.reading` / `.kanji` /
  `.ex` classes; secondary lines rendered dim like the grammar note.

## Extraction (one LLM pass, anthropic)
`anthropic_extract_vocab(sentences, model, source_lang, user_lang)` — sends the
numbered source sentences, returns a JSON array aligned to sentences, each a list
of content-word objects `{word, word_ruby, pos, reading, meaning, kanji_info}`.
- `word` = dictionary/plain form; skip particles/copula/aux/pronouns/proper nouns.
- `word_ruby` = furigana copy of the dictionary form (JA only; empty for all-kana).
- `pos` = part of speech; JA verbs tagged u-verb/ru-verb, adjectives i-/na-.
- `reading` = pronunciation, empty for phonetic-script source langs.
- `kanji_info` = per-kanji `kanji = keyword` meanings joined by ` · ` (JA; empty
  when the word has no kanji).
- Flatten with sentence index → **dedup by lemma**, first occurrence wins →
  carry that sentence's furigana text + translation + clip.
- Output cached to `<vid>.vocab.tsv` (reuse-gated). TSV cols:
  `word · word_ruby · pos · reading · meaning · kanji_info · sentence_index`.

## Pipeline wiring (keeps 5 stages)
- **Stage 4 (translate):** when `--vocab`, also run `anthropic_extract_vocab` and
  write `<vid>.vocab.tsv` (`stage_extract_vocab`). Reuses the furigana/translation
  already produced.
- **Stage 5 (build deck):**
  - Cut per-cue clips (`stage_build_deck`); still cuts them under `--vocab-only`.
  - Build the sentence deck `<vid>.apkg`, **unless `--vocab-only`**.
  - If `--vocab`: `build_vocab_deck` reads `<vid>.vocab.tsv`, reuses each word's
    first-occurrence cue (`Sentence`/`SentenceMeaning`/`Audio`), writes
    `<vid>.vocab.apkg`.
- `write_vocab_apkg` emits the Vocab note type without touching the sentence path.

## Scope / limits (v1)
- anthropic backend only (`anthropic_extract_vocab`); other backends → warn +
  skip the vocab deck (falling back to the sentence deck). All source languages.
- `Reading` empty for phonetic-script langs; furigana (`word_ruby`, `Sentence`)
  and `KanjiInfo` only for JA.
- Sentence-level audio only (no word clips).
- No JLPT/CEFR filtering → vocab decks may be large/noisy; mitigated only by
  dedup + skipping particles/copula/aux/proper nouns in extraction.

## Deferred (not implemented)
- JLPT tags (`jlpt::N5`…`N1`) + `--vocab-jlpt` build-time filter.
- Reverse/production cards.
- `--known-words FILE` to skip lemmas already learned across videos.
- Pitch accent.
- IPA in `Reading` for phonetic-script langs (left empty for MVP).
- Word-level audio (Whisper word timestamps): considered, parked as too fragile
  for JA; sentence clip used instead.
- Combined single-apkg multi-deck packaging (separate files chosen instead).
