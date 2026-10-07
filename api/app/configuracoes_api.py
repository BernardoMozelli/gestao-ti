"""Modulo Configuracoes: servidor de e-mail (SMTP), limites dos disparos e banco do Wi-Fi Visitante (RADIUS)."""
from email.utils import formataddr

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import ajustes, auditoria, auth, config, radius
from .db import Administrador, Evento, SessionLocal
from .email_lote import EMAIL_RE, abrir_smtp, montar_mensagem

router = APIRouter(prefix="/api/configuracoes")
ConfigDep = auth.exigir_modulo("configuracoes")

ROTULOS = {"SMTP_HOST": "servidor", "SMTP_PORT": "porta", "SMTP_SEGURANCA": "segurança", "SMTP_USER": "usuário",
           "SMTP_PASSWORD": "senha", "SMTP_FROM": "e-mail do remetente", "SMTP_FROM_NOME": "nome do remetente",
           "ENVIO_PAUSA_MS": "pausa entre e-mails", "ANEXOS_MAX_MB": "limite de anexos",
           "IMAGEM_MAX_MB": "limite por imagem", "LISTA_MAX_EMAILS": "máximo de e-mails por disparo",
           "EMAIL_TESTE_ATIVO": "modo de teste do e-mail", "EMAIL_TESTE": "e-mail de teste"}


class ConfigEmail(BaseModel):
    SMTP_HOST: str | None = Field(default=None, max_length=200)
    SMTP_PORT: int | None = Field(default=None, ge=1, le=65535)
    SMTP_SEGURANCA: str | None = None
    SMTP_USER: str | None = Field(default=None, max_length=200)
    SMTP_PASSWORD: str | None = Field(default=None, max_length=500)   # None ou vazio = mantem a senha atual
    apagar_senha: bool = False
    SMTP_FROM_NOME: str | None = Field(default=None, max_length=150)
    ENVIO_PAUSA_MS: int | None = Field(default=None, ge=0, le=60000)
    ANEXOS_MAX_MB: int | None = Field(default=None, ge=1, le=50)
    IMAGEM_MAX_MB: int | None = Field(default=None, ge=1, le=20)
    LISTA_MAX_EMAILS: int | None = Field(default=None, ge=1, le=500000)
    EMAIL_TESTE_ATIVO: str | None = None            # sim | nao
    EMAIL_TESTE: str | None = Field(default=None, max_length=200)


def _validar(d: ConfigEmail) -> None:
    if d.EMAIL_TESTE_ATIVO is not None and d.EMAIL_TESTE_ATIVO not in ("sim", "nao"):
        raise HTTPException(422, "Modo de teste: use sim ou não.")
    if d.EMAIL_TESTE and not EMAIL_RE.fullmatch(d.EMAIL_TESTE.strip()):
        raise HTTPException(422, "O e-mail de teste é inválido.")
    if d.EMAIL_TESTE_ATIVO == "sim" and not (d.EMAIL_TESTE or ajustes.obter("EMAIL_TESTE")):
        raise HTTPException(422, "Informe o e-mail de teste para ligar o modo de teste.")
    if d.SMTP_SEGURANCA is not None and d.SMTP_SEGURANCA not in ("nenhuma", "starttls", "ssl"):
        raise HTTPException(422, "Segurança inválida. Use nenhuma, STARTTLS ou SSL.")


def _atual() -> dict:
    dados = {k: ajustes.obter(k) for k in ajustes.CAMPOS if k != "SMTP_PASSWORD"}
    dados["tem_senha"] = bool(ajustes.obter("SMTP_PASSWORD"))
    dados["CONTA_OFICIAL"] = config.CONTA_ENVIO
    dados["conta_efetiva"] = ajustes.obter("SMTP_FROM")
    dados["origem"] = {k: ajustes.origem(k) for k in ajustes.CAMPOS}
    return dados


def _mesclar(d: ConfigEmail) -> dict:
    """Configuracao para testar: o que veio da tela por cima do que esta salvo (sem gravar)."""
    c = {k: ajustes.obter(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_SEGURANCA", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
    for k in c:
        if k == "SMTP_FROM":
            continue   # conta de envio fixa (geral@)
        v = getattr(d, k)
        if v is not None and not (k == "SMTP_PASSWORD" and v == ""):
            c[k] = v.strip() if isinstance(v, str) else v
    if d.apagar_senha:
        c["SMTP_PASSWORD"] = ""
    # Modo de teste como esta na tela (ainda sem salvar)
    if d.EMAIL_TESTE_ATIVO == "sim" and (d.EMAIL_TESTE or "").strip():
        c["SMTP_FROM"] = d.EMAIL_TESTE.strip().lower()
    elif d.EMAIL_TESTE_ATIVO == "nao":
        c["SMTP_FROM"] = config.CONTA_ENVIO
    return c


@router.get("/email")
def ver_email(admin: Administrador = ConfigDep) -> dict:
    return _atual()


@router.put("/email")
def salvar_email(dados: ConfigEmail, request: Request, admin: Administrador = ConfigDep) -> dict:
    _validar(dados)
    valores = dados.model_dump(exclude={"apagar_senha"})
    if not valores.get("SMTP_PASSWORD"):
        valores["SMTP_PASSWORD"] = None          # nao mexe na senha guardada
    if dados.apagar_senha:
        valores["SMTP_PASSWORD"] = ""
    mudou = ajustes.salvar(valores, admin.usuario)
    if "EMAIL_TESTE_ATIVO" in mudou or ("EMAIL_TESTE" in mudou and ajustes.obter("EMAIL_TESTE_ATIVO") == "sim"):
        ligado = ajustes.obter("EMAIL_TESTE_ATIVO") == "sim"
        auditoria.registrar("configuracao_alterada", admin.usuario,
                            f"Modo de teste do e-mail LIGADO: os envios saem de {ajustes.obter('SMTP_FROM')}" if ligado
                            else f"Modo de teste do e-mail DESLIGADO: os envios voltam a sair de {config.CONTA_ENVIO}")
        mudou = [m for m in mudou if m not in ("EMAIL_TESTE_ATIVO", "EMAIL_TESTE")]
    if mudou:
        with SessionLocal() as s:
            s.add(Evento(acao="configuracao_alterada", autor=admin.usuario, ip=auth.ip_do_cliente(request),
                         detalhe="E-mail: " + ", ".join(ROTULOS.get(m, m) for m in mudou)))
            s.commit()
    return _atual()


@router.post("/email/testar")
def testar_conexao(dados: ConfigEmail, admin: Administrador = ConfigDep) -> dict:
    """Conecta e faz login no servidor de e-mail com os dados da tela, sem salvar e sem enviar nada."""
    _validar(dados)
    cfg = _mesclar(dados)
    try:
        with abrir_smtp(cfg, timeout=15) as smtp:
            smtp.noop()
    except Exception as erro:  # noqa: BLE001 - devolve o motivo para a tela
        auditoria.registrar("configuracao_testada", admin.usuario, f"SMTP {cfg.get('SMTP_HOST')}:{cfg.get('SMTP_PORT')} · falhou: {erro}")
        raise HTTPException(502, f"Não conectou: {erro}")
    auditoria.registrar("configuracao_testada", admin.usuario, f"SMTP {cfg.get('SMTP_HOST')}:{cfg.get('SMTP_PORT')} · conectou")
    return {"ok": True}


class TesteEnvio(ConfigEmail):
    para: str = Field(max_length=254)


@router.post("/email/enviar-teste")
def enviar_teste(dados: TesteEnvio, request: Request, admin: Administrador = ConfigDep) -> dict:
    _validar(dados)
    para = dados.para.strip().lower()
    if not EMAIL_RE.fullmatch(para):
        raise HTTPException(422, "E-mail de destino inválido.")
    cfg = _mesclar(dados)
    msg = montar_mensagem("Teste do servidor de e-mail da Gestão TI",
                          "<p>Olá, {nome}!</p><p>Este é um e-mail de teste enviado pelo módulo <b>Configurações</b> "
                          "da Gestão TI. Se você recebeu, o servidor de e-mail está funcionando.</p>",
                          dados.SMTP_FROM_NOME, None, para, admin.nome, [])
    del msg["From"]
    msg["From"] = formataddr((dados.SMTP_FROM_NOME or ajustes.obter("SMTP_FROM_NOME"), cfg["SMTP_FROM"]))
    try:
        with abrir_smtp(cfg) as smtp:
            smtp.send_message(msg)
    except Exception as erro:  # noqa: BLE001
        raise HTTPException(502, f"O servidor recusou o envio: {erro}")
    with SessionLocal() as s:
        s.add(Evento(acao="email_teste", autor=admin.usuario, ip=auth.ip_do_cliente(request),
                     detalhe=f"Teste do servidor de e-mail para {para}"))
        s.commit()
    return {"ok": True}


# =========================================================================== Wi-Fi Visitante (banco do RADIUS)
CAMPOS_RADIUS = [k for k in ajustes.CAMPOS if k.startswith("RADIUS_")]
ROTULOS_RADIUS = {"RADIUS_DB_HOST": "servidor do banco", "RADIUS_DB_PORTA": "porta do banco", "RADIUS_DB_NOME": "nome do banco",
                  "RADIUS_DB_USUARIO": "usuário do banco", "RADIUS_DB_SENHA": "senha do banco", "RADIUS_SSH": "conexão por SSH",
                  "RADIUS_SSH_HOST": "servidor SSH", "RADIUS_SSH_PORTA": "porta SSH", "RADIUS_SSH_USUARIO": "usuário SSH",
                  "RADIUS_SSH_SENHA": "senha SSH", "RADIUS_TABELA_USUARIOS": "tabela de cadastros",
                  "RADIUS_TABELA_SENHAS": "tabela de senhas"}


class ConfigRadius(BaseModel):
    RADIUS_DB_HOST: str | None = Field(default=None, max_length=200)
    RADIUS_DB_PORTA: int | None = Field(default=None, ge=1, le=65535)
    RADIUS_DB_NOME: str | None = Field(default=None, max_length=64)
    RADIUS_DB_USUARIO: str | None = Field(default=None, max_length=100)
    RADIUS_DB_SENHA: str | None = Field(default=None, max_length=300)       # vazio = mantem a guardada
    apagar_senha_db: bool = False
    RADIUS_SSH: str | None = None                                           # sim | nao
    RADIUS_SSH_HOST: str | None = Field(default=None, max_length=200)
    RADIUS_SSH_PORTA: int | None = Field(default=None, ge=1, le=65535)
    RADIUS_SSH_USUARIO: str | None = Field(default=None, max_length=100)
    RADIUS_SSH_SENHA: str | None = Field(default=None, max_length=300)      # vazio = mantem a guardada
    apagar_senha_ssh: bool = False
    RADIUS_TABELA_USUARIOS: str | None = Field(default=None, max_length=64)
    RADIUS_TABELA_SENHAS: str | None = Field(default=None, max_length=64)


def _radius_atual() -> dict:
    d = {k: ajustes.obter(k) for k in CAMPOS_RADIUS if k not in ajustes.SEGREDOS}
    d["tem_senha_db"] = bool(ajustes.obter("RADIUS_DB_SENHA"))
    d["tem_senha_ssh"] = bool(ajustes.obter("RADIUS_SSH_SENHA"))
    d["origem"] = {k: ajustes.origem(k) for k in CAMPOS_RADIUS}
    return d


def _validar_radius(d: ConfigRadius) -> None:
    if d.RADIUS_SSH is not None and d.RADIUS_SSH not in ("sim", "nao"):
        raise HTTPException(422, "Conexão por SSH: use sim ou não.")
    for k in ("RADIUS_TABELA_USUARIOS", "RADIUS_TABELA_SENHAS", "RADIUS_DB_NOME"):
        v = getattr(d, k)
        if v and not radius._NOME_TABELA.match(v.strip()):
            raise HTTPException(422, f"{ROTULOS_RADIUS[k].capitalize()} inválido: use só letras, números e _.")


def _radius_mesclado(d: ConfigRadius) -> dict:
    """Configuracao para testar: o que veio da tela por cima do que esta salvo (sem gravar)."""
    c = {k: ajustes.obter(k) for k in CAMPOS_RADIUS}
    for k in CAMPOS_RADIUS:
        v = getattr(d, k, None)
        if v is None or (k in ajustes.SEGREDOS and v == ""):
            continue
        c[k] = v.strip() if isinstance(v, str) else v
    if d.apagar_senha_db:
        c["RADIUS_DB_SENHA"] = ""
    if d.apagar_senha_ssh:
        c["RADIUS_SSH_SENHA"] = ""
    c["ssh"] = str(c["RADIUS_SSH"]).lower() in ("sim", "true", "1")
    return c


@router.get("/radius")
def ver_radius(admin: Administrador = ConfigDep) -> dict:
    return _radius_atual()


@router.put("/radius")
def salvar_radius(dados: ConfigRadius, request: Request, admin: Administrador = ConfigDep) -> dict:
    _validar_radius(dados)
    valores = dados.model_dump(exclude={"apagar_senha_db", "apagar_senha_ssh"})
    for k, apagar in (("RADIUS_DB_SENHA", dados.apagar_senha_db), ("RADIUS_SSH_SENHA", dados.apagar_senha_ssh)):
        if not valores.get(k):
            valores[k] = None              # nao mexe na senha guardada
        if apagar:
            valores[k] = ""
    mudou = ajustes.salvar(valores, admin.usuario)
    if mudou:
        radius.fechar_tunel()              # proxima consulta usa os dados novos
        auditoria.registrar("configuracao_alterada", admin.usuario,
                            "Wi-Fi Visitante: " + ", ".join(ROTULOS_RADIUS.get(m, m) for m in mudou))
    return _radius_atual()


@router.post("/radius/testar")
def testar_radius(dados: ConfigRadius, admin: Administrador = ConfigDep) -> dict:
    _validar_radius(dados)
    c = _radius_mesclado(dados)
    try:
        r = radius.testar(c)
    except Exception as erro:  # noqa: BLE001 - ErroRadius ou inesperado: sempre mostra a causa na tela
        msg = str(erro) if isinstance(erro, radius.ErroRadius) else f"Erro inesperado ({type(erro).__name__}): {erro}"
        if not isinstance(erro, radius.ErroRadius):
            radius.log.exception("Erro inesperado ao testar o banco do Wi-Fi Visitante")
        auditoria.registrar("configuracao_testada", admin.usuario, f"Banco do Wi-Fi Visitante · falhou: {msg}")
        raise HTTPException(502, msg)
    finally:
        radius.fechar_tunel()
    auditoria.registrar("configuracao_testada", admin.usuario,
                        f"Banco do Wi-Fi Visitante · conectou · {r['cadastros']} cadastros")
    return r


class Conferir(ConfigRadius):
    login: str = Field(min_length=1, max_length=100)
    senha: str = Field(min_length=1, max_length=100)


@router.post("/radius/conferir")
def conferir_radius(dados: Conferir, admin: Administrador = ConfigDep) -> dict:
    """Confere se a senha conhecida de um visitante bate com o gravado (confirma o formato da senha)."""
    _validar_radius(dados)
    try:
        r = radius.conferir(dados.login, dados.senha, _radius_mesclado(dados))
    except Exception as erro:  # noqa: BLE001
        if not isinstance(erro, radius.ErroRadius):
            radius.log.exception("Erro inesperado ao conferir a senha")
        raise HTTPException(502, str(erro) if isinstance(erro, radius.ErroRadius) else f"Erro inesperado ({type(erro).__name__}): {erro}")
    finally:
        radius.fechar_tunel()
    auditoria.registrar("configuracao_testada", admin.usuario,
                        f"Banco do Wi-Fi Visitante · conferiu o formato da senha de um login · "
                        + ("bate" if any(x["bate"] for x in r.get("resultados", [])) else "não bate"))
    return r
