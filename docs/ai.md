# AI workflows, retrieval and evaluation

## Principles

1. **Extraction ≠ eligibility.** Extraction confidence (how sure we are we read the clause correctly) is stored
   separately from the eligibility decision.
2. **Deterministic decisions.** `app/services/eligibility/rules.py` decides statuses with `Decimal` arithmetic and
   explicit dates. Models never override it; `machine_status` is immutable and reviewer overrides are audited.
3. **No invented evidence.** Every fact carries a source version, page and verbatim text. Unknowns become
   `insufficient_evidence` or `[TO BE COMPLETED: …]` placeholders.
4. **Untrusted text is data.** Document text reaches models inside `<document>`/`<untrusted>` tags under a system guard,
   and MCP results expose it in `untrusted_text` fields.

## Requirement extraction

* **Rules** (`app/extraction/tender_rules.py`) — clause segmentation, Indian money (lakh/crore) and day-first date
  normalisation, fiscal years, mandatory/conditional detection. Conditional language (e.g. MSE EMD exemptions) is
  kept verbatim in `conditions` and marked `conditional`; it is never generalised.
* **LLM** (`app/extraction/tender_llm.py`, when `TP_ANTHROPIC_API_KEY` is set) — only relevance-ranked pages are sent
  (≤60k chars), output is a Pydantic schema via structured outputs, and every `source_quote` must be found verbatim in
  the parsed text or the requirement is dropped. LLM-only requirements are capped at 0.75 confidence (review).

## Decision states

`verified_satisfied`, `verified_not_satisfied`, `insufficient_evidence`, `conflicting_evidence`, `not_applicable`,
`needs_human_review`. "Verified" means verified against uploaded, document-stated evidence — not external authenticity.
The overall status is `not_eligible` if any mandatory condition fails, `gaps_found`/`needs_review` while any mandatory
item is unresolved, and `eligible_per_policy` only when every mandatory condition is verified or not applicable. The
readiness score is explanatory and is capped at 40 when a mandatory condition fails.

## LangGraph

* **Tender analysis** (`app/ai/workflows/tender_analysis.py`) — typed state, PostgreSQL checkpointer
  (`langgraph-checkpoint-postgres`), thread id `assessment:<id>`, bounded by `TP_AGENT_MAX_STEPS`, cancellation checked
  at each node, every node idempotent and committing before return. `human_review` uses `interrupt()`; reviewers resume
  with `POST /assessments/{id}/complete-review` (`Command(resume=…)`). Remediation tasks use dedup keys so retries or
  resumes cannot duplicate them.
* **Copilot** (`app/ai/agents/copilot.py`) — plan → act → respond graph. With an LLM: Claude tool-use loop (max 6
  rounds, per-run token/cost budget) over the MCP tool schemas, executed through an in-process MCP client. Without an
  LLM: deterministic intent routing, clearly labelled. Mutation tools become *pending actions* the user must approve.
  Citations are kept only if the cited document appeared in this turn's tool results.

## Models and cost control

Defaults: `claude-opus-5-5` for extraction/copilot, `claude-haiku-5-5` for low-effort summaries (configurable). Server-side
refusal fallbacks are enabled. Every call records tokens, latency and USD cost in `ai_usage_records`, increments the
org's monthly `ai_tokens` counter (quota-enforced) and is attributed to the tender where applicable. Cost levers:
deterministic parsing first, targeted page selection, hash-dedup of processed files (chunks/embeddings reused),
embedding cache, model choice by task.

## Retrieval (RAG)

Structure-preserving chunks (headings carried, clauses/tables not split) → embeddings (`hashing-v1` offline, or any
OpenAI-compatible embeddings endpoint) → `document_chunks` with HNSW cosine index and generated `tsvector`. Hybrid
search = Postgres FTS (websearch query + OR fallback) + pgvector, fused with RRF and reranked by term overlap; always
filtered by the caller's `organization_id` and current document versions. `retrieval_confidence` drives abstention.

## Evaluation

`uv run tenderpilot eval` (→ `var/eval_report.json`) measures extraction precision/recall, critical-field accuracy,
citation correctness, unsupported-claim rate, abstention on unrelated and prompt-injection text, rule-engine accuracy
and latency; with an API key it also evaluates the LLM extractor. Thresholds run in CI (`tests/unit/test_evaluations.py`).

Current results (rule extractor + rule engine): precision 1.0, recall 1.0, field accuracy 1.0, abstention OK, rule
accuracy 1.0. **Caveat:** the held-out set is small (12 extraction + 8 rule cases) and was written alongside the
extractor; treat it as a regression gate, not evidence of real-world accuracy. Expanding it with real (anonymised)
tenders is the most valuable next step. The LLM path has not been evaluated (no API key was configured).
