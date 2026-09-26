"""Voice labels + short character notes for conversation UI buttons."""

from __future__ import annotations

# OpenAI Realtime / TTS voices (see OpenAI voice options docs).
OPENAI_VOICES: list[dict[str, str]] = [
    {"id": "cedar", "label": "Cedar", "hint": "Rich, natural — OpenAI’s top pick for Realtime"},
    {"id": "marin", "label": "Marin", "hint": "Bright, expressive — OpenAI’s other Realtime pick"},
    {"id": "coral", "label": "Coral", "hint": "Warm and friendly — upbeat, approachable"},
    {"id": "sage", "label": "Sage", "hint": "Calm and thoughtful — steady, reassuring"},
    {"id": "shimmer", "label": "Shimmer", "hint": "Light and lively — energetic, smiling"},
    {"id": "verse", "label": "Verse", "hint": "Dynamic storyteller — conversational flow"},
    {"id": "ballad", "label": "Ballad", "hint": "Soft and melodic — gentle, narrative"},
    {"id": "echo", "label": "Echo", "hint": "Clear and even — neutral presenter tone"},
    {"id": "ash", "label": "Ash", "hint": "Soft and relaxed — quiet confidence"},
    {"id": "alloy", "label": "Alloy", "hint": "Balanced neutral — general-purpose"},
]

GEMINI_VOICES: list[dict[str, str]] = [
    {"id": "Kore", "label": "Kore", "hint": "Firm, clear default — direct and composed"},
    {"id": "Aoede", "label": "Aoede", "hint": "Breezy and light — casual, airy"},
    {"id": "Puck", "label": "Puck", "hint": "Upbeat and playful — mischievous energy"},
    {"id": "Charon", "label": "Charon", "hint": "Informative — steady explainer tone"},
    {"id": "Fenrir", "label": "Fenrir", "hint": "Excitable — fast, enthusiastic"},
    {"id": "Leda", "label": "Leda", "hint": "Youthful — younger, curious"},
    {"id": "Orus", "label": "Orus", "hint": "Firm — authoritative, no-nonsense"},
    {"id": "Zephyr", "label": "Zephyr", "hint": "Bright — crisp and forward"},
]

HF_VOICES: list[dict[str, str]] = [
    {"id": "Aiden", "label": "Aiden", "hint": "Default HF speaker — neutral male"},
    {"id": "Ryan", "label": "Ryan", "hint": "Male — casual American"},
    {"id": "Serena", "label": "Serena", "hint": "Female — warm and clear"},
    {"id": "Vivian", "label": "Vivian", "hint": "Female — bright and friendly"},
    {"id": "Dylan", "label": "Dylan", "hint": "Male — younger tone"},
    {"id": "Eric", "label": "Eric", "hint": "Male — deeper voice"},
    {"id": "Sohee", "label": "Sohee", "hint": "Female — Korean speaker profile"},
    {"id": "Ono_Anna", "label": "Ono Anna", "hint": "Female — soft and gentle"},
    {"id": "Uncle_Fu", "label": "Uncle Fu", "hint": "Male — mature, storyteller"},
]

_CATALOGS = {
    "openai": OPENAI_VOICES,
    "gemini": GEMINI_VOICES,
    "huggingface": HF_VOICES,
}


def catalog_for_ids(voice_ids: list[str]) -> list[dict[str, str]]:
    """Return voice cards for known IDs, preserving API order with hints."""
    by_id: dict[str, dict[str, str]] = {}
    for voices in _CATALOGS.values():
        for v in voices:
            by_id[v["id"]] = v
    out: list[dict[str, str]] = []
    for vid in voice_ids:
        if vid in by_id:
            out.append(by_id[vid])
        else:
            out.append({"id": vid, "label": vid.replace("_", " ").title(), "hint": "Available on robot"})
    return out
