# rag.py
# Retrieval-augmented generation helpers for email-rag-simple.
# Loads documents from the knowledge/ folder into ChromaDB, embeds and
# indexes them, and retrieves the most relevant chunks for a given incoming
# email so Gemini can draft a grounded reply.
#
# The knowledge base is treated as ONE combined collection — there is no
# per-niche split, so retrieve() always searches across every chunk.

import os
import time

import chromadb
from google import genai

from config import CHROMA_DIR, COLLECTION_NAME, EMBED_MODEL, GEMINI_API_KEY

KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
EMBED_BATCH_SIZE = 10

_genai_client = genai.Client(api_key=GEMINI_API_KEY)


def _get_collection():
    """Open (or create) the single persistent Chroma collection."""
    client = chromadb.PersistentClient(path=CHROMA_DIR)
    return client.get_or_create_collection(name=COLLECTION_NAME)


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping ~size-char chunks."""
    chunks = []
    start = 0
    while start < len(text):
        chunk = text[start:start + size].strip()
        if chunk:
            chunks.append(chunk)
        start += size - overlap
    return chunks


def ingest() -> int:
    """
    Read every .txt/.md file in knowledge/, chunk it, embed the chunks with
    Gemini, and upsert them into the collection. Uses deterministic IDs
    (filename + chunk index) so re-running this is safe and never duplicates.
    Returns the total number of chunks now stored.
    """
    collection = _get_collection()
    total_chunks = 0

    for filename in sorted(os.listdir(KNOWLEDGE_DIR)):
        if not filename.endswith((".txt", ".md")):
            continue

        with open(os.path.join(KNOWLEDGE_DIR, filename), "r", encoding="utf-8") as f:
            text = f.read()

        chunks = _chunk_text(text)
        if not chunks:
            continue

        ids = [f"{filename}::{i}" for i in range(len(chunks))]
        metadatas = [{"source": filename} for _ in chunks]

        # Embed in small batches, pausing between them to stay under free-tier
        # rate limits.
        embeddings = []
        for i in range(0, len(chunks), EMBED_BATCH_SIZE):
            batch = chunks[i:i + EMBED_BATCH_SIZE]
            result = _genai_client.models.embed_content(model=EMBED_MODEL, contents=batch)
            embeddings.extend([e.values for e in result.embeddings])
            time.sleep(1)

        # upsert (not add) so re-ingesting the same file overwrites its
        # existing chunks instead of duplicating them.
        collection.upsert(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
        total_chunks += len(chunks)

    return total_chunks


def retrieve(query: str, k: int = 4) -> list[str]:
    """
    Embed the query and search the WHOLE collection (no niche/metadata
    filter, since the knowledge base is one combined file). Returns the
    top-k chunk texts, or [] if the collection is empty / nothing matches.
    """
    collection = _get_collection()
    if collection.count() == 0:
        return []

    result = _genai_client.models.embed_content(model=EMBED_MODEL, contents=[query])
    query_embedding = result.embeddings[0].values

    results = collection.query(query_embeddings=[query_embedding], n_results=k)
    documents = results.get("documents") or [[]]
    return documents[0] or []


def count() -> int:
    """Number of chunks currently stored in the collection."""
    return _get_collection().count()
