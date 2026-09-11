# Economics engine v1

Первый snapshot использует `Decimal` и RUB с округлением до копейки.

```text
direct = supplier + payroll + logistics + other + onboarding
tax = contract_value * tax_percent
contingency = direct * contingency_percent
net_profit = contract_value - direct - tax - contingency
margin = net_profit / contract_value
```

Conservative scenario уменьшает revenue и увеличивает direct costs по явным
policy inputs. Working capital оценивается из monthly recurring direct cost,
payment delay, securities и onboarding.

Минимальная допустимая цена (`stop_price`) решает уравнение для заданной minimum
margin:

```text
stop_price = direct * (1 + contingency_rate)
             / (1 - tax_rate - minimum_margin_rate)
```

## Auction forecast boundary

The decision snapshot accepts an owner-provided expected auction discount and
persists a deterministic forecast of the resulting bid, profit and margin. This
is an assumption analysis, not an AI prediction. The forecast fails closed when
the expected bid is below `stop_price`, never enables automatic bidding, and
records the invariant and its result in the immutable decision snapshot.

Если знаменатель неположительный, policy невалидна. Ни одно protected действие
не разрешается на основании provisional economics. В v1 ещё не моделируются VAT
credit, financing price, probability distributions, penalties и actual accounting
facts; они отмечены roadmap gates, а не скрыты внутри коэффициента.
