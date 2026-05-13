import asyncio
import re
import wave

import edge_tts
from pydub import AudioSegment

import config


def is_malayalam(text):
    """
    Detect Malayalam text using Unicode range.
    Requires at least 2 Malayalam characters
    to avoid false positives.
    """
    chars = re.findall(r'[\u0D00-\u0D7F]', text)
    return len(chars) >= 2


async def async_generate_tts(text, mp3_path, voice, rate, pitch):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch
    )

    await communicate.save(str(mp3_path))


def generate_tts_mp3(text, mp3_path):
    """
    Generate neural TTS MP3 with automatic language detection.
    """

    text = str(text).strip()

    if not text:
        text = "Please try again."

    # Language-based voice selection
    if is_malayalam(text):
        voice = config.MALAYALAM_TTS_VOICE
        rate = config.MALAYALAM_TTS_RATE
        pitch = config.MALAYALAM_TTS_PITCH
        language = "Malayalam"
    else:
        voice = config.ENGLISH_TTS_VOICE
        rate = config.ENGLISH_TTS_RATE
        pitch = config.ENGLISH_TTS_PITCH
        language = "English"
    print("\n[AI RESPONSE]")
    print(repr(text))
    print(f"[TTS] Language: {language}")
    print(f"[TTS] Voice: {voice}")
    print(f"[TTS] Text: {text}")

    asyncio.run(
        async_generate_tts(
            text,
            mp3_path,
            voice,
            rate,
            pitch
        )
    )


def create_esp32_wav(text, output_path):
    """
    Generate ESP32-compatible WAV:
    - unsigned 8-bit PCM
    - mono
    - 8000 Hz
    """

    temp_mp3 = output_path.with_suffix(".mp3")

    generate_tts_mp3(text, temp_mp3)

    audio = AudioSegment.from_file(temp_mp3)

    # IMPORTANT:
    # Order matters for cleaner DAC audio
    audio = (
        audio
        .set_frame_rate(config.OUTPUT_SAMPLE_RATE)
        .set_channels(config.OUTPUT_CHANNELS)
        .set_sample_width(config.OUTPUT_SAMPLE_WIDTH)
    )

    audio.export(
        output_path,
        format="wav",
        codec=config.OUTPUT_CODEC
    )

    temp_mp3.unlink(missing_ok=True)

    validate_esp32_wav(output_path)

    return output_path


def validate_esp32_wav(wav_path):
    """
    Validate generated WAV format.
    """

    with wave.open(str(wav_path), "rb") as wav:

        print("\n[WAV INFO]")
        print("Channels:", wav.getnchannels())
        print("Sample Rate:", wav.getframerate())
        print("Sample Width:", wav.getsampwidth())
        print("Compression:", wav.getcomptype())

        if wav.getnchannels() != config.OUTPUT_CHANNELS:
            raise ValueError("WAV must be mono.")

        if wav.getframerate() != config.OUTPUT_SAMPLE_RATE:
            raise ValueError("WAV must be 8000 Hz.")

        if wav.getsampwidth() != config.OUTPUT_SAMPLE_WIDTH:
            raise ValueError("WAV must be 8-bit.")

        if wav.getcomptype() != "NONE":
            raise ValueError("WAV must be uncompressed PCM.")

    return True