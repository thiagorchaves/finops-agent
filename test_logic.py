"""
Teste rápido (sem AWS real) da lógica de média móvel + threshold, usando
mocks das respostas do Cost Explorer. Não faz parte do agente em produção,
é só para validar o cálculo antes do primeiro uso real.

Rodar: python3 test_logic.py
"""
from unittest.mock import patch

import finops_agent as fa


def make_ce_response(daily_amounts):
    """daily_amounts: lista de floats, do dia mais antigo para o mais recente."""
    from datetime import date, timedelta

    reference_day = date.today() - timedelta(days=1)
    start = reference_day - timedelta(days=len(daily_amounts) - 1)
    results = []
    for i, amount in enumerate(daily_amounts):
        day = (start + timedelta(days=i)).isoformat()
        results.append(
            {
                "TimePeriod": {"Start": day, "End": day},
                "Total": {"UnblendedCost": {"Amount": str(amount)}},
            }
        )
    return {"ResultsByTime": results}


class FakeCEClient:
    def __init__(self, response):
        self._response = response

    def get_cost_and_usage(self, **kwargs):
        return self._response


def run_case(name, daily_amounts, threshold_pct, min_daily_cost, expect_alert, min_absolute_increase_usd=0):
    cfg = {
        "ce_region": "us-east-1",
        "lookback_days": 7,
        "threshold_pct": threshold_pct,
        "min_daily_cost_usd": min_daily_cost,
        "min_absolute_increase_usd": min_absolute_increase_usd,
    }
    account_cfg = {"profile": "fake-profile", "name": name}

    fake_response = make_ce_response(daily_amounts)

    with patch("boto3.Session") as mock_session:
        mock_session.return_value.client.return_value = FakeCEClient(fake_response)
        result = fa.evaluate_account(cfg, account_cfg)

    status = "ALERTA" if result["alert"] else "ok"
    assert result["alert"] == expect_alert, (
        f"[{name}] esperado alert={expect_alert}, veio {result['alert']} "
        f"(pct_diff={result['pct_diff']})"
    )
    print(f"[{name}] custo_ref={result['reference_cost']} média={result['avg_cost']:.2f} "
          f"pct={result['pct_diff']:.1f}% -> {status}  (OK)")


def main():
    # Caso 1: custo estável, 7 dias em ~10 + dia de referência em 11 (10% de alta) -> sem alerta (threshold 30%)
    run_case("estavel", [10, 10, 10, 10, 10, 10, 10, 11], threshold_pct=30, min_daily_cost=5.0, expect_alert=False)

    # Caso 2: pico claro, média ~10, dia de referência 25 (150% de alta) -> alerta
    run_case("pico", [10, 10, 10, 10, 10, 10, 10, 25], threshold_pct=30, min_daily_cost=5.0, expect_alert=True)

    # Caso 3: conta pequena (média ~1, referência 3 = +200%) mas abaixo do min_daily_cost -> sem alerta
    run_case("conta-pequena", [1, 1, 1, 1, 1, 1, 1, 3], threshold_pct=30, min_daily_cost=5.0, expect_alert=False)

    # Caso 4: conta que começa do zero (média 0) e passa a custar -> alerta
    run_case("do-zero", [0, 0, 0, 0, 0, 0, 0, 20], threshold_pct=30, min_daily_cost=5.0, expect_alert=True)

    # Caso 5: conta grande, +15% mas +$450 em valor absoluto -> alerta (mesmo com % baixo)
    run_case("grande-delta-alto", [3000] * 7 + [3450], threshold_pct=10, min_daily_cost=5.0,
             expect_alert=True, min_absolute_increase_usd=300)

    # Caso 6: conta pequena, +80% mas só +$40 em valor absoluto -> sem alerta (delta mínimo não bate)
    run_case("pequena-delta-baixo", [50] * 7 + [90], threshold_pct=30, min_daily_cost=5.0,
             expect_alert=False, min_absolute_increase_usd=300)

    print("\nTodos os casos passaram.")


if __name__ == "__main__":
    main()
