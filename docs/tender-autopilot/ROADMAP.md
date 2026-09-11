# Implementation roadmap

Каждая фаза поставляется маленькими vertical slices и получает production flag
только после тестов, observability и runbook.

1. **Decision foundation — in progress.** Immutable fast-prequalification and
   decision snapshots, Decimal economics, evidence binding, a deterministic
   application checklist and approval card. The 1000-pack A4 structured fixture
   now reaches this boundary with two immutable supplier quotes. Далее:
   extend Company Digital Twin with registry verification, role/signature/insurance
   and contract-history evidence, move legacy evaluator to the same engine and
   introduce conflict records.
2. **Discovery/normalization — in progress.** Generic HTTP(S) JSON items now have
   canonical append-only observed versions, provider-scoped external identity and
   replay idempotency plus durable per-source attempt receipts and deterministic
   last-success freshness incidents. The generic `tender-page-v1` boundary now has
   transaction-bound protected next-page checkpoints, hashed acknowledgements and
   explicit `partial/complete/unknown` receipts. Далее: official provider interface
   with stable provider keys, portal-specific cursor/completeness verification and a
   normalized procurement model.
3. **Document intelligence — in progress.** Checksum-bound local PDF/DOCX/TXT
   extraction now produces evidence-linked UNKNOWN candidates; requirement and
   product-specification manager reviews bind checksum/extractor/exact candidate
   sets and preserve versioned history. Далее: independent four-eyes AI review,
   MIME/AV/archive sandbox, OCR/table map and
   a broader prompt-injection corpus.
4. **Qualification/suppliers — in progress.** Immutable evidence-bound Company
   Digital Twin, manual/imported quote registry, freshness and exact prequalification/
   decision binding are connected. Далее: automated company registry verification,
   supplier discovery, RFQ idempotency, external quote parsing, backup/reservation.
5. **Application readiness — in progress.** A versioned seven-gate checklist,
   exact missing/blocking codes, completeness percent and checklist hash are
   persisted in the decision snapshot. Далее: templates, generated package,
   four-eyes review and explicit missing-item remediation.
6. **Assisted submission.** The persisted protected-action capability registry now
   gates Task/Orchestrator execution before approval plus the direct SMTP outreach
   and social-publication worker boundaries. Record-bound participation/submission
   also has an isolated per-tender stop before approval. Next: enforce the same
   controls at every future direct adapter; external receipt required.
   CAPTCHA/MFA always human takeover.
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
