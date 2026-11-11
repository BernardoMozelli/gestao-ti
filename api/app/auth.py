"""Sessoes da Gestao TI e do portal do Wi-Fi (cookies assinados), permissoes por modulo e chave do agente."""
import hmac
import logging
import os
import time
from collections import defaultdict

import bcrypt
from fastapi import Cookie, Depends, Header, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from sqlalchemy import select

from . import ajustes, auditoria, config, modulos
from .db import Administrador, Permissao, SessaoEncerrada, SessionLocal

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


def ip_e_do_proxy(request: Request) -> bool:
    """True quando o IP visto e de um proxy (reverso ou de navegacao) que o uvicorn nao foi autorizado a atravessar:
    a requisicao trouxe X-Forwarded-For, mas quem conectou nao esta em FORWARDED_ALLOW_IPS. Nesse caso o IP nao
    identifica o computador do usuario (todo mundo chega com o mesmo IP) e nao serve para DNS reverso."""
    if not request.headers.get("x-forwarded-for"):
        return False
    confiaveis = [i.strip() for i in os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1").split(",") if i.strip()]
    if "*" in confiaveis:
        return False
    ip = request.client.host if request.client else ""
    if ip in confiaveis:
        return False   # nao deveria acontecer (o uvicorn ja trocaria o IP), mas por garantia
    _avisar_proxy(ip)
    return True


_proxies_avisados: set[str] = set()


def _avisar_proxy(ip: str) -> None:
    if ip and ip not in _proxies_avisados:
        _proxies_avisados.add(ip)
        logging.getLogger("api").warning(
            "Acesso chegando pelo proxy %s: o IP real do usuario nao e conhecido. Para identificar os computadores "
            "pelo IP, coloque %s em FORWARDED_ALLOW_IPS no .env (o proxy precisa enviar X-Forwarded-For).", ip, ip)


def bloqueado(chave: str) -> bool:
    limite = time.time() - _JANELA
    _falhas[chave] = [t for t in _falhas[chave] if t > limite]
    return len(_falhas[chave]) >= _MAX_FALHAS


def registrar_falha(chave: str) -> None:
    _falhas[chave].append(time.time())


def limpar_falhas(chave: str) -> None:
    _falhas.pop(chave, None)


# --------------------------------------------------------------------------- painel da TI
MSG_INATIVIDADE = "Sua sessão expirou por inatividade. Entre novamente."


def _validade(ser, sessao: str, horas_max: int) -> tuple[dict, object]:
    """Confere o cookie: assinatura, teto de horas desde o login e inatividade desde a ultima renovacao.
    O cookie e reemitido (renovado) so quando a pessoa usa a tela, entao o horario dele = ultima atividade."""
    try:
        dados, emitido = ser.loads(sessao, max_age=horas_max * 3600, return_timestamp=True)
    except (BadSignature, SignatureExpired):
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    agora_s = time.time()
    if agora_s - float(dados.get("ini") or emitido.timestamp()) > horas_max * 3600:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    minutos = config.SESSAO_INATIVIDADE_MINUTOS
    if minutos > 0 and agora_s - emitido.timestamp() > minutos * 60:
        raise HTTPException(401, MSG_INATIVIDADE)
    return dados, emitido


def token_admin(admin_id: int, inicio: float | None = None, senha_cifrada: str | None = None) -> str:
    """Cookie do painel. senha_cifrada ("sm"): senha do AD criptografada (Fernet), usada so para entrar no servidor
    de e-mail com a conta da propria pessoa. Fica so no cookie (nada no banco) e some ao sair ou expirar a sessao."""
    dados = {"id": admin_id, "ini": int(inicio or time.time())}
    if senha_cifrada:
        dados["sm"] = senha_cifrada
    return _ser_admin.dumps(dados)


def gravar_cookie_admin(response, token: str) -> None:
    response.set_cookie(COOKIE_ADMIN, token, httponly=True, samesite="strict", secure=config.COOKIE_SEGURO,
                        max_age=config.SESSAO_HORAS * 3600)


def senha_da_sessao(sessao: str | None) -> str | None:
    """Senha do AD guardada (criptografada) no cookie no login; None se nao houver."""
    if not sessao:
        return None
    try:
        return ajustes.decifrar(ler_sessao_admin(sessao).get("sm"))
    except HTTPException:
        return None


def trocar_senha_na_sessao(response, sessao: str | None, senha: str) -> None:
    """A pessoa digitou a senha atual (ex.: trocou no AD depois de entrar): passa a valer na sessao."""
    try:
        dados = ler_sessao_admin(sessao)
    except HTTPException:
        return
    gravar_cookie_admin(response, token_admin(dados["id"], dados.get("ini"), ajustes.cifrar(senha)))


def ler_sessao_admin(sessao: str | None) -> dict:
    """Dados do cookie do painel (id e horario do login), ja validado. Usado para renovar a sessao."""
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    return _validade(_ser_admin, sessao, config.SESSAO_HORAS)[0]


def ler_sessao_usuario(sessao: str | None) -> dict:
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    return _validade(_ser_usuario, sessao, SESSAO_USUARIO_HORAS)[0]


def admin_atual(sessao: str | None = Cookie(default=None, alias=COOKIE_ADMIN)) -> Administrador:
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    dados, emitido = _validade(_ser_admin, sessao, config.SESSAO_HORAS)
    with SessionLocal() as s:
        admin = s.get(Administrador, dados.get("id"))
        if not admin or not admin.ativo:
            raise HTTPException(401, "Usuário desativado na Gestão TI.")
        if sessao_finalizada(s, admin.usuario, emitido):
            raise HTTPException(401, MSG_FINALIZADA)
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
def token_usuario(login: str, nome: str, email: str | None, inicio: float | None = None) -> str:
    return _ser_usuario.dumps({"login": login, "nome": nome, "email": email, "ini": int(inicio or time.time())})


def usuario_atual(sessao: str | None = Cookie(default=None, alias=COOKIE_USUARIO)) -> dict:
    if not sessao:
        raise HTTPException(401, "Sessão expirada. Entre novamente.")
    dados, emitido = _validade(_ser_usuario, sessao, SESSAO_USUARIO_HORAS)
    with SessionLocal() as s:
        if sessao_finalizada(s, dados.get("login"), emitido):
            raise HTTPException(401, MSG_FINALIZADA)
    return dados


MSG_FINALIZADA = "Sua sessão foi finalizada pela TI. Entre novamente."


def sessao_finalizada(s, login: str | None, emitido) -> bool:
    """True se a TI finalizou a sessao desse login depois que este cookie foi emitido."""
    if not login:
        return False
    fim = s.get(SessaoEncerrada, login.lower())
    # o horario do cookie e em segundos inteiros: emitido no mesmo segundo do encerramento tambem cai
    return bool(fim and emitido and int(emitido.timestamp()) <= int(fim.encerrada_em.timestamp()))


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
SessaoCookie = Cookie(default=None, alias=COOKIE_ADMIN)
AgenteDep = Depends(exigir_agente)
WifiDep = exigir_modulo("wifi")
