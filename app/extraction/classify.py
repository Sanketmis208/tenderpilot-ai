"""Document category classification (keyword evidence scoring with an explicit confidence)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.db.models.documents import DocumentCategory as C

_SIGNALS: dict[str, list[tuple[str, float]]] = {
    C.ISO_CERTIFICATE: [(r"\biso\s*9001|iso\s*14001|iso\s*45001|iso/iec\s*27001", 4), (r"quality management system", 2), (r"certificate of registration|certification body|accredit", 1.5)],
    C.GST_CERTIFICATE: [(r"goods and services tax", 3), (r"\bgstin\b", 3), (r"form gst reg-06|registration certificate", 2)],
    C.MSME_REGISTRATION: [(r"udyam", 4), (r"\bmsme\b|micro,? small and medium", 2.5), (r"enterprise type", 1)],
    C.COMPANY_REGISTRATION: [(r"certificate of incorporation", 4), (r"registrar of companies|\bcin\b", 2), (r"companies act", 1.5)],
    C.FINANCIAL_STATEMENT: [(r"balance sheet", 3), (r"profit (and|&) loss|statement of profit", 3), (r"revenue from operations", 2), (r"audited", 1)],
    C.TURNOVER_CERTIFICATE: [(r"turnover certificate|certificate of turnover", 4), (r"chartered accountant|\budin\b", 2), (r"annual turnover", 2)],
    C.COMPLETION_CERTIFICATE: [(r"completion certificate|work completion|certificate of completion", 4), (r"satisfactorily completed|successfully completed", 2.5)],
    C.EXPERIENCE_CERTIFICATE: [(r"experience certificate|performance certificate", 4), (r"to whom it may concern", 1), (r"executed|supplied and commissioned", 1.5)],
    C.PURCHASE_ORDER: [(r"purchase order|work order|\bp\.?o\.? no", 4), (r"terms of delivery|order value", 1.5)],
    C.PRODUCT_CATALOGUE: [(r"product catalogue|catalog|product range", 4), (r"model\s*(no|number)?[:.]", 1.5), (r"specifications?", 1)],
    C.TECHNICAL_DATASHEET: [(r"datasheet|data sheet|technical specifications", 3.5), (r"rated (voltage|current)|ip\d{2}", 1.5)],
    C.AUTHORIZATION_LETTER: [(r"authori[sz]ation letter|manufacturer'?s? authori[sz]ation|\bmaf\b", 4), (r"hereby authori[sz]e", 2)],
    C.OEM_CERTIFICATE: [(r"\boem\b", 2.5), (r"original equipment manufacturer", 3)],
    C.TENDER_NOTICE: [(r"notice inviting tender|\bnit\b|request for proposal|\brfp\b|e-?tender", 4), (r"eligibility criteria|bid submission|earnest money", 2), (r"tender (id|reference|no)", 2)],
    C.CORRIGENDUM: [(r"corrigendum|addendum|amendment no", 5)],
    C.BOQ: [(r"bill of quantit|\bboq\b|schedule of quantities|price schedule", 4), (r"\bqty\b|quantity|unit rate", 1.5)],
    C.LEGAL_COMPLIANCE: [(r"affidavit|undertaking|declaration|power of attorney", 3), (r"non-?blacklist", 2)],
}

_COMPILED = {k: [(re.compile(p, re.I), w) for p, w in v] for k, v in _SIGNALS.items()}


@dataclass
class Classification:
    category: str
    confidence: float
    scores: dict[str, float]


def classify(text: str, filename: str = "") -> Classification:
    sample = (filename.replace("_", " ") + "\n" + text[:12000]).lower()
    scores: dict[str, float] = {}
    for cat, pats in _COMPILED.items():
        score = 0.0
        for rx, w in pats:
            hits = len(rx.findall(sample))
            if hits:
                score += w * min(hits, 3) ** 0.5
        if score:
            scores[str(cat)] = round(score, 2)
    if not scores:
        return Classification(C.OTHER, 0.0, {})
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence grows with absolute evidence and with the margin over the runner-up.
    conf = min(1.0, (top / 8.0)) * (0.5 + 0.5 * (top - second) / top)
    return Classification(best, round(conf, 2), scores)
