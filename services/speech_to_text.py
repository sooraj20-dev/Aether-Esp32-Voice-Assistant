import wave

import speech_recognition as sr

import config


def pcm_to_wav(pcm_path, wav_path):
    """
    Wrap ESP32 raw PCM bytes in a WAV container.

    The ESP32 sends bare samples, but Google STT through SpeechRecognition reads
    WAV files more reliably. No audio data is changed here; we only add headers.
    """
    pcm_data = pcm_path.read_bytes()

    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(config.INPUT_CHANNELS)
        wav.setsampwidth(config.INPUT_SAMPLE_WIDTH)
        wav.setframerate(config.INPUT_SAMPLE_RATE)
        wav.writeframes(pcm_data)

    return wav_path


def validate_audio(wav_path):
    """Reject empty or broken WAV files before sending them to speech recognition."""
    with wave.open(str(wav_path), "rb") as wav:
        frames = wav.getnframes()
        if frames == 0:
            raise ValueError("Audio file has no samples.")
        if wav.getnchannels() != config.INPUT_CHANNELS:
            raise ValueError("Unexpected channel count in uploaded audio.")

    return True


def recognize_speech(wav_path):
    """
    Convert speech to text using SpeechRecognition + Google STT.

    Returns an empty string for silence/unclear audio so app.py can safely reply
    without crashing the ESP32 request.
    """
    recognizer = sr.Recognizer()

    with sr.AudioFile(str(wav_path)) as source:
        audio = recognizer.record(source)

    try:
        return recognizer.recognize_google(audio).strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as error:
        raise RuntimeError(f"Google STT request failed: {error}") from error
