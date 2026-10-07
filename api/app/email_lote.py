"""Modulo de e-mails em lote da Gestao TI.

Duas origens de destinatarios, com permissoes separadas:
  - ad:    usuarios ativos do AD que tem e-mail (modulo email_ad, so TI)
  - lista: arquivo TXT enviado pelo usuario (modulo email_lista, pode ser liberado para fora da TI)

O envio e feito em blocos por uma thread em segundo plano. Cada destinatario fica gravado no banco
com a situacao (pendente, enviado, erro), entao o envio continua de onde parou se o servidor reiniciar.
Modo automatico: os blocos saem sozinhos com um intervalo. Modo manual: a pessoa libera bloco por bloco.
"""
import base64
import csv
import html
import io
import logging
import mimetypes
import re
import smtplib
import threading
import time
import uuid
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update

from . import ad, ajustes, auditoria, auth, config
from .db import (Administrador, Disparo, DisparoAnexo, DisparoDestinatario,
                 Evento, SessionLocal, agora)

log = logging.getLogger("email")
router = APIRouter(prefix="/api/email")

EmailDep = auth.exigir_modulo("email_ad", "email_lista")
MODULO_DA_ORIGEM = {"ad": "email_ad", "lista": "email_lista"}
EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")
EXTENSOES_BLOQUEADAS = {".exe", ".bat", ".cmd", ".com", ".js", ".vbs", ".vbe", ".ps1", ".scr", ".msi", ".jar",
                        ".hta", ".lnk", ".reg", ".dll", ".cpl", ".wsf"}
NOMES_STATUS = {"rascunho": "Rascunho", "enviando": "Enviando", "aguardando_bloco": "Aguardando liberar o próximo bloco",
                "pausado": "Pausado", "concluido": "Concluído", "cancelado": "Cancelado"}


# =========================================================================== utilitarios
def _evento(s, acao: str, autor: str, detalhe: str, ip: str | None = None) -> None:
    s.add(Evento(acao=acao, autor=autor[:150], detalhe=detalhe, ip=ip))


def _iso(dt):
    return dt.isoformat() if dt else None


def _exigir_origem(admin: Administrador, origem: str) -> None:
    modulo = MODULO_DA_ORIGEM.get(origem)
    if not modulo:
        raise HTTPException(422, "Origem inválida.")
    if modulo not in admin.modulos:
        raise HTTPException(403, "Você não tem permissão para esse tipo de disparo.")


def _visiveis(admin: Administrador):
    """Disparos que a pessoa pode ver: da TI, todos das origens liberadas; fora da TI, so os proprios."""
    origens = [o for o, m in MODULO_DA_ORIGEM.items() if m in admin.modulos]
    cond = Disparo.origem.in_(origens)
    if not auth.eh_ti(admin):
        cond = cond & (Disparo.criado_por == admin.usuario)
    return cond


def _obter(s, admin: Administrador, disparo_id: int, para_escrita: bool = False) -> Disparo:
    q = select(Disparo).where(Disparo.id == disparo_id, _visiveis(admin))
    if para_escrita:
        q = q.with_for_update()
    d = s.scalar(q)
    if not d:
        raise HTTPException(404, "Disparo não encontrado.")
    return d


_TAGS_PERIGOSAS = re.compile(r"<\s*(script|iframe|object|embed|form|meta|link|base)\b[^>]*>.*?<\s*/\s*\1\s*>|"
                             r"<\s*(script|iframe|object|embed|form|meta|link|base)\b[^>]*/?>", re.I | re.S)
_ATRIBUTOS_EVENTO = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_JS_URL = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.I)


def limpar_html(corpo: str) -> str:
    """Remove scripts, formularios e atributos de evento do corpo da mensagem."""
    corpo = _TAGS_PERIGOSAS.sub("", corpo or "")
    corpo = _ATRIBUTOS_EVENTO.sub("", corpo)
    return _JS_URL.sub(r'\1="#"', corpo)


def html_para_texto(corpo: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>", "\n", corpo)
    t = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "• ", t)
    t = re.sub(r"(?is)<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 (\1)", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _substituir(texto: str, nome: str, email: str, em_html: bool) -> str:
    f = html.escape if em_html else (lambda v: v)
    return texto.replace("{nome}", f(nome)).replace("{email}", f(email))


# ---------------------------------------------------------------- imagens dentro do corpo da mensagem
# O editor envia a imagem para /api/email/imagens e coloca <img src="/api/email/imagens/NOME">.
# Na hora do envio, cada uma dessas imagens vai embutida no e-mail (cid:), entao aparece mesmo
# para quem nao tem acesso a rede da prefeitura.
TIPOS_IMAGEM = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
NOME_IMAGEM_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
SRC_IMAGEM_RE = re.compile(r"""src=(["'])(?:https?://[^"'/]+)?/api/email/imagens/([0-9a-f]{32}\.(?:png|jpg|gif|webp))\1""", re.I)
DATA_URI_RE = re.compile(r"""src=(["'])data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)\1""", re.I)


def _pasta_imagens() -> Path:
    pasta = Path(config.DADOS_DIR) / "imagens"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def salvar_imagem(dados: bytes, tipo: str) -> str:
    nome = uuid.uuid4().hex + TIPOS_IMAGEM[tipo]
    (_pasta_imagens() / nome).write_bytes(dados)
    return nome


def extrair_imagens_coladas(corpo: str) -> str:
    """Imagens coladas como data:base64 (ex.: copiadas do Word) viram arquivos, como as enviadas pelo editor.
    Muitos programas de e-mail (Gmail, Outlook) nao mostram imagens em base64 dentro do HTML."""
    def trocar(m):
        try:
            dados = base64.b64decode(re.sub(r"\s", "", m.group(3)))
        except ValueError:
            return m.group(0)
        if len(dados) > ajustes.obter("IMAGEM_MAX_MB") * 1024 * 1024:
            return m.group(0)
        return f'src="/api/email/imagens/{salvar_imagem(dados, m.group(2).lower())}"'
    return DATA_URI_RE.sub(trocar, corpo or "")


def _ler_imagem(nome: str, cache: dict) -> bytes | None:
    if nome not in cache:
        caminho = _pasta_imagens() / nome
        cache[nome] = caminho.read_bytes() if NOME_IMAGEM_RE.match(nome) and caminho.exists() else None
    return cache[nome]


def montar_mensagem(assunto: str, corpo_html: str, remetente_nome: str | None, responder_para: str | None,
                    email: str, nome: str | None, anexos: list[tuple[str, str, bytes]],
                    cache_imagens: dict | None = None, remetente_email: str | None = None) -> EmailMessage:
    nome_txt = (nome or "").strip() or email.split("@")[0]
    corpo = _substituir(corpo_html, nome_txt, email, True)
    pagina = ('<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
              f'line-height:1.5;color:#14152b">{corpo}</body></html>')
    msg = EmailMessage()
    msg["Subject"] = _substituir(assunto, nome_txt, email, False)
    remetente = remetente_email or ajustes.obter("SMTP_FROM")
    msg["From"] = formataddr((remetente_nome or ajustes.obter("SMTP_FROM_NOME"), remetente))
    msg["To"] = formataddr((nome, email)) if nome else email
    if responder_para:
        msg["Reply-To"] = responder_para
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(remetente.split("@")[-1] or None))
    # Imagens do editor: troca o endereco por cid: e embute a imagem na mensagem
    cache = cache_imagens if cache_imagens is not None else {}
    embutidas: dict[str, str] = {}

    def para_cid(m):
        arquivo = m.group(2).lower()
        if _ler_imagem(arquivo, cache) is None:
            return m.group(0)
        if arquivo not in embutidas:
            embutidas[arquivo] = make_msgid(domain="gestaoti")
        return f'src="cid:{embutidas[arquivo][1:-1]}"'
    pagina = SRC_IMAGEM_RE.sub(para_cid, pagina)
    msg.set_content(html_para_texto(corpo))
    msg.add_alternative(pagina, subtype="html")
    if embutidas:
        parte_html = msg.get_body(("html",))
        for arquivo, cid in embutidas.items():
            sub = {"jpg": "jpeg"}.get(arquivo.rsplit(".", 1)[1], arquivo.rsplit(".", 1)[1])
            parte_html.add_related(cache[arquivo], maintype="image", subtype=sub, cid=cid, filename=arquivo)
    for nome_arq, tipo, dados in anexos:
        principal, _, secundario = (tipo or "application/octet-stream").partition("/")
        msg.add_attachment(dados, maintype=principal, subtype=secundario or "octet-stream", filename=nome_arq)
    return msg


def smtp_atual() -> dict:
    return {k: ajustes.obter(k) for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_SEGURANCA", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}


def smtp_configurado() -> bool:
    return bool(ajustes.obter("SMTP_HOST") and ajustes.obter("SMTP_FROM"))


def abrir_smtp(cfg: dict | None = None, timeout: int = 30) -> smtplib.SMTP:
    """Conecta no servidor de e-mail com as configuracoes atuais (ou as informadas, para testar antes de salvar)."""
    c = cfg or smtp_atual()
    if not (c.get("SMTP_HOST") and c.get("SMTP_FROM")):
        raise RuntimeError("O servidor de e-mail não está configurado (módulo Configurações).")
    porta = int(c.get("SMTP_PORT") or 587)
    if c.get("SMTP_SEGURANCA") == "ssl":
        smtp = smtplib.SMTP_SSL(c["SMTP_HOST"], porta, timeout=timeout)
    else:
        smtp = smtplib.SMTP(c["SMTP_HOST"], porta, timeout=timeout)
        if c.get("SMTP_SEGURANCA") == "starttls":
            smtp.starttls()
    if c.get("SMTP_USER"):
        smtp.login(c["SMTP_USER"], c.get("SMTP_PASSWORD") or "")
    return smtp


def ler_lista(conteudo: bytes) -> dict:
    """Le um TXT com um e-mail por linha. Aceita 'email', 'Nome;email', 'email;Nome', 'Nome <email>'
    e varios e-mails na mesma linha separados por virgula, ponto e virgula ou espaco."""
    try:
        texto = conteudo.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = conteudo.decode("latin-1")
    validos, invalidos, vistos, duplicados = [], [], set(), 0
    for n, linha in enumerate(texto.splitlines(), start=1):
        bruta = linha.strip()
        if not bruta or bruta.startswith("#"):
            continue
        achados = EMAIL_RE.findall(bruta)
        if not achados:
            invalidos.append({"linha": n, "texto": bruta[:200]})
            continue
        nome = None
        if len(achados) == 1:
            resto = bruta.replace(achados[0], " ")
            resto = re.sub(r"[<>;,\t\"']", " ", resto).strip()
            nome = re.sub(r"\s+", " ", resto)[:200] or None
        for e in achados:
            e = e.strip().lower()
            if e in vistos:
                duplicados += 1
                continue
            vistos.add(e)
            validos.append({"email": e, "nome": nome})
    return {"validos": validos, "invalidos": invalidos[:500], "total_invalidos": len(invalidos), "duplicados": duplicados}


# =========================================================================== configuracao e origem AD
@router.get("/config")
def configuracao(admin: Administrador = EmailDep) -> dict:
    return {"smtp_configurado": smtp_configurado(), "remetente": ajustes.obter("SMTP_FROM"),
            "remetente_nome": ajustes.obter("SMTP_FROM_NOME"), "anexos_max_mb": ajustes.obter("ANEXOS_MAX_MB"),
            "imagem_max_mb": ajustes.obter("IMAGEM_MAX_MB"),
            "max_emails": ajustes.obter("LISTA_MAX_EMAILS"), "pausa_ms": ajustes.obter("ENVIO_PAUSA_MS"),
            "pode_configurar": "configuracoes" in admin.modulos,
            "pode_ad": "email_ad" in admin.modulos, "pode_lista": "email_lista" in admin.modulos,
            "busca_ad": bool(config.AD_BIND_USUARIO and config.AD_BIND_SENHA)}


_cache_ad: dict = {"dados": None, "em": 0.0}


@router.get("/ad/usuarios")
def usuarios_ad(atualizar: bool = False, admin: Administrador = auth.exigir_modulo("email_ad")) -> dict:
    idade = time.time() - _cache_ad["em"]
    if atualizar or _cache_ad["dados"] is None or idade > config.CACHE_AD_MINUTOS * 60:
        try:
            _cache_ad["dados"] = ad.listar_usuarios_com_email()
        except ad.ErroAD as erro:
            raise HTTPException(503, str(erro))
        _cache_ad["em"] = time.time()
    dados = _cache_ad["dados"]
    unidades = sorted({u["unidade"] for u in dados if u["unidade"]}, key=str.lower)
    return {"usuarios": dados, "unidades": unidades, "total": len(dados),
            "atualizado_em": time.strftime("%d/%m/%Y %H:%M", time.localtime(_cache_ad["em"]))}


@router.post("/lista/ler")
async def ler_arquivo(arquivo: UploadFile = File(...), admin: Administrador = auth.exigir_modulo("email_lista")) -> dict:
    nome = (arquivo.filename or "").lower()
    if not (nome.endswith(".txt") or nome.endswith(".csv")):
        raise HTTPException(422, "Envie um arquivo .txt (um e-mail por linha).")
    conteudo = await arquivo.read(20 * 1024 * 1024 + 1)
    if len(conteudo) > 20 * 1024 * 1024:
        raise HTTPException(413, "Arquivo muito grande (máximo 20 MB).")
    resultado = ler_lista(conteudo)
    auditoria.registrar("email_lista_lida", admin.usuario,
                        f"Arquivo {arquivo.filename}: {len(resultado['validos'])} e-mails válidos")
    if len(resultado["validos"]) > ajustes.obter("LISTA_MAX_EMAILS"):
        raise HTTPException(422, f"A lista tem {len(resultado['validos'])} e-mails. O máximo por disparo é "
                                 f"{ajustes.obter('LISTA_MAX_EMAILS')}. Divida o arquivo.")
    return resultado


# =========================================================================== imagens do corpo
@router.post("/imagens")
async def enviar_imagem(arquivo: UploadFile = File(...), admin: Administrador = EmailDep) -> dict:
    tipo = (arquivo.content_type or "").lower()
    if tipo not in TIPOS_IMAGEM:
        raise HTTPException(422, "Use uma imagem PNG, JPG, GIF ou WEBP.")
    limite = ajustes.obter("IMAGEM_MAX_MB") * 1024 * 1024
    dados = await arquivo.read(limite + 1)
    if len(dados) > limite:
        raise HTTPException(413, f"A imagem passa de {ajustes.obter('IMAGEM_MAX_MB')} MB. Diminua o tamanho e tente de novo.")
    nome_salvo = salvar_imagem(dados, tipo)
    auditoria.registrar("email_imagem_enviada", admin.usuario, f"{arquivo.filename} ({len(dados) // 1024} KB) → {nome_salvo}")
    return {"url": f"/api/email/imagens/{nome_salvo}"}


@router.get("/imagens/{nome}")
def ver_imagem(nome: str) -> FileResponse:
    """Mostra a imagem no editor e na previa. O nome e aleatorio (nao da para adivinhar)."""
    caminho = _pasta_imagens() / nome
    if not NOME_IMAGEM_RE.match(nome) or not caminho.exists():
        raise HTTPException(404, "Imagem não encontrada.")
    return FileResponse(caminho, headers={"Cache-Control": "private, max-age=86400"})


# =========================================================================== remetentes permitidos
def conta_de_envio() -> str:
    """Todos os disparos saem da geral@ (fixa); com o modo de teste ligado, do e-mail de teste."""
    return (ajustes.obter("SMTP_FROM") or "").strip().lower()


def modo_teste() -> bool:
    return str(ajustes.obter("EMAIL_TESTE_ATIVO")).lower() == "sim" and conta_de_envio() != config.CONTA_ENVIO


def remetentes_permitidos(admin: Administrador) -> list[dict]:
    conta = conta_de_envio()
    if not conta:
        return []
    if modo_teste():
        return [{"email": conta, "tipo": "teste", "rotulo": "Modo de teste ligado em Configurações"}]
    return [{"email": conta, "tipo": "padrao", "rotulo": "Conta de envio da Gestão TI"}]


def escolher_remetente(admin: Administrador, pedido: str | None = None) -> str:
    """Ignora qualquer outro endereco: o envio e sempre pela conta unica."""
    conta = conta_de_envio()
    if not conta or not EMAIL_RE.fullmatch(conta):
        raise HTTPException(422, "A conta de envio dos e-mails não está configurada. Configure em Configurações → E-mail do remetente.")
    return conta


@router.get("/remetentes")
def listar_remetentes(admin: Administrador = EmailDep) -> list[dict]:
    return remetentes_permitidos(admin)


# =========================================================================== teste de envio
class Teste(BaseModel):
    para: str = Field(max_length=254)
    assunto: str = Field(min_length=1, max_length=300)
    corpo_html: str = Field(min_length=1, max_length=20_000_000)
    remetente_nome: str | None = Field(default=None, max_length=150)
    remetente_email: str | None = Field(default=None, max_length=254)
    responder_para: str | None = Field(default=None, max_length=200)
    disparo_id: int | None = None


@router.post("/teste")
def enviar_teste(dados: Teste, request: Request, admin: Administrador = EmailDep) -> dict:
    """Envia a mensagem para um endereco so (o da propria pessoa), para conferir antes do disparo."""
    para = dados.para.strip().lower()
    if not EMAIL_RE.fullmatch(para):
        raise HTTPException(422, "E-mail de teste inválido.")
    anexos = []
    if dados.disparo_id:
        with SessionLocal() as s:
            d = _obter(s, admin, dados.disparo_id)
            anexos = [(a.nome, a.tipo, Path(a.caminho).read_bytes()) for a in d.anexos if Path(a.caminho).exists()]
    remetente = escolher_remetente(admin, dados.remetente_email)
    msg = montar_mensagem("[TESTE] " + dados.assunto, extrair_imagens_coladas(limpar_html(dados.corpo_html)), dados.remetente_nome,
                          dados.responder_para, para, admin.nome, anexos, remetente_email=remetente)
    try:
        with abrir_smtp() as smtp:
            smtp.send_message(msg)
    except Exception as erro:  # noqa: BLE001 - devolve o motivo para a tela
        raise HTTPException(502, f"O servidor de e-mail recusou o envio de teste: {erro}")
    with SessionLocal() as s:
        _evento(s, "email_teste", admin.usuario, f"Teste enviado para {para}", ip=auth.ip_do_cliente(request))
        s.commit()
    return {"ok": True}


# =========================================================================== disparos
class Destinatario(BaseModel):
    email: str = Field(max_length=254)
    nome: str | None = Field(default=None, max_length=200)


class NovoDisparo(BaseModel):
    titulo: str = Field(min_length=3, max_length=200)
    origem: str
    lista_nome: str | None = Field(default=None, max_length=200)
    assunto: str = Field(min_length=1, max_length=300)
    corpo_html: str = Field(min_length=1, max_length=20_000_000)
    remetente_nome: str | None = Field(default=None, max_length=150)
    remetente_email: str | None = Field(default=None, max_length=254)
    responder_para: str | None = Field(default=None, max_length=200)
    tamanho_bloco: int = Field(ge=1)
    modo: str = "automatico"
    intervalo_minutos: int = Field(default=0, ge=0, le=10080)
    destinatarios: list[Destinatario]


def _resumo(s, d: Disparo) -> dict:
    return {"id": d.id, "titulo": d.titulo, "origem": d.origem, "lista_nome": d.lista_nome, "assunto": d.assunto,
            "status": d.status, "status_nome": NOMES_STATUS.get(d.status, d.status), "total": d.total,
            "enviados": d.enviados, "falhas": d.falhas, "total_blocos": d.total_blocos,
            "tamanho_bloco": d.tamanho_bloco, "modo": d.modo, "intervalo_minutos": d.intervalo_minutos,
            "proximo_bloco_em": _iso(d.proximo_bloco_em), "criado_por": d.criado_por, "criado_em": _iso(d.criado_em),
            "iniciado_em": _iso(d.iniciado_em), "concluido_em": _iso(d.concluido_em), "ultimo_erro": d.ultimo_erro,
            "remetente_nome": d.remetente_nome, "remetente_email": d.remetente_email or ajustes.obter("SMTP_FROM"),
            "responder_para": d.responder_para,
            "anexos": [{"id": a.id, "nome": a.nome, "tamanho": a.tamanho} for a in d.anexos]}


def _atualizar_contagem(s, d: Disparo) -> None:
    linhas = dict(s.execute(select(DisparoDestinatario.status, func.count()).where(
        DisparoDestinatario.disparo_id == d.id).group_by(DisparoDestinatario.status)).all())
    d.enviados = linhas.get("enviado", 0)
    d.falhas = linhas.get("erro", 0)


@router.get("/disparos")
def listar_disparos(admin: Administrador = EmailDep) -> list[dict]:
    with SessionLocal() as s:
        lista = s.scalars(select(Disparo).where(_visiveis(admin)).order_by(Disparo.id.desc()).limit(200)).all()
        return [_resumo(s, d) for d in lista]


@router.post("/disparos")
def criar_disparo(dados: NovoDisparo, request: Request, admin: Administrador = EmailDep) -> dict:
    _exigir_origem(admin, dados.origem)
    if dados.modo not in ("automatico", "manual"):
        raise HTTPException(422, "Modo de envio inválido.")
    remetente = escolher_remetente(admin, dados.remetente_email)
    vistos, dest = set(), []
    for x in dados.destinatarios:
        e = x.email.strip().lower()
        if EMAIL_RE.fullmatch(e) and e not in vistos:
            vistos.add(e)
            dest.append((e, (x.nome or "").strip() or None))
    if not dest:
        raise HTTPException(422, "Selecione pelo menos um destinatário com e-mail válido.")
    if len(dest) > ajustes.obter("LISTA_MAX_EMAILS"):
        raise HTTPException(422, f"O máximo por disparo é {ajustes.obter('LISTA_MAX_EMAILS')} e-mails.")
    bloco = min(dados.tamanho_bloco, len(dest))
    with SessionLocal() as s:
        d = Disparo(titulo=dados.titulo.strip(), origem=dados.origem, lista_nome=dados.lista_nome,
                    assunto=dados.assunto.strip(), corpo_html=extrair_imagens_coladas(limpar_html(dados.corpo_html)),
                    remetente_nome=(dados.remetente_nome or "").strip() or None,
                    remetente_email=remetente,
                    responder_para=(dados.responder_para or "").strip() or None,
                    tamanho_bloco=bloco, modo=dados.modo, intervalo_minutos=dados.intervalo_minutos,
                    total=len(dest), total_blocos=(len(dest) + bloco - 1) // bloco, criado_por=admin.usuario)
        s.add(d)
        s.flush()
        s.execute(DisparoDestinatario.__table__.insert(), [
            {"disparo_id": d.id, "email": e, "nome": n, "bloco": i // bloco + 1, "status": "pendente"}
            for i, (e, n) in enumerate(dest)])
        _evento(s, "email_disparo_criado", admin.usuario, ip=auth.ip_do_cliente(request),
                detalhe=f"Disparo nº {d.id} '{d.titulo}' ({'AD' if d.origem == 'ad' else 'lista ' + (d.lista_nome or '')}): "
                        f"{d.total} e-mails em {d.total_blocos} bloco(s), remetente {remetente}")
        s.commit()
        return _resumo(s, d)


@router.get("/disparos/{disparo_id}")
def detalhe_disparo(disparo_id: int, admin: Administrador = EmailDep) -> dict:
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id)
        dados = _resumo(s, d)
        dados["corpo_html"] = d.corpo_html
        blocos: dict[int, dict] = {}
        for bloco, status, qtd in s.execute(select(DisparoDestinatario.bloco, DisparoDestinatario.status, func.count())
                                            .where(DisparoDestinatario.disparo_id == d.id)
                                            .group_by(DisparoDestinatario.bloco, DisparoDestinatario.status)):
            b = blocos.setdefault(bloco, {"bloco": bloco, "total": 0, "enviado": 0, "erro": 0, "pendente": 0, "cancelado": 0})
            b[status] = qtd
            b["total"] += qtd
        dados["blocos"] = [blocos[k] for k in sorted(blocos)]
        dados["falhas_recentes"] = [{"email": x.email, "erro": x.erro} for x in s.scalars(
            select(DisparoDestinatario).where(DisparoDestinatario.disparo_id == d.id, DisparoDestinatario.status == "erro")
            .order_by(DisparoDestinatario.id.desc()).limit(20))]
        return dados


@router.get("/disparos/{disparo_id}/destinatarios")
def destinatarios_disparo(disparo_id: int, admin: Administrador = EmailDep) -> list[dict]:
    """Usado para duplicar um disparo (reaproveitar a lista)."""
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id)
        return [{"email": x.email, "nome": x.nome} for x in s.scalars(
            select(DisparoDestinatario).where(DisparoDestinatario.disparo_id == d.id).order_by(DisparoDestinatario.id))]


@router.get("/disparos/{disparo_id}/relatorio.csv")
def relatorio_disparo(disparo_id: int, admin: Administrador = EmailDep) -> StreamingResponse:
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id)
        _evento(s, "email_relatorio_exportado", admin.usuario, f"Disparo nº {d.id} '{d.titulo}'")
        s.commit()
        saida = io.StringIO()
        w = csv.writer(saida, delimiter=";")
        w.writerow(["E-mail", "Nome", "Bloco", "Situação", "Enviado em", "Erro"])
        for x in s.scalars(select(DisparoDestinatario).where(DisparoDestinatario.disparo_id == d.id)
                           .order_by(DisparoDestinatario.id)):
            w.writerow([x.email, x.nome or "", x.bloco, {"enviado": "Enviado", "erro": "Falha", "pendente": "Pendente",
                                                         "cancelado": "Cancelado"}.get(x.status, x.status),
                        x.enviado_em.astimezone().strftime("%d/%m/%Y %H:%M:%S") if x.enviado_em else "", x.erro or ""])
    return StreamingResponse(iter(["﻿" + saida.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="disparo-{disparo_id}.csv"'})


@router.post("/disparos/{disparo_id}/anexos")
async def anexar(disparo_id: int, arquivo: UploadFile = File(...), admin: Administrador = EmailDep) -> dict:
    nome = Path(arquivo.filename or "anexo").name[:200]
    if Path(nome).suffix.lower() in EXTENSOES_BLOQUEADAS:
        raise HTTPException(422, f"Arquivos {Path(nome).suffix} não podem ser anexados.")
    limite = ajustes.obter("ANEXOS_MAX_MB") * 1024 * 1024
    dados = await arquivo.read(limite + 1)
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id, para_escrita=True)
        if d.status != "rascunho":
            raise HTTPException(409, "Só é possível anexar antes de iniciar o envio.")
        usado = sum(a.tamanho for a in d.anexos)
        if usado + len(dados) > limite:
            raise HTTPException(413, f"Os anexos passam do limite de {ajustes.obter('ANEXOS_MAX_MB')} MB por disparo.")
        pasta = Path(config.DADOS_DIR) / "anexos" / str(d.id)
        pasta.mkdir(parents=True, exist_ok=True)
        caminho = pasta / f"{uuid.uuid4().hex}_{re.sub(r'[^A-Za-z0-9._-]', '_', nome)}"
        caminho.write_bytes(dados)
        tipo = arquivo.content_type or mimetypes.guess_type(nome)[0] or "application/octet-stream"
        a = DisparoAnexo(disparo_id=d.id, nome=nome, tipo=tipo, tamanho=len(dados), caminho=str(caminho))
        s.add(a)
        _evento(s, "email_anexo_adicionado", admin.usuario, f"Disparo nº {d.id} '{d.titulo}': {nome} ({len(dados) // 1024} KB)")
        s.commit()
        return {"id": a.id, "nome": a.nome, "tamanho": a.tamanho}


@router.delete("/disparos/{disparo_id}/anexos/{anexo_id}")
def remover_anexo(disparo_id: int, anexo_id: int, admin: Administrador = EmailDep) -> dict:
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id, para_escrita=True)
        if d.status != "rascunho":
            raise HTTPException(409, "Só é possível mexer nos anexos antes de iniciar o envio.")
        a = s.get(DisparoAnexo, anexo_id)
        if not a or a.disparo_id != d.id:
            raise HTTPException(404, "Anexo não encontrado.")
        Path(a.caminho).unlink(missing_ok=True)
        s.delete(a)
        _evento(s, "email_anexo_removido", admin.usuario, f"Disparo nº {d.id} '{d.titulo}': {a.nome}")
        s.commit()
        return {"ok": True}


@router.delete("/disparos/{disparo_id}")
def excluir_rascunho(disparo_id: int, admin: Administrador = EmailDep) -> dict:
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id, para_escrita=True)
        if d.status != "rascunho":
            raise HTTPException(409, "Só rascunhos podem ser excluídos. Disparos iniciados ficam no histórico.")
        for a in d.anexos:
            Path(a.caminho).unlink(missing_ok=True)
        s.execute(delete(DisparoDestinatario).where(DisparoDestinatario.disparo_id == d.id))
        _evento(s, "email_rascunho_excluido", admin.usuario, f"Disparo nº {d.id} '{d.titulo}'")
        s.delete(d)
        s.commit()
        return {"ok": True}


def _mudar_status(disparo_id: int, admin: Administrador, request: Request, de: tuple, para: str, acao: str,
                  texto: str) -> dict:
    with SessionLocal() as s:
        d = _obter(s, admin, disparo_id, para_escrita=True)
        _exigir_origem(admin, d.origem)
        if d.status not in de:
            raise HTTPException(409, f"Não é possível fazer isso com o disparo na situação '{NOMES_STATUS.get(d.status)}'.")
        if para == "enviando":
            if not smtp_configurado():
                raise HTTPException(409, "O servidor de e-mail não está configurado. Configure no módulo Configurações.")
            d.iniciado_em = d.iniciado_em or agora()
            d.proximo_bloco_em = None
            d.ultimo_erro = None
        if para == "cancelado":
            s.execute(update(DisparoDestinatario).where(DisparoDestinatario.disparo_id == d.id,
                                                        DisparoDestinatario.status == "pendente").values(status="cancelado"))
            d.concluido_em = agora()
            d.proximo_bloco_em = None
        d.status = para
        _evento(s, acao, admin.usuario, f"Disparo nº {d.id} '{d.titulo}': {texto}", ip=auth.ip_do_cliente(request))
        s.commit()
        _acordar.set()
        return _resumo(s, d)


@router.post("/disparos/{disparo_id}/iniciar")
def iniciar(disparo_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    return _mudar_status(disparo_id, admin, request, ("rascunho",), "enviando", "email_disparo_iniciado", "envio iniciado")


@router.post("/disparos/{disparo_id}/proximo-bloco")
def proximo_bloco(disparo_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    return _mudar_status(disparo_id, admin, request, ("aguardando_bloco",), "enviando", "email_bloco_liberado",
                         "próximo bloco liberado")


@router.post("/disparos/{disparo_id}/pausar")
def pausar(disparo_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    return _mudar_status(disparo_id, admin, request, ("enviando", "aguardando_bloco"), "pausado",
                         "email_disparo_pausado", "pausado")


@router.post("/disparos/{disparo_id}/retomar")
def retomar(disparo_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    return _mudar_status(disparo_id, admin, request, ("pausado",), "enviando", "email_disparo_retomado", "retomado")


@router.post("/disparos/{disparo_id}/cancelar")
def cancelar(disparo_id: int, request: Request, admin: Administrador = EmailDep) -> dict:
    return _mudar_status(disparo_id, admin, request, ("enviando", "aguardando_bloco", "pausado", "rascunho"),
                         "cancelado", "email_disparo_cancelado", "cancelado (os e-mails que faltavam não serão enviados)")


# =========================================================================== envio em segundo plano
_acordar = threading.Event()
_thread: threading.Thread | None = None


def iniciar_envios() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_laco, name="envio-emails", daemon=True)
    _thread.start()


def _laco() -> None:
    while True:
        try:
            if not _ciclo():
                _acordar.wait(5)
                _acordar.clear()
        except Exception:  # noqa: BLE001 - o laco nunca pode morrer
            log.exception("Erro no envio de e-mails em lote")
            time.sleep(10)


def _ciclo() -> bool:
    """Envia um bloco de um disparo que esteja na vez. Devolve True se trabalhou."""
    with SessionLocal() as s:
        d = s.scalar(select(Disparo).where(Disparo.status == "enviando",
                                           or_(Disparo.proximo_bloco_em.is_(None), Disparo.proximo_bloco_em <= agora()))
                     .order_by(Disparo.id))
        if not d:
            return False
        disparo_id = d.id
    enviar_bloco(disparo_id)
    return True


def _situacao(disparo_id: int) -> str | None:
    with SessionLocal() as s:
        return s.scalar(select(Disparo.status).where(Disparo.id == disparo_id))


def enviar_bloco(disparo_id: int) -> None:
    with SessionLocal() as s:
        d = s.get(Disparo, disparo_id)
        bloco = s.scalar(select(func.min(DisparoDestinatario.bloco)).where(
            DisparoDestinatario.disparo_id == disparo_id, DisparoDestinatario.status == "pendente"))
        if bloco is None:
            _finalizar(s, d)
            return
        modelo = (d.assunto, d.corpo_html, d.remetente_nome, d.responder_para)
        remetente_email = conta_de_envio() or d.remetente_email   # sempre a conta unica de envio
        cache_imagens: dict = {}
        anexos = [(a.nome, a.tipo, Path(a.caminho).read_bytes()) for a in d.anexos if Path(a.caminho).exists()]
        pendentes = s.execute(select(DisparoDestinatario.id, DisparoDestinatario.email, DisparoDestinatario.nome)
                              .where(DisparoDestinatario.disparo_id == disparo_id, DisparoDestinatario.bloco == bloco,
                                     DisparoDestinatario.status == "pendente")
                              .order_by(DisparoDestinatario.id)).all()
        total_blocos = d.total_blocos
    log.info("Disparo %s: enviando bloco %s de %s (%s e-mails)", disparo_id, bloco, total_blocos, len(pendentes))

    smtp = None
    try:
        for i, (dest_id, email, nome) in enumerate(pendentes):
            if i % 20 == 0 and _situacao(disparo_id) != "enviando":
                return  # pausado ou cancelado no meio do bloco
            msg = montar_mensagem(*modelo, email, nome, anexos, cache_imagens, remetente_email)
            status, erro = "enviado", None
            for tentativa in (1, 2):
                try:
                    if smtp is None:
                        smtp = abrir_smtp()
                    smtp.send_message(msg)
                    break
                except smtplib.SMTPRecipientsRefused as e:
                    status, erro = "erro", f"Destinatário recusado: {list(e.recipients.values())[0]}"
                    break
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, OSError) as e:
                    smtp = None
                    if tentativa == 2:
                        _pausar_por_erro(disparo_id, f"Sem conexão com o servidor de e-mail: {e}")
                        return
                    time.sleep(5)
                except smtplib.SMTPException as e:
                    status, erro = "erro", str(e)[:500]
                    break
                except RuntimeError as e:  # SMTP nao configurado
                    _pausar_por_erro(disparo_id, str(e))
                    return
            with SessionLocal() as s:
                s.execute(update(DisparoDestinatario).where(DisparoDestinatario.id == dest_id)
                          .values(status=status, erro=erro, enviado_em=agora() if status == "enviado" else None))
                if i % 20 == 19 or i == len(pendentes) - 1:
                    d = s.get(Disparo, disparo_id)
                    _atualizar_contagem(s, d)
                s.commit()
            pausa = ajustes.obter("ENVIO_PAUSA_MS")
            if pausa:
                time.sleep(pausa / 1000)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass

    with SessionLocal() as s:
        d = s.get(Disparo, disparo_id, with_for_update=True)
        _atualizar_contagem(s, d)
        resta = s.scalar(select(func.count()).where(DisparoDestinatario.disparo_id == disparo_id,
                                                    DisparoDestinatario.status == "pendente"))
        _evento(s, "email_bloco_enviado", "sistema",
                f"Disparo nº {d.id} '{d.titulo}': bloco {bloco} de {d.total_blocos} concluído "
                f"({d.enviados} enviados, {d.falhas} falhas até agora)")
        if not resta:
            _finalizar(s, d)
            return
        if d.status == "enviando":
            if d.modo == "manual":
                d.status = "aguardando_bloco"
            else:
                d.proximo_bloco_em = agora() + timedelta(minutes=d.intervalo_minutos)
        s.commit()


def _finalizar(s, d: Disparo) -> None:
    _atualizar_contagem(s, d)
    if d.status in ("enviando", "aguardando_bloco"):
        d.status = "concluido"
        d.concluido_em = agora()
        d.proximo_bloco_em = None
        _evento(s, "email_disparo_concluido", "sistema",
                f"Disparo nº {d.id} '{d.titulo}' concluído: {d.enviados} enviados, {d.falhas} falhas")
    s.commit()


def _pausar_por_erro(disparo_id: int, motivo: str) -> None:
    log.warning("Disparo %s pausado: %s", disparo_id, motivo)
    with SessionLocal() as s:
        d = s.get(Disparo, disparo_id, with_for_update=True)
        _atualizar_contagem(s, d)
        if d.status == "enviando":
            d.status = "pausado"
        d.ultimo_erro = motivo
        _evento(s, "email_disparo_pausado", "sistema", f"Disparo nº {d.id} pausado automaticamente: {motivo}")
        s.commit()
