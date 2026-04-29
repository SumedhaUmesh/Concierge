"""
Conversation Buffer — session-scoped memory for multi-turn continuity.

Stores recent turns so the orchestrator can resolve follow-ups like
"actually make it closer" or "why that one?" without restarting.
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Turn:
    role: str           # "user" | "assistant"
    text: str
    intent: Optional[str] = None
    actions: list = field(default_factory=list)
    ts: float = field(default_factory=time.time)


class ConversationBuffer:
    def __init__(self, max_turns: int = 10):
        self._turns: list[Turn] = []
        self._max = max_turns
        self.last_topic: Optional[str] = None      # e.g. "coffee", "scenic route"
        self.last_poi_results: list[dict] = []     # for "closer" / "different one" refs
        self.last_actions: list[dict] = []         # last executed action plan

    def add_user(self, text: str, intent: str = None):
        self._turns.append(Turn("user", text, intent))
        self._trim()

    def add_assistant(self, text: str, actions: list = None):
        self._turns.append(Turn("assistant", text, actions=actions or []))
        self._trim()

    def context_str(self, max_turns: int = 6) -> str:
        """Recent conversation formatted for LLM injection."""
        recent = self._turns[-max_turns:]
        if not recent:
            return "(no prior conversation)"
        lines = []
        for t in recent:
            prefix = "Driver" if t.role == "user" else "Concierge"
            lines.append(f"{prefix}: {t.text}")
        return "\n".join(lines)

    def resolve(self, text: str) -> str:
        """
        Expand reference-heavy follow-ups with prior context.
        'Actually make it closer' → appends what 'it' was so LLM can resolve.
        """
        lowered = text.lower()
        ref_words = {"it", "that", "closer", "further", "same", "instead",
                     "the other", "different", "another", "more", "less"}
        if any(w in lowered for w in ref_words):
            ctx = self.context_str(4)
            if ctx and ctx != "(no prior conversation)":
                return f"{text}\n[Prior context: {ctx}]"
        return text

    def _trim(self):
        if len(self._turns) > self._max:
            self._turns = self._turns[-self._max:]
