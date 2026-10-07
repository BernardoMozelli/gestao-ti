#!/usr/bin/env bash
# migrar_de_wifiaprovacao.sh
# Troca o sistema antigo do Wi-Fi (pmsl-wifi-aprovacao) pela Gestao TI no MESMO servidor, copiando os dados.
#
#   cd gestao-ti            (pasta extraida do zip)
#   chmod +x migrar_de_wifiaprovacao.sh aplicar_gestao_ti.sh
#   sudo ./migrar_de_wifiaprovacao.sh
#
# Opcoes (variaveis antes do comando):
#   URL_GESTAO=https://gestaoti.santaluzia.mg.gov.br   endereco da Gestao TI (padrao: este)
#   ANTIGO_DIR=/opt/pmsl-wifi-aprovacao                 pasta do sistema antigo
#   DESTINO=/opt/gestao-ti                              pasta da Gestao TI
#   SO_DADOS=1                                          so copia os dados (Gestao TI ja instalada e no ar)
#   FORCAR=1                                            copia de novo mesmo se ja tiver sido feita
#
# O que faz, nesta ordem (as estacoes ficam menos de 1 minuto sem resposta, e nesse caso mantem o estado atual):
#   1. Backup do banco antigo em /opt/backups
#   2. Prepara o .env da Gestao TI com a MESMA chave das estacoes (AGENT_KEY) e a mesma porta do sistema antigo
#   3. Instala a Gestao TI numa porta provisoria (18090), com o sistema antigo ainda atendendo
#   4. Para a API antiga, copia os dados (pedidos, liberacoes, historico, computadores, administradores)
#   5. A Gestao TI assume a porta do sistema antigo (8090)
#   6. Confere se a API e o endpoint das estacoes respondem e desliga o sistema antigo (o banco antigo e mantido)
set -euo pipefail

ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTIGO_DIR="${ANTIGO_DIR:-/opt/pmsl-wifi-aprovacao}"
DESTINO="${DESTINO:-/opt/gestao-ti}"
URL_GESTAO="${URL_GESTAO:-https://gestaoti.santaluzia.mg.gov.br}"
PORTA_TEMP="${PORTA_TEMP:-18090}"
DB_ANTIGO="${DB_ANTIGO:-pmsl-wifi-db}"
API_ANTIGA="${API_ANTIGA:-pmsl-wifi-api}"
DB_NOVO="${DB_NOVO:-gestao-ti-db}"
# Comandos do psql (podem ser trocados para testar fora do Docker)
read -r -a PSQL_ANTIGO <<< "${PSQL_ANTIGO:-docker exec -i $DB_ANTIGO psql -U wifi -d wifi_aprovacao -X -q -v ON_ERROR_STOP=1}"
read -r -a PSQL_NOVO <<< "${PSQL_NOVO:-docker exec -i $DB_NOVO psql -U gestao -d gestao_ti -X -q -v ON_ERROR_STOP=1}"

msg()   { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
aviso() { printf '\033[1;33mAVISO:\033[0m %s\n' "$*"; }
erro()  { printf '\033[1;31mERRO:\033[0m %s\n' "$*" >&2; exit 1; }
aleatorio() { (LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${1:-40}") || true; }
valor() { grep -E "^$1=" "$2" 2>/dev/null | head -1 | cut -d= -f2- || true; }
definir() {  # definir CHAVE VALOR arquivo
  if grep -qE "^$1=" "$3"; then sed -i "s|^$1=.*|$1=$2|" "$3"; else echo "$1=$2" >> "$3"; fi
}

# --------------------------------------------------------------------------- copia dos dados
colunas() {  # colunas <psql...> tabela -> lista separada por virgula
  local tabela="${@: -1}"
  "${@:1:$#-1}" -At -c "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = '$tabela'"
}
comuns() {  # comuns "a,b,c" "b,c,d" [excluir] -> "b,c" (na ordem da primeira)
  local excluir=",${3:-},"
  local saida=""
  IFS=',' read -r -a A <<< "$1"
  for c in "${A[@]}"; do
    [[ ",$2," == *",$c,"* ]] || continue
    [[ "$excluir" == *",$c,"* ]] && continue
    saida="${saida:+$saida,}$c"
  done
  echo "$saida"
}

copiar_dados() {
  msg "Conferindo se a copia ja foi feita"
  JA="$("${PSQL_NOVO[@]}" -At -c "SELECT COUNT(*) FROM configuracoes WHERE chave = 'migracao:wifi_aprovacao'")"
  if [ "$JA" != "0" ] && [ "${FORCAR:-0}" != "1" ]; then
    aviso "Os dados do sistema antigo ja foram copiados antes. Nada foi feito (use FORCAR=1 para copiar de novo)."
    return 0
  fi

  SQL="$(mktemp)"
  echo "BEGIN;" > "$SQL"
  declare -A CMN
  for t in administradores solicitacoes eventos computadores; do
    velho="$(colunas "${PSQL_ANTIGO[@]}" "$t")"
    novo="$(colunas "${PSQL_NOVO[@]}" "$t")"
    [ -n "$velho" ] || erro "Tabela '$t' nao encontrada no banco antigo."
    [ -n "$novo" ] || erro "Tabela '$t' nao encontrada na Gestao TI (ela precisa ter subido ao menos uma vez)."
    case "$t" in
      administradores) c="$(comuns "$velho" "$novo" "id")" ;;
      *)               c="$(comuns "$velho" "$novo")" ;;
    esac
    CMN[$t]="$c"
    ordem="1"; [[ ",$c," == *",id,"* ]] && ordem="id"
    {
      echo "CREATE TEMP TABLE m_$t AS SELECT $c FROM $t WITH NO DATA;"
      echo "COPY m_$t ($c) FROM STDIN WITH (FORMAT csv, HEADER true);"
      "${PSQL_ANTIGO[@]}" -c "\\copy (SELECT $c FROM $t ORDER BY $ordem) TO STDOUT WITH (FORMAT csv, HEADER true)"
      echo '\.'
    } >> "$SQL"
    printf '    %-16s %s linhas\n' "$t" "$("${PSQL_ANTIGO[@]}" -At -c "SELECT COUNT(*) FROM $t")"
  done
  sem_id() { echo "$1" | tr ',' '\n' | grep -vx 'id' | paste -sd, -; }
  ADM="${CMN[administradores]}"
  EVE="$(sem_id "${CMN[eventos]}")"
  cat >> "$SQL" <<SQL
-- Administradores: quem ja existe na Gestao TI (ex.: admin) fica como esta. Os do sistema antigo eram da TI.
INSERT INTO administradores ($ADM, ti)
  SELECT $ADM, true FROM m_administradores ON CONFLICT (usuario) DO NOTHING;
-- Pedidos e liberacoes com o MESMO numero (as estacoes e o historico continuam batendo)
INSERT INTO solicitacoes (${CMN[solicitacoes]})
  SELECT ${CMN[solicitacoes]} FROM m_solicitacoes ON CONFLICT (id) DO NOTHING;
SELECT setval(pg_get_serial_sequence('solicitacoes', 'id'), GREATEST((SELECT COALESCE(MAX(id), 1) FROM solicitacoes), 1));
-- Historico (registro de atividades), na ordem original
INSERT INTO eventos ($EVE)
  SELECT $EVE FROM m_eventos
  WHERE solicitacao_id IS NULL OR solicitacao_id IN (SELECT id FROM solicitacoes) ORDER BY id;
INSERT INTO eventos (acao, autor, detalhe, criado_em)
  VALUES ('configuracao_alterada', 'migracao', 'Dados copiados do sistema antigo do Wi-Fi (wifiaprovacao)', now());
-- Computadores: o que a Gestao TI ja viu e mais novo e fica
INSERT INTO computadores (${CMN[computadores]})
  SELECT ${CMN[computadores]} FROM m_computadores ON CONFLICT (hostname) DO NOTHING;
INSERT INTO configuracoes (chave, valor, atualizado_por, atualizado_em)
  VALUES ('migracao:wifi_aprovacao', to_char(now(), 'YYYY-MM-DD HH24:MI'), 'migracao', now())
  ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor, atualizado_em = now();
COMMIT;
SQL
  msg "Gravando na Gestao TI"
  "${PSQL_NOVO[@]}" < "$SQL" >/dev/null
  rm -f "$SQL"
  msg "Conferindo"
  for t in administradores solicitacoes eventos computadores; do
    printf '    %-16s %s linhas na Gestao TI\n' "$t" "$("${PSQL_NOVO[@]}" -At -c "SELECT COUNT(*) FROM $t")"
  done
}

if [ "${SO_DADOS:-0}" = "1" ]; then
  copiar_dados
  exit 0
fi

# --------------------------------------------------------------------------- 0. conferencias
[ "$(id -u)" = 0 ] || erro "Rode com sudo."
command -v docker >/dev/null || erro "Docker nao encontrado."
command -v curl >/dev/null || erro "curl nao encontrado (apt install curl)."
[ -f "$ANTIGO_DIR/.env" ] || erro "Nao achei o sistema antigo em $ANTIGO_DIR (.env). Use ANTIGO_DIR=... se estiver em outra pasta."
[ -f "$ORIGEM/aplicar_gestao_ti.sh" ] || erro "Rode este script de dentro da pasta extraida do zip da Gestao TI."
docker inspect "$DB_ANTIGO" >/dev/null 2>&1 || erro "Container do banco antigo '$DB_ANTIGO' nao existe."
[ "$(docker inspect -f '{{.State.Running}}' "$DB_ANTIGO")" = "true" ] || { msg "Ligando o banco antigo"; docker start "$DB_ANTIGO" >/dev/null; sleep 5; }

AGENT_KEY_ANTIGA="$(valor AGENT_KEY "$ANTIGO_DIR/.env")"
PORTA_FINAL="$(valor PORTA "$ANTIGO_DIR/.env")"; PORTA_FINAL="${PORTA_FINAL:-8090}"
[ -n "$AGENT_KEY_ANTIGA" ] || erro "AGENT_KEY vazio em $ANTIGO_DIR/.env."

echo
echo "  Sistema antigo:  $ANTIGO_DIR (porta $PORTA_FINAL)"
echo "  Gestao TI:       $DESTINO  ->  $URL_GESTAO (porta $PORTA_FINAL)"
echo
read -r -p "Confirma a troca? A API antiga sera desligada (o banco antigo e mantido). [s/N] " ok
[[ "$ok" =~ ^[sS]$ ]] || erro "Cancelado."

# --------------------------------------------------------------------------- 1. backup
mkdir -p /opt/backups
BACKUP="/opt/backups/wifi_aprovacao-$(date +%Y%m%d-%H%M).sql.gz"
msg "Backup do banco antigo: $BACKUP"
docker exec "$DB_ANTIGO" pg_dump -U wifi -d wifi_aprovacao | gzip > "$BACKUP"
[ -s "$BACKUP" ] || erro "O backup ficou vazio. Nada foi alterado."

# --------------------------------------------------------------------------- 2. .env da Gestao TI
mkdir -p "$DESTINO"
ENV="$DESTINO/.env"
if [ ! -f "$ENV" ]; then
  if [ -f "$ORIGEM/.env" ]; then cp "$ORIGEM/.env" "$ENV"; else cp "$ORIGEM/.env.example" "$ENV"; fi
  chmod 600 "$ENV"
fi
cp "$ENV" "$ENV.antes-da-migracao"
msg "Preparando $ENV"
definir AGENT_KEY "$AGENT_KEY_ANTIGA" "$ENV"                    # mesma chave dos scripts das estacoes
for k in COOKIE_SEGURO FORWARDED_ALLOW_IPS; do                  # dizem respeito ao servidor/proxy: valem os do antigo
  v="$(valor "$k" "$ANTIGO_DIR/.env")"; [ -n "$v" ] && definir "$k" "$v" "$ENV"
done
if [ -z "$(valor DB_PASSWORD "$ENV")" ]; then
  if docker volume ls -q | grep -q '^gestao-ti_dados_db$'; then
    erro "DB_PASSWORD vazio em $ENV, mas o banco da Gestao TI ja existe. Coloque a senha que foi usada nele."
  fi
  definir DB_PASSWORD "$(aleatorio 32)" "$ENV"
fi
SENHA_ADMIN_NOVA=""
if [ -z "$(valor ADMIN_PASSWORD "$ENV")" ]; then SENHA_ADMIN_NOVA="$(aleatorio 16)"; definir ADMIN_PASSWORD "$SENHA_ADMIN_NOVA" "$ENV"; fi
if [ -d "$ANTIGO_DIR/marca" ] && [ -z "$(ls -A "$DESTINO/marca" 2>/dev/null)" ]; then
  mkdir -p "$DESTINO/marca"; cp -a "$ANTIGO_DIR/marca/." "$DESTINO/marca/" 2>/dev/null || true
fi
definir PORTA "$PORTA_TEMP" "$ENV"

# Se algo der errado depois de desligar a API antiga (qualquer erro ou saida antes do fim), ela volta sozinha
ANTIGA_PARADA=0
CONCLUIDO=0
voltar() {
  if [ "$ANTIGA_PARADA" = 1 ] && [ "$CONCLUIDO" != 1 ]; then
    aviso "Algo deu errado: religando o sistema antigo."
    (cd "$DESTINO" && docker compose stop api >/dev/null 2>&1) || true
    docker start "$API_ANTIGA" >/dev/null 2>&1 || true
  fi
}
trap voltar EXIT

# --------------------------------------------------------------------------- 3. instala na porta provisoria
msg "Instalando a Gestao TI na porta provisoria $PORTA_TEMP (o sistema antigo continua atendendo)"
PAINEL_URL="$URL_GESTAO" DESTINO="$DESTINO" MODO=servidor bash "$ORIGEM/aplicar_gestao_ti.sh"

# --------------------------------------------------------------------------- 4. para a API antiga e copia os dados
msg "Desligando a API antiga (as estacoes mantem o estado atual enquanto isso)"
docker stop "$API_ANTIGA" >/dev/null 2>&1 || true
ANTIGA_PARADA=1
copiar_dados

# --------------------------------------------------------------------------- 5. assume a porta definitiva
msg "Gestao TI assumindo a porta $PORTA_FINAL"
definir PORTA "$PORTA_FINAL" "$ENV"
(cd "$DESTINO" && docker compose up -d --force-recreate api >/dev/null)
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:${PORTA_FINAL}/health" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "http://127.0.0.1:${PORTA_FINAL}/health" >/dev/null || erro "A Gestao TI nao respondeu na porta $PORTA_FINAL."
CODIGO="$(curl -s -o /dev/null -w '%{http_code}' -H "X-Agent-Key: $AGENT_KEY_ANTIGA" "http://127.0.0.1:${PORTA_FINAL}/api/agente/liberacao?hostname=TESTE-MIGRACAO")"
[ "$CODIGO" = "200" ] || erro "O endpoint das estacoes respondeu $CODIGO (esperado 200)."
CONCLUIDO=1

# --------------------------------------------------------------------------- 6. desliga o sistema antigo
msg "Desligando o sistema antigo (o banco antigo fica guardado no volume do Docker)"
(cd "$ANTIGO_DIR" && docker compose stop >/dev/null 2>&1) || docker stop "$DB_ANTIGO" >/dev/null 2>&1 || true

echo
echo "Pronto. A Gestao TI esta no ar na porta $PORTA_FINAL deste servidor."
echo "  Endereco:          $URL_GESTAO   (pedir a equipe de redes: DNS + proxy apontando para $(hostname -I | awk '{print $1}'):$PORTA_FINAL)"
echo "  Endereco antigo:   continua funcionando e abre o portal do Wi-Fi"
echo "  Backup do antigo:  $BACKUP"
[ -n "$SENHA_ADMIN_NOVA" ] && echo "  Usuario local:     admin / $SENHA_ADMIN_NOVA   (anote e troque depois)"
echo
echo "Para voltar ao sistema antigo, se precisar:"
echo "  cd $DESTINO && sudo docker compose stop"
echo "  cd $ANTIGO_DIR && sudo docker compose up -d"
