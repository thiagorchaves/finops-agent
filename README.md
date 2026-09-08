# finops-agent

Agente de FinOps simples para rodar **localmente, no seu notebook**. Para cada
conta AWS que você tem acesso (via profile próprio — sem depender da conta
"main"/management), ele consulta o AWS Cost Explorer todo dia e te avisa
(email e/ou notificação desktop) se o custo subiu muito acima do padrão
recente daquela conta.

## Como funciona

1. Para cada `profile` no `config.yaml`, busca o custo diário (`UnblendedCost`)
   dos últimos `lookback_days + 1` dias via `ce:GetCostAndUsage`.
2. O "dia de referência" é sempre **ontem** (não hoje) — o Cost Explorer tem
   um atraso natural de 24-48h para consolidar os dados do dia corrente, então
   olhar para "hoje" costuma dar números incompletos e falsos positivos.
3. Calcula a média dos `lookback_days` dias anteriores e compara com o dia de
   referência.
4. Se a variação passar do `threshold_pct` configurado (e o custo do dia não
   for pequeno demais para importar — veja `min_daily_cost_usd`), dispara um
   alerta com os 3 serviços que mais custaram naquele dia, para você já saber
   por onde começar a investigar.
5. Guarda um pequeno arquivo de estado local para não te alertar duas vezes no
   mesmo dia pela mesma conta, caso o agente rode mais de uma vez.

## Instalação

Pré-requisitos: Python 3.9+, e os profiles AWS já configurados em
`~/.aws/credentials` (um por conta que você acessa).

```bash
cd finops-agent
./install.sh
```

O script `install.sh`:
- cria um virtualenv e instala as dependências (`boto3`, `PyYAML`);
- copia `config.example.yaml` → `config.yaml` (se ainda não existir);
- cria um `.env` vazio para a senha do SMTP;
- instala e habilita um **systemd --user timer** que roda o agente
  diariamente às 08:00 (horário configurável em
  `systemd/finops-agent.timer`).

Depois de rodar, edite dois arquivos antes do primeiro alerta valer a pena:

- **`config.yaml`** — troque `conta-cliente-a` / `conta-cliente-b` pelos
  nomes reais dos seus profiles AWS (veja `aws configure list-profiles`), e
  ajuste `threshold_pct` / `min_daily_cost_usd` se quiser.
- **`.env`** — se for usar email, descomente e preencha
  `FINOPS_SMTP_PASSWORD=...` (uma **senha de app**, nunca a senha normal da
  conta, se for Gmail: https://myaccount.google.com/apppasswords). O arquivo
  já é criado com permissão `600` (só você lê).

## Permissão IAM necessária

Cada profile/usuário IAM só precisa de uma permissão para isso funcionar —
**não precisa de acesso à conta main nem a nada além da própria conta**:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "FinOpsAgentReadOnly",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage"
      ],
      "Resource": "*"
    }
  ]
}
```

Anexe essa policy ao usuário/role de cada conta que você quer monitorar. Se o
Cost Explorer nunca foi habilitado naquela conta, habilite uma vez pelo
console (Billing → Cost Explorer → Enable) — pode levar até 24h para os
primeiros dados aparecerem.

## Rodar manualmente / testar

```bash
source venv/bin/activate
python3 finops_agent.py --dry-run          # mostra o que faria, sem enviar nada
python3 finops_agent.py                     # roda de verdade
```

Ou, já com o systemd instalado:

```bash
systemctl --user start finops-agent.service   # roda agora
journalctl --user -u finops-agent.service -f  # acompanha os logs
systemctl --user status finops-agent.timer    # vê quando roda de novo
```

## Notificação Slack

Crie um [Incoming Webhook](https://api.slack.com/messaging/webhooks) no
workspace/canal desejado e defina a URL na variável de ambiente
`FINOPS_SLACK_WEBHOOK_URL` (mesmo esquema do `FINOPS_SMTP_PASSWORD`, nunca no
`config.yaml`). Depois é só habilitar `notifications.slack.enabled: true`.

## Painel gerencial

A cada execução (real ou `--dry-run`), o agente gera um arquivo HTML
(`dashboard_file`, padrão `~/.finops-agent/dashboard.html`) com uma tabela
resumo de todas as contas, o gráfico de custo diário de cada uma (usando os
dados já buscados no Cost Explorer nesta execução, cobrindo `lookback_days`
dias) e os maiores serviços do dia para contas em alerta. Basta abrir o
arquivo no navegador — não precisa de servidor.

## Notificação desktop

Usa `notify-send` (pacote `libnotify-bin` na maioria das distros Debian/Ubuntu
— já vem instalado por padrão no GNOME/Ubuntu). Só aparece na tela quando
você está logado numa sessão gráfica ativa no momento em que o timer dispara;
por isso o email é o canal "garantido" — a notificação desktop é o bônus para
quando você está com o notebook aberto.

## Ajustando a regra de alerta

Tudo isso é por conta (`accounts:` no config.yaml) ou global:

- `threshold_pct`: % acima da média de 7 dias que dispara o alerta (padrão 30%).
- `lookback_days`: quantos dias usar para calcular essa média (padrão 7).
- `min_daily_cost_usd`: custo mínimo do dia para o alerta valer a pena (padrão
  US$5 — evita alerta de "100% de aumento" numa conta que foi de US$0,50 para
  US$1,00).

Você pode sobrescrever `threshold_pct` por conta individualmente, adicionando
`threshold_pct: 50` dentro do item da conta em `accounts:`.

## Limitações conhecidas

- Cost Explorer tem custo por chamada de API (US$0,01 por request) — com uma
  execução por dia por conta, isso é centavos por mês, irrelevante.
- Isso monitora **custo por conta como um todo**, não por serviço/tag
  individualmente (embora o breakdown por serviço apareça no corpo do
  alerta). Se quiser alertar por serviço ou por tag de projeto, dá para
  estender `get_service_breakdown` para isso.
- O agente só roda quando o notebook está ligado no horário do timer
  (`Persistent=true` faz ele rodar assim que o notebook ligar, se perdeu o
  horário exato).
