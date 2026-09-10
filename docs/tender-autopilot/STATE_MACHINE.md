# Procurement state machine

## Target lifecycle

```text
DISCOVERED -> NORMALIZED -> DOCUMENTS_READY -> REQUIREMENTS_READY
 -> QUALIFIED -> QUOTES_READY -> ECONOMICS_VALID -> RISK_REVIEWED
 -> READY_FOR_OWNER_REVIEW -> PARTICIPATION_APPROVED
 -> PACKAGE_READY -> SUBMISSION_APPROVED -> SUBMITTED_CONFIRMED
 -> WON | LOST
 WON -> CONTRACT_REVIEW -> SIGNING_APPROVED -> CONTRACT_ACTIVE
 -> FULFILLMENT -> ACCEPTED -> PAID -> RECONCILED -> CLOSED
```

Из любого pre-submission state возможны `NEEDS_VERIFICATION`, `PAUSED`,
`CANCELLED` или `EXPIRED`. Amendment переводит процесс к затронутому этапу и
инвалидирует downstream snapshots/approvals.

## Реализованный subset

Сейчас `BusinessRecord.status` остаётся backward-compatible projection. Новый
prequalification snapshot реализует `needs_verification`, `ineligible` и
`eligible`; только `eligible` выставляет внутренний guard
`supplier_discovery_allowed`. Новый decision snapshot реализует состояния:

- `needs_verification` — неизвестный обязательный факт, expired quote или missing evidence;
- `not_viable` — подтверждённый hard stop;
- `owner_risk_review_required` — подтверждённые данные, но risk выше policy;
- `ready_for_owner_review` — verification/economics/risk gates пройдены.

Только последнее состояние может породить task `tender_participation`. Исполнение
этой task готовит пакет; оно не переводит закупку в `submitted`.

## Guards

- Deadline открыт и привязан к source snapshot.
- Evidence document принадлежит tender и checksum совпадает.
- FAST DISQUALIFICATION использует полный фиксированный набор checks; missing,
  UNKNOWN или известный факт без evidence никогда не дают `eligible`.
- Mandatory requirements и qualification имеют `satisfied` + evidence.
- Supplier stock confirmed, quote не просрочен.
- Economics inputs verified; base/conservative margin и capital проходят policy.
- Approval относится к exact snapshot hash и не истёк.
- Submission boundary в будущем повторяет все guards и требует отдельный token.
