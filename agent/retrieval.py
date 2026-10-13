"""Retrieval over the venue docs (plan §9).

Each `##` section of each doc in venue/docs is one chunk, with metadata {doc_id, section}
that stays stable across runs. The vector retriever embeds every chunk once at startup
(deterministic, so it's safe to reuse across eval runs). The keyword retriever needs no
API key; tests and offline runs use it.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DOCS = Path(__file__).resolve().parents[1] / "venue" / "docs"
WORD = re.compile(r"[a-z0-9]+")
STOP = set(
    "a an and are as at be by can do does for from have how i in is it of on or our "
    "the there to we what when where which will with you your".split()
)


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    title: str
    section: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.doc_id}#{self.section}"

    def as_dict(self) -> dict[str, str]:
        return {"doc_id": self.doc_id, "section": self.section, "text": self.text}


def load_chunks(folder: Path = DOCS) -> list[Chunk]:
    chunks = []
    for path in sorted(folder.glob("*.md")):
        title, section, lines = path.stem, None, []
        for line in path.read_text(encoding="utf-8").splitlines() + ["## "]:
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                if section and "".join(lines).strip():
                    chunks.append(Chunk(path.stem, title, section, "\n".join(lines).strip()))
                section, lines = line[3:].strip(), []
            elif section is not None:
                lines.append(line)
    return chunks


class Retriever(Protocol):
    def search(self, query: str, k: int = 4) -> list[Chunk]: ...


def _terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


class KeywordRetriever:
    """BM25-style scoring over title, section and text. Offline and deterministic."""

    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.docs = [Counter(_terms(f"{c.title} {c.section} {c.text}")) for c in chunks]
        df = Counter(t for d in self.docs for t in d)
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.avg = sum(sum(d.values()) for d in self.docs) / max(n, 1)

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        q = set(_terms(query))
        # crude stemming so "guests" finds "guest" and "tours" finds "tour"
        q |= {t[:-1] for t in q if t.endswith("s") and len(t) > 3}
        scored = []
        for chunk, d in zip(self.chunks, self.docs, strict=True):
            length = sum(d.values())
            score = 0.0
            for t in q:
                f = d.get(t, 0) + d.get(t + "s", 0)
                if f:
                    score += (
                        self.idf.get(t, 0.5)
                        * f
                        * 2.2
                        / (f + 1.2 * (0.25 + 0.75 * length / self.avg))
                    )
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]


class VectorRetriever:
    """Embeds every chunk once, then answers queries by cosine similarity."""

    def __init__(self, chunks: list[Chunk], embeddings: Any):
        from langchain_core.documents import Document
        from langchain_core.vectorstores import InMemoryVectorStore

        self.store = InMemoryVectorStore(embeddings)
        self.by_id = {c.id: c for c in chunks}
        self.store.add_documents(
            [Document(page_content=f"{c.title} > {c.section}\n{c.text}", id=c.id) for c in chunks],
            ids=[c.id for c in chunks],
        )

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        return [self.by_id[d.id] for d in self.store.similarity_search(query, k=k)]


def default_retriever() -> Retriever:
    """The real retriever (OpenAI embeddings) when a key is set, else keywords."""
    import os

    if os.environ.get("OPENAI_API_KEY"):
        from langchain_openai import OpenAIEmbeddings

        model = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
        return VectorRetriever(load_chunks(), OpenAIEmbeddings(model=model))
    return KeywordRetriever(load_chunks())
