# Implementation roadmap

Каждая фаза поставляется маленькими vertical slices и получает production flag
только после тестов, observability и runbook.

1. **Decision foundation — in progress.** Immutable decision snapshot, Decimal
   economics, evidence binding, approval card. Далее: move legacy evaluator to the
   same engine and introduce conflict/freshness records.
2. **Discovery/normalization — in progress.** Generic HTTP(S) JSON items now have
   canonical append-only observed versions and replay idempotency. Далее: official
   provider interface, cursors/receipts, immutable document-byte amendments,
   normalized procurement model and source SLO.
3. **Document intelligence — in progress.** Checksum-bound local PDF/DOCX/TXT
   extraction now produces evidence-linked UNKNOWN candidates. Далее: immutable
   object versions, reviewer workflow, MIME/AV/archive sandbox, OCR/table map and
   a broader prompt-injection corpus.
4. **Qualification/suppliers.** Company digital twin, supplier registry, RFQ
   idempotency, quote parsing/freshness, backup/reservation.
5. **Application readiness.** Requirement graph, templates, package hash,
   four-eyes review and explicit missing-item remediation.
6. **Assisted submission.** Capability registry and mock adapters first; external
   receipt required. CAPTCHA/MFA always human takeover.
7. **Auction safety.** Simulator, property/replay/failure tests and hard-stop
   invariant before controlled autobid can be proposed.
8. **Post-win.** Contract obligations, purchase orders, delivery/acceptance,
   accounting integration and actual-profit reconciliation.
9. **Controlled learning.** Golden procurement dataset, backtests, calibration,
   shadow/champion/challenger and reviewed promotion.

## Gate before any real submission

- official adapter terms and credentials are approved;
- zero stop-price violations in simulation/replay;
- latest amendment and exact package hash verified;
- company/signature/accreditation valid;
- owner approval is current and action-specific;
- external receipt persistence and recovery test pass;
- kill switches, alerts and operator runbook are exercised.
