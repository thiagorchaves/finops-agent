#!/usr/bin/env python3
"""
finops-agent
------------
Roda localmente (no seu notebook) e verifica, para cada conta AWS listada no
config.yaml, se o custo de ontem subiu muito em relação à média móvel dos
últimos N dias. Se sim, dispara alerta (email e/ou notificação desktop).

Pensado para contas às quais você tem acesso via profile próprio (sem acesso
à conta "main"/management) — cada profile é consultado isoladamente via
AWS Cost Explorer.

Uso:
    python3 finops_agent.py                  # roda normal, usando ./config.yaml
    python3 finops_agent.py --config outro.yaml
    python3 finops_agent.py --dry-run         # não envia alertas, só imprime o que faria
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import subprocess
import sys
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import BotoCoreError, ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("finops-agent")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["state_file"] = str(Path(cfg.get("state_file", "~/.finops-agent/state.json")).expanduser())
    return cfg


# --------------------------------------------------------------------------- #
# Cost Explorer
# --------------------------------------------------------------------------- #
def get_daily_costs(profile: str, region: str, lookback_days: int) -> list[tuple[str, float]]:
    """
    Retorna [(data, custo_usd), ...] em ordem crescente de data, cobrindo
    `lookback_days` dias anteriores + o dia de referência (ontem).
    Total de `lookback_days + 1` pontos.
    """
    session = boto3.Session(profile_name=profile)
    ce = session.client("ce", region_name=region)

    reference_day = date.today() - timedelta(days=1)  # ontem: último dia "fechado"
    start = reference_day - timedelta(days=lookback_days)
    end = date.today()  # End é exclusivo na API, então isso cobre até "ontem"

    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
    )

    daily = []
    for period in resp["ResultsByTime"]:
        day = period["TimePeriod"]["Start"]
        amount = float(period["Total"]["UnblendedCost"]["Amount"])
        daily.append((day, amount))
    daily.sort(key=lambda x: x[0])
    return daily


def get_cost_breakdown(profile: str, region: str, day: str, group_by_key: str, top_n: int = 3,
                        filter_service: str | None = None) -> list[tuple[str, float]]:
    """Top N valores agrupados por SERVICE ou USAGE_TYPE no dia informado (contexto do alerta)."""
    session = boto3.Session(profile_name=profile)
    ce = session.client("ce", region_name=region)

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    kwargs = dict(
        TimePeriod={"Start": day, "End": next_day},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": group_by_key}],
    )
    if filter_service:
        kwargs["Filter"] = {"Dimensions": {"Key": "SERVICE", "Values": [filter_service]}}

    resp = ce.get_cost_and_usage(**kwargs)
    items = []
    for group in resp["ResultsByTime"][0].get("Groups", []):
        name = group["Keys"][0]
        amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
        items.append((name, amount))
    items.sort(key=lambda x: -x[1])
    return items[:top_n]


# --------------------------------------------------------------------------- #
# Estado (dedupe de alertas por dia)
# --------------------------------------------------------------------------- #
def load_state(state_file: str) -> dict:
    p = Path(state_file)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state_file: str, state: dict) -> None:
    p = Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Notificações
# --------------------------------------------------------------------------- #
def send_email(cfg: dict, subject: str, body: str) -> None:
    email_cfg = cfg["notifications"]["email"]
    password = os.environ.get("FINOPS_SMTP_PASSWORD")
    if not password:
        log.error("FINOPS_SMTP_PASSWORD não definida no ambiente — email não enviado.")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_cfg["from"]
    msg["To"] = email_cfg["to"]

    with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=20) as server:
        server.starttls()
        server.login(email_cfg["smtp_user"], password)
        server.sendmail(email_cfg["from"], [email_cfg["to"]], msg.as_string())
    log.info("Email enviado para %s", email_cfg["to"])


def send_desktop(subject: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "--urgency=critical", subject, body],
            check=True,
            timeout=10,
        )
        log.info("Notificação desktop enviada.")
    except FileNotFoundError:
        log.warning("notify-send não encontrado — instale libnotify-bin para notificações desktop.")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("Falha ao enviar notificação desktop: %s", exc)


def notify(cfg: dict, subject: str, body: str, dry_run: bool) -> None:
    if dry_run:
        log.info("[DRY-RUN] Alerta que seria enviado:\n%s\n%s", subject, body)
        return

    notif_cfg = cfg.get("notifications", {})
    if notif_cfg.get("email", {}).get("enabled"):
        try:
            send_email(cfg, subject, body)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Falha ao enviar email: %s", exc)
    if notif_cfg.get("desktop", {}).get("enabled"):
        send_desktop(subject, body)


# --------------------------------------------------------------------------- #
# Avaliação por conta
# --------------------------------------------------------------------------- #
def evaluate_account(cfg: dict, account_cfg: dict) -> dict:
    profile = account_cfg["profile"]
    name = account_cfg.get("name", profile)
    region = cfg.get("ce_region", "us-east-1")
    lookback_days = cfg.get("lookback_days", 7)
    threshold_pct = account_cfg.get("threshold_pct", cfg.get("threshold_pct", 30))
    min_daily_cost = cfg.get("min_daily_cost_usd", 5.0)
    min_absolute_increase = account_cfg.get(
        "min_absolute_increase_usd", cfg.get("min_absolute_increase_usd", 0)
    )

    result = {
        "profile": profile,
        "name": name,
        "ok": False,
        "alert": False,
        "reference_day": None,
        "reference_cost": None,
        "avg_cost": None,
        "pct_diff": None,
        "absolute_increase": None,
        "message": None,
    }

    try:
        daily = get_daily_costs(profile, region, lookback_days)
    except (BotoCoreError, ClientError, Exception) as exc:  # noqa: BLE001 - queremos seguir para as outras contas
        log.error("[%s] Falha ao consultar Cost Explorer: %s", name, exc)
        result["message"] = f"erro ao consultar: {exc}"
        return result

    if len(daily) < lookback_days + 1:
        log.warning("[%s] Histórico insuficiente (%d dias) — ainda coletando dados.", name, len(daily))
        result["message"] = "histórico insuficiente"
        return result

    reference_day, reference_cost = daily[-1]
    previous_days = daily[:-1]
    avg_cost = sum(c for _, c in previous_days) / len(previous_days)

    if avg_cost > 0:
        pct_diff = (reference_cost / avg_cost - 1) * 100
    else:
        pct_diff = 100.0 if reference_cost > 0 else 0.0

    result.update(
        ok=True,
        reference_day=reference_day,
        reference_cost=reference_cost,
        avg_cost=avg_cost,
        pct_diff=pct_diff,
    )

    log.info(
        "[%s] %s: US$%.2f (média %d dias: US$%.2f, %+.1f%%)",
        name, reference_day, reference_cost, lookback_days, avg_cost, pct_diff,
    )

    absolute_increase = reference_cost - avg_cost
    result["absolute_increase"] = absolute_increase

    if reference_cost < min_daily_cost:
        return result  # custo pequeno demais para valer alerta, mesmo com %alta

    if pct_diff > threshold_pct and absolute_increase >= min_absolute_increase:
        result["alert"] = True
        try:
            top_services = get_cost_breakdown(profile, region, reference_day, "SERVICE")
        except (BotoCoreError, ClientError, Exception):  # noqa: BLE001
            top_services = []

        lines = [
            f"Conta: {name} (profile: {profile})",
            f"Dia: {reference_day}",
            f"Custo: US$ {reference_cost:.2f}  (média dos últimos {lookback_days} dias: US$ {avg_cost:.2f})",
            f"Variação: +{pct_diff:.1f}% / +US$ {absolute_increase:.2f} (limite: {threshold_pct}% / US$ {min_absolute_increase:.2f})",
        ]
        if top_services:
            lines.append("Maiores serviços do dia:")
            for svc, amount in top_services:
                lines.append(f"  - {svc}: US$ {amount:.2f}")

            # drill-down: usage types do serviço que mais custou, pra apontar a causa direto
            top_service_name = top_services[0][0]
            try:
                top_usage_types = get_cost_breakdown(
                    profile, region, reference_day, "USAGE_TYPE", filter_service=top_service_name
                )
            except (BotoCoreError, ClientError, Exception):  # noqa: BLE001
                top_usage_types = []
            if top_usage_types:
                lines.append(f"Detalhe de '{top_service_name}' por usage type:")
                for usage_type, amount in top_usage_types:
                    lines.append(f"  - {usage_type}: US$ {amount:.2f}")

        result["message"] = "\n".join(lines)

    return result


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="FinOps agent — alerta de aumento de custo AWS por conta.")
    parser.add_argument("--config", default="config.yaml", help="Caminho do config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Não envia alertas, só mostra o que faria.")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        log.error(
            "Config não encontrado em %s. Copie config.example.yaml para config.yaml e ajuste.",
            config_path,
        )
        return 1

    cfg = load_config(config_path)
    state = load_state(cfg["state_file"])
    state.setdefault("alerted", {})  # {"profile|data": true}

    accounts = cfg.get("accounts", [])
    if not accounts:
        log.error("Nenhuma conta configurada em 'accounts:' no config.yaml.")
        return 1

    any_alert = False
    results = []
    for account_cfg in accounts:
        result = evaluate_account(cfg, account_cfg)
        results.append(result)

        if not result["alert"]:
            continue

        dedupe_key = f"{result['profile']}|{result['reference_day']}"
        if state["alerted"].get(dedupe_key):
            log.info("[%s] Alerta de %s já foi enviado hoje, pulando.", result["name"], result["reference_day"])
            continue

        subject = f"[FinOps] {result['name']}: custo +{result['pct_diff']:.0f}% em {result['reference_day']}"
        notify(cfg, subject, result["message"], args.dry_run)
        any_alert = True

        if not args.dry_run:
            state["alerted"][dedupe_key] = True

    if not args.dry_run:
        save_state(cfg["state_file"], state)

    print_summary_table(results)

    if any_alert:
        log.info("Execução concluída com alertas disparados.")
    else:
        log.info("Execução concluída, nenhuma conta acima do limite.")
    return 0


def print_summary_table(results: list[dict]) -> None:
    """Resumo tipo 'top increases': todas as contas avaliadas, ordenadas pela maior variação."""
    ok_results = [r for r in results if r["ok"]]
    if not ok_results:
        return
    ok_results.sort(key=lambda r: r["pct_diff"], reverse=True)

    print(f"\n{'CONTA':<20} {'ONTEM':>12} {'MÉDIA 7D':>12} {'VARIAÇÃO':>12}")
    print("-" * 60)
    for r in ok_results:
        flag = " !" if r["alert"] else ""
        print(
            f"{r['name']:<20} {r['reference_cost']:>10.2f}$ {r['avg_cost']:>10.2f}$ "
            f"{r['pct_diff']:>+10.1f}%{flag}"
        )

    failed = [r for r in results if not r["ok"]]
    for r in failed:
        print(f"{r['name']:<20} {r['message']}")


if __name__ == "__main__":
    sys.exit(main())
