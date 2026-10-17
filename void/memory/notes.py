"""Note model, wikilink parsing and the on-disk markdown format.

The markdown files in an agent's vault are the source of truth for memory content
(DESIGN §10); the ``notes`` table is only an index over them. This module owns the file
format: :func:`render_note` produces a YAML-frontmatter markdown file that Obsidian can
open, and :func:`parse_note` is its inverse (byte-exact for the body). It also owns the
``## Related`` section convention used for explicit links, auto-links and backlinks, and
the split between a note's *content* (what gets embedded) and that section. Nothing here
touches the database or the filesystem, so the format can be tested in isolation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import yaml

__all__ = [
    "CHANNELS",
    "DEFAULT_CHANNEL",
    "OPERATOR_CHANNEL",
    "DEFAULT_TITLE",
    "RELATED_HEADING",
    "Provenance",
    "Note",
    "parse_wikilinks",
    "strip_wikilinks",
    "normalise_title",
    "render_frontmatter",
    "split_frontmatter",
    "body_of",
    "render_note",
    "parse_note",
    "split_related",
    "content_body",
    "add_related_links",
]

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_YAML_WIDTH = 1_000_000  # never fold long scalars onto several lines
DEFAULT_TITLE = "untitled"

# Frontmatter is *read* with libyaml when PyYAML was built with it (several times faster than the pure loader and
# identical on everything the kernel writes; the two differ only on malformed hand edits, e.g. a tab before a value).
# It is *written* with the pure dumper only: libyaml escapes characters outside the Basic Multilingual Plane (an
# emoji in a title) and treats NEL and long mapping keys differently, so its bytes differ and manifest hashes would
# move; the speed comes from memoising fragments instead (``_FRAGMENTS``).
_SAFE_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# A frontmatter block: the first line is ``---``, then whole lines up to the first other ``---`` line (each
# delimiter may carry trailing carriage returns, as ``split_frontmatter`` allows), then a newline or the end.
_FRONTMATTER_RE = re.compile(r"---\r*\n(?:[^\n]*\n)*?---\r*(?:\n|\Z)")

# Memo of rendered ``key: value`` fragments. A note's frontmatter is rendered at least twice per write (the file
# and its manifest hash) and again on every backlink, and most fragments (importance, created tick, tags, the
# native provenance block) repeat across notes; the pure dumper costs about a millisecond a call. Values are
# frozen with their types so ``1``, ``1.0`` and ``True`` never share an entry; the memo is cleared when full.
_FRAGMENTS: dict[tuple[str, object], str] = {}
_FRAGMENTS_MAX = 65_536

# ``operator`` is never written by the kernel on a fresh note: ``MemoryStore.reindex`` assigns it to a vault file
# whose content hash does not match the manifest (a hand edit in Obsidian, or a file the kernel never wrote), so
# human edits stay visible in the analysis instead of passing as the channel their frontmatter claims (§5.9).
CHANNELS: tuple[str, ...] = ("observed", "gossip", "inherited", "chronicle", "tracer", "operator")
DEFAULT_CHANNEL = "observed"
OPERATOR_CHANNEL = "operator"

RELATED_HEADING = "## Related"
_RELATED_RE = re.compile(r"(?m)^## Related[ \t]*$")
_HEADING_RE = re.compile(r"(?m)^#{1,6} ")


@dataclass
class Provenance:
    """Where a note came from. Native notes have ``hop == 0``, no source and channel ``observed``."""

    source_agent_id: str | None = None
    hop: int = 0
    origin_note_id: str | None = None
    origin_generation: int | None = None
    channel: str = DEFAULT_CHANNEL
    transfer_tick: int | None = None
    path_agents: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        """The frontmatter form, using the key names from DESIGN §10 (``path`` for path_agents)."""
        return {
            "source_agent": self.source_agent_id,
            "hop": int(self.hop),
            "origin_note": self.origin_note_id,
            "origin_generation": self.origin_generation,
            "channel": self.channel,
            "transfer_tick": self.transfer_tick,
            "path": [str(a) for a in self.path_agents],
        }

    @classmethod
    def from_mapping(cls, data: Any) -> Provenance:
        """Inverse of :meth:`to_mapping`; tolerates the dataclass field names and missing keys.

        An unknown channel falls back to ``observed`` so hand-edited files still index.
        """
        if not isinstance(data, dict):
            return cls()
        source = data.get("source_agent", data.get("source_agent_id"))
        origin = data.get("origin_note", data.get("origin_note_id"))
        gen = data.get("origin_generation")
        channel = _as_str(data.get("channel"), DEFAULT_CHANNEL).strip()
        if channel not in CHANNELS:
            channel = DEFAULT_CHANNEL
        transfer = data.get("transfer_tick")
        path = data.get("path", data.get("path_agents"))
        return cls(
            source_agent_id=_opt_str(source),
            hop=_as_int(data.get("hop"), 0),
            origin_note_id=_opt_str(origin),
            origin_generation=None if gen is None else _as_int(gen, 0),
            channel=channel,
            transfer_tick=None if transfer is None else _as_int(transfer, 0),
            path_agents=_as_str_list(path),
        )


@dataclass
class Note:
    note_id: str
    agent_id: str
    title: str
    body: str
    created_tick: int
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)
    archived: bool = False
    path: str | None = None


# --- wikilinks ----------------------------------------------------------------------------


def parse_wikilinks(body: str) -> list[str]:
    """Unique link targets in order of first appearance.

    Handles ``[[Title]]`` and ``[[Title|alias]]``; targets are trimmed and empty ones dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for match in _WIKILINK_RE.finditer(body):
        target = match.group(1).split("|", 1)[0].strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def strip_wikilinks(text: str) -> str:
    """Replace every ``[[Title|alias]]`` with its display text (used to derive titles)."""

    def repl(match: re.Match[str]) -> str:
        target, _, alias = match.group(1).partition("|")
        return (alias or target).strip()

    return _WIKILINK_RE.sub(repl, text)


def normalise_title(title: str) -> str:
    """Case-insensitive, whitespace-normalised key used to resolve links to notes."""
    return " ".join(title.split()).casefold()


# --- the ``## Related`` section -----------------------------------------------------------


def split_related(body: str) -> tuple[str, str]:
    """``(content, section)``: the ``## Related`` heading up to the next heading or the end."""
    match = _RELATED_RE.search(body)
    if match is None:
        return body, ""
    nxt = _HEADING_RE.search(body, match.end())
    end = len(body) if nxt is None else nxt.start()
    return body[: match.start()] + body[end:], body[match.start() : end]


def content_body(body: str) -> str:
    """The body without its ``## Related`` section: what a note *says*, not what it links to."""
    return split_related(body)[0].strip()


def add_related_links(body: str, titles: Iterable[str]) -> tuple[str, list[str]]:
    """Add ``- [[title]]`` lines under the ``## Related`` section (created at the end if missing).

    A title already linked anywhere in the body (case-insensitively) is skipped, so links are
    never duplicated. Returns the new body and the titles actually added.
    """
    present = {normalise_title(t) for t in parse_wikilinks(body)}
    added: list[str] = []
    for raw in titles:
        title = " ".join(raw.split())
        key = normalise_title(title)
        if title and key not in present:
            present.add(key)
            added.append(title)
    if not added:
        return body, []
    bullets = "\n".join(f"- [[{t}]]" for t in added)
    match = _RELATED_RE.search(body)
    if match is None:
        base = body.rstrip("\n")
        prefix = base + "\n\n" if base else ""
        return f"{prefix}{RELATED_HEADING}\n{bullets}", added
    nxt = _HEADING_RE.search(body, match.end())
    if nxt is None:
        return body.rstrip("\n") + "\n" + bullets, added
    head = body[: nxt.start()].rstrip("\n") + "\n" + bullets + "\n\n"
    return head + body[nxt.start() :], added


# --- frontmatter --------------------------------------------------------------------------


def render_frontmatter(mapping: dict[str, Any]) -> str:
    """``---`` block for ``mapping``: scalar keys in block style, collections in flow style.

    Keys are the module's own identifiers (never user text), so they are written verbatim.
    Fragments are memoised (see ``_FRAGMENTS``); the bytes are exactly what the pure dumper writes.
    """
    return "---\n" + "".join(_fragment(key, value) for key, value in mapping.items()) + "---\n"


def _fragment(key: str, value: Any) -> str:
    """The ``key: value`` line(s) for one frontmatter entry, from the memo when this exact value was rendered before."""
    try:
        memo_key = (key, _freeze(value))
    except TypeError:  # not a plain value: render it without the memo
        return _render_fragment(key, value)
    hit = _FRAGMENTS.get(memo_key)
    if hit is None:
        hit = _render_fragment(key, value)
        if len(_FRAGMENTS) >= _FRAGMENTS_MAX:
            _FRAGMENTS.clear()
        _FRAGMENTS[memo_key] = hit
    return hit


def _render_fragment(key: str, value: Any) -> str:
    if isinstance(value, (list, tuple, dict)):
        flow = yaml.safe_dump(
            list(value) if isinstance(value, tuple) else value,
            default_flow_style=True,
            sort_keys=False,
            allow_unicode=True,
            width=_YAML_WIDTH,
        ).strip()
        return f"{key}: {flow}\n"
    return yaml.safe_dump(
        {key: value},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )


def _freeze(value: Any) -> object:
    """A hashable stand-in for a plain value (str, int, bool, float, None, and lists, tuples and dicts of them) that
    keeps every exact type and the dict key order, so two values share a key only when the dumper writes the same
    bytes for both. Anything else raises ``TypeError`` and is rendered without the memo."""
    kind = type(value)
    if kind is list or kind is tuple:
        return (kind, tuple(_freeze(v) for v in value))
    if kind is dict:
        return (kind, tuple((_freeze(k), _freeze(v)) for k, v in value.items()))
    if kind is float:
        return (kind, repr(value))  # repr tells -0.0 from 0.0 (equal as floats, written differently)
    if kind is str or kind is int or kind is bool or value is None:
        return (kind, value)
    raise TypeError(f"{kind.__name__} values are not memoised")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into its frontmatter mapping and the rest, verbatim.

    Text without a well-formed frontmatter block (or with invalid YAML in it) is returned
    whole as the body with an empty mapping.
    """
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") != "---":
            continue
        try:
            data = _safe_load("\n".join(lines[1:i]))
        except yaml.YAMLError:
            return {}, text
        if not isinstance(data, dict):
            data = {}
        return data, "\n".join(lines[i + 1 :])
    return {}, text


def _safe_load(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_SAFE_LOADER)  # a safe loader: the C one or the pure one
    except UnicodeError:
        # libyaml cannot encode a lone surrogate (possible in agent text decoded from JSON); the pure loader
        # reports it as a YAMLError, which is what callers expect for a malformed block
        return yaml.safe_load(text)


def body_of(text: str) -> str:
    """The text after the frontmatter block, without parsing the YAML in it: ``split_frontmatter(text)[1]``.

    For the read paths that discard the mapping (``MemoryStore._load``). It equals ``split_frontmatter`` on every
    file the kernel writes and wherever there is no block (no leading ``---`` line, or no closing one). The one
    difference is a delimited block that is not valid YAML (only a hand edit can produce one): ``split_frontmatter``
    then returns the whole text, ``body_of`` still the text after the block.
    """
    match = _FRONTMATTER_RE.match(text)
    return text if match is None else text[match.end() :]


# --- note <-> markdown --------------------------------------------------------------------


def render_note(note: Note) -> str:
    """Markdown file content for ``note`` in the DESIGN §10 format.

    Exactly one newline is appended after the body; :func:`parse_note` removes exactly one,
    so the body round-trips byte-exact.
    """
    front: dict[str, Any] = {
        "id": note.note_id,
        "title": note.title,
        "created_tick": int(note.created_tick),
        "importance": float(note.importance),
        "tags": [str(t) for t in note.tags],
        "provenance": note.provenance.to_mapping(),
    }
    return render_frontmatter(front) + note.body + "\n"


def parse_note(text: str, agent_id: str | None = None, path: str | None = None) -> Note:
    """Inverse of :func:`render_note`. Missing frontmatter keys fall back to defaults."""
    data, rest = split_frontmatter(text)
    body = rest[:-1] if rest.endswith("\n") else rest
    title = _as_str(data.get("title"), "").strip() or DEFAULT_TITLE
    note_id = _as_str(data.get("id"), "").strip() or _fallback_id(title, body, path)
    return Note(
        note_id=note_id,
        agent_id=agent_id or "",
        title=title,
        body=body,
        created_tick=_as_int(data.get("created_tick"), 0),
        importance=_as_float(data.get("importance"), 0.5),
        tags=_as_str_list(data.get("tags")),
        links=parse_wikilinks(body),
        provenance=Provenance.from_mapping(data.get("provenance")),
        archived=False,
        path=path,
    )


# --- coercion helpers ---------------------------------------------------------------------


def _fallback_id(title: str, body: str, path: str | None) -> str:
    """Stable id for a file that carries none (hand-written or foreign notes)."""
    key = path if path else f"{title}\x1f{body}"
    return "note_" + hashlib.blake2b(key.encode("utf-8"), digest_size=7).hexdigest()


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return [str(value)]
