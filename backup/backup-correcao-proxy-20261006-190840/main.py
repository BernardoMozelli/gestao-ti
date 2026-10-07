"""API da Gestao TI (PMSL): plataforma com modulos e permissoes por usuario.

Modulos: Rede Sem Fio (wifi), E-mails em lote (email_ad / email_lista), Gestao de Usuarios.
Fluxo do modulo Rede Sem Fio:
  1. Usuario entra no portal (/wifi) com o login do AD e pede a liberacao do computador.
  2. TI entra na Gestao TI (/) e, no modulo Rede Sem Fio, libera (por tempo ou
     indeterminado), nega ou bloqueia de novo.
  3. O script da GPO (SYSTEM) consulta GET /api/agente/liberacao a cada 5 minutos.
     Liberacao valida -> libera as redes. Prazo vencido ou bloqueio -> volta a regra normal.
Tudo fica registrado na tabela de eventos (quem, o que, quando, computador e IP).
"""
import csv
import io
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import dns.resolver
import dns.reversename
from fastapi import BackgroundTasks, Cookie, FastAPI, HTTPException, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from . import ad, ad_senhas, auditoria, auth, config, configuracoes_api, email_lote, logs_api, modulos, notificar, senhas_api, visitante_api
from .db import (Administrador, Computador, Configuracao, Evento, Permissao,
                 SessionLocal, Solicitacao, agora, criar_tabelas)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")

app = FastAPI(title=config.APP_NOME, docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)   # listas grandes (usuarios, logs) chegam comprimidas
STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def guardar_ip(request: Request, chamar):
    """Guarda o IP de quem fez a requisicao: todo evento gravado sem IP recebe este (auditoria/Logs)."""
    auditoria.IP_ATUAL.set(auth.ip_do_cliente(request))
    return await chamar(request)
FUSO = ZoneInfo("America/Sao_Paulo")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.]{0,62}$")
ABERTOS = ("pendente", "aprovada")


# =========================================================================== utilitarios
@app.on_event("startup")
def iniciar() -> None:
    config.validar()
    criar_tabelas()
    with SessionLocal() as s:
        if s.scalar(select(func.count(Administrador.id))) == 0:
            if not config.ADMIN_PASSWORD:
                raise RuntimeError("Nenhum administrador cadastrado e ADMIN_PASSWORD nao definido.")
            s.add(Administrador(usuario=config.ADMIN_USER.lower(), nome=config.ADMIN_NOME, origem="local",
                                ti=True, senha_hash=auth.gerar_hash(config.ADMIN_PASSWORD)))
            s.commit()
            log.info("Administrador local '%s' criado.", config.ADMIN_USER)
        # Usuarios locais sem nenhuma permissao (instalacao nova ou vinda da versao so do Wi-Fi) recebem todos os modulos
        for a in s.scalars(select(Administrador).where(Administrador.origem == "local")).all():
            conceder_padrao_ti(s, a, "sistema")
        s.commit()
        # Modulos que surgiram depois: quem ja gerencia usuarios recebe uma vez (depois pode ser retirado na tela)
        for novo in ("configuracoes", "wifi_visitante", "logs"):   # logs e configuracoes: quem gerencia usuarios
            marca = f"modulo_concedido:{novo}"
            if s.get(Configuracao, marca):
                continue
            if novo == "wifi_visitante":   # quem ja cuida do Wi-Fi dos computadores
                gestores = s.scalars(select(Permissao.usuario_id).where(Permissao.modulo == "wifi")).all()
            else:
                gestores = s.scalars(select(Permissao.usuario_id).where(Permissao.modulo == "usuarios")).all()
            ja_tem = set(s.scalars(select(Permissao.usuario_id).where(Permissao.modulo == novo)).all())
            for uid in set(gestores) - ja_tem:
                s.add(Permissao(usuario_id=uid, modulo=novo, concedido_por="atualização do sistema"))
            s.add(Configuracao(chave=marca, valor="1", atualizado_por="sistema"))
        s.commit()
    email_lote.iniciar_envios()
    ad_senhas.aquecer()   # le a lista do AD em segundo plano para a Gestao de Usuarios abrir na hora


def conceder_padrao_ti(s, admin: Administrador, por: str) -> bool:
    """Da os modulos padrao a quem e da TI e ainda nao tem nenhuma permissao registrada."""
    if s.scalar(select(func.count(Permissao.id)).where(Permissao.usuario_id == admin.id)):
        return False
    for m in modulos.PADRAO_TI:
        s.add(Permissao(usuario_id=admin.id, modulo=m, concedido_por=por))
    return True


def registrar(s, acao: str, autor: str, sol: Solicitacao | None = None, detalhe: str | None = None,
              ip: str | None = None, hostname: str | None = None) -> None:
    s.add(Evento(solicitacao_id=sol.id if sol else None, acao=acao, autor=autor[:150],
                 hostname=hostname or (sol.hostname if sol else None), ip=ip, detalhe=detalhe))


def fmt(dt: datetime | None) -> str:
    return dt.astimezone(FUSO).strftime("%d/%m/%Y %H:%M") if dt else "—"


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def expirar_vencidas(s) -> None:
    """Bloqueio automatico: liberacoes por tempo cujo prazo acabou viram 'expirada'."""
    vencidas = s.scalars(select(Solicitacao).where(Solicitacao.status == "aprovada",
                                                   Solicitacao.indeterminado.is_(False),
                                                   Solicitacao.valido_ate <= agora())).all()
    for sol in vencidas:
        sol.status = "expirada"
        sol.bloqueado_em = sol.valido_ate
        sol.bloqueado_por = "sistema (prazo encerrado)"
        registrar(s, "expirada", "sistema", sol, f"Prazo encerrado em {fmt(sol.valido_ate)}. Bloqueio automático.")
    if vencidas:
        s.commit()


def liberacao_ativa(s, hostname: str) -> Solicitacao | None:
    return s.scalar(select(Solicitacao).where(
        Solicitacao.hostname == hostname, Solicitacao.status == "aprovada",
        or_(Solicitacao.indeterminado.is_(True), Solicitacao.valido_ate > agora()))
        .order_by(Solicitacao.id.desc()))


def normalizar_host(hostname: str) -> str:
    hostname = (hostname or "").strip().upper()
    if not HOSTNAME_RE.match(hostname):
        raise HTTPException(422, "Nome de computador inválido.")
    return hostname


_cache_dns: dict[str, tuple[float, str | None]] = {}


def nome_pelo_dns(ip: str) -> str | None:
    """Consulta o PTR do IP nos DNS do AD (ex.: 10.1.5.23 -> ST-FKQ04N3.santaluzia.int -> ST-FKQ04N3)."""
    if not config.DETECTAR_POR_DNS or not ip or ip in ("desconhecido", "127.0.0.1"):
        return None
    guardado = _cache_dns.get(ip)
    if guardado and time.time() - guardado[0] < 300:
        return guardado[1]
    nome = None
    try:
        resolvedor = dns.resolver.Resolver(configure=False)
        resolvedor.nameservers = config.DNS_SERVIDORES
        resolvedor.timeout = resolvedor.lifetime = 2
        resposta = resolvedor.resolve(dns.reversename.from_address(ip), "PTR")
        candidato = str(resposta[0]).rstrip(".").split(".")[0].upper()
        nome = candidato if HOSTNAME_RE.match(candidato) else None
    except Exception as erro:  # noqa: BLE001 - zona reversa inexistente, timeout etc.
        log.debug("PTR de %s indisponivel: %s", ip, erro)
    _cache_dns[ip] = (time.time(), nome)
    return nome


def computadores_do_usuario(s, login: str | None) -> list[str]:
    """Computadores em que o login esta logado agora (informado pelo script da GPO), do mais recente ao mais antigo."""
    if not login:
        return []
    limite = agora() - timedelta(minutes=config.MINUTOS_DETECCAO_COMPUTADOR)
    return list(s.scalars(select(Computador.hostname).where(
        Computador.usuario_logado == login, Computador.visto_em >= limite).order_by(Computador.visto_em.desc())))


def detectar_computador(s, ip: str, login: str | None = None, sugestao: str | None = None) -> str | None:
    return detectar_com_fonte(s, ip, login, sugestao)[0]


def detectar_com_fonte(s, ip: str, login: str | None = None,
                       sugestao: str | None = None) -> tuple[str | None, str | None]:
    """Descobre de qual computador o usuario esta acessando, nesta ordem:
    1. IP que o script da GPO informou nos ultimos minutos (quando o navegador nao passa por proxy);
    2. computador em que o login do AD esta logado agora (funciona mesmo atras do proxy de navegacao);
       se estiver logado em mais de um, usa o nome do atalho (?h=) quando for um deles;
    3. DNS reverso do AD (se DETECTAR_POR_DNS=true)."""
    limite = agora() - timedelta(minutes=config.MINUTOS_DETECCAO_COMPUTADOR)
    nome = s.scalar(select(Computador.hostname).where(Computador.ip == ip, Computador.visto_em >= limite)
                    .order_by(Computador.visto_em.desc()))
    if nome:
        return nome, "detectado"
    maquinas = computadores_do_usuario(s, login)
    if len(maquinas) == 1:
        return maquinas[0], "detectado"
    if sugestao and sugestao.strip().upper() in maquinas:
        return sugestao.strip().upper(), "detectado"
    nome = nome_pelo_dns(ip)
    return (nome, "dns") if nome else (None, None)


def descrever_prazo(horas: int | None, indeterminado: bool, valido_ate: datetime | None = None) -> str:
    if indeterminado:
        return "por tempo indeterminado (até a TI bloquear)"
    texto = f"por {nome_horas(horas)}"
    return f"{texto}, até {fmt(valido_ate)}" if valido_ate else texto


def nome_horas(h: int | None) -> str:
    if not h:
        return "tempo indeterminado"
    d, r = divmod(h, 24)
    texto_h = f"{r} hora" + ("s" if r > 1 else "")
    if d == 0:
        return texto_h
    texto_d = f"{d} dia" + ("s" if d > 1 else "")
    return texto_d if r == 0 else f"{texto_d} e {texto_h}"


def usuario_no_computador(s, hostname: str) -> str | None:
    """Login de quem esta logado no computador agora (visto pelo script nos ultimos minutos)."""
    pc = s.get(Computador, hostname)
    limite = agora() - timedelta(minutes=config.MINUTOS_DETECCAO_COMPUTADOR)
    return pc.usuario_logado if pc and pc.visto_em and pc.visto_em >= limite else None


def nomes_por_login(s, logins) -> dict[str, str]:
    """Nome de exibicao de cada login, pelo historico de pedidos e pela equipe do painel."""
    logins = {l for l in logins if l}
    if not logins:
        return {}
    nomes: dict[str, str] = {}
    for login, nome in s.execute(select(Solicitacao.usuario_login, func.max(Solicitacao.usuario))
                                 .where(Solicitacao.usuario_login.in_(logins)).group_by(Solicitacao.usuario_login)):
        nomes[login] = nome
    for login, nome in s.execute(select(Administrador.usuario, Administrador.nome).where(Administrador.usuario.in_(logins))):
        nomes[login] = nome
    return nomes


def para_dict(sol: Solicitacao) -> dict:
    return {
        "id": sol.id, "hostname": sol.hostname, "usuario": sol.usuario, "usuario_login": sol.usuario_login,
        "motivo": sol.motivo, "horas_solicitadas": sol.horas_solicitadas, "status": sol.status,
        "origem": sol.origem, "hostname_fonte": sol.hostname_fonte, "ip_origem": sol.ip_origem,
        "criado_em": iso(sol.criado_em), "decidido_em": iso(sol.decidido_em), "decidido_por": sol.decidido_por,
        "horas_aprovadas": sol.horas_aprovadas, "indeterminado": bool(sol.indeterminado),
        "valido_ate": iso(sol.valido_ate), "observacao_ti": sol.observacao_ti,
        "bloqueado_por": sol.bloqueado_por, "bloqueado_em": iso(sol.bloqueado_em),
        "ultima_consulta_agente": iso(sol.ultima_consulta_agente),
        "bloqueio_aplicado_em": iso(sol.bloqueio_aplicado_em),
        "usuario_no_computador": sol.usuario_no_computador,
    }


def validar_prazo_usuario(horas: int | None, indeterminado: bool) -> tuple[int | None, bool]:
    if indeterminado:
        # Tempo indeterminado e exclusivo da TI (Painel Administrativo)
        raise HTTPException(422, "Escolha um tempo de liberação. Tempo indeterminado só pode ser definido pela TI.")
    if horas in config.OPCOES_USUARIO:
        return horas, False
    if config.PERSONALIZADO_USUARIO and horas and 1 <= horas <= config.HORAS_MAXIMAS_USUARIO:
        return horas, False
    if config.PERSONALIZADO_USUARIO:
        raise HTTPException(422, f"O tempo deve ficar entre 1 hora e {nome_horas(config.HORAS_MAXIMAS_USUARIO)}.")
    raise HTTPException(422, "Escolha um dos tempos disponíveis.")


def validar_prazo_admin(horas: int | None, indeterminado: bool) -> tuple[int | None, bool]:
    if indeterminado:
        if not config.PERMITIR_INDETERMINADO:
            raise HTTPException(422, "Liberação por tempo indeterminado não está habilitada.")
        return None, True
    if not horas or horas < 1 or horas > config.HORAS_MAXIMAS:
        raise HTTPException(422, f"Informe um prazo entre 1 hora e {nome_horas(config.HORAS_MAXIMAS)}.")
    return horas, False


def aplicar_liberacao(sol: Solicitacao, horas: int | None, indeterminado: bool, quem: str, obs: str | None) -> None:
    sol.status = "aprovada"
    sol.decidido_em = agora()
    sol.decidido_por = quem
    sol.horas_aprovadas = horas
    sol.indeterminado = indeterminado
    sol.valido_ate = None if indeterminado else sol.decidido_em + timedelta(hours=horas)
    sol.observacao_ti = (obs or "").strip() or None
    sol.ultima_consulta_agente = None
    sol.bloqueio_aplicado_em = None


# =========================================================================== publico
@app.get("/health")
def saude() -> dict:
    with SessionLocal() as s:
        s.execute(select(1))
    return {"ok": True}


@app.get("/api/config-publica")
def config_publica() -> dict:
    logo = next((f"/static/marca/{n}" for n in ("logo.svg", "logo.png") if (STATIC / "marca" / n).exists()), None)
    return {
        "app_nome": config.APP_NOME, "app_subtitulo": config.APP_SUBTITULO,
        "orgao_linha1": config.ORGAO_LINHA1, "orgao_linha2": config.ORGAO_LINHA2, "rodape": config.RODAPE,
        "wifi_nome": config.WIFI_NOME, "wifi_subtitulo": config.WIFI_SUBTITULO,
        "logo": logo, "horas_permitidas": config.OPCOES_USUARIO, "horas_admin": config.OPCOES_ADMIN,
        "horas_maximas": config.HORAS_MAXIMAS, "personalizado_usuario": config.PERSONALIZADO_USUARIO,
        "horas_maximas_usuario": config.HORAS_MAXIMAS_USUARIO, "permitir_indeterminado": config.PERMITIR_INDETERMINADO,
        "ad_habilitado": config.AD_HABILITADO, "busca_ad": bool(config.AD_BIND_USUARIO and config.AD_BIND_SENHA),
    }


class Login(BaseModel):
    usuario: str = Field(max_length=150)
    senha: str = Field(max_length=200)


# =========================================================================== portal do usuario
@app.post("/api/portal/login")
def portal_login(dados: Login, request: Request, response: Response) -> dict:
    ip = auth.ip_do_cliente(request)
    chave = f"portal:{ip}"
    if auth.bloqueado(chave):
        raise HTTPException(429, "Muitas tentativas. Aguarde 10 minutos.")
    login = ad.normalizar_login(dados.usuario)
    try:
        usuario = ad.autenticar(dados.usuario, dados.senha, exigir_admin=False)
    except ad.ErroAD as erro:
        with SessionLocal() as s:
            if erro.credencial:
                auth.registrar_falha(chave)
            registrar(s, "portal_login_falhou", login or "?", detalhe=str(erro), ip=ip)
            s.commit()
        raise HTTPException(401 if erro.credencial else 503, str(erro))
    auth.limpar_falhas(chave)
    with SessionLocal() as s:
        registrar(s, "portal_login", usuario["usuario"], detalhe=usuario["nome"], ip=ip,
                  hostname=detectar_computador(s, ip, usuario["usuario"]))
        s.commit()
    response.set_cookie(auth.COOKIE_USUARIO, auth.token_usuario(usuario["usuario"], usuario["nome"], usuario["email"]),
                        httponly=True, samesite="strict", secure=config.COOKIE_SEGURO,
                        max_age=auth.SESSAO_USUARIO_HORAS * 3600)
    return {"login": usuario["usuario"], "nome": usuario["nome"]}


@app.post("/api/portal/logout")
def portal_logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_USUARIO)
    return {"ok": True}


@app.get("/api/portal/estado")
def portal_estado(request: Request, h: str | None = None, usuario: dict = auth.UsuarioDep) -> dict:
    """Quem esta logado, de qual computador e a situacao das solicitacoes."""
    ip = auth.ip_do_cliente(request)
    with SessionLocal() as s:
        expirar_vencidas(s)
        hostname, fonte = detectar_com_fonte(s, ip, usuario["login"], h)
        if not hostname and h and HOSTNAME_RE.match(h.strip()):
            hostname, fonte = h.strip().upper(), "atalho"
        atual = None
        if hostname:
            atual = s.scalar(select(Solicitacao).where(Solicitacao.hostname == hostname)
                             .order_by(Solicitacao.id.desc()))
        minhas = s.scalars(select(Solicitacao).where(Solicitacao.usuario_login == usuario["login"])
                           .order_by(Solicitacao.id.desc()).limit(10)).all()
        return {"usuario": {"login": usuario["login"], "nome": usuario["nome"]},
                "computador": {"hostname": hostname, "fonte": fonte if hostname else None, "ip": ip},
                "atual": para_dict(atual) if atual else None,
                "minhas": [para_dict(x) for x in minhas]}


class PedidoPortal(BaseModel):
    hostname: str
    motivo: str = Field(min_length=10, max_length=1000)
    horas: int | None = None
    indeterminado: bool = False


@app.post("/api/portal/solicitacoes")
def portal_solicitar(dados: PedidoPortal, request: Request, tarefas: BackgroundTasks,
                     usuario: dict = auth.UsuarioDep) -> dict:
    hostname = normalizar_host(dados.hostname)
    horas, indeterminado = validar_prazo_usuario(dados.horas, dados.indeterminado)
    motivo = dados.motivo.strip()
    if len(motivo) < 10:
        raise HTTPException(422, "Descreva o motivo com pelo menos 10 caracteres.")
    ip = auth.ip_do_cliente(request)
    with SessionLocal() as s:
        expirar_vencidas(s)
        aberta = s.scalar(select(Solicitacao).where(Solicitacao.hostname == hostname,
                                                    Solicitacao.status.in_(ABERTOS)).order_by(Solicitacao.id.desc()))
        if aberta:
            situacao = "aguardando a TI" if aberta.status == "pendente" else "já liberada"
            raise HTTPException(409, f"O computador {hostname} já tem uma solicitação {situacao} (nº {aberta.id}).")
        desde = agora() - timedelta(hours=24)
        qtd = s.scalar(select(func.count(Solicitacao.id)).where(
            or_(Solicitacao.hostname == hostname, Solicitacao.usuario_login == usuario["login"]),
            Solicitacao.criado_em >= desde, Solicitacao.origem == "usuario"))
        if qtd >= config.MAX_SOLICITACOES_POR_DIA:
            raise HTTPException(429, "Limite de solicitações nas últimas 24 horas atingido. Procure a TI.")
        fonte = "detectado" if (detectar_computador(s, ip, usuario["login"], hostname) == hostname
                                 or hostname in computadores_do_usuario(s, usuario["login"])) else "informado"
        sol = Solicitacao(hostname=hostname, usuario=usuario["nome"], usuario_login=usuario["login"],
                          motivo=motivo, horas_solicitadas=horas, indeterminado=False, hostname_fonte=fonte,
                          ip_origem=ip, usuario_no_computador=usuario_no_computador(s, hostname))
        s.add(sol)
        s.flush()
        registrar(s, "solicitada", usuario["login"], sol,
                  f"Pediu liberação {descrever_prazo(horas, indeterminado)}. Motivo: {motivo}", ip=ip)
        s.commit()
        tarefas.add_task(notificar.nova_solicitacao, sol)
        return para_dict(sol)


@app.post("/api/portal/solicitacoes/{sol_id}/cancelar")
def portal_cancelar(sol_id: int, request: Request, usuario: dict = auth.UsuarioDep) -> dict:
    with SessionLocal() as s:
        sol = s.get(Solicitacao, sol_id, with_for_update=True)
        if not sol or sol.usuario_login != usuario["login"]:
            raise HTTPException(404, "Solicitação não encontrada.")
        if sol.status != "pendente":
            raise HTTPException(409, "Só é possível cancelar solicitações que ainda aguardam a TI.")
        sol.status = "cancelada"
        sol.decidido_em = agora()
        registrar(s, "cancelada", usuario["login"], sol, "Cancelada pelo próprio usuário", ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


# =========================================================================== script da GPO (SYSTEM)
@app.get("/api/agente/liberacao", dependencies=[auth.AgenteDep])
def agente_liberacao(hostname: str, request: Request, usuario: str | None = None) -> dict:
    hostname = normalizar_host(hostname)
    ip = auth.ip_do_cliente(request)
    with SessionLocal() as s:
        pc = s.get(Computador, hostname)
        if pc:
            pc.ip, pc.visto_em = ip, agora()
        else:
            pc = Computador(hostname=hostname, ip=ip, visto_em=agora())
            s.add(pc)
        # usuario=None: script antigo (nao informa); usuario="": ninguem logado
        if usuario is not None:
            logado = ad.normalizar_login(usuario)[:80] or None
            if logado != pc.usuario_logado:
                anterior = pc.usuario_logado
                pc.usuario_logado, pc.usuario_desde = logado, agora()
                if logado:
                    registrar(s, "sessao_iniciada", logado, hostname=hostname, ip=ip,
                              detalhe="Usuário logado no computador" + (f" (antes: {anterior})" if anterior else ""))
                elif anterior:
                    registrar(s, "sessao_encerrada", anterior, hostname=hostname, ip=ip,
                              detalhe="Nenhum usuário logado no computador")
        expirar_vencidas(s)
        sol = liberacao_ativa(s, hostname)
        if sol:
            if sol.ultima_consulta_agente is None:
                registrar(s, "aplicada", "computador", sol, "O computador aplicou a liberação (todas as redes visíveis)", ip=ip)
            sol.ultima_consulta_agente = agora()
            s.commit()
            return {"liberado": True, "solicitacao_id": sol.id, "indeterminado": bool(sol.indeterminado),
                    "valido_ate": iso(sol.valido_ate)}
        # Confirma no historico quando o computador voltou a bloquear depois de um bloqueio/expiracao
        encerrada = s.scalar(select(Solicitacao).where(
            Solicitacao.hostname == hostname, Solicitacao.status.in_(["revogada", "expirada"]),
            Solicitacao.ultima_consulta_agente.is_not(None), Solicitacao.bloqueio_aplicado_em.is_(None))
            .order_by(Solicitacao.id.desc()))
        if encerrada:
            encerrada.bloqueio_aplicado_em = agora()
            registrar(s, "bloqueio_aplicado", "computador", encerrada, "O computador voltou à regra de bloqueio", ip=ip)
        s.commit()
        return {"liberado": False}


# =========================================================================== login da Gestao TI
@app.post("/api/auth/login")
def admin_login(dados: Login, request: Request, response: Response) -> dict:
    """Login unico: qualquer conta do AD (ou local). O que a pessoa ve depende dos modulos liberados."""
    ip = auth.ip_do_cliente(request)
    chave = f"admin:{ip}"
    if auth.bloqueado(chave):
        raise HTTPException(429, "Muitas tentativas. Aguarde 10 minutos.")
    login = ad.normalizar_login(dados.usuario)
    with SessionLocal() as s:
        admin = s.scalar(select(Administrador).where(Administrador.usuario == login))

        def falhar(msg: str, status: int = 401, contar: bool = True):
            if contar:
                auth.registrar_falha(chave)
            registrar(s, "login_falhou", login or "?", detalhe=msg, ip=ip)
            s.commit()
            raise HTTPException(status, msg)

        if admin and admin.origem == "local":
            if not config.LOGIN_LOCAL_HABILITADO:
                falhar("O login local está desativado. Entre com o usuário do AD.")
            if not admin.ativo or not auth.conferir_senha(dados.senha, admin.senha_hash):
                falhar("Usuário ou senha incorretos.")
        else:
            try:
                dados_ad = ad.autenticar(dados.usuario, dados.senha)
            except ad.ErroAD as erro:
                falhar(str(erro), 401 if erro.credencial else 503, contar=erro.credencial)
            if admin and not admin.ativo:
                falhar("Seu acesso à Gestão TI foi desativado.", contar=False)
            if not admin:
                # Qualquer conta ativa do AD entra. Quem e da TI recebe os modulos padrao logo abaixo;
                # os demais entram sem permissao e veem os modulos de e-mail bloqueados ate a TI liberar.
                admin = Administrador(usuario=dados_ad["usuario"], nome=dados_ad["nome"], senha_hash="", origem="ad")
                s.add(admin)
                registrar(s, "admin_ad_primeiro_acesso", dados_ad["usuario"], detalhe=dados_ad["dn"], ip=ip)
            admin.nome, admin.email, admin.unidade = dados_ad["nome"], dados_ad["email"], dados_ad.get("unidade")
            admin.ti = bool(dados_ad["ti"])
        s.flush()
        if auth.eh_ti(admin):
            conceder_padrao_ti(s, admin, "primeiro acesso")
            s.flush()
        admin.ultimo_login = agora()
        auth.limpar_falhas(chave)
        registrar(s, "login", admin.usuario, detalhe=f"Origem: {'AD' if admin.origem == 'ad' else 'local'}", ip=ip)
        s.commit()
        response.set_cookie(auth.COOKIE_ADMIN, auth.token_admin(admin.id), httponly=True, samesite="strict",
                            secure=config.COOKIE_SEGURO, max_age=config.SESSAO_HORAS * 3600)
        return {"usuario": admin.usuario, "nome": admin.nome, "origem": admin.origem, "unidade": admin.unidade}


@app.post("/api/auth/logout")
def admin_logout(response: Response, sessao: str | None = Cookie(default=None, alias=auth.COOKIE_ADMIN)) -> dict:
    try:
        quem = auth.admin_atual(sessao)
        auditoria.registrar("logout", quem.usuario, "Saiu da Gestão TI")
    except HTTPException:
        pass
    response.delete_cookie(auth.COOKIE_ADMIN)
    return {"ok": True}


@app.get("/api/auth/eu")
def admin_eu(admin: Administrador = auth.AdminDep) -> dict:
    return {"usuario": admin.usuario, "nome": admin.nome, "origem": admin.origem, "unidade": admin.unidade,
            "email": admin.email, "ti": auth.eh_ti(admin), "modulos": admin.modulos, "catalogo": modulos.catalogo(),
            "grupos": modulos.grupos(),
            # Modulos que a pessoa ve na tela inicial, mas bloqueados, ate a TI liberar (so para quem nao e da TI)
            "admin_geral": auth.eh_admin_geral(admin), "pode_conceder": auth.pode_conceder(admin),
            "aguardando": [] if (auth.eh_ti(admin) or admin.modulos) else modulos.aguardando()}


# =========================================================================== painel: solicitacoes
@app.get("/api/admin/solicitacoes")
def listar(status: str | None = None, busca: str | None = None, usuario_login: str | None = None,
           hostname: str | None = None, limite: int = 200, admin: Administrador = auth.WifiDep) -> dict:
    with SessionLocal() as s:
        expirar_vencidas(s)
        q = select(Solicitacao)
        if status == "ativas":
            q = q.where(Solicitacao.status == "aprovada")
        elif status:
            q = q.where(Solicitacao.status == status)
        if usuario_login:
            q = q.where(Solicitacao.usuario_login == usuario_login.lower())
        if hostname:
            q = q.where(Solicitacao.hostname == hostname.strip().upper())
        if busca:
            termo = f"%{busca.strip()}%"
            q = q.where(or_(Solicitacao.hostname.ilike(termo), Solicitacao.usuario.ilike(termo),
                            Solicitacao.usuario_login.ilike(termo), Solicitacao.motivo.ilike(termo),
                            Solicitacao.decidido_por.ilike(termo), Solicitacao.bloqueado_por.ilike(termo)))
        q = q.order_by(Solicitacao.id.desc()).limit(min(max(limite, 1), 1000))
        itens = [para_dict(x) for x in s.scalars(q).all()]
        pendentes = s.scalar(select(func.count(Solicitacao.id)).where(Solicitacao.status == "pendente"))
        ativas = s.scalar(select(func.count(Solicitacao.id)).where(Solicitacao.status == "aprovada"))
        return {"itens": itens, "contagem": {"pendentes": pendentes, "ativas": ativas}}


@app.get("/api/admin/solicitacoes/{sol_id}")
def detalhe(sol_id: int, admin: Administrador = auth.WifiDep) -> dict:
    with SessionLocal() as s:
        sol = s.get(Solicitacao, sol_id)
        if not sol:
            raise HTTPException(404, "Solicitação não encontrada.")
        dados = para_dict(sol)
        dados["eventos"] = [{"acao": e.acao, "autor": e.autor, "detalhe": e.detalhe, "ip": e.ip,
                             "criado_em": iso(e.criado_em)} for e in sol.eventos]
        return dados


class Decisao(BaseModel):
    horas: int | None = None
    indeterminado: bool = False
    observacao: str | None = Field(default=None, max_length=1000)


def _obter(s, sol_id: int, status: str) -> Solicitacao:
    sol = s.get(Solicitacao, sol_id, with_for_update=True)
    if not sol:
        raise HTTPException(404, "Solicitação não encontrada.")
    if sol.status != status:
        raise HTTPException(409, f"Esta solicitação não está mais nesse estado (situação atual: {sol.status}).")
    return sol


@app.post("/api/admin/solicitacoes/{sol_id}/aprovar")
def aprovar(sol_id: int, dados: Decisao, request: Request, admin: Administrador = auth.WifiDep) -> dict:
    horas, indeterminado = validar_prazo_admin(dados.horas, dados.indeterminado)
    with SessionLocal() as s:
        sol = _obter(s, sol_id, "pendente")
        aplicar_liberacao(sol, horas, indeterminado, admin.usuario, dados.observacao)
        registrar(s, "aprovada", admin.usuario, sol,
                  f"Liberou {descrever_prazo(horas, indeterminado, sol.valido_ate)}."
                  + (f" Obs.: {sol.observacao_ti}" if sol.observacao_ti else ""), ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


@app.post("/api/admin/solicitacoes/{sol_id}/negar")
def negar(sol_id: int, dados: Decisao, request: Request, admin: Administrador = auth.WifiDep) -> dict:
    with SessionLocal() as s:
        sol = _obter(s, sol_id, "pendente")
        sol.status = "negada"
        sol.decidido_em = agora()
        sol.decidido_por = admin.usuario
        sol.observacao_ti = (dados.observacao or "").strip() or None
        registrar(s, "negada", admin.usuario, sol, sol.observacao_ti, ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


@app.post("/api/admin/solicitacoes/{sol_id}/bloquear")
def bloquear(sol_id: int, dados: Decisao, request: Request, admin: Administrador = auth.WifiDep) -> dict:
    """Encerra uma liberacao ativa: o computador volta a bloquear em ate 5 minutos."""
    with SessionLocal() as s:
        sol = _obter(s, sol_id, "aprovada")
        sol.status = "revogada"
        sol.bloqueado_em = agora()
        sol.bloqueado_por = admin.usuario
        if not sol.indeterminado:
            sol.valido_ate = sol.bloqueado_em
        obs = (dados.observacao or "").strip() or None
        registrar(s, "bloqueada", admin.usuario, sol, f"Bloqueou novamente.{f' Motivo: {obs}' if obs else ''}",
                  ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


@app.post("/api/admin/solicitacoes/{sol_id}/alterar-prazo")
def alterar_prazo(sol_id: int, dados: Decisao, request: Request, admin: Administrador = auth.WifiDep) -> dict:
    """Troca o prazo de uma liberacao ativa (ex.: de indeterminado para 2 horas, ou +4 horas)."""
    horas, indeterminado = validar_prazo_admin(dados.horas, dados.indeterminado)
    with SessionLocal() as s:
        sol = _obter(s, sol_id, "aprovada")
        antes = descrever_prazo(sol.horas_aprovadas, sol.indeterminado, sol.valido_ate)
        sol.indeterminado = indeterminado
        sol.horas_aprovadas = horas
        sol.valido_ate = None if indeterminado else agora() + timedelta(hours=horas)
        registrar(s, "prazo_alterado", admin.usuario, sol,
                  f"Prazo alterado. Antes: liberado {antes}. Agora: liberado {descrever_prazo(horas, indeterminado, sol.valido_ate)}.",
                  ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


class LiberacaoDireta(BaseModel):
    hostname: str
    horas: int | None = None
    indeterminado: bool = False
    observacao: str = Field(min_length=3, max_length=1000)
    usuario_login: str | None = Field(default=None, max_length=80)
    usuario_nome: str | None = Field(default=None, max_length=150)


@app.post("/api/admin/liberacoes")
def liberar_direto(dados: LiberacaoDireta, request: Request, admin: Administrador = auth.WifiDep) -> dict:
    """A TI libera um computador sem pedido pelo portal (ex.: chamado por telefone)."""
    hostname = normalizar_host(dados.hostname)
    horas, indeterminado = validar_prazo_admin(dados.horas, dados.indeterminado)
    with SessionLocal() as s:
        expirar_vencidas(s)
        aberta = s.scalar(select(Solicitacao).where(Solicitacao.hostname == hostname, Solicitacao.status.in_(ABERTOS)))
        if aberta:
            raise HTTPException(409, f"{hostname} já tem uma solicitação {aberta.status} (nº {aberta.id}). Trate-a na lista.")
        login = ad.normalizar_login(dados.usuario_login) if dados.usuario_login else None
        sol = Solicitacao(hostname=hostname, usuario=dados.usuario_nome or (login or f"(TI) {admin.nome}"),
                          usuario_login=login, motivo=dados.observacao.strip(), horas_solicitadas=horas,
                          origem="ti", hostname_fonte="ti")
        aplicar_liberacao(sol, horas, indeterminado, admin.usuario, dados.observacao)
        s.add(sol)
        s.flush()
        registrar(s, "liberacao_direta", admin.usuario, sol,
                  f"Liberou {descrever_prazo(horas, indeterminado, sol.valido_ate)} sem pedido pelo portal."
                  f"{f' Para: {sol.usuario}.' if login else ''} Motivo: {dados.observacao.strip()}",
                  ip=auth.ip_do_cliente(request))
        s.commit()
        return para_dict(sol)


# =========================================================================== painel: computadores e pessoas
@app.get("/api/admin/computadores")
def computadores(busca: str | None = None, limite: int = 300, admin: Administrador = auth.WifiDep) -> list[dict]:
    with SessionLocal() as s:
        expirar_vencidas(s)
        q = select(Computador)
        if busca:
            termo = f"%{busca.strip()}%"
            # nome exibido (historico de pedidos) -> logins, para buscar "joao silva" e achar o computador
            logins_nome = s.scalars(select(Solicitacao.usuario_login).where(Solicitacao.usuario.ilike(termo),
                                    Solicitacao.usuario_login.is_not(None)).distinct()).all()
            q = q.where(or_(Computador.hostname.ilike(termo), Computador.ip.ilike(termo),
                            Computador.usuario_logado.ilike(termo), Computador.usuario_logado.in_(logins_nome)))
        lista = s.scalars(q.order_by(Computador.visto_em.desc()).limit(min(max(limite, 1), 2000))).all()
        nomes = nomes_por_login(s, [pc.usuario_logado for pc in lista])
        resultado = []
        for pc in lista:
            ativa = liberacao_ativa(s, pc.hostname)
            resultado.append({"hostname": pc.hostname, "ip": pc.ip, "visto_em": iso(pc.visto_em),
                              "usuario_logado": pc.usuario_logado, "usuario_nome": nomes.get(pc.usuario_logado),
                              "usuario_desde": iso(pc.usuario_desde),
                              "liberacao": para_dict(ativa) if ativa else None})
        return resultado


@app.get("/api/admin/pessoas")
def pessoas(q: str, admin: Administrador = auth.WifiDep) -> list[dict]:
    """Busca por nome ou login: pessoas que ja usaram o sistema e, se configurado, o diretorio do AD."""
    termo = q.strip()
    if len(termo) < 2:
        return []
    like = f"%{termo}%"
    with SessionLocal() as s:
        linhas = s.execute(
            select(Solicitacao.usuario_login, func.max(Solicitacao.usuario), func.count(Solicitacao.id),
                   func.max(Solicitacao.criado_em))
            .where(Solicitacao.usuario_login.is_not(None),
                   or_(Solicitacao.usuario_login.ilike(like), Solicitacao.usuario.ilike(like)))
            .group_by(Solicitacao.usuario_login).order_by(func.max(Solicitacao.criado_em).desc()).limit(30)).all()
        encontrados: dict[str, dict] = {}
        for login, nome, total, ultima in linhas:
            ultimo_pc = s.scalar(select(Solicitacao.hostname).where(Solicitacao.usuario_login == login)
                                 .order_by(Solicitacao.id.desc()))
            encontrados[login] = {"login": login, "nome": nome, "email": None, "solicitacoes": total,
                                  "ultima_solicitacao": iso(ultima), "ultimo_computador": ultimo_pc, "fonte": "historico"}
        limite_pc = agora() - timedelta(minutes=config.MINUTOS_DETECCAO_COMPUTADOR)
        logado_em = {login: host for host, login in s.execute(
            select(Computador.hostname, Computador.usuario_logado)
            .where(Computador.usuario_logado.is_not(None), Computador.visto_em >= limite_pc,
                   Computador.usuario_logado.ilike(like)))}
        for login, host in logado_em.items():
            if login not in encontrados:
                encontrados[login] = {"login": login, "nome": nomes_por_login(s, [login]).get(login, login),
                                      "email": None, "solicitacoes": 0, "ultima_solicitacao": None,
                                      "ultimo_computador": None, "fonte": "computador"}
        for login, dados_p in encontrados.items():
            dados_p["logado_em"] = logado_em.get(login) or s.scalar(
                select(Computador.hostname).where(Computador.usuario_logado == login, Computador.visto_em >= limite_pc)
                .order_by(Computador.visto_em.desc()))
    for p in ad.buscar_usuarios(termo):
        if p["login"] in encontrados:
            encontrados[p["login"]].update(nome=p["nome"], email=p["email"])
        else:
            encontrados[p["login"]] = {**p, "solicitacoes": 0, "ultima_solicitacao": None,
                                       "ultimo_computador": None, "fonte": "ad", "logado_em": None}
    return list(encontrados.values())[:40]


# =========================================================================== painel: registro de atividades
def _filtro_eventos(busca: str | None, acao: str | None, desde: str | None, ate: str | None):
    condicoes = []
    if busca:
        termo = f"%{busca.strip()}%"
        condicoes.append(or_(Evento.autor.ilike(termo), Evento.hostname.ilike(termo), Evento.detalhe.ilike(termo),
                             Evento.ip.ilike(termo)))
    if acao:
        condicoes.append(Evento.acao.in_([a.strip() for a in acao.split(",") if a.strip()]))
    try:
        if desde:
            condicoes.append(Evento.criado_em >= datetime.fromisoformat(desde).replace(tzinfo=FUSO))
        if ate:
            condicoes.append(Evento.criado_em < datetime.fromisoformat(ate).replace(tzinfo=FUSO) + timedelta(days=1))
    except ValueError:
        raise HTTPException(422, "Data inválida. Use o formato AAAA-MM-DD.")
    return and_(*condicoes) if condicoes else None


def _evento_dict(e: Evento) -> dict:
    return {"id": e.id, "solicitacao_id": e.solicitacao_id, "acao": e.acao, "autor": e.autor,
            "hostname": e.hostname, "ip": e.ip, "detalhe": e.detalhe, "criado_em": iso(e.criado_em)}


@app.get("/api/admin/eventos")
def eventos(busca: str | None = None, acao: str | None = None, desde: str | None = None, ate: str | None = None,
            limite: int = 200, admin: Administrador = auth.WifiDep) -> list[dict]:
    with SessionLocal() as s:
        q = select(Evento)
        filtro = _filtro_eventos(busca, acao, desde, ate)
        if filtro is not None:
            q = q.where(filtro)
        itens = s.scalars(q.order_by(Evento.id.desc()).limit(min(max(limite, 1), 1000))).all()
        return [_evento_dict(e) for e in itens]


NOMES_ACAO = {
    "solicitada": "Solicitação enviada", "cancelada": "Solicitação cancelada", "aprovada": "Liberado",
    "negada": "Negado", "bloqueada": "Bloqueado pela TI", "revogada": "Bloqueado pela TI",
    "expirada": "Bloqueio automático (prazo)", "prazo_alterado": "Prazo alterado", "estendida": "Prazo estendido",
    "liberacao_direta": "Liberação direta pela TI", "aplicada": "Liberação aplicada no computador",
    "bloqueio_aplicado": "Bloqueio aplicado no computador", "login": "Entrada no painel",
    "login_falhou": "Falha de entrada no painel", "portal_login": "Entrada no portal",
    "portal_login_falhou": "Falha de entrada no portal", "admin_criado": "Usuário do painel criado",
    "admin_senha": "Senha alterada", "admin_ativado": "Usuário do painel ativado",
    "admin_desativado": "Usuário do painel desativado", "admin_ad_primeiro_acesso": "Primeiro acesso ao painel (AD)",
    "sessao_iniciada": "Usuário logado no computador", "sessao_encerrada": "Usuário saiu do computador",
    "usuario_adicionado": "Usuário adicionado à Gestão TI", "permissoes_alteradas": "Permissões alteradas",
}


@app.get("/api/admin/eventos.csv")
def eventos_csv(busca: str | None = None, acao: str | None = None, desde: str | None = None, ate: str | None = None,
                admin: Administrador = auth.WifiDep) -> StreamingResponse:
    with SessionLocal() as s:
        q = select(Evento)
        filtro = _filtro_eventos(busca, acao, desde, ate)
        if filtro is not None:
            q = q.where(filtro)
        itens = s.scalars(q.order_by(Evento.id.desc()).limit(50000)).all()
        saida = io.StringIO()
        w = csv.writer(saida, delimiter=";")
        w.writerow(["Data e hora", "Ação", "Quem", "Computador", "IP", "Solicitação", "Detalhe"])
        for e in itens:
            w.writerow([fmt(e.criado_em), NOMES_ACAO.get(e.acao, e.acao), e.autor, e.hostname or "", e.ip or "",
                        e.solicitacao_id or "", e.detalhe or ""])
        registrar(s, "exportou_log", admin.usuario, detalhe=f"{len(itens)} registros")
        s.commit()
    nome = f"registro-rede-sem-fio-{datetime.now(FUSO):%Y%m%d-%H%M}.csv"
    return StreamingResponse(iter(["﻿" + saida.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})


# =========================================================================== usuarios e permissoes
UsuariosDep = auth.exigir_modulo("usuarios")


class NovoUsuarioAD(BaseModel):
    login: str = Field(min_length=2, max_length=80)
    nome: str | None = Field(default=None, max_length=150)
    modulos: list[str] = []


class NovoUsuarioLocal(BaseModel):
    usuario: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._\-]+$")
    nome: str = Field(min_length=3, max_length=150)
    senha: str = Field(min_length=8, max_length=200)
    modulos: list[str] = []


class AlterarUsuario(BaseModel):
    senha: str | None = Field(default=None, min_length=8, max_length=200)
    ativo: bool | None = None
    modulos: list[str] | None = None
    pode_conceder: bool | None = None   # so o adm geral altera


def _usuario_dict(s, a: Administrador) -> dict:
    return {"id": a.id, "usuario": a.usuario, "nome": a.nome, "ativo": a.ativo, "origem": a.origem,
            "email": a.email, "unidade": a.unidade, "ti": auth.eh_ti(a), "ultimo_login": iso(a.ultimo_login),
            "modulos": auth.modulos_de(s, a), "admin_geral": auth.eh_admin_geral(a), "pode_conceder": auth.pode_conceder(a)}


def _validar_modulos(lista: list[str], ti: bool) -> list[str]:
    desconhecidos = [m for m in lista if m not in modulos.MODULOS]
    if desconhecidos:
        raise HTTPException(422, f"Módulo desconhecido: {', '.join(desconhecidos)}.")
    indisponiveis = [modulos.MODULOS[m]["nome"] for m in lista if m not in modulos.permitidos_para()]
    if indisponiveis:
        raise HTTPException(422, "Estes módulos ainda não estão disponíveis: " + ", ".join(indisponiveis) + ".")
    return list(dict.fromkeys(lista))


def _gravar_modulos(s, alvo: Administrador, lista: list[str], por: str) -> None:
    atuais = {p.modulo: p for p in s.scalars(select(Permissao).where(Permissao.usuario_id == alvo.id)).all()}
    for m, p in atuais.items():
        if m not in lista:
            s.delete(p)
    for m in lista:
        if m not in atuais:
            s.add(Permissao(usuario_id=alvo.id, modulo=m, concedido_por=por))


def _checar_alvo(admin: Administrador, alvo: Administrador, mexe_modulos: bool = False) -> None:
    """Regras de quem pode alterar quem (alem de ter autorizacao para conceder, verificada na rota)."""
    if alvo.id == admin.id:
        raise HTTPException(409, "Você não pode alterar as próprias permissões nem o próprio acesso.")
    if auth.eh_admin_geral(alvo):
        if not auth.eh_admin_geral(admin):
            raise HTTPException(403, "Só o adm geral pode alterar outro adm geral.")
        if mexe_modulos:
            raise HTTPException(409, "O adm geral sempre tem todos os módulos.")


def _alterar_delegacao(s, admin: Administrador, alvo: Administrador, valor: bool | None, ip: str) -> None:
    """Liga/desliga 'pode conceder permissoes'. So o adm geral."""
    if valor is None or bool(alvo.pode_conceder) == valor:
        return
    if not auth.eh_admin_geral(admin):
        raise HTTPException(403, "Só o adm geral pode autorizar alguém a conceder permissões.")
    alvo.pode_conceder = valor
    registrar(s, "delegacao_alterada", admin.usuario, ip=ip,
              detalhe=f"{alvo.usuario}: {'pode' if valor else 'não pode mais'} conceder permissões a outros usuários")


@app.get("/api/usuarios")
def listar_usuarios(admin: Administrador = UsuariosDep) -> list[dict]:
    with SessionLocal() as s:
        lista = s.scalars(select(Administrador).order_by(Administrador.ativo.desc(), Administrador.nome)).all()
        return [_usuario_dict(s, a) for a in lista]


def _sem_acento(texto) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower()) if unicodedata.category(c) != "Mn")


def _dn_na_ou_ti(dn: str) -> bool:
    ou = (config.AD_OU_PERMITIDA or "").strip().lower()
    return bool(ou) and dn.lower().endswith("," + ou)


def _registro_dict(s, a: Administrador | None) -> dict | None:
    """Situacao da pessoa dentro da Gestao TI (None = nunca entrou nem foi cadastrada)."""
    if not a:
        return None
    return {"id": a.id, "ativo": a.ativo, "ultimo_login": iso(a.ultimo_login), "modulos": auth.modulos_de(s, a),
            "admin_geral": auth.eh_admin_geral(a), "pode_conceder": auth.pode_conceder(a)}


def _registros_em_lote(s) -> dict[str, dict]:
    """Situacao na Gestao TI de todos os cadastrados, com 2 consultas (em vez de uma por pessoa)."""
    cadastros = s.scalars(select(Administrador)).all()
    concedidos: dict[int, list[str]] = {}
    for uid, mod in s.execute(select(Permissao.usuario_id, Permissao.modulo)).all():
        concedidos.setdefault(uid, []).append(mod)
    def mods(a):
        return modulos.permitidos_para() if auth.eh_admin_geral(a) else modulos.efetivos(auth.eh_ti(a), concedidos.get(a.id, []))
    return {a.usuario: {"adm": a, "reg": {"id": a.id, "ativo": a.ativo, "ultimo_login": iso(a.ultimo_login),
                                          "modulos": mods(a), "admin_geral": auth.eh_admin_geral(a),
                                          "pode_conceder": auth.pode_conceder(a)}}
            for a in cadastros}


def _montar_diretorio(atualizar: bool) -> tuple[list[dict], str | None]:
    """Junta as contas do AD com os cadastros da Gestao TI (usuarios locais entram tambem)."""
    aviso = None
    try:
        contas, grupo_ti = ad_senhas.diretorio(atualizar)
    except ad.ErroAD as erro:
        contas, grupo_ti, aviso = None, set(), f"Não foi possível ler o AD agora: {erro} Mostrando só quem já está cadastrado na Gestão TI."
    with SessionLocal() as s:
        cadastros = _registros_em_lote(s)
    itens, vistos = [], set()
    for c in contas or []:
        cad = cadastros.get(c["login"])
        vistos.add(c["login"])
        itens.append({**c, "origem": "ad", "no_ad": True,
                      "ti": _dn_na_ou_ti(c["dn"]) or c["login"] in grupo_ti or bool(cad and cad["adm"].ti),
                      "registro": cad["reg"] if cad else None})
    for login, cad in cadastros.items():
        if login in vistos:
            continue
        a = cad["adm"]
        local = a.origem == "local"
        item = {"login": login, "nome": a.nome, "email": a.email, "unidade": a.unidade or "", "dn": "",
                "cpf": None, "cpf_digitos": "", "matricula": None, "descricao": None,
                # Local: ativo = acesso ao sistema. Do AD mas fora da lista: so sabemos o cadastro se o AD nao respondeu
                "ativo": a.ativo if (local or contas is None) else False,
                "bloqueado": False, "senha_expirada": False, "deve_trocar_senha": False, "protegida": False,
                "senha_trocada_em": None, "ultimo_logon": None,
                "origem": a.origem, "no_ad": local or contas is None, "ti": auth.eh_ti(a), "registro": cad["reg"]}
        item["_busca"] = ad_senhas._chave_busca(item)
        itens.append(item)
    return itens, aviso


def _bate_busca(u: dict, q: str, digitos: str) -> bool:
    if not q:
        return True
    if q in (u.get("_busca") or ""):   # texto de busca ja preparado quando a lista do AD foi lida
        return True
    # CPF: compara so os numeros (no AD pode estar com ou sem pontos)
    return len(digitos) >= 3 and digitos == re.sub(r"[\s.\-/]", "", q) and digitos in (u.get("cpf_digitos") or "")


def _bate_filtro(u: dict, filtro: str) -> bool:
    reg = u["registro"]
    com_acesso = bool(reg and reg["ativo"] and reg["modulos"])
    return {"todos": True, "com_acesso": com_acesso, "sem_acesso": not com_acesso, "ti": u["ti"],
            "bloqueados": u["bloqueado"], "locais": u["origem"] == "local", "ad": u["origem"] == "ad"}.get(filtro, True)


@app.get("/api/usuarios/diretorio")
def diretorio_usuarios(q: str = "", situacao: str = "ativos", filtro: str = "todos", limite: int = 100,
                       atualizar: bool = False, admin: Administrador = UsuariosDep) -> dict:
    """Todos os usuarios do AD e os locais, com busca por login, nome, CPF (employeeID) ou matricula (initials)."""
    itens, aviso = _montar_diretorio(atualizar)
    q = q.strip()
    digitos = re.sub(r"\D", "", q)
    q_norm = _sem_acento(q)
    achados = [u for u in itens if _bate_busca(u, q_norm, digitos) and _bate_filtro(u, filtro)]
    contagem = {"ativos": sum(1 for u in achados if u["ativo"]), "desativados": sum(1 for u in achados if not u["ativo"])}
    da_aba = sorted((u for u in achados if u["ativo"] == (situacao != "desativados")), key=lambda u: u.get("_ordem") or _sem_acento(u["nome"]))
    limite = max(20, min(limite, 2000))
    for u in da_aba:
        u.pop("cpf_digitos", None)   # o CPF completo nao sai do servidor
        u.pop("dn", None)
        u.pop("_busca", None)
        u.pop("_ordem", None)
    return {"aviso": aviso, "contagem": contagem, "total": len(da_aba), "itens": da_aba[:limite],
            "eu": admin.usuario}


class ModulosAD(BaseModel):
    modulos: list[str] = []
    pode_conceder: bool | None = None   # so o adm geral altera


@app.put("/api/usuarios/ad/{login}/modulos")
def modulos_usuario_ad(login: str, dados: ModulosAD, request: Request, admin: Administrador = auth.ConcederDep) -> dict:
    """Define os modulos de uma pessoa do AD. Se ela nunca entrou, o cadastro e criado agora."""
    login = ad.normalizar_login(login)
    ip = auth.ip_do_cliente(request)
    with SessionLocal() as s:
        alvo = s.scalar(select(Administrador).where(Administrador.usuario == login))
        if alvo and alvo.origem == "local":
            raise HTTPException(409, "Esse login é de um usuário local.")
        if not alvo:
            try:
                info = ad.consultar_usuario(login)
            except ad.ErroAD as erro:
                raise HTTPException(502, str(erro))
            if not info:
                raise HTTPException(404, f"O usuário '{login}' não foi encontrado no AD.")
            alvo = Administrador(usuario=login, nome=info["nome"], email=info["email"], unidade=info["unidade"],
                                 origem="ad", ti=bool(info["ti"]), senha_hash="")
            s.add(alvo)
            s.flush()
            registrar(s, "usuario_adicionado", admin.usuario, ip=ip, detalhe=login)
        _checar_alvo(admin, alvo, mexe_modulos=True)
        _alterar_delegacao(s, admin, alvo, dados.pode_conceder, ip)
        lista = _validar_modulos(dados.modulos, auth.eh_ti(alvo))
        antes = modulos.efetivos(auth.eh_ti(alvo), s.scalars(select(Permissao.modulo).where(Permissao.usuario_id == alvo.id)).all())
        _gravar_modulos(s, alvo, lista, admin.usuario)
        registrar(s, "permissoes_alteradas", admin.usuario, ip=ip,
                  detalhe=f"{alvo.usuario}: antes [{', '.join(antes) or 'nenhum'}] · agora [{', '.join(lista) or 'nenhum'}]")
        s.commit()
        return _registro_dict(s, alvo)


@app.get("/api/usuarios/buscar-ad")
def buscar_usuario_ad(q: str, admin: Administrador = UsuariosDep) -> list[dict]:
    """Busca no AD (precisa da conta de servico) para adicionar alguem a Gestao TI."""
    if len(q.strip()) < 2:
        return []
    return ad.buscar_usuarios(q)


@app.post("/api/usuarios")
def adicionar_usuario_ad(dados: NovoUsuarioAD, request: Request, admin: Administrador = auth.ConcederDep) -> dict:
    login = ad.normalizar_login(dados.login)
    info = None
    try:
        info = ad.consultar_usuario(login)
    except ad.ErroAD:
        info = None  # sem conta de servico: cadastra pelo login informado; a OU e confirmada no primeiro login
    if info is None and config.AD_BIND_USUARIO and config.AD_BIND_SENHA:
        raise HTTPException(404, f"O usuário '{login}' não foi encontrado no AD.")
    ti = bool(info and info["ti"])
    lista = _validar_modulos(dados.modulos, ti)
    with SessionLocal() as s:
        if s.scalar(select(Administrador).where(Administrador.usuario == login)):
            raise HTTPException(409, "Esse usuário já está cadastrado. Edite as permissões dele na lista.")
        novo = Administrador(usuario=login, nome=(info or {}).get("nome") or (dados.nome or login).strip(),
                             email=(info or {}).get("email"), unidade=(info or {}).get("unidade"),
                             origem="ad", ti=ti, senha_hash="")
        s.add(novo)
        s.flush()
        _gravar_modulos(s, novo, lista, admin.usuario)
        registrar(s, "usuario_adicionado", admin.usuario, ip=auth.ip_do_cliente(request),
                  detalhe=f"{login} · módulos: {', '.join(modulos.MODULOS[m]['nome'] for m in lista) or 'nenhum'}")
        s.commit()
        return _usuario_dict(s, novo)


@app.post("/api/usuarios/local")
def adicionar_usuario_local(dados: NovoUsuarioLocal, request: Request, admin: Administrador = auth.ConcederDep) -> dict:
    lista = _validar_modulos(dados.modulos, True)
    with SessionLocal() as s:
        usuario = dados.usuario.lower()
        if s.scalar(select(Administrador).where(Administrador.usuario == usuario)):
            raise HTTPException(409, "Já existe um usuário com esse login.")
        novo = Administrador(usuario=usuario, nome=dados.nome.strip(), origem="local", ti=True,
                             senha_hash=auth.gerar_hash(dados.senha))
        s.add(novo)
        s.flush()
        _gravar_modulos(s, novo, lista, admin.usuario)
        registrar(s, "admin_criado", admin.usuario, detalhe=f"Usuário local {usuario}", ip=auth.ip_do_cliente(request))
        s.commit()
        return _usuario_dict(s, novo)


@app.patch("/api/usuarios/{usuario_id}")
def alterar_usuario(usuario_id: int, dados: AlterarUsuario, request: Request, admin: Administrador = auth.ConcederDep) -> dict:
    ip = auth.ip_do_cliente(request)
    with SessionLocal() as s:
        alvo = s.get(Administrador, usuario_id)
        if not alvo:
            raise HTTPException(404, "Usuário não encontrado.")
        if dados.modulos is not None or dados.ativo is not None or dados.pode_conceder is not None:
            _checar_alvo(admin, alvo, mexe_modulos=dados.modulos is not None)
        elif auth.eh_admin_geral(alvo) and not auth.eh_admin_geral(admin):
            raise HTTPException(403, "Só o adm geral pode alterar outro adm geral.")
        _alterar_delegacao(s, admin, alvo, dados.pode_conceder, ip)
        if dados.senha:
            if alvo.origem == "ad":
                raise HTTPException(409, "A senha de usuários do AD é trocada no próprio Windows/AD.")
            alvo.senha_hash = auth.gerar_hash(dados.senha)
            registrar(s, "admin_senha", admin.usuario, detalhe=alvo.usuario, ip=ip)
        if dados.modulos is not None:
            lista = _validar_modulos(dados.modulos, auth.eh_ti(alvo))
            antes = modulos.efetivos(auth.eh_ti(alvo), s.scalars(select(Permissao.modulo).where(Permissao.usuario_id == alvo.id)).all())
            _gravar_modulos(s, alvo, lista, admin.usuario)
            registrar(s, "permissoes_alteradas", admin.usuario, ip=ip,
                      detalhe=f"{alvo.usuario}: antes [{', '.join(antes) or 'nenhum'}] · agora [{', '.join(lista) or 'nenhum'}]")
        if dados.ativo is not None:
            alvo.ativo = dados.ativo
            registrar(s, "admin_ativado" if dados.ativo else "admin_desativado", admin.usuario, detalhe=alvo.usuario, ip=ip)
        s.commit()
        return _usuario_dict(s, alvo)


@app.get("/api/usuarios/eventos")
def eventos_usuarios(admin: Administrador = UsuariosDep) -> list[dict]:
    acoes = ["login", "login_falhou", "usuario_adicionado", "admin_criado", "admin_senha", "admin_ativado",
             "admin_desativado", "admin_ad_primeiro_acesso", "permissoes_alteradas", "configuracao_alterada",
             "senha_redefinida", "conta_desbloqueada", "conta_reativada", "senha_reset_falhou", "delegacao_alterada"]
    with SessionLocal() as s:
        itens = s.scalars(select(Evento).where(Evento.acao.in_(acoes)).order_by(Evento.id.desc()).limit(300)).all()
        return [_evento_dict(e) for e in itens]


# =========================================================================== paginas
app.mount("/static", StaticFiles(directory=STATIC), name="static")


app.include_router(email_lote.router)
app.include_router(configuracoes_api.router)
app.include_router(senhas_api.router)
app.include_router(visitante_api.router)
app.include_router(logs_api.router)


def _pagina(nome: str) -> FileResponse:
    return FileResponse(STATIC / nome, headers={"Cache-Control": "no-cache"})


@app.get("/", response_model=None)
def pagina_inicio(request: Request):
    """Login da Gestao TI e tela inicial com os modulos liberados para a pessoa.
    Compatibilidade com o sistema antigo do Wi-Fi: o endereco antigo (wifiaprovacao...) e o atalho da GPO
    (/?h=COMPUTADOR) levam ao portal do Wi-Fi, como antes."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if "h" in request.query_params or any(host.startswith(h) for h in config.HOSTS_PORTAL_WIFI):
        destino = "/wifi" + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(destino, status_code=307)
    return _pagina("inicio.html")


@app.get("/wifi")
def pagina_portal_wifi() -> FileResponse:
    """Portal publico do Wi-Fi: qualquer usuario do AD pede a liberacao do computador."""
    return _pagina("portal.html")


@app.get("/m/wifi")
def pagina_wifi() -> FileResponse:
    return _pagina("wifi.html")


@app.get("/m/email")
def pagina_email() -> FileResponse:
    return _pagina("email.html")


@app.get("/m/usuarios")
def pagina_usuarios() -> FileResponse:
    return _pagina("usuarios.html")


@app.get("/m/rede-sem-fio")
def pagina_rede_sem_fio() -> FileResponse:
    return _pagina("rede-sem-fio.html")


@app.get("/m/wifi-visitante")
def pagina_wifi_visitante() -> FileResponse:
    return _pagina("wifi-visitante.html")


@app.get("/m/senhas")
def pagina_senhas_antiga() -> RedirectResponse:
    """O Reset de senha agora fica dentro da Gestao de Usuarios."""
    return RedirectResponse("/m/usuarios")


@app.get("/m/configuracoes")
def pagina_configuracoes() -> FileResponse:
    return _pagina("configuracoes.html")


@app.get("/m/logs")
def pagina_logs() -> FileResponse:
    return _pagina("logs.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/admin")
def pagina_admin_antiga() -> RedirectResponse:
    return RedirectResponse("/m/wifi")
