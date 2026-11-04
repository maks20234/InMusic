# InMusic 1.20 — synchronized lyrics

- Added a minimalist lyrics overlay and player button.
- Supports importing/pasting timestamped LRC text.
- Current line glows; earlier and upcoming lines are visually muted.
- Tapping a line seeks playback to its timestamp.
- Lyrics are stored in this browser localStorage, keyed by song ID.

## Word timing note
LRC normally provides timestamps per line, not per word. In this first version, word highlighting timing is estimated evenly within each line using the next line timestamp. For accurate word-by-word timing and AI alignment, a backend alignment service and an authorized lyric/audio source are still needed. No external AI service or API key is connected in this build.
