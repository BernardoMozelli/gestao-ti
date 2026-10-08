"""Modulo Logs: todas as acoes registradas na Gestao TI, de todos os modulos e de todos os usuarios.

Le a tabela de eventos (a mesma que cada modulo ja grava). Cada acao tem uma categoria (o modulo de origem)
e um nome legivel. Acoes novas que ainda nao estao no catalogo aparecem em "Outros", com o nome tecnico.
"""
import csv
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, not_, or_, select

from . import auditoria, auth
from .db import Administrador, Evento, SessionLocal

router = APIRouter(prefix="/api/logs")
LogsDep = auth.exigir_modulo("logs")
FUSO = ZoneInfo("America/Sao_Paulo")

CATEGORIAS = {
    "acesso": "Acesso à Gestão TI",
    "usuarios": "Gestão de Usuários",
    "contas_ad": "Contas do AD (senha e reativação)",
    "wifi": "Wi-Fi no computador",
    "portal": "Portal do Wi-Fi (usuários)",
    "wifi_visitante": "Wi-Fi Visitante",
    "email": "E-mails em lote",
    "configuracoes": "Configurações",
    "logs": "Logs",
    "outros": "Outros",
}

ACOES = {
    # acesso
    "login": ("acesso", "Entrou na Gestão TI"),
    "login_falhou": ("acesso", "Falha ao entrar na Gestão TI"),
    "logout": ("acesso", "Saiu da Gestão TI"),
    "admin_ad_primeiro_acesso": ("acesso", "Primeiro acesso (AD)"),
    "acesso_negado": ("acesso", "Tentou abrir módulo sem permissão"),
    # usuarios
    "usuario_adicionado": ("usuarios", "Cadastrou usuário"),
    "admin_criado": ("usuarios", "Criou usuário local"),
    "admin_senha": ("usuarios", "Trocou senha de usuário local"),
    "admin_ativado": ("usuarios", "Liberou acesso à Gestão TI"),
    "admin_desativado": ("usuarios", "Bloqueou acesso à Gestão TI"),
    "permissoes_alteradas": ("usuarios", "Alterou permissões"),
    "delegacao_alterada": ("usuarios", "Alterou quem pode conceder permissões"),
    "sessao_finalizada": ("usuarios", "Finalizou sessão na Gestão TI"),
    "logoff_solicitado": ("usuarios", "Pediu logoff no Windows"),
    "logoff_executado": ("usuarios", "Logoff feito no Windows"),
    "logoff_falhou": ("usuarios", "Logoff no Windows não foi feito"),
    # contas do AD
    "senha_redefinida": ("contas_ad", "Redefiniu senha no AD"),
    "conta_desbloqueada": ("contas_ad", "Desbloqueou conta no AD"),
    "conta_reativada": ("contas_ad", "Reativou conta no AD"),
    "senha_reset_falhou": ("contas_ad", "O AD recusou a alteração"),
    # wifi no computador
    "aprovada": ("wifi", "Liberou o Wi-Fi"),
    "negada": ("wifi", "Negou o pedido"),
    "bloqueada": ("wifi", "Bloqueou o Wi-Fi"),
    "revogada": ("wifi", "Bloqueou o Wi-Fi"),
    "expirada": ("wifi", "Bloqueio automático (prazo)"),
    "prazo_alterado": ("wifi", "Alterou o prazo"),
    "estendida": ("wifi", "Estendeu o prazo"),
    "liberacao_direta": ("wifi", "Liberou sem pedido"),
    "aplicada": ("wifi", "Liberação aplicada no computador"),
    "bloqueio_aplicado": ("wifi", "Bloqueio aplicado no computador"),
    "sessao_iniciada": ("wifi", "Usuário entrou no computador"),
    "sessao_encerrada": ("wifi", "Usuário saiu do computador"),
    "exportou_log": ("wifi", "Exportou o registro do Wi-Fi"),
    # portal
    "portal_login": ("portal", "Entrou no portal do Wi-Fi"),
    "portal_login_falhou": ("portal", "Falha ao entrar no portal"),
    "solicitada": ("portal", "Pediu liberação do Wi-Fi"),
    "cancelada": ("portal", "Cancelou o pedido"),
    # visitante
    "visitante_cadastrada": ("wifi_visitante", "Cadastrou rede de visitantes"),
    "visitante_editada": ("wifi_visitante", "Alterou rede de visitantes"),
    "visitante_senha_trocada": ("wifi_visitante", "Trocou a senha de um visitante"),
    "visitante_senha_vista": ("wifi_visitante", "Viu a senha do Wi-Fi Visitante"),
    "visitante_senha_falhou": ("wifi_visitante", "Troca de senha de visitante falhou"),
    "visitante_ativado": ("wifi_visitante", "Ativou cadastro de visitante"),
    "visitante_desativado": ("wifi_visitante", "Desativou cadastro de visitante"),
    # e-mail
    "email_disparo_criado": ("email", "Criou disparo de e-mail"),
    "email_disparo_iniciado": ("email", "Iniciou o envio"),
    "email_bloco_liberado": ("email", "Liberou o próximo bloco"),
    "email_bloco_enviado": ("email", "Bloco enviado"),
    "email_disparo_pausado": ("email", "Pausou o envio"),
    "email_disparo_retomado": ("email", "Retomou o envio"),
    "email_disparo_cancelado": ("email", "Cancelou o disparo"),
    "email_disparo_concluido": ("email", "Disparo concluído"),
    "email_rascunho_excluido": ("email", "Excluiu rascunho"),
    "email_anexo_adicionado": ("email", "Anexou arquivo"),
    "email_anexo_removido": ("email", "Removeu anexo"),
    "email_imagem_enviada": ("email", "Enviou imagem para o corpo"),
    "email_lista_lida": ("email", "Carregou lista TXT"),
    "email_relatorio_exportado": ("email", "Exportou relatório do disparo"),
    "email_teste": ("email", "Enviou e-mail de teste"),
    # configuracoes
    "configuracao_alterada": ("configuracoes", "Alterou configurações"),
    "configuracao_testada": ("configuracoes", "Testou conexão com o servidor de e-mail"),
    # logs
    "logs_exportados": ("logs", "Exportou os logs"),
}


def _acoes_da_categoria(categoria: str) -> list[str]:
    return [a for a, (c, _) in ACOES.items() if c == categoria]


def _filtro(busca, categoria, acao, autor, desde, ate):
    cond = []
    if busca and busca.strip():
        termo = f"%{busca.strip()}%"
        cond.append(or_(Evento.autor.ilike(termo), Evento.detalhe.ilike(termo), Evento.ip.ilike(termo),
                        Evento.hostname.ilike(termo)))
    if categoria:
        if categoria == "outros":
            cond.append(not_(Evento.acao.in_(list(ACOES))))
        else:
            cond.append(Evento.acao.in_(_acoes_da_categoria(categoria)))
    if acao:
        cond.append(Evento.acao.in_([a for a in acao.split(",") if a]))
    if autor and autor.strip():
        cond.append(func.lower(Evento.autor) == autor.strip().lower())
    try:
        if desde:
            cond.append(Evento.criado_em >= datetime.fromisoformat(desde).replace(tzinfo=FUSO))
        if ate:
            cond.append(Evento.criado_em < datetime.fromisoformat(ate).replace(tzinfo=FUSO) + timedelta(days=1))
    except ValueError:
        raise HTTPException(422, "Data inválida. Use o formato AAAA-MM-DD.")
    return and_(*cond) if cond else None


def _nomes(s, autores) -> dict[str, str]:
    logins = {a for a in autores if a}
    if not logins:
        return {}
    return {a.usuario: a.nome for a in s.scalars(select(Administrador).where(Administrador.usuario.in_(logins)))}


def _item(e: Evento, nomes: dict) -> dict:
    cat, rotulo = ACOES.get(e.acao, ("outros", e.acao))
    return {"id": e.id, "criado_em": e.criado_em.isoformat() if e.criado_em else None,
            "categoria": cat, "categoria_nome": CATEGORIAS[cat], "acao": e.acao, "acao_nome": rotulo,
            "autor": e.autor, "autor_nome": nomes.get(e.autor), "ip": e.ip, "hostname": e.hostname,
            "solicitacao_id": e.solicitacao_id, "detalhe": e.detalhe}


@router.get("")
def listar(busca: str | None = None, categoria: str | None = None, acao: str | None = None, autor: str | None = None,
           desde: str | None = None, ate: str | None = None, pagina: int = 1, por_pagina: int = 50,
           admin: Administrador = LogsDep) -> dict:
    por_pagina = max(10, min(por_pagina, 200))
    pagina = max(1, pagina)
    f = _filtro(busca, categoria, acao, autor, desde, ate)
    with SessionLocal() as s:
        base = select(Evento) if f is None else select(Evento).where(f)
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        itens = s.scalars(base.order_by(Evento.id.desc()).offset((pagina - 1) * por_pagina).limit(por_pagina)).all()
        nomes = _nomes(s, [e.autor for e in itens])
        return {"total": total, "pagina": pagina, "paginas": max(1, -(-total // por_pagina)),
                "itens": [_item(e, nomes) for e in itens]}


@router.get("/opcoes")
def opcoes(admin: Administrador = LogsDep) -> dict:
    with SessionLocal() as s:
        autores = s.execute(select(Evento.autor, func.count()).group_by(Evento.autor)
                            .order_by(func.count().desc()).limit(500)).all()
        nomes = _nomes(s, [a for a, _ in autores])
    return {"categorias": [{"id": k, "nome": v} for k, v in CATEGORIAS.items()],
            "acoes": [{"id": a, "nome": n, "categoria": c} for a, (c, n) in ACOES.items() if a != "revogada"],
            "autores": sorted(({"login": a, "nome": nomes.get(a)} for a, _ in autores if a),
                              key=lambda x: (x["nome"] or x["login"]).lower())}


@router.get("/resumo")
def resumo(admin: Administrador = LogsDep) -> dict:
    """Numeros do dia (e falhas da semana) para os cards do topo."""
    hoje = datetime.now(FUSO).replace(hour=0, minute=0, second=0, microsecond=0)
    semana = hoje - timedelta(days=6)
    with SessionLocal() as s:
        contar = lambda *c: s.scalar(select(func.count(Evento.id)).where(*c))  # noqa: E731
        return {
            "acoes_hoje": contar(Evento.criado_em >= hoje),
            "pessoas_hoje": s.scalar(select(func.count(func.distinct(Evento.autor)))
                                     .where(Evento.criado_em >= hoje, Evento.acao == "login")),
            "falhas_login_7d": contar(Evento.criado_em >= semana, Evento.acao.in_(["login_falhou", "portal_login_falhou"])),
            "acessos_negados_7d": contar(Evento.criado_em >= semana, Evento.acao == "acesso_negado"),
            "alteracoes_ad_7d": contar(Evento.criado_em >= semana, Evento.acao.in_(
                ["senha_redefinida", "conta_desbloqueada", "conta_reativada"])),
        }


@router.get("/exportar.csv")
def exportar(busca: str | None = None, categoria: str | None = None, acao: str | None = None, autor: str | None = None,
             desde: str | None = None, ate: str | None = None, admin: Administrador = LogsDep) -> StreamingResponse:
    f = _filtro(busca, categoria, acao, autor, desde, ate)
    with SessionLocal() as s:
        q = select(Evento) if f is None else select(Evento).where(f)
        itens = s.scalars(q.order_by(Evento.id.desc()).limit(100000)).all()
        nomes = _nomes(s, {e.autor for e in itens})
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["Data e hora", "Módulo", "Ação", "Login", "Nome", "IP", "Computador", "Detalhe"])
    for e in itens:
        i = _item(e, nomes)
        quando = e.criado_em.astimezone(FUSO).strftime("%d/%m/%Y %H:%M:%S") if e.criado_em else ""
        w.writerow([quando, i["categoria_nome"], i["acao_nome"], e.autor or "", i["autor_nome"] or "", e.ip or "",
                    e.hostname or "", e.detalhe or ""])
    filtros = ", ".join(f"{k}={v}" for k, v in (("busca", busca), ("modulo", categoria), ("acao", acao), ("autor", autor),
                                                 ("de", desde), ("ate", ate)) if v)
    auditoria.registrar("logs_exportados", admin.usuario, f"{len(itens)} registros" + (f" · filtros: {filtros}" if filtros else ""))
    nome = f"logs-gestao-ti-{datetime.now(FUSO):%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter(["﻿" + saida.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})
