"""Conexao com o PostgreSQL e modelos das tabelas da Gestao TI."""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, func)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

from . import config

engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def agora() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Administrador(Base):
    """Usuarios da plataforma Gestao TI (do AD ou locais). O que cada um ve depende das permissoes."""
    __tablename__ = "administradores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    nome: Mapped[str] = mapped_column(String(150))
    senha_hash: Mapped[str] = mapped_column(String(200))
    origem: Mapped[str] = mapped_column(String(10), default="local", server_default="local")  # local | ad
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    unidade: Mapped[str | None] = mapped_column(String(150), nullable=True)   # OU do AD
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    # True quando a conta esta na OU/grupo da TI no AD (ou e um usuario local). So a TI recebe os modulos restritos.
    ti: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # Autorizado pelo adm geral a conceder permissoes (modulos) a outros usuarios
    pode_conceder: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    permissoes: Mapped[list["Permissao"]] = relationship(back_populates="usuario", cascade="all, delete-orphan")


class Permissao(Base):
    """Modulo que um usuario pode acessar (ex.: wifi, email_ad, email_lista, usuarios)."""
    __tablename__ = "permissoes"
    __table_args__ = (UniqueConstraint("usuario_id", "modulo", name="uq_permissao_usuario_modulo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario_id: Mapped[int] = mapped_column(ForeignKey("administradores.id", ondelete="CASCADE"), index=True)
    modulo: Mapped[str] = mapped_column(String(40))
    concedido_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    concedido_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)

    usuario: Mapped[Administrador] = relationship(back_populates="permissoes")


class Solicitacao(Base):
    """Um pedido de liberacao (ou uma liberacao feita direto pela TI)."""
    __tablename__ = "solicitacoes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), index=True)
    usuario: Mapped[str] = mapped_column(String(150))                      # nome exibido
    usuario_login: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)  # login do AD
    motivo: Mapped[str] = mapped_column(Text)
    horas_solicitadas: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = indeterminado
    # pendente | aprovada | negada | expirada | revogada (bloqueada pela TI) | cancelada
    status: Mapped[str] = mapped_column(String(20), index=True, default="pendente")
    origem: Mapped[str] = mapped_column(String(20), default="usuario")      # usuario | ti
    hostname_fonte: Mapped[str | None] = mapped_column(String(20), nullable=True)  # detectado | atalho | manual | ti
    ip_origem: Mapped[str | None] = mapped_column(String(64), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    decidido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decidido_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    horas_aprovadas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indeterminado: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    valido_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    observacao_ti: Mapped[str | None] = mapped_column(Text, nullable=True)
    bloqueado_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    bloqueado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ultima_consulta_agente: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bloqueio_aplicado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usuario_no_computador: Mapped[str | None] = mapped_column(String(80), nullable=True)  # logado no PC ao pedir
    # Logado no PC quando a TI liberou: login; "" = ninguem logado; NULL = o computador nao tinha informado (desligado/sem script)
    usuario_na_liberacao: Mapped[str | None] = mapped_column(String(80), nullable=True)
    usuario_na_liberacao_nome: Mapped[str | None] = mapped_column(String(150), nullable=True)

    eventos: Mapped[list["Evento"]] = relationship(back_populates="solicitacao", order_by="Evento.id")


class Evento(Base):
    """Trilha de auditoria: tudo o que acontece no sistema."""
    __tablename__ = "eventos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    solicitacao_id: Mapped[int | None] = mapped_column(ForeignKey("solicitacoes.id"), nullable=True, index=True)
    acao: Mapped[str] = mapped_column(String(40), index=True)
    autor: Mapped[str] = mapped_column(String(150), index=True)
    hostname: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detalhe: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora, index=True)

    solicitacao: Mapped[Solicitacao | None] = relationship(back_populates="eventos")


class SessaoEncerrada(Base):
    """Sessao finalizada pela TI: todo login (painel ou portal do Wi-Fi) feito antes de encerrada_em deixa de valer."""
    __tablename__ = "sessoes_encerradas"

    login: Mapped[str] = mapped_column(String(80), primary_key=True)
    encerrada_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    encerrada_por: Mapped[str | None] = mapped_column(String(150), nullable=True)


class ComandoEstacao(Base):
    """Pedido para o script da GPO executar no computador (ex.: logoff de um usuario). Entregue na consulta seguinte."""
    __tablename__ = "comandos_estacao"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), index=True)
    acao: Mapped[str] = mapped_column(String(20))                    # logoff
    usuario: Mapped[str] = mapped_column(String(80))                 # login de quem deve sair
    avisar_segundos: Mapped[int] = mapped_column(Integer, default=0)  # mensagem na tela antes do logoff
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    criado_por: Mapped[str] = mapped_column(String(150))
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    entregue_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resultado: Mapped[str | None] = mapped_column(Text, nullable=True)   # ok | nao_logado | erro: ...


class Computador(Base):
    """Ultimo IP visto de cada computador (informado pelo script da GPO a cada 5 minutos)."""
    __tablename__ = "computadores"

    hostname: Mapped[str] = mapped_column(String(64), primary_key=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    visto_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora, index=True)
    # Usuario logado na sessao fisica do computador (informado pelo script da GPO)
    usuario_logado: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    usuario_desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Disparo(Base):
    """Um envio de e-mails em lote (para usuarios do AD ou para uma lista TXT)."""
    __tablename__ = "disparos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    titulo: Mapped[str] = mapped_column(String(200))
    origem: Mapped[str] = mapped_column(String(10), index=True)          # ad | lista
    lista_nome: Mapped[str | None] = mapped_column(String(200), nullable=True)  # nome do arquivo TXT
    assunto: Mapped[str] = mapped_column(String(300))
    corpo_html: Mapped[str] = mapped_column(Text)
    remetente_nome: Mapped[str | None] = mapped_column(String(150), nullable=True)
    remetente_email: Mapped[str | None] = mapped_column(String(254), nullable=True)   # vazio = SMTP_FROM
    responder_para: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tamanho_bloco: Mapped[int] = mapped_column(Integer)
    modo: Mapped[str] = mapped_column(String(12), default="automatico")  # automatico | manual
    intervalo_minutos: Mapped[int] = mapped_column(Integer, default=0)
    # rascunho | enviando | aguardando_bloco | pausado | concluido | cancelado
    status: Mapped[str] = mapped_column(String(20), index=True, default="rascunho")
    total: Mapped[int] = mapped_column(Integer, default=0)
    enviados: Mapped[int] = mapped_column(Integer, default=0)
    falhas: Mapped[int] = mapped_column(Integer, default=0)
    total_blocos: Mapped[int] = mapped_column(Integer, default=1)
    proximo_bloco_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    criado_por: Mapped[str] = mapped_column(String(150), index=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    iniciado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ultimo_erro: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Envio pela conta de e-mail de quem criou (login e senha do AD). False = conta do sistema (disparos antigos / modo de teste)
    conta_propria: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    login_envio: Mapped[str | None] = mapped_column(String(254), nullable=True)      # usuario de login no SMTP
    # Senha do e-mail (criptografada), guardada so enquanto o disparo esta em andamento; apagada ao concluir/cancelar
    senha_cifrada: Mapped[str | None] = mapped_column(Text, nullable=True)
    credito_usado: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    credito_devolvido: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    anexos: Mapped[list["DisparoAnexo"]] = relationship(back_populates="disparo", cascade="all, delete-orphan",
                                                         order_by="DisparoAnexo.id")


class DisparoDestinatario(Base):
    __tablename__ = "disparo_destinatarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disparo_id: Mapped[int] = mapped_column(ForeignKey("disparos.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    nome: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bloco: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(12), default="pendente", index=True)  # pendente|enviado|erro|cancelado
    enviado_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    erro: Mapped[str | None] = mapped_column(Text, nullable=True)


class DisparoAnexo(Base):
    __tablename__ = "disparo_anexos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disparo_id: Mapped[int] = mapped_column(ForeignKey("disparos.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(255))
    tipo: Mapped[str] = mapped_column(String(120))
    tamanho: Mapped[int] = mapped_column(Integer)
    caminho: Mapped[str] = mapped_column(String(500))

    disparo: Mapped[Disparo] = relationship(back_populates="anexos")


class CreditoEmail(Base):
    """Saldo de creditos de disparo de e-mail de cada usuario (1 credito = 1 disparo)."""
    __tablename__ = "creditos_email"

    usuario: Mapped[str] = mapped_column(String(80), primary_key=True)    # login (minusculo)
    saldo: Mapped[int] = mapped_column(Integer, default=0)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class MovimentoCredito(Base):
    """Extrato dos creditos: concessoes, pedidos aprovados, consumo pelos disparos e devolucoes."""
    __tablename__ = "creditos_movimentos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario: Mapped[str] = mapped_column(String(80), index=True)
    quantidade: Mapped[int] = mapped_column(Integer)                       # + entrada, - saida
    saldo_depois: Mapped[int] = mapped_column(Integer)
    tipo: Mapped[str] = mapped_column(String(20))       # inicial | concessao | remocao | pedido | consumo | devolucao
    disparo_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pedido_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    por: Mapped[str] = mapped_column(String(150))
    em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora, index=True)


class PedidoCredito(Base):
    """Pedido de mais creditos feito pelo usuario, aprovado ou recusado por quem gerencia os creditos."""
    __tablename__ = "creditos_pedidos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    usuario: Mapped[str] = mapped_column(String(80), index=True)
    nome: Mapped[str | None] = mapped_column(String(150), nullable=True)
    quantidade: Mapped[int] = mapped_column(Integer)
    motivo: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pendente", index=True)  # pendente|aprovado|recusado|cancelado
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)
    decidido_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    decidido_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quantidade_aprovada: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resposta: Mapped[str | None] = mapped_column(Text, nullable=True)


class Configuracao(Base):
    """Configuracoes feitas pela tela (modulo Configuracoes). Quando existem, valem no lugar do .env."""
    __tablename__ = "configuracoes"

    chave: Mapped[str] = mapped_column(String(60), primary_key=True)
    valor: Mapped[str | None] = mapped_column(Text, nullable=True)
    atualizado_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class RedeVisitante(Base):
    """Rede Wi-Fi de visitantes (modulo Wi-Fi Visitante). A senha fica criptografada."""
    __tablename__ = "redes_visitante"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nome: Mapped[str] = mapped_column(String(150))                     # ex.: Recepção da Prefeitura
    local: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ssid: Mapped[str] = mapped_column(String(64))
    seguranca: Mapped[str] = mapped_column(String(10), default="WPA")  # WPA (WPA2/WPA3) | nopass
    senha_cifrada: Mapped[str | None] = mapped_column(Text, nullable=True)
    equipamento: Mapped[str | None] = mapped_column(String(200), nullable=True)   # onde a senha e aplicada
    observacao: Mapped[str | None] = mapped_column(Text, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    senha_trocada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    senha_trocada_por: Mapped[str | None] = mapped_column(String(150), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


_MIGRACOES = [
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS pode_conceder BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS origem VARCHAR(10) NOT NULL DEFAULT 'local'",
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS email VARCHAR(200)",
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS ultimo_login TIMESTAMPTZ",
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS unidade VARCHAR(150)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS usuario_login VARCHAR(80)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS hostname_fonte VARCHAR(20)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS indeterminado BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS bloqueado_por VARCHAR(150)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS bloqueado_em TIMESTAMPTZ",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS bloqueio_aplicado_em TIMESTAMPTZ",
    "ALTER TABLE solicitacoes ALTER COLUMN horas_solicitadas DROP NOT NULL",
    "ALTER TABLE eventos ADD COLUMN IF NOT EXISTS hostname VARCHAR(64)",
    "ALTER TABLE eventos ADD COLUMN IF NOT EXISTS ip VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_solicitacoes_usuario_login ON solicitacoes (usuario_login)",
    "CREATE INDEX IF NOT EXISTS ix_eventos_hostname ON eventos (hostname)",
    "ALTER TABLE computadores ADD COLUMN IF NOT EXISTS usuario_logado VARCHAR(80)",
    "ALTER TABLE computadores ADD COLUMN IF NOT EXISTS usuario_desde TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS ix_computadores_usuario_logado ON computadores (usuario_logado)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS usuario_no_computador VARCHAR(80)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS usuario_na_liberacao VARCHAR(80)",
    "ALTER TABLE solicitacoes ADD COLUMN IF NOT EXISTS usuario_na_liberacao_nome VARCHAR(150)",
    "ALTER TABLE administradores ADD COLUMN IF NOT EXISTS ti BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS remetente_email VARCHAR(254)",
    "CREATE INDEX IF NOT EXISTS ix_disparo_dest_pendentes ON disparo_destinatarios (disparo_id, bloco, status)",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS conta_propria BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS login_envio VARCHAR(254)",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS senha_cifrada TEXT",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS credito_usado BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE disparos ADD COLUMN IF NOT EXISTS credito_devolvido BOOLEAN NOT NULL DEFAULT false",
]


def criar_tabelas() -> None:
    Base.metadata.create_all(engine)
    # Migracoes simples para bancos criados por versoes anteriores
    with engine.begin() as conn:
        for sql in _MIGRACOES:
            conn.exec_driver_sql(sql)
