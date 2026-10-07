"""Acoes em contas do AD (modulo Gestao de Usuarios): redefinir senha, desbloquear e reativar. Tudo fica no registro."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from . import ad, ad_senhas, auth
from .db import Administrador, Evento, SessionLocal

router = APIRouter(prefix="/api/senhas")
# O antigo modulo "Reset de senha" foi incorporado a Gestao de Usuarios
SenhasDep = ContaDep = auth.exigir_modulo("usuarios")


def _registrar(acao: str, admin: Administrador, request: Request, detalhe: str) -> None:
    with SessionLocal() as s:
        s.add(Evento(acao=acao, autor=admin.usuario, ip=auth.ip_do_cliente(request), detalhe=detalhe))
        s.commit()


def _chamar(func, *args):
    try:
        return func(*args)
    except ad.ErroAD as erro:
        raise HTTPException(409 if erro.credencial else 502, str(erro))


def _nao_e_voce(admin: Administrador, login: str) -> None:
    if ad.normalizar_login(login) == admin.usuario:
        raise HTTPException(409, "Para trocar a sua própria senha, use o Windows (Ctrl+Alt+Del → Alterar uma senha).")


@router.get("/buscar")
def buscar(q: str, admin: Administrador = SenhasDep) -> list[dict]:
    return _chamar(ad_senhas.buscar, q)


@router.get("/conta/{login}")
def conta(login: str, admin: Administrador = SenhasDep) -> dict:
    return _chamar(ad_senhas.consultar, login)


class Redefinir(BaseModel):
    login: str = Field(max_length=80)
    gerar: bool = True
    senha: str | None = Field(default=None, max_length=128)
    exigir_troca: bool = True
    desbloquear: bool = True
    motivo: str = Field(min_length=3, max_length=500)


@router.post("/redefinir")
def redefinir(dados: Redefinir, request: Request, admin: Administrador = ContaDep) -> dict:
    _nao_e_voce(admin, dados.login)
    if dados.gerar:
        senha = ad_senhas.gerar_senha()
    else:
        senha = dados.senha or ""
        if len(senha) < 8:
            raise HTTPException(422, "A senha precisa ter pelo menos 8 caracteres.")
    try:
        conta = ad_senhas.redefinir(dados.login, senha, dados.exigir_troca, dados.desbloquear)
    except ad.ErroAD as erro:
        _registrar("senha_reset_falhou", admin, request, f"{ad.normalizar_login(dados.login)}: {erro}")
        raise HTTPException(409 if erro.credencial else 502, str(erro))
    # A senha nunca vai para o registro: so quem fez, para quem, como e por que
    _registrar("senha_redefinida", admin, request,
               f"{conta['login']} ({conta['nome']}) · senha {'gerada pelo sistema' if dados.gerar else 'digitada pela TI'}"
               f"{' · troca obrigatória no próximo logon' if dados.exigir_troca else ''}"
               f"{' · conta desbloqueada' if conta.get('desbloqueada_agora') else ''} · motivo: {dados.motivo.strip()}")
    return {"conta": conta, "senha": senha if dados.gerar else None}


class Desbloquear(BaseModel):
    login: str = Field(max_length=80)
    motivo: str = Field(min_length=3, max_length=500)


@router.post("/desbloquear")
def desbloquear(dados: Desbloquear, request: Request, admin: Administrador = ContaDep) -> dict:
    conta = _chamar(ad_senhas.desbloquear_conta, dados.login)
    _registrar("conta_desbloqueada", admin, request, f"{conta['login']} ({conta['nome']}) · motivo: {dados.motivo.strip()}")
    return conta


class Reativar(BaseModel):
    login: str = Field(max_length=80)
    nova_senha: bool = False          # tambem definir uma senha (o AD exige senha valida para reativar)
    gerar: bool = True
    senha: str | None = Field(default=None, max_length=128)
    exigir_troca: bool = True
    ciente_sei: bool = False          # conta parada ha mais de DIAS_REATIVACAO_SEI: a TI confirma que existe processo SEI
    processo_sei: str | None = Field(default=None, max_length=60)
    motivo: str = Field(min_length=3, max_length=500)


@router.post("/reativar")
def reativar(dados: Reativar, request: Request, admin: Administrador = ContaDep) -> dict:
    _nao_e_voce(admin, dados.login)
    senha = None
    if dados.nova_senha:
        senha = ad_senhas.gerar_senha() if dados.gerar else (dados.senha or "")
        if len(senha) < 8:
            raise HTTPException(422, "A senha precisa ter pelo menos 8 caracteres.")
    try:
        conta = ad_senhas.reativar(dados.login, senha, dados.exigir_troca, dados.ciente_sei)
    except ad.ErroAD as erro:
        _registrar("senha_reset_falhou", admin, request, f"{ad.normalizar_login(dados.login)} (reativar): {erro}")
        raise HTTPException(409 if erro.credencial else 502, str(erro))
    _registrar("conta_reativada", admin, request,
               f"{conta['login']} ({conta['nome']})"
               + (f" · senha {'gerada pelo sistema' if dados.gerar else 'digitada pela TI'}" if senha else "")
               + (f" · sem uso há {conta.get('dias_sem_uso_antes') or '?'} dias · ciente de que a reativação exige SEI"
                  + (f" (SEI {dados.processo_sei.strip()})" if dados.processo_sei and dados.processo_sei.strip() else "")
                  if dados.ciente_sei else "")
               + f" · motivo: {dados.motivo.strip()}")
    return {"conta": conta, "senha": senha if senha and dados.gerar else None}


@router.get("/historico")
def historico(admin: Administrador = SenhasDep) -> list[dict]:
    with SessionLocal() as s:
        itens = s.scalars(select(Evento).where(Evento.acao.in_(["senha_redefinida", "conta_desbloqueada", "conta_reativada", "senha_reset_falhou"]))
                          .order_by(Evento.id.desc()).limit(300)).all()
        return [{"acao": e.acao, "autor": e.autor, "ip": e.ip, "detalhe": e.detalhe,
                 "criado_em": e.criado_em.isoformat() if e.criado_em else None} for e in itens]
