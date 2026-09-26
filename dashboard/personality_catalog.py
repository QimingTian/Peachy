"""Personality labels + demo hints for conversation UI buttons."""

from __future__ import annotations

DEFAULT_OPTION = "(built-in default)"

# Built-in Reachy profiles — friendly labels for demo switching.
PERSONALITIES: list[dict[str, str]] = [
    {"id": DEFAULT_OPTION, "label": "Default", "hint": "Factory Peachy — balanced classroom helper"},
    {"id": "default", "label": "Default+", "hint": "Standard profile folder"},
    {"id": "hype_bot", "label": "Hype Bot", "hint": "High energy — great for demos and kids"},
    {"id": "victorian_butler", "label": "Butler", "hint": "Formal, dry wit — faculty-room favorite"},
    {"id": "mad_scientist_assistant", "label": "Mad Scientist", "hint": "Excited about experiments"},
    {"id": "noir_detective", "label": "Noir Detective", "hint": "Mysterious, deadpan"},
    {"id": "nature_documentarian", "label": "Documentarian", "hint": "Calm David Attenborough vibes"},
    {"id": "captain_circuit", "label": "Captain Circuit", "hint": "Robotics captain — on-theme"},
    {"id": "chess_coach", "label": "Chess Coach", "hint": "Patient strategy tutor"},
    {"id": "cosmic_kitchen", "label": "Cosmic Kitchen", "hint": "Whimsical cooking show host"},
    {"id": "mars_rover", "label": "Mars Rover", "hint": "Slow, wonder-filled explorer"},
    {"id": "time_traveler", "label": "Time Traveler", "hint": "Anachronistic storyteller"},
    {"id": "bored_teenager", "label": "Bored Teen", "hint": "Deadpan sarcasm"},
    {"id": "sorry_bro", "label": "Sorry Bro", "hint": "Apologetic bro energy"},
    {"id": "tedai", "label": "TED Talk", "hint": "Inspirational speaker mode"},
    {"id": "example", "label": "Example", "hint": "Template profile"},
]

_BY_ID = {p["id"]: p for p in PERSONALITIES}


def catalog_for_ids(profile_ids: list[str]) -> list[dict[str, str]]:
    """Return personality cards for API choices, preserving order."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for pid in profile_ids:
        if pid in seen:
            continue
        seen.add(pid)
        if pid in _BY_ID:
            out.append(_BY_ID[pid])
        else:
            label = pid.split("/")[-1].replace("_", " ").title()
            out.append({"id": pid, "label": label, "hint": "Custom profile"})
    return out
