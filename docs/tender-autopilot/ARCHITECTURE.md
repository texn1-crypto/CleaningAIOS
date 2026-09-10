# Target architecture

## Контуры

```text
Official sources / imports / operator
                |
                v
 Procurement providers -> normalized tender + immutable originals
                |
                v
 Document pipeline -> evidence facts -> requirement graph
                |
                +-> company qualification
                +-> supplier quote verification
                +-> deterministic Decimal economics
                +-> legal/operational risk rules
                |
                v
 Immutable TenderPrequalificationSnapshot
        |
        v
 Immutable TenderSupplierQuoteSnapshot
        |
        v
 Immutable TenderAssessmentSnapshot
                |
                v
 Task + Approval Engine -> assisted action adapters -> external receipt
                |
                v
 Contract execution -> accounting facts -> actual profit -> controlled learning
```

## Три плоскости

1. **Evidence plane** — оригиналы, hashes, extracts, durable source-run receipts,
   source timestamps, conflicts.
2. **Decision plane** — deterministic rules, economics, risk, frozen snapshots.
3. **Execution plane** — scoped adapters. Любое внешнее write-действие требует
   idempotency key, актуальный approval token и receipt.

Developer plane (Codex/CI/CD) отделён от business execution plane. Production
agents не получают shell или право изменять production code.

## Неподвижные инварианты

- `UNKNOWN -> needs_verification`; неизвестное не подменяется нулём.
- Critical money считается `Decimal`, результаты хранятся как точные строки.
- Snapshot связывает source, facts, document checksums, quote, policy и rules version.
- Supplier work разрешается только полным evidence-bound prequalification snapshot
  со статусом `eligible`; missing/UNKNOWN не проходят guard.
- Economics может ссылаться на supplier quote snapshot только со статусом
  `verified`, с совпадающими tender, prequalification hash и canonical quote.
- Изменение документа, quote или economics создаёт новый hash и новый approval.
- Participation approval не разрешает submission, signing, bid или payment.
- LLM может предложить факт, но не активирует его без evidence policy.
- Внешний успех существует только после сохранённого provider receipt.

## Масштабирование

До подтверждённой нагрузки сохраняется модульный монолит: это уменьшает
распределённые транзакции и использует существующий outbox. Отдельные workers и
queues выделяются по профилю нагрузки (`documents`, `extraction`, `supplier`,
`economics`, `submission`) без смены system of record.
