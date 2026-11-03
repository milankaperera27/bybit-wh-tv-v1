# Svādhyāya

A mobile-first study companion for the Vedas, the Upaniṣads, the Bhagavad Gītā and Vedānta, read alongside the work of Itzhak Bentov (*Stalking the Wild Pendulum*, *A Cosmic Book*).

It's a single static file (`index.html`) with no build step and no server.

## Run it

- Open `index.html` in any browser, or
- serve the folder (`python3 -m http.server` from inside `svadhyaya/`) and open it on your phone over the same network, or
- host it on GitHub Pages / any static host.

On a phone, use "Add to Home Screen" to keep it like an app.

## What's inside

- **Today**: a verse of the day, your progress, and an animated pendulum (Īśa Up. 5: "it moves, and it does not move").
- **Texts**: 38 key verses from 18 works (four Vedas, eight Upaniṣads, the Gītā, Brahma Sūtra, Śaṅkara, Aṣṭāvakra Gītā, Yoga Sūtra). Each has Devanagari, IAST transliteration, an English rendering, a study note, and where relevant a "Bentov lens".
- **Bentov**: his two books and eight core ideas, each linked to the verses they echo.
- **Bridges**: nine Vedic ↔ Bentov pairings, each with a "where they differ" note.
- **Study**: a Sanskrit glossary, flashcards with simple spaced repetition, and your saved verses and reflections.
- **Search**: across Sanskrit, transliteration (diacritics optional), translations and notes.

Progress, bookmarks and notes are stored in the browser's `localStorage` on your device only.

## A note on the content

The Sanskrit is the traditional text. English renderings and notes are original study paraphrases, not quotations from any published translation. Bentov's ideas are summarised in our own words; read his books for his actual arguments, and treat his physiological claims (such as the physio-kundalini model) as hypotheses.

To add verses, edit the `TEXTS` array near the top of the `<script>` in `index.html`.
