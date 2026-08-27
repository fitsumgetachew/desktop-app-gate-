"""List the voices this machine can speak with, and try them out.

    .venv/bin/python scripts/list_voices.py            # list voices
    .venv/bin/python scripts/list_voices.py --test 2   # speak a sample with #2

Whatever this prints is what TTS_VOICE in .env accepts: any part of a voice's
name or id (case-insensitive) with the pyttsx3 engine, or one of spd-say's
fixed voice types when that fallback is active. TTS_RATE is words per minute
(~120 slow, ~170 normal, ~220 fast); TTS_VOLUME is 0.0-1.0.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SAMPLE = "Thank you, Fitsum."


def try_pyttsx3(test_index: int | None) -> bool:
    try:
        import pyttsx3

        engine = pyttsx3.init()
    except Exception:
        return False

    voices = engine.getProperty("voices") or []
    print(f"Speech engine: pyttsx3 ({len(voices)} voices installed)\n")
    print(f"{'#':>3}  {'name':40} id")
    for index, voice in enumerate(voices):
        name = getattr(voice, "name", "") or "?"
        print(f"{index:>3}  {name:40} {voice.id}")
    print(
        "\nSet TTS_VOICE in .env to any part of a name or id above,"
        ' e.g. TTS_VOICE=zira or TTS_VOICE=female'
    )
    if test_index is not None and 0 <= test_index < len(voices):
        chosen = voices[test_index]
        print(f"\nSpeaking with #{test_index} ({getattr(chosen, 'name', chosen.id)})...")
        engine.setProperty("voice", chosen.id)
        engine.say(SAMPLE)
        engine.runAndWait()
    return True


def try_spd_say(test_index: int | None) -> bool:
    from smart_gate.services.speech_service import SpdSaySpeaker

    try:
        subprocess.run(["spd-say", "--version"], capture_output=True, timeout=5)
    except Exception:
        return False

    types = SpdSaySpeaker.VOICE_TYPES
    print("Speech engine: spd-say (speech-dispatcher fallback)\n")
    for index, voice_type in enumerate(types):
        print(f"{index:>3}  {voice_type}")
    print(
        "\nSet TTS_VOICE in .env to one of the types above, e.g. TTS_VOICE=FEMALE1"
        "\n(For many more voices: sudo apt install espeak libespeak1, then rerun"
        " this script — pyttsx3 will take over.)"
    )
    if test_index is not None and 0 <= test_index < len(types):
        print(f"\nSpeaking with {types[test_index]}...")
        subprocess.run(["spd-say", "-w", "-t", types[test_index], SAMPLE], timeout=30)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", type=int, default=None, metavar="N",
                        help="speak a sample sentence with voice number N")
    args = parser.parse_args()

    if try_pyttsx3(args.test):
        return 0
    if try_spd_say(args.test):
        return 0
    print("No speech engine on this machine.")
    print("  Linux:   sudo apt install espeak libespeak1   (or speech-dispatcher)")
    print("  Windows: SAPI is built in — this should not happen")
    return 1


if __name__ == "__main__":
    sys.exit(main())
