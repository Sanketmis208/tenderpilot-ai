# User guide

1. **Sign up** and create your organization. You start on a 14-day Professional trial.
2. **Onboarding** — enter business details, registrations (GSTIN, PAN, CIN, Udyam), capabilities and tender interests,
   then upload your qualification documents: incorporation, GST, MSME, ISO certificates, audited financials or CA
   turnover certificates, completion certificates, purchase orders, catalogues and datasheets.
3. **Document Vault** — each file is classified automatically. Confirm the category and any flagged fields. Under
   *Facts available to eligibility checks* confirm correct extractions or reject wrong ones. To renew a certificate,
   open it and *Upload new version*; earlier assessments keep the version they used.
4. **Add a tender** — *My Tenders → Add tender*: upload the NIT/RFP, annexures, BOQ (XLSX) or a ZIP; or enter details
   manually; or import a URL you are authorised to download; or upload a feed file (CSV, OCDS JSON, RSS/Atom, JSON)
   or a saved portal alert email (`.eml`). Processing progress is live.
   To have tenders arrive on their own, go to *Settings → Tender feeds & email alerts*:
   * **Feeds**: add the https URL of a licensed data provider's feed or an open-data OCDS publication (with an
     optional auth header, stored encrypted). It is checked every 15 minutes to 24 hours.
   * **Portal email alerts**: subscribe to CPPP, GeM or state-portal alerts with your own portal account, click
     *Enable email alerts*, copy the private address (shown once) and have your mail service forward the alerts to
     it. You can restrict accepted sender domains.
   New items appear as *Discovered* tenders (duplicates are skipped by reference number), match scores are recomputed,
   and bid managers get a notification. Open one and upload its documents to run extraction and eligibility.
5. **Review requirements** — every condition shows the verbatim clause, page and extraction confidence. *Confirm*,
   *Edit* the interpretation (the clause is kept) or *Reject* non-requirements.
6. **Run eligibility check** — results per requirement: verified, not satisfied, insufficient evidence, conflicting
   evidence, needs review, not applicable. Open *evidence* to jump to the exact page with the passage highlighted.
7. **Review** — accept findings, override with a justification, annotate, and resolve conflicts (decide which source is
   authoritative). Then *Complete review & resume*.
8. **Bid workspace** — remediation tasks are created from gaps; assign owners and due dates, comment, track status.
   *Generate drafts* builds a technical response and capability summary only from your records, with `[Source: …]`
   citations and `[TO BE COMPLETED: …]` placeholders. Edit drafts (each save is a new version).
   *Run agent* (Drafts tab) runs the bid-preparation agent: it plans the document structure, drafts from verified
   evidence, adds an executive summary (AI-written only when every figure in it is found in the draft), lists the
   attachment for each requirement (on record or missing), flags risks, and then **waits for you** to choose *Submit for
   internal review* or *Keep as draft*.
9. **BOQ** — analyse the attached workbook: duplicates, unit mismatches, missing quantities/rates, locked cells and
   formulas are flagged. Enter internal rates, save scenarios (exact decimal totals with markup and GST) and export a
   priced *copy* — formulas and locked cells are preserved.
10. **Approvals** — submit for review; a reviewer other than the author approves or requests changes; mark *ready for
    external submission*. Corrigenda that change mandatory conditions reopen approvals (configurable).
11. **Export** the package (technical response, compliance matrix, checklist, evidence and risk reports, supporting
    documents, SHA-256 manifest) and **submit it yourself on the official portal**.
12. **Copilot** — ask "Can we qualify for this tender?", "Which document supports our ISO certification?", "Which
    tenders close within seven days?". With an LLM provider configured the answer streams in as it is written. Proposed
    changes require your approval. From any answer you can *Save as note*, *Create task* or *Draft response* into the
    tender's bid workspace.
13. **Settings → Document template & branding** (owners/admins) — letterhead, address, footer and brand colour for all
    generated DOCX/XLSX/PDF files, plus up to three standard sections (e.g. quality policy) inserted verbatim into
    technical responses.
