"""Outcome-feedback memory — a persistent store of past fix outcomes.

This is the retrieval half of the outcome-feedback RAG: it embeds each recorded
:class:`~averyloop.outcomes.Outcome` (accepted / rejected / reverted) into a
**dedicated, persistent** ChromaDB collection and recalls prior outcomes for
*similar code* when auditing.  The recalled note ("previous fixes to similar
code: 2 accepted, 1 reverted — the revert removed a guard clause") is injected
into the audit as **additive context only**.

Why a separate collection?  ``rag/indexer.build_index`` deletes and recreates
the ``codebase_index`` collection on every run, so an outcome memory living
there would be wiped.  This module uses its own collection name
(``outcome_memory`` by default) under the same ``.chromadb/`` persist dir, so it
**survives index rebuilds** and accumulates across runs — the substrate that lets
the loop improve within a project over time.

Offline by design
------------------
Embeddings use a deterministic, dependency-free **hashed bag-of-words** vectorizer
(:func:`hashed_bow_embedding`) computed *here* and passed to Chroma as explicit
``embeddings=`` — so the store never triggers Chroma's default model download and
runs fully offline (no network, no API key) in tests and the benchmark.  A
different embedder can be injected via the ``embed_fn`` parameter.  Similarity is
token-overlap based: code/findings sharing identifiers and words land closer,
which is enough for within-project recall (and is stated plainly as a limit).

The note-synthesis logic (:func:`synthesize_note`) is **pure** and unit-testable
without a live DB; only the record/recall functions touch ChromaDB, and they
degrade to no-ops (never raise) when ``chromadb`` is absent or the store is empty.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

from averyloop.outcomes import ACCEPTED, REJECTED, REVERTED, Outcome

# Type of an embedding function: list of texts -> list of float vectors.
EmbedFn = Callable[[Sequence[str]], List[List[float]]]

_DEFAULT_PERSIST_DIR = ".chromadb"
_DEFAULT_COLLECTION = "outcome_memory"
_DEFAULT_DIM = 256

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


# ---------------------------------------------------------------------------
# Deterministic, offline embedding (hashed bag-of-words / feature hashing)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric/underscore tokens (identifiers + words)."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _hash_bucket(token: str, dim: int) -> tuple[int, float]:
    """Map *token* to a bucket index and a sign in ``{-1, +1}`` (feature hashing).

    Signed hashing keeps collisions roughly unbiased.
    """
    digest = hashlib.md5(token.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % dim
    sign = 1.0 if (digest[4] & 1) == 0 else -1.0
    return idx, sign


def hashed_bow_embedding(
    texts: Sequence[str], dim: int = _DEFAULT_DIM
) -> List[List[float]]:
    """Embed *texts* as L2-normalized signed hashed bag-of-words vectors.

    Deterministic and dependency-free — identical input always yields identical
    output, and texts sharing tokens have higher cosine similarity.  An empty or
    token-less text yields a zero vector.
    """
    vectors: List[List[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for tok in _tokenize(text):
            idx, sign = _hash_bucket(tok, dim)
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        vectors.append(vec)
    return vectors


def _make_default_embed_fn(dim: int) -> EmbedFn:
    return lambda texts: hashed_bow_embedding(texts, dim=dim)


# ---------------------------------------------------------------------------
# Config / collection helpers
# ---------------------------------------------------------------------------

def _cfg():
    from averyloop.loop_config import get_config
    return get_config()


def _collection_name(name: Optional[str]) -> str:
    if name:
        return name
    return getattr(_cfg(), "outcome_collection_name", _DEFAULT_COLLECTION) \
        or _DEFAULT_COLLECTION


def _embed_dim() -> int:
    return int(getattr(_cfg(), "outcome_embed_dim", _DEFAULT_DIM) or _DEFAULT_DIM)


def _get_collection(repo_root: str, persist_dir: str, collection_name: str,
                    create: bool):
    """Return the outcome-memory ChromaDB collection, or ``None`` on failure.

    ``create=True`` uses ``get_or_create_collection`` (for recording);
    ``create=False`` returns ``None`` if the collection does not yet exist (for
    recall on a cold store).  Never raises — a missing ``chromadb`` or store
    simply disables the feature.
    """
    try:
        import chromadb  # local import: optional dependency at call time
    except Exception:
        return None
    try:
        client = chromadb.PersistentClient(path=os.path.join(repo_root, persist_dir))
        if create:
            # cosine space pairs naturally with our normalized vectors.
            return client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return client.get_collection(collection_name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------

def record_outcome(
    outcome: Outcome,
    repo_root: str = ".",
    *,
    persist_dir: str = _DEFAULT_PERSIST_DIR,
    collection_name: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> bool:
    """Embed and upsert a single *outcome* into the persistent store.

    Returns ``True`` on success, ``False`` if the store is unavailable (so the
    caller can proceed unaffected).  Upsert (by a stable id derived from branch +
    merge sha / iteration) makes re-recording idempotent.
    """
    return record_outcomes(
        [outcome], repo_root,
        persist_dir=persist_dir,
        collection_name=collection_name,
        embed_fn=embed_fn,
    )


def _outcome_id(outcome: Outcome) -> str:
    suffix = outcome.merge_sha or f"iter{outcome.iteration}"
    branch = outcome.branch_name or outcome.file or "unknown"
    return f"{branch}::{suffix}"


def record_outcomes(
    outcomes: Iterable[Outcome],
    repo_root: str = ".",
    *,
    persist_dir: str = _DEFAULT_PERSIST_DIR,
    collection_name: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> bool:
    """Batch version of :func:`record_outcome`."""
    items = list(outcomes)
    if not items:
        return True

    name = _collection_name(collection_name)
    collection = _get_collection(repo_root, persist_dir, name, create=True)
    if collection is None:
        return False

    # Dedupe by id within the batch (Chroma rejects duplicate ids in one call);
    # last write wins, matching upsert semantics.
    by_id: Dict[str, Outcome] = {_outcome_id(o): o for o in items}
    ids = list(by_id.keys())
    batch = [by_id[i] for i in ids]

    embed = embed_fn or _make_default_embed_fn(_embed_dim())
    documents = [o.embed_text() for o in batch]
    try:
        embeddings = embed(documents)
        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=[o.to_metadata() for o in batch],
        )
    except Exception:
        return False
    return True


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def _query_text(query: Union[str, Outcome, dict, object]) -> str:
    """Coerce a query (raw string / finding dict / Finding / Outcome) to text."""
    if isinstance(query, str):
        return query
    if isinstance(query, Outcome):
        return query.embed_text()
    if isinstance(query, dict):
        return "\n".join(
            f"{k}: {query[k]}"
            for k in ("file", "dimension", "description", "fix")
            if query.get(k)
        ) or str(query)
    # Finding-like object
    parts = []
    for attr in ("file", "dimension", "description", "fix"):
        val = getattr(query, attr, None)
        if val:
            parts.append(f"{attr}: {val}")
    return "\n".join(parts) if parts else str(query)


def recall_outcomes(
    query: Union[str, Outcome, dict, object],
    repo_root: str = ".",
    k: int = 5,
    *,
    persist_dir: str = _DEFAULT_PERSIST_DIR,
    collection_name: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> List[dict]:
    """Recall up to *k* prior outcomes for code similar to *query*.

    Returns a list of dicts (``label``, ``file``, ``dimension``, ``importance``,
    ``reason``, ``branch_name``, ``distance`` + ``delta_*`` keys), nearest first.
    Returns ``[]`` when the store is empty/unavailable — never raises.
    """
    if k <= 0:
        return []
    name = _collection_name(collection_name)
    collection = _get_collection(repo_root, persist_dir, name, create=False)
    if collection is None:
        return []

    embed = embed_fn or _make_default_embed_fn(_embed_dim())
    try:
        count = collection.count()
        if count == 0:
            return []
        q_emb = embed([_query_text(query)])
        results = collection.query(
            query_embeddings=q_emb,
            n_results=min(k, count),
        )
    except Exception:
        return []

    hits: List[dict] = []
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]
    for i, meta in enumerate(metadatas):
        hit = dict(meta or {})
        hit["distance"] = distances[i] if i < len(distances) else None
        hits.append(hit)
    return hits


# ---------------------------------------------------------------------------
# Note synthesis (pure)
# ---------------------------------------------------------------------------

def synthesize_note(recalled: Sequence[dict], max_examples: int = 3) -> str:
    """Synthesize a short advisory note from *recalled* outcome dicts.

    Pure — testable without a DB.  Returns ``""`` for an empty recall.  Example::

        Prior fixes to similar code: 2 accepted, 1 reverted, 0 rejected.
        Reverts removed/undid:
          - [correctness] merged then reverted
        Treat similar changes with extra caution; prefer the patterns that were
        accepted and avoid those that were reverted.
    """
    if not recalled:
        return ""

    counts = {ACCEPTED: 0, REVERTED: 0, REJECTED: 0}
    for r in recalled:
        label = r.get("label")
        if label in counts:
            counts[label] += 1

    lines = [
        f"Prior fixes to similar code: {counts[ACCEPTED]} accepted, "
        f"{counts[REVERTED]} reverted, {counts[REJECTED]} rejected."
    ]

    reverts = [r for r in recalled if r.get("label") == REVERTED]
    if reverts:
        lines.append("Reverts removed/undid:")
        for r in reverts[:max_examples]:
            dim = r.get("dimension", "?")
            reason = r.get("reason") or "reverted after merge"
            desc = r.get("description") or ""
            detail = f" — {desc}" if desc else ""
            lines.append(f"  - [{dim}] {reason}{detail}")

    rejects = [r for r in recalled if r.get("label") == REJECTED]
    if rejects:
        lines.append("Rejected before merge:")
        for r in rejects[:max_examples]:
            dim = r.get("dimension", "?")
            reason = r.get("reason") or "rejected"
            lines.append(f"  - [{dim}] {reason}")

    if counts[REVERTED] or counts[REJECTED]:
        lines.append(
            "Treat similar changes with extra caution; prefer patterns that "
            "were accepted and avoid those that were reverted/rejected."
        )
    return "\n".join(lines)


def recall_note(
    query: Union[str, Outcome, dict, object],
    repo_root: str = ".",
    k: int = 5,
    *,
    persist_dir: str = _DEFAULT_PERSIST_DIR,
    collection_name: Optional[str] = None,
    embed_fn: Optional[EmbedFn] = None,
) -> str:
    """Convenience: recall outcomes and synthesize the advisory note.

    Returns ``""`` when nothing is recalled, so callers can append it
    unconditionally as additive context.
    """
    recalled = recall_outcomes(
        query, repo_root, k,
        persist_dir=persist_dir,
        collection_name=collection_name,
        embed_fn=embed_fn,
    )
    return synthesize_note(recalled)
