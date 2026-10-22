"""Pick one resume version per posting, on this computer, before anything is sent.

With ``ai.resume_choice = "match"`` and a folder of resume versions, each posting
goes to the AI with the one version that fits it, instead of with every version:
smaller prompts, and less of your resume leaves the computer. The pick is plain
rules and word counting, so the same posting and files always give the same pick.

1. ``ai.resume_rules``: a version's terms, checked in the order written. A
   4-digit term matches the posting's job series ("0501"), any other term a whole
   word or phrase in its title ("budget", "financ*").
2. Otherwise, the version whose distinctive words (the ones the other versions
   don't share) best cover the posting's title and qualifications.
3. When no version clearly wins (this is deliberately strict), the posting goes with every version, as with
   ``resume_choice = "all"``, and the AI picks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .jobs import as_text, details, job_category_codes
from .resume_text import ResumeError, pick_resumes, read_resume, resume_names, wrap_versions
from .review import contains_term

# Tuned on a real folder of four similar budget resumes. Duties, requirements and education
# text is mostly generic and blurred the versions together, so only the title and the
# qualifications (the specialized experience HR screens for) count. And a version must
# lead the next by half again, else the AI compares them all: a wrong pick costs a note
# written against the wrong resume, while sending them all only costs a bigger prompt.
CLEAR_LEAD = 1.5
TITLE_WEIGHT = 3
POSTING_FIELDS = ("QualificationSummary",)  # on the posting, or else under its details
SERIES = re.compile(r"\d{4}")
WORD = re.compile(r"[a-z][a-z0-9]{2,}")
STOP_WORDS = frozenset("""
and the for with from that this are was were will have has had not but you your our their they them its
who what when where which while into onto over under about above below than then also such each all any
may must can could would should shall more most other others some one two three per via etc including
include includes included experience experienced work worked working job position duties duty responsible
responsibilities year years month months level grade specialized equivalent federal government service
""".split())


@dataclass
class ResumeSet:
    """The resume versions for a run, read once. ``pick`` says what each posting gets."""

    paths: list[Path]
    names: list[str]
    texts: list[str]
    left_out: int = 0
    match: bool = False
    rules: list[tuple[int, str]] = field(default_factory=list)  # (version index, term), in order
    _words: list[set[str]] = field(default_factory=list)
    _weights: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.combined = self.texts[0] if len(self.texts) == 1 else wrap_versions(self.names, self.texts)
        self._words = [words(text) for text in self.texts]
        count = len(self.texts)
        # Words every version has can't tell them apart, so they weigh nothing.
        for vocabulary in self._words:
            for word in vocabulary:
                self._weights[word] = self._weights.get(word, 0) + 1
        self._weights = {word: math.log(count / seen) for word, seen in self._weights.items() if seen < count}

    def pick(self, job: Mapping[str, Any]) -> "Pick":
        """What to send with this posting. Without ``match``, or when nothing clearly
        wins, every version (and the AI names the one it used)."""
        if not self.match or len(self.texts) < 2:
            return Pick(self.combined, self.names if len(self.texts) > 1 else [])
        index, reason = self.choose(job)
        if index is None:
            return Pick(self.combined, self.names)
        return Pick(self.texts[index], [], self.names[index], reason)

    def choose(self, job: Mapping[str, Any]) -> tuple[int | None, str]:
        """``(version index, why)``, or ``(None, "")`` when no version clearly wins."""
        title = as_text(job.get("PositionTitle"))
        codes = set(job_category_codes(dict(job)))
        for index, term in self.rules:
            if SERIES.fullmatch(term):
                if term in codes:
                    return index, f"your rule: series {term}"
            elif contains_term(title, term):
                return index, f'your rule: "{term}" in the title'
        scores = self.scores(job)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        best, runner_up = scores[ranked[0]], scores[ranked[1]]
        if best <= 0 or best < runner_up * CLEAR_LEAD:
            return None, ""
        return ranked[0], "closest wording to the posting"

    def scores(self, job: Mapping[str, Any]) -> list[float]:
        """How well each version's distinctive words cover the posting, in version order."""
        wanted = posting_words(job)
        scores = []
        for vocabulary in self._words:
            total = sum(self._weights.get(word, 0.0) * weight for word, weight in wanted.items() if word in vocabulary)
            scores.append(total / math.sqrt(max(len(vocabulary), 1)))
        return scores


@dataclass(frozen=True)
class Pick:
    text: str  # the resume text for the prompt
    names: list[str]  # version names the AI may answer with; empty when one version is sent
    name: str = ""  # the version picked here, if one was
    reason: str = ""  # why it was picked


def words(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower()) if word not in STOP_WORDS}


def posting_words(job: Mapping[str, Any]) -> dict[str, int]:
    """The words of the posting's title (counting extra) and ``POSTING_FIELDS``."""
    detail = details(dict(job))
    wanted: dict[str, int] = {}
    for name in POSTING_FIELDS:
        for word in words(as_text(job.get(name) or detail.get(name))):
            wanted[word] = 1
    for word in words(as_text(job.get("PositionTitle"))):
        wanted[word] = TITLE_WEIGHT
    return wanted


def load_resume_set(resume: Path | None, folder: Path | None, ai: Mapping[str, Any]) -> ResumeSet:
    """Read the resume versions and check ``ai.resume_rules`` against their names."""
    paths, left_out = pick_resumes(resume, folder)
    names = resume_names(paths)
    texts = [read_resume(path) for path in paths]
    match = ai.get("resume_choice") == "match"
    rules: list[tuple[int, str]] = []
    if match and len(paths) > 1:
        for key, terms in (ai.get("resume_rules") or {}).items():
            index = version_index(str(key), names, paths)
            rules += [(index, str(term).strip()) for term in terms if str(term).strip()]
    return ResumeSet(paths, names, texts, left_out, match, rules)


def version_index(key: str, names: list[str], paths: list[Path]) -> int:
    """A rule may name a version by its short name, file name or file name without
    the suffix, in any case."""
    wanted = key.strip().casefold()
    for index, (name, path) in enumerate(zip(names, paths)):
        if wanted in {name.casefold(), path.name.casefold(), path.stem.casefold()}:
            return index
    raise ResumeError(f'ai.resume_rules: no resume version named "{key}"; the versions are '
                      + ", ".join(f'"{name}"' for name in names))
