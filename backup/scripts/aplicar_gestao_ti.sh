#!/usr/bin/env bash
# aplicar_gestao_ti.sh
# Instala ou atualiza a Gestao TI (modulos Rede Sem Fio, E-mails em lote, Gestao de Usuarios, Logs) com Docker.
#
# Teste local (na sua maquina, com servidor de e-mail falso em http://localhost:8025):
#   sudo MODO=local ./aplicar_gestao_ti.sh
# Servidor:
#   sudo PAINEL_URL=http://gestaoti.santaluzia.int:8090 ./aplicar_gestao_ti.sh
#
# - Copia os arquivos para /opt/gestao-ti (ou DESTINO=...)
# - Na primeira vez, cria o .env com senhas e chaves aleatorias
# - Sobe os containers (docker compose up -d --build)
# - Acrescenta ao .env as configuracoes novas de cada versao
# - Gera em saida-windows/ o script da GPO ja com o endereco e a chave
#
# Rodar de novo atualiza o codigo e mantem o .env e o banco de dados.
set -euo pipefail

ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINO="${DESTINO:-/opt/gestao-ti}"
MODO="${MODO:-servidor}"
PERFIL=""
[ "$MODO" = "local" ] && PERFIL="--profile teste"

msg()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
erro() { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }
aleatorio() { (LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-40}") || true; }

command -v docker >/dev/null || erro "Docker nao encontrado neste servidor."
docker compose version >/dev/null 2>&1 || erro "Plugin 'docker compose' nao encontrado."

mkdir -p "$DESTINO"
if [ "$(cd "$DESTINO" && pwd)" != "$ORIGEM" ]; then
  msg "Copiando arquivos para $DESTINO"
  for item in api cliente-windows docker-compose.yml .env.example README.md aplicar_gestao_ti.sh; do
    [ -e "$ORIGEM/$item" ] || continue
    rm -rf "${DESTINO:?}/$item"
    cp -a "$ORIGEM/$item" "$DESTINO/$item"
  done
  # .env que veio no pacote: so e usado na primeira instalacao (nunca sobrescreve o .env existente)
  if [ -f "$ORIGEM/.env" ] && [ ! -f "$DESTINO/.env" ]; then
    msg "Usando o .env que veio no pacote"
    cp "$ORIGEM/.env" "$DESTINO/.env"
    chmod 600 "$DESTINO/.env"
  fi
fi
cd "$DESTINO"

PRIMEIRA_VEZ=0
if [ ! -f .env ]; then
  PRIMEIRA_VEZ=1
  msg "Criando .env com valores aleatorios"
  IP_SERVIDOR="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [ "$MODO" = "local" ]; then URL="${PAINEL_URL:-http://localhost:8090}"
  else URL="${PAINEL_URL:-http://${IP_SERVIDOR:-localhost}:8090}"; fi
  SENHA_ADMIN="$(aleatorio 16)"
  cp .env.example .env
  sed -i \
    -e "s|^DB_PASSWORD=.*|DB_PASSWORD=$(aleatorio 32)|" \
    -e "s|^AGENT_KEY=.*|AGENT_KEY=$(aleatorio 48)|" \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=$(aleatorio 64)|" \
    -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${SENHA_ADMIN}|" \
    -e "s|^PAINEL_URL=.*|PAINEL_URL=${URL}|" \
    .env
  chmod 600 .env
  {
    echo "Gestao TI: ${URL}"
    echo "Usuario:   admin   (acesso local de emergencia)"
    echo "Senha:     ${SENHA_ADMIN}"
    echo
    echo "Troque a senha em Gestao de Usuarios e apague este arquivo."
  } > CREDENCIAIS-INICIAIS.txt
  chmod 600 CREDENCIAIS-INICIAIS.txt
elif [ -n "${PAINEL_URL:-}" ]; then
  msg "Atualizando PAINEL_URL no .env"
  sed -i "s|^PAINEL_URL=.*|PAINEL_URL=${PAINEL_URL}|" .env
fi

# Acrescenta ao .env as configuracoes novas desta versao (as existentes nao sao alteradas)
NOVAS=0
while IFS= read -r linha; do
  case "$linha" in ''|\#*) continue ;; esac
  chave="${linha%%=*}"
  if ! grep -qE "^${chave}=" .env; then
    [ "$NOVAS" = 0 ] && printf '\n# ---- adicionado na atualizacao de %s ----\n' "$(date +%F)" >> .env
    echo "$linha" >> .env
    NOVAS=$((NOVAS + 1))
  fi
done < .env.example
[ "$NOVAS" -gt 0 ] && msg "$NOVAS configuracao(oes) nova(s) adicionada(s) ao .env"
mkdir -p marca

valor() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
if [ -z "$(valor DB_PASSWORD)" ]; then
  erro "DB_PASSWORD esta vazio em $DESTINO/.env. Preencha a senha do banco antes de instalar
       (ela fica gravada no banco na primeira vez; trocar depois faz a aplicacao perder o acesso):
         sudo nano $DESTINO/.env
       e rode este script de novo."
fi
# Endereco que as estacoes usam para consultar a API: AGENTE_URL (direto no servidor, sem proxy);
# se estiver vazio, usa o PAINEL_URL
API_URL="$(valor AGENTE_URL)"
[ -n "$API_URL" ] || API_URL="$(valor PAINEL_URL)"
AGENT_KEY="$(valor AGENT_KEY)"
[ -n "$API_URL" ] && [ -n "$AGENT_KEY" ] || erro "AGENTE_URL/PAINEL_URL ou AGENT_KEY vazio no .env"

msg "Gerando scripts das estacoes em $DESTINO/saida-windows"
rm -rf saida-windows && mkdir -p saida-windows
for arq in cliente-windows/*.ps1; do
  nome="$(basename "$arq")"
  # BOM UTF-8 + quebra de linha do Windows: o PowerShell 5.1 le os acentos corretamente
  { printf '\xEF\xBB\xBF'
    sed -e "s|__API_URL__|${API_URL}|g" -e "s|__AGENT_KEY__|${AGENT_KEY}|g" "$arq" | sed 's/$/\r/'
  } > "saida-windows/$nome"
done
# Icone do atalho do portal (vai para o NETLOGON junto com o script)
cp -f cliente-windows/icone/*.ico saida-windows/ 2>/dev/null || true
chmod 600 saida-windows/*

if [ "$MODO" != "local" ] && [ "$(valor SMTP_HOST)" = "mailpit" ]; then
  printf '\033[1;33mAVISO:\033[0m SMTP_HOST=mailpit e o servidor de e-mail FALSO de teste. Configure o SMTP real no .env.\n'
fi

msg "Subindo os containers${PERFIL:+ (modo local, com o servidor de e-mail de teste)}"
# shellcheck disable=SC2086
docker compose $PERFIL up -d --build

msg "Aguardando a API responder"
PORTA="$(valor PORTA)"; PORTA="${PORTA:-8090}"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORTA}/health" >/dev/null 2>&1; then
    msg "API no ar: ${API_URL}"
    break
  fi
  sleep 2
done
curl -fsS "http://127.0.0.1:${PORTA}/health" >/dev/null 2>&1 || {
  docker compose $PERFIL logs --tail 40 api
  erro "A API nao respondeu. Veja os logs acima."
}

echo
if [ "$PRIMEIRA_VEZ" = 1 ]; then
  cat CREDENCIAIS-INICIAIS.txt
  echo
fi
echo "Gestao TI (login e modulos):    ${API_URL}"
echo "Portal do Wi-Fi (usuarios):     ${API_URL%/}/wifi"
[ "$MODO" = "local" ] && echo "Caixa de e-mail de TESTE:       http://localhost:8025"
echo
echo "Script do Wi-Fi para a GPO (copie para \\\\santaluzia.int\\NETLOGON\\PMSL-WiFi):"
ls -1 "$DESTINO/saida-windows"
