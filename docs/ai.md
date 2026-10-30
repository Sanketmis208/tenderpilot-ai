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

Providers are pluggable behind `app/ai/llm/client.py` (`TP_LLM_PROVIDER`):

* `anthropic` — `claude-opus-5-5` for extraction/copilot, `claude-haiku-5-5` for low-effort summaries; structured outputs
  and server-side refusal fallbacks.
* `groq` — `openai/gpt-oss-120b` / `openai/gpt-oss-20b` over Groq's OpenAI-compatible API; JSON-schema outputs validated
  with Pydantic (one automatic repair attempt), function calling for the copilot, retries with back-off on 429/5xx.

Both backends expose the same structured-output, text and provider-neutral tool-conversation interfaces, so extraction,
narratives and the copilot work unchanged. The requirement `metric` field is a closed vocabulary shared with the rule
engine, so model output cannot invent metric names the engine would not recognise. Every call records tokens, latency and USD cost in `ai_usage_records`, increments the
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

Current results:

| Extractor | Precision | Recall | Critical-field accuracy | Citation correctness | Abstention | Latency (12 cases) |
|---|---|---|---|---|---|---|
| Rules | 1.00 | 1.00 | 1.00 | 1.00 | OK | ~1 ms |
| Groq `openai/gpt-oss-120b` (production prompt) | 0.80 | 0.89 | 0.92 | 1.00 | OK | ~120 s |

Rule-engine accuracy: 1.00. Groq differences: it splits an EMD clause into deposit + exemption, and sometimes types an
IS product-conformity clause as a certification. Field accuracy rose from 0.68 to 0.92 after constraining `metric` to
the rule-engine vocabulary and adding explicit conventions to the prompt. In production the LLM only *adds*
requirements the rules missed (deduplicated by identity and by same-clause/same-kind), capped at 0.75 confidence so
they always go to review. Measured cost: ~$0.006 to extract the three seeded tenders.

**Caveat:** the held-out set is small (12 extraction + 8 rule cases) and was written alongside the rule extractor, which
favours the rules. Treat it as a regression gate, not evidence of real-world accuracy; expanding it with real
(anonymised) tenders is the most valuable next step.
