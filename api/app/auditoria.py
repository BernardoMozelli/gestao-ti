"""Registro de auditoria (tabela de eventos) usado pelo modulo Logs.

- registrar(): grava uma acao em uma sessao propria (para quem nao tem uma sessao do banco aberta).
- O IP de quem fez a requisicao e preenchido automaticamente em qualquer evento gravado sem IP
  (o middleware em main.py guarda o IP em IP_ATUAL no inicio de cada requisicao).
"""
from contextvars import ContextVar

from sqlalchemy import event

from .db import Evento, SessionLocal

IP_ATUAL: ContextVar[str | None] = ContextVar("ip_atual", default=None)


def registrar(acao: str, autor: str, detalhe: str | None = None, ip: str | None = None,
              hostname: str | None = None) -> None:
    with SessionLocal() as s:
        s.add(Evento(acao=acao, autor=(autor or "?")[:150], detalhe=detalhe, ip=ip, hostname=hostname))
        s.commit()


@event.listens_for(Evento, "before_insert")
def _ip_padrao(_mapper, _conn, alvo: Evento) -> None:
    if not alvo.ip:
        alvo.ip = IP_ATUAL.get()
