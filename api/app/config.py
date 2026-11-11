"""Configuracao lida das variaveis de ambiente (arquivo .env do docker compose)."""
import os
from urllib.parse import quote


def _env(nome: str, padrao: str = "") -> str:
    return os.environ.get(nome, padrao).strip()


def _url_banco() -> str:
    """Endereco do banco. A senha e codificada, entao pode ter @ : / # ? e outros simbolos."""
    if _env("DATABASE_URL"):
        return _env("DATABASE_URL")
    usuario = quote(_env("DB_USER", "gestao"), safe="")
    senha = quote(os.environ.get("DB_PASSWORD", "gestao"), safe="")
    return (f"postgresql+psycopg://{usuario}:{senha}@{_env('DB_HOST', 'db')}:{_env('DB_PORT', '5432')}"
            f"/{_env('DB_NAME', 'gestao_ti')}")


DATABASE_URL = _url_banco()

# Chave compartilhada usada pelo app do usuario e pelo script da GPO
AGENT_KEY = _env("AGENT_KEY")

# Assinatura dos cookies de sessao do painel
SECRET_KEY = _env("SECRET_KEY")
SESSAO_HORAS = int(_env("SESSAO_HORAS", "12"))   # tempo maximo de uma sessao, mesmo com uso continuo
# Sessao expira apos N minutos sem uso (painel e portal do Wi-Fi). 0 = desliga a expiracao por inatividade
SESSAO_INATIVIDADE_MINUTOS = int(_env("SESSAO_INATIVIDADE_MINUTOS", "30"))
COOKIE_SEGURO = _env("COOKIE_SEGURO", "false").lower() == "true"  # true quando atras de HTTPS

# Primeiro administrador (criado so se nao existir nenhum)
ADMIN_USER = _env("ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
# Adm geral: o usuario local ADMIN_USER e os logins do AD listados aqui (separados por virgula).
# So o adm geral (e quem ele autorizar) concede permissoes; ele sempre tem todos os modulos.
ADMIN_GERAL = [s.strip().lower() for s in _env("ADMIN_GERAL", "").split(",") if s.strip()]
ADMIN_NOME = _env("ADMIN_NOME", "Administrador TI")

# Regras das solicitacoes
# Opcoes de tempo que o usuario pode pedir no portal
HORAS_PERMITIDAS = [int(h) for h in _env("HORAS_PERMITIDAS", "1,2,4,8,24").split(",") if h.strip()]
# Opcoes de tempo oferecidas a TI ao liberar (a TI tambem pode digitar outro valor)
HORAS_ADMIN = [int(h) for h in _env("HORAS_ADMIN", "1,2,4,8,12,24,48,168").split(",") if h.strip()]
HORAS_MAXIMAS = int(_env("HORAS_MAXIMAS", "720"))
# Permite pedir/liberar por tempo indeterminado (ate a TI bloquear)
PERMITIR_INDETERMINADO = _env("PERMITIR_INDETERMINADO", "true").lower() == "true"
# Opcoes em dias (somadas as de horas): portal do usuario e Painel Administrativo
DIAS_PERMITIDOS = [int(d) for d in _env("DIAS_PERMITIDOS", "1,2,3,5,7").split(",") if d.strip()]
DIAS_ADMIN = [int(d) for d in _env("DIAS_ADMIN", "1,2,3,7,15,30").split(",") if d.strip()]
# Tempo personalizado no portal: o usuario digita a quantidade e escolhe horas ou dias
PERSONALIZADO_USUARIO = _env("PERSONALIZADO_USUARIO", "true").lower() == "true"
HORAS_MAXIMAS_USUARIO = int(_env("HORAS_MAXIMAS_USUARIO", "720"))
# Listas finais (horas + dias convertidos em horas)
OPCOES_USUARIO = sorted({h for h in HORAS_PERMITIDAS + [d * 24 for d in DIAS_PERMITIDOS] if h > 0})
OPCOES_ADMIN = sorted({h for h in HORAS_ADMIN + [d * 24 for d in DIAS_ADMIN] if 0 < h <= HORAS_MAXIMAS})
# Minutos em que o IP informado pelo script da GPO vale para identificar o computador no portal
MINUTOS_DETECCAO_COMPUTADOR = int(_env("MINUTOS_DETECCAO_COMPUTADOR", "30"))
# Se o script da GPO ainda nao informou o computador, tenta descobrir o nome pelo DNS reverso (PTR) do AD
DETECTAR_POR_DNS = _env("DETECTAR_POR_DNS", "true").lower() == "true"
DNS_SERVIDORES = [s.strip() for s in _env("DNS_SERVIDORES", _env("AD_SERVIDORES", "10.1.100.2,10.1.100.3")).split(",") if s.strip()]
MAX_SOLICITACOES_POR_DIA = int(_env("MAX_SOLICITACOES_POR_DIA", "5"))

# Link do painel usado nas notificacoes
PAINEL_URL = _env("PAINEL_URL", "http://localhost:8090")
# Enderecos do sistema antigo do Wi-Fi: quem abre por eles cai direto no portal do Wi-Fi (/wifi)
HOSTS_PORTAL_WIFI = [h.strip().lower() for h in _env("HOSTS_PORTAL_WIFI", "wifiaprovacao,wifi-aprovacao").split(",") if h.strip()]

# Notificacao por Telegram (opcional)
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

# Notificacao por WhatsApp via Evolution API (opcional)
EVOLUTION_URL = _env("EVOLUTION_URL").rstrip("/")
EVOLUTION_INSTANCE = _env("EVOLUTION_INSTANCE")
EVOLUTION_APIKEY = _env("EVOLUTION_APIKEY")
WHATSAPP_NUMEROS = [n.strip() for n in _env("WHATSAPP_NUMEROS").split(",") if n.strip()]

# Servidor de e-mail (SMTP): usado pelos disparos em lote e pelos avisos de novos pedidos de Wi-Fi
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_TLS = _env("SMTP_TLS", "true").lower() == "true"     # STARTTLS (porta 587)
SMTP_SSL = _env("SMTP_SSL", "false").lower() == "true"    # SSL direto (porta 465)
# Todos os e-mails da Gestao TI saem desta conta. Fixo no sistema: nao e configuravel.
CONTA_ENVIO = "geral@santaluzia.mg.gov.br"
SMTP_FROM = CONTA_ENVIO
SMTP_FROM_NOME = _env("SMTP_FROM_NOME", "Prefeitura Municipal de Santa Luzia")
# Avisos de novos pedidos de Wi-Fi por e-mail (opcional)

# Disparos em lote
DADOS_DIR = _env("DADOS_DIR", "/srv/dados")                       # anexos dos disparos (volume do Docker)
ENVIO_PAUSA_MS = int(_env("ENVIO_PAUSA_MS", "200"))              # pausa entre um e-mail e outro
ANEXOS_MAX_MB = int(_env("ANEXOS_MAX_MB", "10"))                 # soma dos anexos de um disparo
IMAGEM_MAX_MB = int(_env("IMAGEM_MAX_MB", "3"))                  # cada imagem colocada dentro da mensagem
LISTA_MAX_EMAILS = int(_env("LISTA_MAX_EMAILS", "50000"))        # limite de e-mails por disparo
CACHE_AD_MINUTOS = int(_env("CACHE_AD_MINUTOS", "10"))           # lista de e-mails do AD guardada em memoria
# Disparos saem da conta de e-mail de quem cria, com o login e a senha do AD. Formato do usuario no SMTP:
# email (joao@santaluzia.mg.gov.br) | usuario (joao) | upn (joao@santaluzia.int) | dominio (SANTALUZIA\joao)
SMTP_LOGIN_FORMATO = _env("SMTP_LOGIN_FORMATO", "email").lower()
CREDITOS_INICIAIS = int(_env("CREDITOS_INICIAIS", "0"))
# Modulos de e-mail liberados para todos (sim) ou so para quem recebe a permissao (nao)
EMAIL_PARA_TODOS = _env("EMAIL_PARA_TODOS", "sim").lower()
# Respostas: todo e-mail enviado leva "Responder para" este endereco (vazio = sem fixo) e um aviso no fim (vazio = sem aviso)
EMAIL_RESPONDER_PARA = _env("EMAIL_RESPONDER_PARA", "naoresponda@santaluzia.mg.gov.br")
EMAIL_RODAPE = _env("EMAIL_RODAPE", "Esta é uma mensagem automática da Prefeitura Municipal de Santa Luzia. "
                                    "Por favor, não responda a este e-mail.")          # creditos de quem usa os disparos pela primeira vez


# Identidade visual e nomes exibidos no painel e nas notificacoes
APP_NOME = _env("APP_NOME", "Gestão TI")
APP_SUBTITULO = _env("APP_SUBTITULO", "Central de Gestão de Tecnologia da Informação")
# Textos do portal publico do Wi-Fi (/wifi)
WIFI_NOME = _env("WIFI_NOME", "Acesso à Rede Sem Fio")
WIFI_SUBTITULO = _env("WIFI_SUBTITULO", "Sistema de Autorização de Acesso à Rede Sem Fio")
ORGAO_LINHA1 = _env("ORGAO_LINHA1", "Prefeitura Municipal de")
ORGAO_LINHA2 = _env("ORGAO_LINHA2", "Santa Luzia")
RODAPE = _env("RODAPE", "Desenvolvido pela Secretaria de Planejamento, Ciência, Tecnologia e Inovação")

# Login pelo Active Directory
AD_HABILITADO = _env("AD_HABILITADO", "true").lower() == "true"
AD_SERVIDORES = [s.strip() for s in _env("AD_SERVIDORES", "10.1.100.2,10.1.100.3").split(",") if s.strip()]
AD_DOMINIO = _env("AD_DOMINIO", "santaluzia.int")              # usado no login usuario@dominio
AD_BASE_DN = _env("AD_BASE_DN", "DC=santaluzia,DC=int")
# Quem pode entrar: usuarios dentro desta OU (inclui sub-OUs) ...
AD_OU_PERMITIDA = _env("AD_OU_PERMITIDA")
# ... e/ou membros deste grupo (DN completo; grupos aninhados contam)
AD_GRUPO_PERMITIDO = _env("AD_GRUPO_PERMITIDO")
# Seguranca da conexao: auto (tenta LDAPS 636, depois StartTLS 389) | ldaps | starttls | nenhuma
AD_SEGURANCA = _env("AD_SEGURANCA", "auto").lower()
AD_PERMITIR_SEM_TLS = _env("AD_PERMITIR_SEM_TLS", "false").lower() == "true"
AD_VALIDAR_CERTIFICADO = _env("AD_VALIDAR_CERTIFICADO", "false").lower() == "true"
AD_TIMEOUT = int(_env("AD_TIMEOUT", "5"))
# Conta de servico (opcional, so leitura) usada na busca de usuarios do AD pelo painel
AD_BIND_USUARIO = _env("AD_BIND_USUARIO")
AD_BIND_SENHA = _env("AD_BIND_SENHA")
# Conta para redefinir senhas/reativar contas na Gestao de Usuarios (precisa da permissao delegada "Redefinir senha" nas OUs dos usuarios).
# Se ficar vazia, usa a conta de servico acima.
AD_RESET_USUARIO = _env("AD_RESET_USUARIO")
AD_RESET_SENHA = _env("AD_RESET_SENHA")

# Wi-Fi Visitante: banco do RADIUS (portal de cadastro dos visitantes). Normalmente configurado pela tela Configuracoes.
RADIUS_DB_HOST = _env("RADIUS_DB_HOST", "10.1.0.4")
RADIUS_DB_PORTA = int(_env("RADIUS_DB_PORTA", "3306"))
RADIUS_DB_NOME = _env("RADIUS_DB_NOME", "radio")
RADIUS_DB_USUARIO = _env("RADIUS_DB_USUARIO", "")
RADIUS_DB_SENHA = _env("RADIUS_DB_SENHA", "")
RADIUS_SSH = _env("RADIUS_SSH", "nao")              # sim = conecta por um tunel SSH no servidor do banco
RADIUS_SSH_HOST = _env("RADIUS_SSH_HOST", "")       # vazio = o mesmo servidor do banco
RADIUS_SSH_PORTA = int(_env("RADIUS_SSH_PORTA", "22"))
RADIUS_SSH_USUARIO = _env("RADIUS_SSH_USUARIO", "")
RADIUS_SSH_SENHA = _env("RADIUS_SSH_SENHA", "")
RADIUS_TABELA_USUARIOS = _env("RADIUS_TABELA_USUARIOS", "reg_users")
RADIUS_TABELA_SENHAS = _env("RADIUS_TABELA_SENHAS", "radcheck")
# Conta desativada e sem uso ha mais que isso (dias) so e reativada com processo SEI (a TI confirma na tela)
DIAS_REATIVACAO_SEI = int(_env("DIAS_REATIVACAO_SEI", "90"))

# Login local (usuario admin de emergencia e usuarios criados no painel)
LOGIN_LOCAL_HABILITADO = _env("LOGIN_LOCAL_HABILITADO", "true").lower() == "true"


def validar() -> None:
    faltando = [n for n, v in (("AGENT_KEY", AGENT_KEY), ("SECRET_KEY", SECRET_KEY)) if not v]
    if faltando:
        raise RuntimeError(f"Variaveis obrigatorias nao definidas: {', '.join(faltando)}")
