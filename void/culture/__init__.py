"""Culture: horizontal transmission (gossip) and the shared record (chronicle).

Gossip copies notes between agents that talk, with provenance and a per-hop drift
metric (DESIGN §10, §11.1). The chronicle turns the public event log into a daily
newspaper of sanitized names and numbers (DESIGN §11.2, §9.5).
"""

from void.culture.chronicle import Chronicle, ChronicleDoc
from void.culture.gossip import Gossip, paraphrase_scripted

__all__ = ["Chronicle", "ChronicleDoc", "Gossip", "paraphrase_scripted"]
