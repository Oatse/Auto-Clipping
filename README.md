# CLIP-AUTOMATION

**Video Clip Automation System** — A robust end-to-end pipeline for automatically processing videos, generating transcriptions, translating subtitles, and hard-subbing them back into the video. 

This project provides both a **Web UI** and a **CLI Interface** for seamless video subtitle automation.

## Features

The system runs a **4-Phase Pipeline**:
1. **Transcription & Diarization**: Extracts audio and detects speakers using [WhisperX](https://github.com/m-bain/whisperX) (for local processing) or [ElevenLabs STT](https://elevenlabs.io/) (via API).
2. **Translation**: Automatically translates the resulting transcripts to a target language using LLM (Gemini).
3. **Subtitle Rendering**: Renders dynamic, highly customizable subtitles directly onto the video frames using `Pycaps`.
4. **Final Muxing**: Combines the processed video frames with the original audio using FFmpeg.

**Additional Features:**
- **Web Interface**: A FastAPI-based interactive web dashboard to upload videos, monitor processing in real-time, and download the final result.
- **After Effects Export**: Generates `.jsx` ExtendScript files to explicitly import the synchronized subtitles into Adobe After Effects for advanced styling.
- **Hardware Acceleration**: Full support for PyTorch CUDA to leverage local GPU power for transcription and rendering.
- **Speaker Detection**: Supports automatic speaker diarization and manual speaker count overrides.

## Requirements

Before running the project, make sure you have the following installed:
- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) (must be accessible in your system PATH)
- NVIDIA GPU with CUDA support (Recommended for WhisperX and PyTorch)

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/Oatse/Auto-Clipping.git
   cd Auto-Clipping
   ```

2. Set up a virtual environment (Recommended):
   ```bash
   python -m venv venv_python311
   # Activate on Windows:
   .\venv_python311\Scripts\activate
   # Activate on Linux/Mac:
   source venv_python311/bin/activate
   ```

3. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure your environment variables by copying the example environment file:
   ```bash
   cp .env.example .env
   ```
   *Edit `.env` to include your necessary API keys (like `GEMINI_API_KEYS` or `ELEVENLABS_API_KEY`) and customize default settings.*

## Usage

### 1. Web UI (Recommended)
You can start the web interface by running:
```bash
# On Windows
run_web_v311.bat

# Or directly via python
python run_web.py
```
Then, open your browser and navigate to `http://localhost:8000` (or the port specified in your console).

### 2. Command Line Interface (CLI)
You can also run the pipeline purely from the terminal:
```bash
python main.py --input path/to/your/video.mp4 --lang id --output ./output
```
**Arguments:**
- `--input` (`-i`): Path to the input video file.
- `--output` (`-o`): Output directory for processed files (default is `./output`).
- `--lang` (`-l`): Target language BCP-47 code for translation (default is `id`).

## Project Structure

- `main.py`: The CLI orchestrator chaining all pipeline phases.
- `web/`: Contains the FastAPI backend (`server.py`) and static frontend files (HTML/JS/CSS).
- `processors/`: Core logic modules for each phase (Transcription, Translator, Subtitle Renderer, Muxer) and exporters (`ae_export.py`).
- `models/`: Python data models (e.g., Pydantic schemas for the transcript).
- `utils/`: Reusable utility functions (FFmpeg wrappers, file operations).
- `output/`: Default directory where processed jobs, temporary files, and final renders are saved (Ignored in Git).

## License

*(Add your specific license here, e.g., MIT, GPL, etc.)*
