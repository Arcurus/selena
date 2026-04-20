# LEARNINGS.md

## 2026-04-20: mmx music generate vs mmx speech synthesize

**What happened:** I tried to create a "song" from a poem using `mmx speech synthesize`, but this creates spoken word audio (text-to-speech), not actual music.

**What to do differently:**
- `mmx speech synthesize` — Text-to-speech, converts text to spoken audio with a voice
- `mmx music generate` — Creates actual music with melody, harmony, and optionally lyrics

**How to create a song with mmx music generate:**
```bash
mmx music generate --prompt "description of music style and mood" --lyrics "the lyrics here" --out song.mp3
```
Or use `--instrumental` for music without lyrics.

**Example for a morning song:**
```bash
mmx music generate \
  --prompt "A gentle morning song, acoustic guitar and soft piano, ethereal vocals, dawn breaking over a quiet garden" \
  --lyrics "The dawn does not ask permission to arrive..." \
  --out morning_song.mp3
```

