"""Creditos dos disparos de e-mail.

Cada disparo iniciado consome 1 credito de quem o criou. Sem credito, o envio nao comeca.
Quem tem o modulo "Creditos de e-mail" (o adm geral sempre tem) envia sem limite, aprova ou recusa os pedidos
e libera creditos para qualquer usuario sem precisar de pedido. Disparo que nao entregou nenhum e-mail
(cancelado antes de sair ou com falha em todos) devolve o credito.
Todo movimento fica no extrato (creditos_movimentos) e nos Logs.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import ajustes, auth, config, notificar
from .db import (Administrador, CreditoEmail, Disparo, Evento, MovimentoCredito, PedidoCredito, Permissao,
                 SessionLocal, agora)

log = logging.getLogger("creditos")
router = APIRouter(prefix="/api/email/creditos")

MODULO = "creditos_email"
EmailDep = auth.exigir_modulo("email_ad", "email_lista")
GestorDep = auth.exigir_modulo(MODULO)
NOMES_TIPO = {"inicial": "Créditos iniciais", "concessao": "Liberado pela TI", "remocao": "Removido pela TI",
              "pedido": "Pedido aprovado", "consumo": "Usado em disparo", "devolucao": "Devolvido (nada foi enviado)"}
NOMES_STATUS = {"pendente": "Aguardando", "aprovado": "Aprovado", "recusado": "Recusado", "cancelado": "Cancelado"}


def ilimitado(admin: Administrador) -> bool:
    """Quem gerencia os creditos nao gasta credito (poderia liberar para si mesmo)."""
    return MODULO in (admin.modulos or [])


def _evento(s, acao: str, autor: str, detalhe: str, ip: str | None = None) -> None:
    s.add(Evento(acao=acao, autor=autor[:150], detalhe=detalhe, ip=ip))


def _iso(dt):
    return dt.isoformat() if dt else None


def _conta(s, login: str, para_escrita: bool = False) -> CreditoEmail:
    """Saldo do usuario; na primeira vez cria com os creditos iniciais (Configuracoes)."""
    login = login.lower()
    q = select(CreditoEmail).where(CreditoEmail.usuario == login)
    c = s.scalar(q.with_for_update() if para_escrita else q)
    if c is None:
        iniciais = max(0, int(ajustes.obter("CREDITOS_INICIAIS") or 0))
        c = CreditoEmail(usuario=login, saldo=iniciais, atualizado_em=agora())
        s.add(c)
        s.flush()
        if iniciais:
            s.add(MovimentoCredito(usuario=login, quantidade=iniciais, saldo_depois=iniciais, tipo="inicial",
                                   por="sistema", motivo="Créditos iniciais do primeiro uso"))
        if para_escrita:   # garante a trava da linha recem-criada
            c = s.scalar(q.with_for_update())
    return c


def _mover(s, c: CreditoEmail, qtd: int, tipo: str, por: str, motivo: str | None = None,
           disparo_id: int | None = None, pedido_id: int | None = None) -> MovimentoCredito:
    c.saldo += qtd
    c.atualizado_em = agora()
    m = MovimentoCredito(usuario=c.usuario, quantidade=qtd, saldo_depois=c.saldo, tipo=tipo, por=por[:150],
                         motivo=motivo, disparo_id=disparo_id, pedido_id=pedido_id)
    s.add(m)
    return m


def texto_creditos(n: int) -> str:
    return f"{n} crédito{'' if n == 1 else 's'}"


MSG_SEM_CREDITO = ("Você não tem créditos para disparar e-mails. Cada disparo usa 1 crédito. "
                   "Peça mais créditos à TI pelo botão \"Solicitar créditos\".")


def exigir_saldo(s, admin: Administrador) -> None:
    """Confere (sem gastar) se a pessoa pode iniciar um disparo."""
    if ilimitado(admin):
        return
    if _conta(s, admin.usuario).saldo < 1:
        raise HTTPException(402, MSG_SEM_CREDITO)


def consumir(s, admin: Administrador, d: Disparo) -> None:
    """Gasta 1 credito ao iniciar o disparo (na mesma transacao que muda a situacao do disparo)."""
    if d.credito_usado or ilimitado(admin):
        return
    c = _conta(s, d.criado_por, para_escrita=True)
    if c.saldo < 1:
        raise HTTPException(402, MSG_SEM_CREDITO)
    _mover(s, c, -1, "consumo", admin.usuario, f"Disparo nº {d.id} '{d.titulo}'", disparo_id=d.id)
    d.credito_usado = True
    _evento(s, "email_credito_consumido", admin.usuario,
            f"Disparo nº {d.id} '{d.titulo}': 1 crédito usado (restam {c.saldo})")


def devolver_se_nada_saiu(s, d: Disparo) -> None:
    """Disparo encerrado sem nenhum e-mail entregue devolve o credito usado."""
    if not d.credito_usado or d.credito_devolvido or d.enviados > 0:
        return
    c = _conta(s, d.criado_por, para_escrita=True)
    _mover(s, c, 1, "devolucao", "sistema", f"Disparo nº {d.id} '{d.titulo}' terminou sem nenhum e-mail enviado",
           disparo_id=d.id)
    d.credito_devolvido = True
    _evento(s, "email_credito_devolvido", "sistema",
            f"Disparo nº {d.id} '{d.titulo}': nenhum e-mail saiu, 1 crédito devolvido a {d.criado_por} (saldo {c.saldo})")


def _pedido_dict(p: PedidoCredito) -> dict:
    return {"id": p.id, "usuario": p.usuario, "nome": p.nome, "quantidade": p.quantidade, "motivo": p.motivo,
            "status": p.status, "status_nome": NOMES_STATUS.get(p.status, p.status), "criado_em": _iso(p.criado_em),
            "decidido_por": p.decidido_por, "decidido_em": _iso(p.decidido_em),
            "quantidade_aprovada": p.quantidade_aprovada, "resposta": p.resposta}


def _mov_dict(m: MovimentoCredito) -> dict:
    return {"id": m.id, "usuario": m.usuario, "quantidade": m.quantidade, "saldo_depois": m.saldo_depois,
            "tipo": m.tipo, "tipo_nome": NOMES_TIPO.get(m.tipo, m.tipo), "motivo": m.motivo, "por": m.por,
            "disparo_id": m.disparo_id, "em": _iso(m.em)}


def resumo_de(admin: Administrador) -> dict:
    with SessionLocal() as s:
        c = _conta(s, admin.usuario)
        s.commit()
        pendente = s.scalar(select(PedidoCredito).where(PedidoCredito.usuario == admin.usuario,
                                                        PedidoCredito.status == "pendente"))
        ultimo = s.scalar(select(PedidoCredito).where(PedidoCredito.usuario == admin.usuario,
                                                      PedidoCredito.status != "pendente")
                          .order_by(PedidoCredito.id.desc()))
        return {"saldo": c.saldo, "ilimitado": ilimitado(admin),
                "pedido_pendente": _pedido_dict(pendente) if pendente else None,
                "ultimo_pedido": _pedido_dict(ultimo) if ultimo else None}


# =========================================================================== quem dispara
@router.get("/meu")
def meus_creditos(admin: Administrador = EmailDep) -> dict:
    dados = resumo_de(admin)
    with SessionLocal() as s:
        dados["movimentos"] = [_mov_dict(m) for m in s.scalars(
            select(MovimentoCredito).where(MovimentoCredito.usuario == admin.usuario)
            .order_by(MovimentoCredito.id.desc()).limit(50))]
        dados["pedidos"] = [_pedido_dict(p) for p in s.scalars(
            select(PedidoCredito).where(PedidoCredito.usuario == admin.usuario)
            .order_by(PedidoCredito.id.desc()).limit(20))]
    return dados


class NovoPedido(BaseModel):
    quantidade: int = Field(ge=1, le=1000)
    motivo: str = Field(min_length=3, max_length=500)


@router.post("/pedidos")
def pedir_creditos(dados: NovoPedido, request: Request, tarefas: BackgroundTasks, admin: Administrador = EmailDep) -> dict:
    if ilimitado(admin):
        raise HTTPException(409, "Você gerencia os créditos e já envia sem limite.")
    with SessionLocal() as s:
        if s.scalar(select(PedidoCredito.id).where(PedidoCredito.usuario == admin.usuario,
                                                   PedidoCredito.status == "pendente")):
            raise HTTPException(409, "Você já tem um pedido aguardando a TI. Espere a resposta ou cancele o pedido para fazer outro.")
        p = PedidoCredito(usuario=admin.usuario, nome=admin.nome, quantidade=dados.quantidade,
                          motivo=dados.motivo.strip())
        s.add(p)
        s.flush()
        _evento(s, "email_credito_pedido", admin.usuario,
                f"Pedido nº {p.id}: {texto_creditos(p.quantidade)} · motivo: {p.motivo}", ip=auth.ip_do_cliente(request))
        s.commit()
        tarefas.add_task(notificar.pedido_creditos, p.id, admin.nome, admin.usuario, p.quantidade, p.motivo)
        return _pedido_dict(p)


@router.post("/pedidos/{pedido_id}/cancelar")
def cancelar_pedido(pedido_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    with SessionLocal() as s:
        p = s.get(PedidoCredito, pedido_id, with_for_update=True)
        if not p or p.usuario != admin.usuario:
            raise HTTPException(404, "Pedido não encontrado.")
        if p.status != "pendente":
            raise HTTPException(409, "Esse pedido já foi respondido.")
        p.status, p.decidido_em, p.decidido_por = "cancelado", agora(), admin.usuario
        _evento(s, "email_credito_pedido_cancelado", admin.usuario, f"Pedido nº {p.id} cancelado pelo próprio usuário",
                ip=auth.ip_do_cliente(request))
        s.commit()
        return _pedido_dict(p)


# =========================================================================== quem gerencia
@router.get("/resumo")
def resumo_gestor(admin: Administrador = GestorDep) -> dict:
    with SessionLocal() as s:
        return {"pendentes": s.scalar(select(func.count()).where(PedidoCredito.status == "pendente")) or 0}


@router.get("/pedidos")
def listar_pedidos(status: str = "pendente", admin: Administrador = GestorDep) -> list[dict]:
    with SessionLocal() as s:
        q = select(PedidoCredito)
        if status != "todos":
            q = q.where(PedidoCredito.status == status)
        pedidos = s.scalars(q.order_by(PedidoCredito.id.desc()).limit(300)).all()
        saldos = dict(s.execute(select(CreditoEmail.usuario, CreditoEmail.saldo)
                                .where(CreditoEmail.usuario.in_({p.usuario for p in pedidos}))).all()) if pedidos else {}
        return [{**_pedido_dict(p), "saldo_atual": saldos.get(p.usuario, 0)} for p in pedidos]


class Decisao(BaseModel):
    quantidade: int | None = Field(default=None, ge=1, le=1000)   # aprovar: pode liberar outra quantidade
    resposta: str = Field(default="", max_length=500)


@router.post("/pedidos/{pedido_id}/aprovar")
def aprovar_pedido(pedido_id: int, dados: Decisao, request: Request, admin: Administrador = GestorDep) -> dict:
    with SessionLocal() as s:
        p = s.get(PedidoCredito, pedido_id, with_for_update=True)
        if not p:
            raise HTTPException(404, "Pedido não encontrado.")
        if p.status != "pendente":
            raise HTTPException(409, f"Esse pedido já está como '{NOMES_STATUS.get(p.status)}'.")
        qtd = dados.quantidade or p.quantidade
        c = _conta(s, p.usuario, para_escrita=True)
        p.status, p.decidido_por, p.decidido_em = "aprovado", admin.usuario, agora()
        p.quantidade_aprovada, p.resposta = qtd, dados.resposta.strip() or None
        _mover(s, c, qtd, "pedido", admin.usuario, p.resposta or f"Pedido nº {p.id}: {p.motivo}", pedido_id=p.id)
        _evento(s, "email_credito_aprovado", admin.usuario,
                f"Pedido nº {p.id} de {p.usuario}: {texto_creditos(qtd)} liberado(s)"
                + (f" (pediu {p.quantidade})" if qtd != p.quantidade else "") + f" · saldo agora {c.saldo}"
                + (f" · resposta: {p.resposta}" if p.resposta else ""), ip=auth.ip_do_cliente(request))
        s.commit()
        return {**_pedido_dict(p), "saldo_atual": c.saldo}


@router.post("/pedidos/{pedido_id}/recusar")
def recusar_pedido(pedido_id: int, dados: Decisao, request: Request, admin: Administrador = GestorDep) -> dict:
    with SessionLocal() as s:
        p = s.get(PedidoCredito, pedido_id, with_for_update=True)
        if not p:
            raise HTTPException(404, "Pedido não encontrado.")
        if p.status != "pendente":
            raise HTTPException(409, f"Esse pedido já está como '{NOMES_STATUS.get(p.status)}'.")
        p.status, p.decidido_por, p.decidido_em = "recusado", admin.usuario, agora()
        p.resposta = dados.resposta.strip() or None
        _evento(s, "email_credito_recusado", admin.usuario,
                f"Pedido nº {p.id} de {p.usuario} ({texto_creditos(p.quantidade)}) recusado"
                + (f" · resposta: {p.resposta}" if p.resposta else ""), ip=auth.ip_do_cliente(request))
        s.commit()
        return _pedido_dict(p)


def _usuarios_dos_disparos(s) -> list[Administrador]:
    """Quem pode disparar e-mails: todos que ja entraram na Gestao TI (os modulos de e-mail sao liberados para todos)."""
    return list(s.scalars(select(Administrador).where(or_(Administrador.ativo.is_(True),
                                                          Administrador.usuario.in_(select(CreditoEmail.usuario))))))


@router.get("/usuarios")
def usuarios_e_saldos(admin: Administrador = GestorDep) -> list[dict]:
    with SessionLocal() as s:
        pessoas = _usuarios_dos_disparos(s)
        saldos = {c.usuario: c for c in s.scalars(select(CreditoEmail))}
        gestores = set(s.scalars(select(Permissao.usuario_id).where(Permissao.modulo == MODULO)))
        usados = dict(s.execute(select(MovimentoCredito.usuario, func.count()).where(MovimentoCredito.tipo == "consumo")
                                .group_by(MovimentoCredito.usuario)).all())
        ultimo = dict(s.execute(select(Disparo.criado_por, func.max(Disparo.iniciado_em)).group_by(Disparo.criado_por)).all())
        pendentes = set(s.scalars(select(PedidoCredito.usuario).where(PedidoCredito.status == "pendente")))
        saida = []
        for a in pessoas:
            c = saldos.get(a.usuario)
            saida.append({"usuario": a.usuario, "nome": a.nome, "email": a.email, "unidade": a.unidade,
                          "ativo": a.ativo, "saldo": c.saldo if c else None,
                          "ilimitado": auth.eh_admin_geral(a) or a.id in gestores,
                          "disparos": usados.get(a.usuario, 0), "ultimo_disparo": _iso(ultimo.get(a.usuario)),
                          "pedido_pendente": a.usuario in pendentes})
        return sorted(saida, key=lambda x: (x["nome"] or x["usuario"]).lower())


class Concessao(BaseModel):
    usuario: str = Field(min_length=1, max_length=80)
    quantidade: int = Field(ge=-1000, le=1000)     # negativo = remover creditos
    motivo: str = Field(default="", max_length=500)


@router.post("/conceder")
def conceder(dados: Concessao, request: Request, admin: Administrador = GestorDep) -> dict:
    if dados.quantidade == 0:
        raise HTTPException(422, "Informe a quantidade de créditos.")
    login = dados.usuario.strip().lower()
    with SessionLocal() as s:
        pessoa = s.scalar(select(Administrador).where(Administrador.usuario == login))
        if not pessoa:
            raise HTTPException(404, "Usuário não encontrado na Gestão TI. Ele precisa ter entrado pelo menos uma vez "
                                     "ou ter recebido a permissão de e-mails na Gestão de Usuários.")
        c = _conta(s, login, para_escrita=True)
        if c.saldo + dados.quantidade < 0:
            raise HTTPException(422, f"{pessoa.nome} tem {texto_creditos(c.saldo)}. Não dá para remover {abs(dados.quantidade)}.")
        tipo = "concessao" if dados.quantidade > 0 else "remocao"
        _mover(s, c, dados.quantidade, tipo, admin.usuario, dados.motivo.strip() or None)
        _evento(s, "email_credito_concedido" if dados.quantidade > 0 else "email_credito_removido", admin.usuario,
                f"{pessoa.nome} ({login}): {'+' if dados.quantidade > 0 else ''}{dados.quantidade} · saldo agora {c.saldo}"
                + (f" · motivo: {dados.motivo.strip()}" if dados.motivo.strip() else ""), ip=auth.ip_do_cliente(request))
        s.commit()
        return {"usuario": login, "saldo": c.saldo}


@router.get("/movimentos")
def movimentos(usuario: str | None = None, admin: Administrador = GestorDep) -> list[dict]:
    with SessionLocal() as s:
        q = select(MovimentoCredito)
        if usuario:
            q = q.where(MovimentoCredito.usuario == usuario.strip().lower())
        nomes = dict(s.execute(select(Administrador.usuario, Administrador.nome)).all())
        return [{**_mov_dict(m), "nome": nomes.get(m.usuario)} for m in
                s.scalars(q.order_by(MovimentoCredito.id.desc()).limit(500))]
