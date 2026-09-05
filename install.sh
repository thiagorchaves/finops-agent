#!/usr/bin/env bash
# Instala o finops-agent como um systemd --user timer (roda diariamente,
# na sessão do seu usuário, com acesso a notify-send para notificação desktop).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo "==> Criando virtualenv em $HERE/venv"
python3 -m venv venv
./venv/bin/pip install --upgrade pip >/dev/null
./venv/bin/pip install -r requirements.txt

if [ ! -f config.yaml ]; then
    echo "==> Copiando config.example.yaml -> config.yaml (edite antes de usar!)"
    cp config.example.yaml config.yaml
fi

if [ ! -f .env ]; then
    echo "==> Criando .env (coloque a senha do SMTP aqui, se for usar email)"
    cat > .env <<'EOF'
# FINOPS_SMTP_PASSWORD=sua-senha-de-app-aqui
EOF
    chmod 600 .env
fi

mkdir -p "$HOME/.config/systemd/user"
cp systemd/finops-agent.service "$HOME/.config/systemd/user/"
cp systemd/finops-agent.timer "$HOME/.config/systemd/user/"

systemctl --user daemon-reload
systemctl --user enable --now finops-agent.timer

echo ""
echo "==> Pronto. Timer instalado e habilitado."
echo "    Edite $HERE/config.yaml com seus profiles AWS antes do primeiro alerta real."
echo "    Se for usar email, edite $HERE/.env com FINOPS_SMTP_PASSWORD."
echo ""
echo "Comandos úteis:"
echo "  systemctl --user status finops-agent.timer     # ver próxima execução"
echo "  systemctl --user start finops-agent.service     # rodar agora, manualmente"
echo "  journalctl --user -u finops-agent.service -f    # ver logs"
echo ""
echo "Se o notebook nunca fica com sessão gráfica aberta o dia todo, rode:"
echo "  loginctl enable-linger \$USER"
echo "para o timer disparar mesmo sem estar logado (a notificação desktop, porém,"
echo "só aparece de fato quando você estiver com a sessão gráfica ativa)."
