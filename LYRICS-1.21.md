# InMusic 1.21 — word-synchronized lyrics

- Supports line timestamps and per-word timestamps such as [00:01.61]<00:01.61>word.
- Lyrics are stored on the server in Data/Lyrics/<Artist>/<Song title>.lrc.
- The lyrics view shows album cover, song title and artist beside the lyrics.
- Word glow updates without rebuilding the whole overlay; line transitions are smooth and do not scale/shake.
- Existing Data files are carried forward unchanged; Data/Lyrics is added.
