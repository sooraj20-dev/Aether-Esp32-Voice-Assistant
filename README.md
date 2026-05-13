# ESP32 AI Voice Assistant Flask Backend

Simple Flask backend for:

1. ESP32 raw PCM upload
2. Google speech recognition
3. OpenRouter AI response
4. TTS reply generation
5. ESP32-compatible WAV serving

## Folder Structure

```text
project/
├── app.py
├── config.py
├── requirements.txt
├── services/
│   ├── speech_to_text.py
│   ├── ai_brain.py
│   └── tts_service.py
├── uploads/
└── generated_audio/
```

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```env
OPENROUTER_API_KEY=your_openrouter_key_here
OPENROUTER_MODEL=openai/gpt-4o-mini
SERVER_URL=http://YOUR_PC_LAN_IP:5000
```

Run:

```bash
python app.py
```

## FFmpeg

This project uses `pydub` to convert gTTS MP3 audio into ESP32-ready WAV.
Install FFmpeg and make sure `ffmpeg` is available in your terminal PATH.

Windows:

```bash
winget install Gyan.FFmpeg
```

Then restart your terminal and test:

```bash
ffmpeg -version
```

## ESP32 Audio Notes

The `/upload_audio` route expects raw PCM bytes.

Default input format:

- 16-bit signed PCM
- mono
- 16000 Hz

If your ESP32 records a different format, edit these values in `.env`:

```env
INPUT_SAMPLE_RATE=16000
INPUT_SAMPLE_WIDTH=2
INPUT_CHANNELS=1
```

Generated reply WAV format:

- unsigned 8-bit PCM
- mono
- 8000 Hz
- uncompressed WAV

This matches simple DAC playback:

```cpp
dacWrite(25, sample);
```

For a future MAX98357A upgrade, you can change the output conversion in
`services/tts_service.py` without rewriting the Flask routes.
