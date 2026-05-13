import requests

import config


def clean_key(key):
    """Remove accidental spaces or quotes copied into the .env file."""
    return key.strip().strip('"').strip("'")


def ask_ai(user_text):
    """
    Ask OpenRouter for a real assistant response.

    The system prompt keeps replies short for the ESP32 OLED and speaker. To
    swap models or providers later, only this file and config.py should change.
    """
    api_key = clean_key(config.OPENROUTER_API_KEY)

    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY. Add it to your .env file.")

    if not api_key.startswith("sk-or-v1-"):
        raise RuntimeError(
            "OPENROUTER_API_KEY does not look like an OpenRouter key. "
            "Create a key at https://openrouter.ai/keys and use a key that starts with sk-or-v1-."
        )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter recommends these optional headers for app identification.
        "HTTP-Referer": config.SERVER_URL or "http://localhost:5000",
        "X-Title": "ESP32 Voice Assistant",
    }

    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are AETHER, an intelligent AI assistant. "
                    "Always reply in the same language as the user. "
                    "If the user writes in Malayalam, reply only in proper Malayalam script. "
                    "Never use Manglish or transliterated Malayalam. "
                    "If the user writes in English, reply in English. "
                    "Keep responses concise, calm, and natural."
                )
                            },
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.4,
        "max_tokens": 80,
    }

    try:
        response = requests.post(config.OPENROUTER_URL, headers=headers, json=payload, timeout=30)
    except requests.RequestException as error:
        raise RuntimeError(f"OpenRouter network error: {error}") from error

    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text[:300]}")

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()
