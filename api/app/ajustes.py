"""Configuracoes editaveis pela tela (modulo Configuracoes).

Cada valor salvo no banco tem prioridade sobre o .env. Sem valor no banco, vale o .env.
A senha do SMTP fica criptografada no banco (chave derivada do SECRET_KEY).
"""
import base64
import hashlib
import time

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from . import config
from .db import Configuracao, SessionLocal, agora

SEGREDOS = {"SMTP_PASSWORD", "RADIUS_DB_SENHA", "RADIUS_SSH_SENHA"}
# Valores fixos no sistema (ignoram banco e .env)
def conta_envio_efetiva() -> str:
    """geral@ (fixa). Com o modo de teste ligado em Configuracoes, o e-mail de teste informado la."""
    if str(obter("EMAIL_TESTE_ATIVO")).lower() == "sim":
        teste = str(obter("EMAIL_TESTE") or "").strip().lower()
        if "@" in teste:
            return teste
    return config.CONTA_ENVIO


FIXOS = {"SMTP_FROM": conta_envio_efetiva}
# Nestes campos, vazio salvo na tela quer dizer "vazio de verdade" (sem login / sem senha), e nao "volta ao .env"
VAZIO_EXPLICITO = {"SMTP_USER", "SMTP_PASSWORD", "RADIUS_DB_SENHA", "RADIUS_SSH_SENHA", "RADIUS_SSH_HOST"}
_VAZIO = "__vazio__"


def _padrao_seguranca() -> str:
    if config.SMTP_SSL:
        return "ssl"
    return "starttls" if config.SMTP_TLS else "nenhuma"


# nome -> (tipo, valor do .env)
CAMPOS = {
    "SMTP_HOST": (str, lambda: config.SMTP_HOST),
    "SMTP_PORT": (int, lambda: config.SMTP_PORT),
    "SMTP_SEGURANCA": (str, _padrao_seguranca),          # nenhuma | starttls | ssl
    "SMTP_USER": (str, lambda: config.SMTP_USER),
    "SMTP_PASSWORD": (str, lambda: config.SMTP_PASSWORD),
    "SMTP_FROM": (str, lambda: config.CONTA_ENVIO),
    "SMTP_FROM_NOME": (str, lambda: config.SMTP_FROM_NOME),
    "EMAIL_TESTE_ATIVO": (str, lambda: "nao"),        # sim = envia pela conta de teste (so para testes)
    "EMAIL_TESTE": (str, lambda: ""),
    "ENVIO_PAUSA_MS": (int, lambda: config.ENVIO_PAUSA_MS),
    "ANEXOS_MAX_MB": (int, lambda: config.ANEXOS_MAX_MB),
    "IMAGEM_MAX_MB": (int, lambda: config.IMAGEM_MAX_MB),
    "LISTA_MAX_EMAILS": (int, lambda: config.LISTA_MAX_EMAILS),
    "SMTP_LOGIN_FORMATO": (str, lambda: config.SMTP_LOGIN_FORMATO),   # email | usuario | upn | dominio
    "CREDITOS_INICIAIS": (int, lambda: config.CREDITOS_INICIAIS),
    "EMAIL_PARA_TODOS": (str, lambda: config.EMAIL_PARA_TODOS),        # sim | nao
    "RADIUS_DB_HOST": (str, lambda: config.RADIUS_DB_HOST),
    "RADIUS_DB_PORTA": (int, lambda: config.RADIUS_DB_PORTA),
    "RADIUS_DB_NOME": (str, lambda: config.RADIUS_DB_NOME),
    "RADIUS_DB_USUARIO": (str, lambda: config.RADIUS_DB_USUARIO),
    "RADIUS_DB_SENHA": (str, lambda: config.RADIUS_DB_SENHA),
    "RADIUS_SSH": (str, lambda: config.RADIUS_SSH),
    "RADIUS_SSH_HOST": (str, lambda: config.RADIUS_SSH_HOST),
    "RADIUS_SSH_PORTA": (int, lambda: config.RADIUS_SSH_PORTA),
    "RADIUS_SSH_USUARIO": (str, lambda: config.RADIUS_SSH_USUARIO),
    "RADIUS_SSH_SENHA": (str, lambda: config.RADIUS_SSH_SENHA),
    "RADIUS_TABELA_USUARIOS": (str, lambda: config.RADIUS_TABELA_USUARIOS),
    "RADIUS_TABELA_SENHAS": (str, lambda: config.RADIUS_TABELA_SENHAS),
}

_cache: dict = {"em": 0.0, "dados": {}}


def _cofre() -> Fernet:
    chave = hashlib.sha256(("gestao-ti-config:" + (config.SECRET_KEY or "")).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(chave))


def cifrar(texto: str) -> str:
    return _cofre().encrypt(texto.encode()).decode()


def decifrar(cifrado: str | None) -> str | None:
    if not cifrado:
        return None
    try:
        return _cofre().decrypt(cifrado.encode()).decode()
    except InvalidToken:
        return None


def _do_banco() -> dict:
    if time.time() - _cache["em"] > 5:
        with SessionLocal() as s:
            _cache["dados"] = {c.chave: c.valor for c in s.scalars(select(Configuracao)).all()}
        _cache["em"] = time.time()
    return _cache["dados"]


def obter(nome: str):
    if nome in FIXOS:
        return FIXOS[nome]()
    tipo, padrao = CAMPOS[nome]
    bruto = _do_banco().get(nome)
    if bruto == _VAZIO:
        return tipo()
    if bruto is None or bruto == "":
        return padrao()
    if nome in SEGREDOS:
        try:
            return _cofre().decrypt(bruto.encode()).decode()
        except InvalidToken:
            return padrao()
    try:
        return tipo(bruto)
    except ValueError:
        return padrao()


def origem(nome: str) -> str:
    if nome in FIXOS:
        return "fixo"
    return "tela" if _do_banco().get(nome) not in (None, "") else ".env"



def salvar(valores: dict, por: str) -> list[str]:
    """Grava os valores informados. None = nao mexe; "" = apaga (volta a valer o .env). Devolve o que mudou."""
    mudou = []
    with SessionLocal() as s:
        for nome, valor in valores.items():
            if nome not in CAMPOS or nome in FIXOS or valor is None:
                continue
            texto = str(valor).strip()
            if texto == "" and nome in VAZIO_EXPLICITO:
                texto = _VAZIO
            if nome in SEGREDOS and texto and texto != _VAZIO:
                texto = _cofre().encrypt(texto.encode()).decode()
            atual = s.get(Configuracao, nome)
            if atual is None:
                if texto == "":
                    continue
                s.add(Configuracao(chave=nome, valor=texto, atualizado_por=por, atualizado_em=agora()))
            else:
                if atual.valor == texto:
                    continue
                atual.valor, atual.atualizado_por, atual.atualizado_em = texto, por, agora()
            mudou.append(nome)
        s.commit()
    _cache["em"] = 0.0
    return mudou
