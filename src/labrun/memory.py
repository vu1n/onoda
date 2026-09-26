"""Provenance-filtered memory lessons and frozen releases (PORTABLE_RUN_SPEC §7, T05/T23).

A lesson is a JSON artifact labelled "memory-lesson" naming its author run and
its provenance: `sources` (invocation ids or artifact digests) and
`derived_from` (other lesson digests). A `MemoryRelease` freezes an ordered
list of lesson digests as its own artifact.

Retrieval through a `RunView` returns a lesson only if every source, every
transitively derived_from lesson, and those lessons' sources are visible to
the view, and if a lesson authored inside the view's lineage was written
before the cut. That rejects both lessons citing future evidence and
"disguised summaries" that cite only early evidence but were derived from, or
written with knowledge of, the future. Every retrieval is journaled.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from labrun.canonical import is_digest
from labrun.errors import LabrunError
from labrun.forks import RunView
from labrun.store import Store, record_ref

LESSON_LABEL = "memory-lesson"
RELEASE_LABEL = "memory-release"
FILTERS = ("provenance-visible", "authored-before-cut", "author-cut-within-view")


@dataclass(frozen=True)
class Lesson:
    digest: str
    run_id: str
    text: str
    sources: tuple[str, ...]
    derived_from: tuple[str, ...]


@dataclass(frozen=True)
class MemoryRelease:
    digest: str
    lessons: tuple[str, ...]


def _source_ref(source: str) -> str:
    return source if is_digest(source) else record_ref("invocation", source)


def load_lesson(store: Store, digest: str) -> Lesson:
    if LESSON_LABEL not in store.descriptor(digest).labels:
        raise LabrunError(f"{digest} is not a memory lesson")
    data = store.get_json(digest)
    prov = data["provenance"]
    return Lesson(
        digest, data["run_id"], data["text"], tuple(prov["sources"]), tuple(prov["derived_from"])
    )


def write_lesson(
    store: Store,
    run_id: str,
    text: str,
    sources: Iterable[str],
    derived_from: Iterable[str] = (),
) -> str:
    """Commit a lesson authored by `run_id`; its provenance must be visible to that run."""
    sources, derived_from = list(sources), list(derived_from)
    view = RunView(store, run_id)
    for ref in [*map(_source_ref, sources), *derived_from]:
        view.check(ref)
    for d in derived_from:
        load_lesson(store, d)
    lesson = {
        "kind": LESSON_LABEL,
        "run_id": run_id,
        "text": text,
        "provenance": {"sources": sources, "derived_from": derived_from},
    }
    digest = store.put_json(lesson, labels=[LESSON_LABEL]).digest
    store.append_event(
        run_id,
        actor="memory",
        type="memory.lesson",
        payload={"sources": sources, "derived_from": derived_from},
        payload_refs={"lesson": digest},
    )
    return digest


def freeze_release(store: Store, lessons: Iterable[str]) -> MemoryRelease:
    ordered = tuple(lessons)
    for d in ordered:
        load_lesson(store, d)
    desc = store.put_json({"kind": RELEASE_LABEL, "lessons": list(ordered)}, labels=[RELEASE_LABEL])
    return MemoryRelease(desc.digest, ordered)


def load_release(store: Store, digest: str) -> MemoryRelease:
    if RELEASE_LABEL not in store.descriptor(digest).labels:
        raise LabrunError(f"{digest} is not a memory release")
    return MemoryRelease(digest, tuple(store.get_json(digest)["lessons"]))


def rejection(view: RunView, lesson: Lesson, _seen: frozenset[str] = frozenset()) -> str | None:
    """Why `lesson` is ineligible for `view`, or None if it may be retrieved."""
    for s in lesson.sources:
        if not view.can_see(_source_ref(s)):
            return f"source-invisible:{s}"
    for d in lesson.derived_from:
        if not view.can_see(d):
            return f"derived-from-invisible:{d}"
        if d in _seen:
            return f"derived-from-cycle:{d}"
        inner = rejection(view, load_lesson(view.store, d), _seen | {lesson.digest})
        if inner:
            return f"derived-from-ineligible:{d}"
    if lesson.run_id in view.lineage and not view.can_see(lesson.digest):
        return "authored-after-cut"
    if lesson.run_id not in view.lineage and _saw_beyond_cut(view, lesson.run_id):
        return "author-saw-beyond-cut"
    return None


def _saw_beyond_cut(view: RunView, author: str) -> bool:
    """A sibling branch forked later from a shared ancestor knows that ancestor's future."""
    checkpoints = view.store.checkpoints(author)
    if not checkpoints:
        return True  # unknown knowledge is treated as unrestricted
    author_cut = checkpoints[0][2].ancestry_cut
    return any(run in view.cut and seq > view.cut[run] for run, seq in author_cut.items())


def _score(text: str, query: str) -> int:
    lowered = text.lower()
    return sum(1 for tok in query.lower().split() if tok in lowered)


def retrieve(view: RunView, release_digest: str, query: str) -> list[Lesson]:
    """Rank release lessons by query, drop ineligible ones, journal the retrieval."""
    store = view.store
    release = load_release(store, release_digest)
    lessons = [load_lesson(store, d) for d in release.lessons]
    ranked = sorted(
        ((i, lesson, _score(lesson.text, query)) for i, lesson in enumerate(lessons)),
        key=lambda t: (-t[2], t[0]),
    )
    candidates = [lesson for _, lesson, score in ranked if score > 0 or not query.strip()]
    rejected: dict[str, str] = {}
    selected: list[Lesson] = []
    for lesson in candidates:
        reason = rejection(view, lesson)
        if reason:
            rejected[lesson.digest] = reason
        else:
            selected.append(lesson)
    store.append_event(
        view.run_id,
        actor="memory",
        type="memory.retrieval",
        payload={
            "query": query,
            "release": release_digest,
            "filters": list(FILTERS),
            "candidates": [c.digest for c in candidates],
            "rejected": rejected,
            "selected": [s.digest for s in selected],
        },
        payload_refs={
            "release": release_digest,
            **{f"lesson.{i}": s.digest for i, s in enumerate(selected)},
        },
    )
    return selected
