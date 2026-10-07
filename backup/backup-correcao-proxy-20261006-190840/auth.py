"""Sessoes da Gestao TI e do portal do Wi-Fi (cookies assinados), permissoes por modulo e chave do agente."""
import hmac
import time
from collections import defaultdict

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from sqlalchemy import select

from . import auditoria, config, modulos
from .db import Administrador, Permissao, SessionLocal

COOKIE_ADMIN = "gestao_ti_sessao"
COOKIE_USUARIO = "gestao_ti_wifi_usuario"
_ser_admin = URLSafeTimedSerializer(config.SECRET_KEY or "nao-configurado", salt="painel-ti")
_ser_usuario = URLSafeTimedSerializer(config.SECRET_KEY or "nao-configurado", salt="portal-usuario")
SESSAO_USUARIO_HORAS = 8

# Bloqueio simples contra tentativa de senha: 5 erros em 10 minutos por IP e por tela
_falhas: dict[str, list[float]] = defaultdict(list)
_JANELA = 600
_MAX_FALHAS = 5


def gerar_hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode(), bcrypt.gensalt()).decode()


def conferir_senha(senha: str, hash_salvo: str) -> bool:
    if not hash_salvo:
        return False
    try:
        return bcrypt.checkpw(senha.encode(), hash_salvo.encode())
    except ValueError:
        return False


def ip_do_cliente(request: Request) -> str:
    # O uvicorn so aplica X-Forwarded-For vindo dos IPs em FORWARDED_ALLOW_IPS (proxy confiavel),
    # entao aqui o endereco ja e o do usuario real e nao pode ser falsificado pelo cabecalho.
    return request.client.host if request.client else "desconhecido"


def bloqueado(chave: str) -> bool:
    limite = time.time() - _JANELA
    _falhas[chave] = [t for t in _falhas[chave] if t > limite]
    return len(_falhas[chave]) >= _MAX_FALHAS


def registrar_falha(chave: str) -> None:
    _falhas[chave].append(time.time())


def limpar_falhas(chave: str) -> None:
    _falhas.pop(chave, None)


# --------------------------------------------------------------------------- painel da TI
def token_admin(admin_id: int) -> str:
    return _ser_admin.dumps({"id": admin_id})


def admin_atual(sessao: str | None = Cookie(default=None, alias=COOKIE_ADMIN)) -> Administrador:
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    try:
        dados = _ser_admin.loads(sessao, max_age=config.SESSAO_HORAS * 3600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    with SessionLocal() as s:
        admin = s.get(Administrador, dados.get("id"))
        if not admin or not admin.ativo:
            raise HTTPException(401, "Usuário desativado na Gestão TI.")
        admin.modulos = modulos_de(s, admin)   # pode ser vazio: a pessoa entra e aguarda a TI liberar
        return admin


def eh_ti(admin: Administrador) -> bool:
    """Usuarios locais sao da TI; do AD, conforme a OU/grupo da TI."""
    return admin.origem == "local" or bool(admin.ti)


def eh_admin_geral(admin: Administrador) -> bool:
    """Usuario local ADMIN_USER ou login do AD listado em ADMIN_GERAL."""
    if admin.origem == "local" and admin.usuario == config.ADMIN_USER.lower():
        return True
    return admin.origem == "ad" and admin.usuario in config.ADMIN_GERAL


def pode_conceder(admin: Administrador) -> bool:
    return eh_admin_geral(admin) or bool(admin.pode_conceder)


def modulos_de(s, admin: Administrador) -> list[str]:
    if eh_admin_geral(admin):
        return modulos.permitidos_para()   # o adm geral sempre tem tudo
    concedidos = s.scalars(select(Permissao.modulo).where(Permissao.usuario_id == admin.id)).all()
    return modulos.efetivos(eh_ti(admin), concedidos)


def exigir_modulo(*nomes: str):
    """Dependencia: o usuario precisa ter pelo menos um dos modulos informados."""
    def verificar(request: Request, admin: Administrador = Depends(admin_atual)) -> Administrador:
        if not any(m in admin.modulos for m in nomes):
            auditoria.registrar("acesso_negado", admin.usuario,
                                f"{request.method} {request.url.path} · precisa de: "
                                + " ou ".join(modulos.MODULOS.get(m, {}).get("nome", m) for m in nomes))
            raise HTTPException(403, "Você não tem permissão para este módulo.")
        return admin
    return Depends(verificar)


# --------------------------------------------------------------------------- portal do usuario
def token_usuario(login: str, nome: str, email: str | None) -> str:
    return _ser_usuario.dumps({"login": login, "nome": nome, "email": email})


def usuario_atual(sessao: str | None = Cookie(default=None, alias=COOKIE_USUARIO)) -> dict:
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    try:
        return _ser_usuario.loads(sessao, max_age=SESSAO_USUARIO_HORAS * 3600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Sessão expirada. Entre novamente.")


# --------------------------------------------------------------------------- script da GPO
def exigir_agente(x_agent_key: str | None = Header(default=None)) -> None:
    if not x_agent_key or not hmac.compare_digest(x_agent_key, config.AGENT_KEY):
        raise HTTPException(401, "Chave do agente inválida.")


def _exigir_concessao(request: Request, admin: Administrador = Depends(admin_atual)) -> Administrador:
    if not pode_conceder(admin):
        auditoria.registrar("acesso_negado", admin.usuario,
                            f"{request.method} {request.url.path} · precisa ser adm geral ou ter autorização para conceder permissões")
        raise HTTPException(403, "Só o adm geral e quem ele autorizou podem alterar permissões.")
    return admin


AdminDep = Depends(admin_atual)
ConcederDep = Depends(_exigir_concessao)
UsuarioDep = Depends(usuario_atual)
AgenteDep = Depends(exigir_agente)
WifiDep = exigir_modulo("wifi")
