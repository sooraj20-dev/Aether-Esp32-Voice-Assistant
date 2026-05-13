import os
from pathlib import Path

from dotenv import load_dotenv


# Load .env if it exists. This keeps API keys out of the code.
load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
GENERATED_AUDIO_DIR = BASE_DIR / "generated_audio"


# Flask server settings.
# Use 0.0.0.0 so an ESP32 on the same Wi-Fi can reach this PC.
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5000"))
DEBUG = os.getenv("DEBUG", "true").lower() == "true"


# Optional public/LAN base URL returned to the ESP32.
# Example: SERVER_URL=http://192.168.1.50:5000
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")


# Raw audio format sent by the ESP32.
# Keep these matching your ESP32 recording code.
INPUT_SAMPLE_RATE = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
INPUT_SAMPLE_WIDTH = int(os.getenv("INPUT_SAMPLE_WIDTH", "2"))  # 2 = 16-bit PCM
INPUT_CHANNELS = int(os.getenv("INPUT_CHANNELS", "1"))  # 1 = mono


# ESP32 DAC playback format.
# dacWrite(GPIO25, sample) expects unsigned 8-bit samples.
OUTPUT_SAMPLE_RATE = 8000
OUTPUT_SAMPLE_WIDTH = 1  # 1 byte = 8-bit
OUTPUT_CHANNELS = 1
OUTPUT_CODEC = "pcm_u8"  # unsigned 8-bit PCM inside WAV
OUTPUT_GAIN_DB = float(os.getenv("OUTPUT_GAIN_DB", "-3"))  # boost small DAC speakers
ENGLISH_TTS_VOICE = os.getenv(
    "ENGLISH_TTS_VOICE",
    "en-US-ChristopherNeural"
)

MALAYALAM_TTS_VOICE = os.getenv(
    "MALAYALAM_TTS_VOICE",
    "ml-IN-SobhanaNeural"
)

ENGLISH_TTS_RATE = "-5%"
ENGLISH_TTS_PITCH = "-4Hz"

MALAYALAM_TTS_RATE = "+0%"
MALAYALAM_TTS_PITCH = "+0Hz"

# OpenRouter settings. Change the model here without touching app logic.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "z-ai/glm-4.5-air:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# Keep replies short because the OLED has limited screen space.
MAX_REPLY_WORDS = int(os.getenv("MAX_REPLY_WORDS", "25"))


def ensure_folders():
    """Create folders needed for uploaded PCM/WAV files and generated replies."""
    UPLOAD_DIR.mkdir(exist_ok=True)
    GENERATED_AUDIO_DIR.mkdir(exist_ok=True)


def cleanup_old_files(folder_path, max_files=3):
    """
    Delete old files from a folder, keeping only the most recent max_files files.
    
    Args:
        folder_path: Path to the folder to clean
        max_files: Maximum number of files to keep (default 3)
    """
    try:
        files = list(folder_path.glob("*"))
        
        # Filter out directories, keep only files
        files = [f for f in files if f.is_file()]
        
        if len(files) > max_files:
            # Sort by modification time, oldest first
            files.sort(key=lambda f: f.stat().st_mtime)
            
            # Delete oldest files
            files_to_delete = files[:-max_files]
            
            for file_path in files_to_delete:
                try:
                    file_path.unlink()
                    print(f"[CLEANUP] Deleted: {file_path.name}", flush=True)
                except Exception as e:
                    print(f"[CLEANUP ERROR] Failed to delete {file_path.name}: {e}", flush=True)
    
    except Exception as e:
        print(f"[CLEANUP ERROR] Error in cleanup: {e}", flush=True)
