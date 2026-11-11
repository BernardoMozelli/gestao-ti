"""Modulo Wi-Fi Visitante: visitantes cadastrados no portal (banco do RADIUS), troca de senha e ativacao.

A senha nova vai direto para o banco do RADIUS: o visitante entra com ela na hora. A senha nunca e gravada
na Gestao TI nem nos logs; aparece uma unica vez na tela (e, se pedido, vai para o e-mail do visitante).
"""
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import auditoria, auth, radius
from .db import Administrador, Evento, SessionLocal

log = logging.getLogger("visitante")
router = APIRouter(prefix="/api/visitante")
VisitanteDep = auth.exigir_modulo("wifi_visitante")

BUSCA_MINIMA = 3   # caracteres digitados antes de consultar o banco do portal
_PALAVRAS = ["sol", "rio", "mar", "lua", "flor", "casa", "verde", "azul", "norte", "pedra", "praia", "serra",
             "campo", "vento", "barco", "folha", "lago", "ninho", "nuvem", "ouro", "cedro", "ipe", "arara", "onda"]


def gerar_senha() -> str:
    """Facil de ditar e digitar no celular: palavra + 4 numeros (ex.: praia4821)."""
    return f"{secrets.choice(_PALAVRAS)}{secrets.randbelow(9000) + 1000}"


def _chamar(func, *args, **kw):
    try:
        return func(*args, **kw)
    except radius.ErroRadius as erro:
        raise HTTPException(502, str(erro))
    except Exception as erro:  # noqa: BLE001 - nunca devolve 500 sem explicar: mostra a causa e grava no log
        log.exception("Erro inesperado no Wi-Fi Visitante")
        raise HTTPException(502, f"Erro inesperado ({type(erro).__name__}): {erro}")


@router.get("/visitantes")
def listar(q: str = "", situacao: str = "ativos", limite: int = 100, admin: Administrador = VisitanteDep) -> dict:
    vazio = {"itens": [], "contagem": {"ativos": 0, "inativos": 0}, "total": 0}
    if not radius.configurado():
        return {"configurado": False, "buscou": False, **vazio}
    # So consulta o banco do portal a partir do que foi digitado (nada e carregado sem busca)
    if len((q or "").strip()) < BUSCA_MINIMA:
        return {"configurado": True, "buscou": False, **vazio}
    return {"configurado": True, "buscou": True, **_chamar(radius.listar, q, situacao, limite)}


@router.get("/totais")
def totais(admin: Administrador = VisitanteDep) -> dict:
    """Total de visitantes ativos e inativos (sem carregar a lista)."""
    if not radius.configurado():
        return {"configurado": False, "ativos": 0, "inativos": 0}
    return {"configurado": True, **_chamar(radius.totais)}


class TrocarSenha(BaseModel):
    gerar: bool = True
    senha: str | None = Field(default=None, max_length=64)
    reativar: bool = True
    enviar_email: bool = False
    motivo: str | None = Field(default=None, max_length=500)


@router.post("/visitantes/{id_}/trocar-senha")
def trocar_senha(id_: int, dados: TrocarSenha, request: Request, admin: Administrador = VisitanteDep,
                 sessao: str | None = auth.SessaoCookie) -> dict:
    senha = gerar_senha() if dados.gerar else (dados.senha or "").strip()
    if len(senha) < 6:
        raise HTTPException(422, "A senha precisa ter pelo menos 6 caracteres.")
    if not all(32 <= ord(ch) <= 126 for ch in senha):
        raise HTTPException(422, "Use só letras sem acento, números e símbolos comuns.")
    try:
        r = _chamar(radius.trocar_senha, id_, senha, dados.reativar)
    except HTTPException as erro:
        auditoria.registrar("visitante_senha_falhou", admin.usuario, f"cadastro nº {id_}: {erro.detail}")
        raise
    v = r["visitante"]
    enviado = None
    if dados.enviar_email:
        enviado = _enviar_por_email(v, senha, admin, auth.senha_da_sessao(sessao))
    auditoria.registrar("visitante_senha_trocada", admin.usuario,
                        f"{v['nome']} (login {radius.mascarar_cpf(v['username']) if len(v['username'] or '') == 11 else v['username']}) · "
                        f"senha {'gerada pelo sistema' if dados.gerar else 'digitada pela TI'} · formato {', '.join(r['formatos'])}"
                        + (" · cadastro reativado" if r["reativado"] else "")
                        + (f" · enviada por e-mail para {v['email']}" if enviado is True else "")
                        + (f" · motivo: {dados.motivo.strip()}" if dados.motivo and dados.motivo.strip() else ""))
    return {"senha": senha, "reativado": r["reativado"], "email_enviado": enviado,
            "email": v["email"] if enviado else None}


def _enviar_por_email(v: dict, senha: str, admin: Administrador, senha_email: str | None):
    """Manda a senha nova para o e-mail do cadastro, pela conta de e-mail de quem esta logado (ou pela conta de
    teste, com o modo de teste ligado). Devolve True ou a mensagem de erro."""
    import smtplib
    from email.message import EmailMessage
    from email.utils import formataddr

    from . import ajustes
    from .email_lote import EMAIL_RE, abrir_smtp, conta_para_envio, smtp_configurado
    if not (v.get("email") and EMAIL_RE.fullmatch(v["email"].strip())):
        return "o cadastro não tem um e-mail válido"
    if not smtp_configurado():
        return "o servidor de e-mail não está configurado"
    try:
        cfg, remetente = conta_para_envio(admin, senha_email)
    except HTTPException as erro:
        if erro.status_code == 428:
            return "a sua sessão não tem a senha do e-mail. Saia e entre de novo na Gestão TI com o seu usuário do AD"
        return str(erro.detail)
    msg = EmailMessage()
    msg["Subject"] = "Sua nova senha do Wi-Fi da Prefeitura de Santa Luzia"
    msg["From"] = formataddr((ajustes.obter("SMTP_FROM_NOME"), remetente))
    msg["To"] = v["email"].strip()
    login = v.get("username") or v.get("cpf")
    msg.set_content(f"Olá, {v['nome'].split()[0]}!\n\nSua senha do Wi-Fi para visitantes foi alterada.\n\n"
                    f"Usuário: {login}\nNova senha: {senha}\n\nSe você não pediu esta troca, procure a recepção.\n")
    try:
        with abrir_smtp(cfg, timeout=10) as smtp:
            smtp.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        return "o servidor de e-mail recusou a sua senha. Se trocou a senha da rede, saia e entre de novo na Gestão TI"
    except Exception as erro:  # noqa: BLE001
        return f"falha no envio: {erro}"


class Ativar(BaseModel):
    ativo: bool
    motivo: str | None = Field(default=None, max_length=500)


@router.post("/visitantes/{id_}/ativo")
def ativar(id_: int, dados: Ativar, request: Request, admin: Administrador = VisitanteDep) -> dict:
    v = _chamar(radius.definir_ativo, id_, dados.ativo)
    auditoria.registrar("visitante_ativado" if dados.ativo else "visitante_desativado", admin.usuario,
                        f"{v['nome']}" + (f" · motivo: {dados.motivo.strip()}" if dados.motivo and dados.motivo.strip() else ""))
    return {"ok": True}


@router.get("/historico")
def historico(admin: Administrador = VisitanteDep) -> list[dict]:
    acoes = ["visitante_senha_trocada", "visitante_senha_falhou", "visitante_ativado", "visitante_desativado"]
    with SessionLocal() as s:
        itens = s.scalars(select(Evento).where(Evento.acao.in_(acoes)).order_by(Evento.id.desc()).limit(300)).all()
        return [{"acao": e.acao, "autor": e.autor, "ip": e.ip, "detalhe": e.detalhe,
                 "criado_em": e.criado_em.isoformat() if e.criado_em else None} for e in itens]
