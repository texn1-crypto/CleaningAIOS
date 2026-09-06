# Implementation roadmap

Каждая фаза поставляется маленькими vertical slices и получает production flag
только после тестов, observability и runbook.

1. **Decision foundation — in progress.** Immutable decision snapshot, Decimal
   economics, evidence binding, approval card. Далее: move legacy evaluator to the
   same engine and introduce conflict/freshness records.
2. **Discovery/normalization.** Official provider interface, amendment versions,
   normalized procurement model, source SLO and fixtures.
3. **Document intelligence.** Immutable object storage, MIME/AV/archive sandbox,
   OCR/page map, typed extraction schema and prompt-injection tests.
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
