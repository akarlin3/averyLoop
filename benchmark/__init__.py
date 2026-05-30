"""AveryLoop benchmark harness — seeded-bug fixtures + offline metrics.

Runs AveryLoop's authored decision logic (signals, safety gate, composite blend,
convergence, outcome memory) against fixtures whose bugs and outcomes have
*known ground truth*, and reports fix rate, false-accept rate, convergence
savings, and judge-objective agreement.  The default mode is fully offline and
deterministic (no LLM, no network); see ``benchmark/README.md``.
"""
