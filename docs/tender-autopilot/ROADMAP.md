# Implementation roadmap

Каждая фаза поставляется маленькими vertical slices и получает production flag
только после тестов, observability и runbook.

1. **Decision foundation — in progress.** Immutable fast-prequalification and
   decision snapshots, Decimal economics, evidence binding, approval card. Далее:
   extend Company Digital Twin with registry verification, role/signature/insurance
   and contract-history evidence, move legacy evaluator to the same engine and
   introduce conflict records.
2. **Discovery/normalization — in progress.** Generic HTTP(S) JSON items now have
   canonical append-only observed versions, provider-scoped external identity and
   replay idempotency plus durable per-source attempt receipts and deterministic
   last-success freshness incidents. Далее: official provider interface with stable
   provider keys, cursors/provider acknowledgements, completeness monitoring and a
   normalized procurement model.
3. **Document intelligence — in progress.** Checksum-bound local PDF/DOCX/TXT
   extraction now produces evidence-linked UNKNOWN candidates. Далее: immutable
   reviewer workflow, MIME/AV/archive sandbox, OCR/table map and
   a broader prompt-injection corpus.
4. **Qualification/suppliers — in progress.** Immutable evidence-bound Company
   Digital Twin, manual/imported quote registry, freshness and exact prequalification/
   decision binding are connected. Далее: automated company registry verification,
   supplier discovery, RFQ idempotency, external quote parsing, backup/reservation.
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
