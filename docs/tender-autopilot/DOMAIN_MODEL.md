# Domain model

## Текущие canonical objects

- `BusinessRecord(record_type=tender)` — агрегат найденной закупки и mutable
  projection её текущего состояния. Для generic feed его external identity задаётся
  парой `(source, external_id)`; одинаковые provider-local ID разных источников не
  объединяются. Для остальных типов `BusinessRecord` сохранена уникальность пары
  `(record_type, external_id)`.
- `TenderDocument` — зарегистрированный оригинал/версия с source URL, storage path,
  MIME, checksum и analysis metadata. Product specification extraction хранится
  как `extracted/needs_verification`; отдельный review привязан к checksum,
  extractor version, полному набору candidate hashes и actor. Проверенные факты
  требований и предложения собираются в persisted draft-историю внутри
  `BusinessRecord.data`, связанную с checksum и review hash каждого документа;
  draft остаётся `needs_verification` и не заменяет decision snapshot.
- `TenderSourceRun` — final receipt каждой настроенной попытки collection: безопасная
  метка и hash источника, timing, HTTP status, outcome counters и bounded error type.
  Raw exception, query/userinfo и provider secret в публичный контракт не входят.
- `TenderAssessmentSnapshot` — append-only паспорт решения: canonical inputs,
  exact hashes, rules version, result и actor. Внутри snapshot хранится
  evidence-bound product compliance matrix; confidence остаётся только
  advisory и не заменяет evidence.
- `Task` + `TaskTransition` — durable workflow и его неизменяемая история.
- `ApprovalRequest` + `ApprovalDecisionRecord` — отдельное разрешение защищённого
  действия и terminal receipt решения.
- `DomainEvent` + `EventConsumerReceipt` — transactional outbox и exactly-once
  consumer effect.

## Следующие relational objects

`Procurement`, `ProcurementVersion`, `DocumentVersion`, `EvidenceFact`,
`Requirement`, `RequirementConflict`, `CompanyCapability`, `Supplier`,
`SupplierQuote`, `QuoteLine`, `EconomicsScenario`, `RiskAssessment`,
`ApplicationPackage`, `ExternalActionReceipt`, `ContractObligation`,
`ActualCost`, `ActualProfit`.

Их следует вводить по vertical slice, не создавая параллельную истину рядом с
существующими records.

## Классы знания

| Класс | Может участвовать в final decision |
|---|---|
| `raw` | Нет, только provenance |
| `extracted` | Нет без evidence binding |
| `verified` | Да, пока freshness валиден |
| `calculated` | Да, если все inputs verified и rules version известна |
| `predicted` | Только как uncertainty-aware сигнал |
| `human_confirmed` | Да, с actor/time/reason и без затирания raw fact |

## Identity и versioning

Юридическое лицо идентифицируется надёжным legal identifier, закупка — парой
provider/external ID, документ — content hash плюс source version, quote — supplier
и quote reference/version. Title, filename и свободный текст не являются
устойчивыми идентификаторами.
