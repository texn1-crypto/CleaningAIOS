# Additional public tender source: Roseltorg

## Access evidence, 2026-09-24

Initial desktop probes could not connect to Roseltorg, RosTender or TenderGuru.
Those failures were **not proof of downtime**: direct unauthenticated HTTPS GETs
from the intended production web container returned HTTP 200 for all three robots
files. No alternate identity, credentials, proxy or challenge was used.
Tender.Pro returned HTTP 503 with a JavaScript proof-of-work/cookie challenge on
its public root; it was not executed or retried. RosTender/TenderGuru catalogs
remain unverified.

The [Roseltorg public GET search form](https://www.roseltorg.ru/) exposes
`query_field` and `region[]`. Observed options 47 (Leningrad Oblast) and 78
(Saint Petersburg) explicitly select the **customer region**. The narrow source
is [the first page for «уборк», regions 47/78](https://www.roseltorg.ru/procedures/search?query_field=%D1%83%D0%B1%D0%BE%D1%80%D0%BA&region%5B%5D=47&region%5B%5D=78).
This is not arbitrary internet search, pagination, a complete feed or an EIS API.

[Robots rules](https://www.roseltorg.ru/robots.txt) allowed the observed
`/procedures/search?...` and `/procedure/<number>/<lot>` for CleaningAIOS;
the different `/search/*?` route is disallowed. Rules are re-read per origin on
each run. The coordinator independently confirmed the catalog and two detail
pages using the application's DNS-pinning transport, identified User-Agent,
HTTPS, no redirects and no authentication. All returned HTTP 200.

Parent-check hashes (page content can change between reads):

- robots: `5d42adb48e71b92ee6dfa96221ad67da8ac4f71376f7fb1c6e7f7aaf904bd5ea`
- catalog: `dad99471e6e712d765f34f42e2814c6accbbbbbccc00d0575cca4113747dfb52`
- first detail: `18abea3c83708538a1a203fdc6ec238c2d35f9696b33ac9fdb662ba97343e618`
- second detail: `4282b04c0c1c5d1162de1447c73efea0ff3fb73916ae3ae8a9ba0c19670fa205`

## Real notice samples, not test fixtures

| Public source | Observed facts | Performance geography |
| --- | --- | --- |
| [0372200177726000099, lot 1](https://www.roseltorg.ru/procedure/0372200177726000099/1) | Roof snow/ice removal and territory cleanup; published 18.09.2026 15:41 MSK; deadline 30.09.2026 08:00 MSK; 2,850,000 RUB; accepting applications. | Explicit Saint Petersburg addresses: Gavanskaya 54 and Opochinina 35. |
| [0345200004026000822, lot 1](https://www.roseltorg.ru/procedure/0345200004026000822/1) | Hospital territory cleaning; published 23.09.2026 14:23 MSK; deadline 30.09.2026 09:00 MSK; 3,000,000 RUB; accepting applications. | Cadastral identifier and service periods only. City remains NEEDS_VERIFICATION; buyer region is not substituted. |

Two upcoming notices were verified, but only one sample has explicit city/address
evidence. Neither has been legally qualified or submitted. Detail labels say
**procedure number**, not EIS registration number. Observed EIS links point to
document downloads/home, not an EIS notice card; no notice URL or cross-provider
identity mapping may be invented from digit length.

## Observed DOM contract

- Catalog: `.search-results__item`, procedure/lot from
  `data-feature-favorite-lots-procedure-number` / `...-lot-number`.
- Exact observed detail link/title: `a.search-results__link--description`.
- Stage: `.search-results__status`; accepting-application cards are candidates.
- Price: `.search-results__sum p.desktop` (fraction nested in `sub`), RUB only.
  Duplicate mobile nodes must not be concatenated.
- Catalog `.search-results__time` lacks timezone: it cannot establish a usable
  future deadline. Some accepting-application cards display months-old dates.
- Detail `.lot-common-info__row` pairs `.lot-common-info__label` and
  `.lot-common-info__value`: publication, deadline, procedure number, title,
  organizer. Ingest only named fields, not contact/person fields.
- Explicit detail time: `до 30.09.26 08:00 (МСК)`; publication omits `до`.
- Current stage: `.lot-steps__heading.steps__item--current .lot-steps__title`.
  Inactive future `Завершен` elsewhere is not the current stage.
- Delivery: `.lot-delivery__text .lot-expand-text__text`. Cadastral identifiers
  or service periods without a city remain unknown, not inside/outside SPb/LO.

## Implementation boundary

Use existing Task/PostgreSQL/PDF/outbox flow, exact catalog URL and bounded public
detail paths. Cache robots separately per origin; failures on one platform must
not invalidate another. Cap: two robots, three catalogs, sixteen details (21 GETs).

Deduplicate exact provider procedure+lot identities. Do not collapse distinct lots
or infer EIS mapping from a numeric Roseltorg number. Cross-source duplication
without explicit shared identity remains an unresolved risk, not a claim of global
deduplication. Failed or ambiguous detail checks never establish a usable deadline.

Synthetic fixtures in `tests/test_tender_search.py` model the observed DOM; they
are not live evidence. Requirement 9 remains PARTIAL. Test/CI/deployment evidence
must be recorded separately before claiming release.
