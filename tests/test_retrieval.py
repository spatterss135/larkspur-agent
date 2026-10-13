"""Retrieval (plan §9): venue docs split by ## heading, with stable {doc_id, section} ids."""

from langchain_core.embeddings import DeterministicFakeEmbedding

from agent.retrieval import KeywordRetriever, VectorRetriever, load_chunks
from evals.schema import DOC_IDS


def test_every_doc_is_split_into_sections_with_stable_ids():
    chunks = load_chunks()
    assert {c.doc_id for c in chunks} == set(DOC_IDS)
    assert len(chunks) == len({(c.doc_id, c.section) for c in chunks})  # unique
    assert [c.id for c in chunks] == [c.id for c in load_chunks()]  # stable
    parking = next(c for c in chunks if c.doc_id == "parking-transport")
    assert parking.section == "On-site parking"
    assert "120 cars" in parking.text and "##" not in parking.text


def test_a_search_returns_chunks_with_their_metadata():
    hits = KeywordRetriever(load_chunks()).search("Is there parking for our guests?", k=4)
    assert 1 <= len(hits) <= 4
    assert hits[0].doc_id == "parking-transport"
    assert {"doc_id", "section", "text"} <= set(hits[0].as_dict())


def test_the_vector_retriever_embeds_once_and_returns_k():
    calls = []

    class Counting(DeterministicFakeEmbedding):
        def embed_documents(self, texts):
            calls.append(len(texts))
            return super().embed_documents(texts)

    r = VectorRetriever(load_chunks(), Counting(size=32))
    r.search("parking", k=3)
    r.search("deposit", k=3)
    assert calls == [len(load_chunks())]
    assert len(r.search("tours", k=3)) == 3
