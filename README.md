# CLIP-AUTOMATION

**Video Clip Automation System** — An end-to-end pipeline that transcribes a
video, translates the subtitles, and burns them back into the video with
broadcast-ready styling.  The project ships both a **Web UI** and a **CLI**.

## Features

The system runs a **4-Phase Pipeline**:
1. **Transcription** — [ElevenLabs Scribe STT](https://elevenlabs.io/) cloud
   API with word-level timestamps and built-in speaker diarisation.
2. **Translation** — [Gemini](https://ai.google.dev/) regroups the words into
   subtitle-sized segments and translates them.  DeepL is used as an
   automatic fallback when Gemini is unavailable.
3. **Subtitle Rendering** — Pycaps for single-speaker, ASS + FFmpeg for
   multi-speaker (stacked, per-speaker colour, simultaneous talk).
4. **Final Muxing** — FFmpeg combines the burned-in video with the original
   audio.

**Additional features**
- **Web Interface** — FastAPI dashboard for upload, monitoring, preview, and
  download.
- **Live Preview Editor** — drag-and-drop timeline with karaoke /
  narration-pop highlighting.  Edits to segment timing rescale word-level
  timestamps so animations stay in sync.
- **After Effects Export** — generates `.jsx` ExtendScript files to import
  the synchronized subtitles into After Effects for advanced styling.
- **Clip Finder** — Gemini-powered detection of highlight clips from
  long-form YouTube videos.
- **Short Maker** — vertical-aspect crop tool for shorts/reels.

## Architecture

```
processors/
  stt/             SttEngine Protocol + ElevenLabsSttEngine
  timing/          TimingPolicy + Sanitizer (single seam for word/segment timing fixes)
  translator/      Gemini client + regrouper + recheck + DeepL fallback + local grouper
  subtitle_renderer.py
  muxer.py
  clip_finder/     YouTube clip detection (yt-dlp + Gemini)
  short_maker.py   Vertical-aspect crop tool

web/
  server.py        FastAPI app + route handlers
  services/        job_models, transcript_sync, pipeline_runner

models/transcript.py
  TranscriptSegment, WordTimestamp, sanitize_timestamps (compat shim)
```

The timing sanitizer is **speaker-aware**: cross-speaker overlap (one
speaker interrupting another) is preserved, while same-speaker overlap is
trimmed.  See `processors/timing/policy.py` for tuning knobs.

## Requirements

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) accessible from `PATH`
- An ElevenLabs API key (mandatory)
- A Gemini API key (mandatory for translation)
- Optional: a DeepL API key for fallback translation

No local Whisper/WhisperX runtime is required — STT is delegated to the
ElevenLabs cloud API.  GPU acceleration is only used by FFmpeg (NVENC) when
available.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Oatse/Auto-Clipping.git
   cd Auto-Clipping
   ```

2. Set up a virtual environment (recommended):
   ```bash
   python -m venv venv_python311
   # Windows:
   .\venv_python311\Scripts\activate
   # Linux/Mac:
   source venv_python311/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure your API keys:
   ```bash
   cp .env.example .env
   # Edit .env and add ELEVENLABS_API_KEY_01 and GEMINI_API_KEY
   ```

## Usage

### 1. Web UI (recommended)
```bash
# Windows
run_web_v311.bat

# Or directly via python
python run_web.py
```
Open `http://localhost:8000` in your browser.

### 2. CLI
```bash
python main.py --input path/to/your/video.mp4 --lang id --output ./output
```
**Arguments**
- `--input` (`-i`): Input video file.
- `--output` (`-o`): Output directory (default `./output`).
- `--lang` (`-l`): Target language BCP-47 code (default `id` = Indonesian).
- `--no-diarize`: Disable speaker detection (assigns all to `SPEAKER_00`).
- `--num-speakers`: Hint for the maximum speaker count (1-6).

## Testing

```bash
pytest tests/ -q
```

The test suite covers the timing sanitizer, word-level recheck, translator
regrouper, JSON salvage from truncated Gemini output, the local grouping
fallback, and the transcript-sync helper used by the preview editor.

## License

*(Add your specific license here, e.g., MIT, GPL, etc.)*
