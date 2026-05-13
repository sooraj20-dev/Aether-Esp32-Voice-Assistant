from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, url_for

import config
from services.ai_brain import ask_ai
from services.speech_to_text import pcm_to_wav, recognize_speech, validate_audio
from services.tts_service import create_esp32_wav


app = Flask(__name__)
config.ensure_folders()


def log(message):
    """Small readable logger for debugging ESP32 requests."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


@app.before_request
def log_request():
    """
    Log every request that reaches Flask.

    If the ESP32 sends audio but this line does not print, the problem is Wi-Fi,
    IP address, firewall, or the ESP32 URL. If it prints with 0 bytes, the ESP32
    reached Flask but did not send an audio body.
    """
    size = request.content_length if request.content_length is not None else "unknown"
    log(f"{request.method} {request.path} from {request.remote_addr} ({size} bytes)")


@app.get("/")
@app.get("/health")
def health():
    """Simple test route for browser and ESP32 connection checks."""
    return jsonify({"status": "ok", "message": "ESP32 Flask backend is running."})


@app.get("/test_ai")
def test_ai():
    """
    Browser test for OpenRouter.

    Use this before ESP32 testing. If this fails, the issue is API key, model,
    internet, or OpenRouter settings, not the microphone or audio upload.
    """
    try:
        reply = ask_ai("Reply with one short sentence saying the AI is working.")
        return jsonify({"status": "ok", "assistant_reply": reply})
    except Exception as error:
        log(f"AI TEST ERROR: {error}")
        return jsonify({"status": "error", "message": str(error)}), 500


def make_audio_url(filename):
    """
    Build the URL that the ESP32 will download.

    SERVER_URL is best for real hardware because the ESP32 needs your PC LAN IP,
    for example: http://192.168.1.50:5000
    """
    if config.SERVER_URL:
        return f"{config.SERVER_URL}/tts/{filename}"
    return url_for("get_tts", filename=filename, _external=True)


def get_esp32_audio_bytes():
    """
    ESP32 usually uploads raw PCM as the whole POST body.

    This also supports form uploads named "audio" so you can test from Postman,
    curl, or a browser without changing the backend.
    """
    if "audio" in request.files:
        return request.files["audio"].read()
    return request.get_data()


@app.post("/upload_audio")
def upload_audio():
    """
    Main ESP32 route:
    1. receive raw PCM
    2. wrap it into a WAV file for SpeechRecognition
    3. send text to the AI brain
    4. generate an ESP32 DAC-compatible WAV reply
    5. return JSON with recognized text, reply text, and audio URL
    """
    try:
        audio_bytes = get_esp32_audio_bytes()
        if not audio_bytes:
            return jsonify({"error": "empty_audio", "message": "No audio received."}), 400

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        pcm_path = config.UPLOAD_DIR / f"recording_{timestamp}.pcm"
        wav_path = config.UPLOAD_DIR / f"recording_{timestamp}.wav"
        reply_path = config.GENERATED_AUDIO_DIR / f"reply_{timestamp}.wav"

        pcm_path.write_bytes(audio_bytes)
        log(f"Upload saved: {pcm_path.name} ({len(audio_bytes)} bytes)")

        # SpeechRecognition needs a normal WAV container, not raw PCM bytes.
        pcm_to_wav(pcm_path, wav_path)
        validate_audio(wav_path)

        recognized_text = recognize_speech(wav_path)
        log(f"STT result: {recognized_text or '[silence]'}")

        if not recognized_text:
            assistant_reply = "I could not hear that clearly. Please try again."
        else:
            try:
                assistant_reply = ask_ai(recognized_text)
            except Exception as error:
                log(f"AI ERROR: {error}")
                assistant_reply = "My AI connection failed. Please try again."

        log(f"AI reply: {assistant_reply}")

        # Important: TTS is generated from the assistant reply, not user text.
        try:
            create_esp32_wav(assistant_reply, reply_path)
            log(f"TTS generated: {reply_path.name}")
        except Exception as tts_error:
            log(f"TTS ERROR: {tts_error}")
            return jsonify({"error": "tts_error", "message": f"TTS generation failed: {str(tts_error)}"}), 500

        # Cleanup old files - keep only 3 most recent in each folder
        config.cleanup_old_files(config.UPLOAD_DIR, max_files=3)
        config.cleanup_old_files(config.GENERATED_AUDIO_DIR, max_files=3)

        return jsonify(
            {
                "recognized_text": recognized_text,
                "assistant_reply": assistant_reply,
                "audio_url": make_audio_url(reply_path.name),
            }
        )

    except Exception as error:
        log(f"ERROR: {error}")
        return jsonify({"error": "server_error", "message": str(error)}), 500


@app.get("/tts/<path:filename>")
def get_tts(filename):
    """Serve generated WAV files so the ESP32 can download and play them."""
    file_path = config.GENERATED_AUDIO_DIR / filename
    if not file_path.exists():
        return jsonify({"error": "missing_wav", "message": "Audio file not found."}), 404

    return send_from_directory(config.GENERATED_AUDIO_DIR, filename, mimetype="audio/wav")


if __name__ == "__main__":
    log(f"Server starting on http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
