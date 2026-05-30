"""Unit tests for the outcome-memory store (offline, temp Chroma collection)."""

from __future__ import annotations

import pytest

from averyloop.outcomes import ACCEPTED, REJECTED, REVERTED, derive_outcome
from averyloop.rag import outcome_memory as om

# Deterministic offline embedding — no model download, no network.
EMBED = lambda texts: om.hashed_bow_embedding(texts, dim=128)


def _outcome(file, branch, desc, **kw):
    fd = {
        "dimension": "correctness",
        "file": file,
        "description": desc,
        "fix": "patch " + desc,
        "importance": 6,
        "branch_name": "improvement/" + branch,
    }
    return derive_outcome(fd, diff=f"+++ b/{file}\n+{desc}",
                          merge_sha=f"sha-{branch}", iteration=1, **kw)


# ── embedding ────────────────────────────────────────────────────────────────

class TestEmbedding:
    def test_deterministic(self):
        a = om.hashed_bow_embedding(["hello world"], dim=64)
        b = om.hashed_bow_embedding(["hello world"], dim=64)
        assert a == b

    def test_l2_normalized(self):
        [vec] = om.hashed_bow_embedding(["some code tokens here"], dim=64)
        norm = sum(v * v for v in vec) ** 0.5
        assert abs(norm - 1.0) < 1e-9

    def test_shared_tokens_are_more_similar(self):
        import math

        def cos(u, v):
            return sum(a * b for a, b in zip(u, v))

        base, near, far = om.hashed_bow_embedding(
            ["login authentication guard clause",
             "authentication guard for login handler",
             "tokenizer loop index arithmetic"],
            dim=256,
        )
        assert cos(base, near) > cos(base, far)


# ── record / recall round-trip ───────────────────────────────────────────────

class TestRecordRecall:
    def test_round_trip(self, tmp_path):
        rr = str(tmp_path)
        o = _outcome("auth.py", "auth-guard", "remove guard clause on login",
                     merged=True, reverted=True)
        assert om.record_outcome(o, repo_root=rr, embed_fn=EMBED) is True

        hits = om.recall_outcomes("login guard clause", repo_root=rr, k=5,
                                  embed_fn=EMBED)
        assert len(hits) == 1
        assert hits[0]["label"] == REVERTED
        assert hits[0]["file"] == "auth.py"

    def test_similar_vs_dissimilar_labels(self, tmp_path):
        rr = str(tmp_path)
        om.record_outcomes([
            _outcome("auth.py", "auth-null", "add null check to login handler",
                     merged=True),
            _outcome("parser.py", "parser-fix", "fix off by one in tokenizer loop",
                     merged=True, reverted=True),
        ], repo_root=rr, embed_fn=EMBED)

        top_auth = om.recall_outcomes("login handler null authentication",
                                      repo_root=rr, k=1, embed_fn=EMBED)[0]
        assert top_auth["file"] == "auth.py"
        assert top_auth["label"] == ACCEPTED

        top_parser = om.recall_outcomes("tokenizer loop index off by one",
                                        repo_root=rr, k=1, embed_fn=EMBED)[0]
        assert top_parser["file"] == "parser.py"
        assert top_parser["label"] == REVERTED

    def test_cold_store_returns_empty(self, tmp_path):
        assert om.recall_outcomes("anything", repo_root=str(tmp_path), k=3,
                                  embed_fn=EMBED) == []

    def test_recall_k_zero_is_empty(self, tmp_path):
        rr = str(tmp_path)
        om.record_outcome(_outcome("a.py", "a", "x", merged=True),
                          repo_root=rr, embed_fn=EMBED)
        assert om.recall_outcomes("x", repo_root=rr, k=0, embed_fn=EMBED) == []

    def test_upsert_is_idempotent(self, tmp_path):
        rr = str(tmp_path)
        o = _outcome("a.py", "a-branch", "fix", merged=True)
        om.record_outcome(o, repo_root=rr, embed_fn=EMBED)
        om.record_outcome(o, repo_root=rr, embed_fn=EMBED)  # same id
        hits = om.recall_outcomes("fix", repo_root=rr, k=5, embed_fn=EMBED)
        assert len(hits) == 1


class TestSurvivesRebuild:
    """The outcome collection must survive a codebase_index rebuild."""

    def test_outcome_memory_survives_codebase_index_wipe(self, tmp_path):
        import chromadb

        rr = str(tmp_path)
        om.record_outcome(_outcome("auth.py", "auth", "guard", merged=True),
                          repo_root=rr, embed_fn=EMBED)

        # Simulate indexer.build_index: delete + recreate ONLY codebase_index.
        client = chromadb.PersistentClient(path=str(tmp_path / ".chromadb"))
        try:
            client.delete_collection("codebase_index")
        except Exception:
            pass
        col = client.create_collection("codebase_index")
        col.add(ids=["x"], embeddings=EMBED(["chunk"]), documents=["chunk"])
        client.delete_collection("codebase_index")  # rebuild again

        hits = om.recall_outcomes("guard", repo_root=rr, k=5, embed_fn=EMBED)
        assert len(hits) == 1
        assert hits[0]["file"] == "auth.py"


# ── note synthesis (pure) ────────────────────────────────────────────────────

class TestSynthesizeNote:
    def test_empty_recall_gives_empty_note(self):
        assert om.synthesize_note([]) == ""

    def test_counts_and_caution(self):
        recalled = [
            {"label": ACCEPTED, "dimension": "correctness"},
            {"label": ACCEPTED, "dimension": "performance"},
            {"label": REVERTED, "dimension": "correctness",
             "reason": "merged then reverted", "description": "removed a guard"},
        ]
        note = om.synthesize_note(recalled)
        assert "2 accepted, 1 reverted, 0 rejected" in note
        assert "removed a guard" in note
        assert "caution" in note.lower()

    def test_all_accepted_has_no_caution(self):
        note = om.synthesize_note([{"label": ACCEPTED, "dimension": "x"}])
        assert "1 accepted" in note
        assert "caution" not in note.lower()

    def test_rejected_section(self):
        note = om.synthesize_note([
            {"label": REJECTED, "dimension": "security", "reason": "safety-gate veto"},
        ])
        assert "Rejected before merge" in note
        assert "safety-gate veto" in note
