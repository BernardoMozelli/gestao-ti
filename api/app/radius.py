"""Banco do RADIUS do Wi-Fi Visitante (portal de cadastro dos visitantes, MySQL/MariaDB).

Tabelas (nomes configuraveis em Configuracoes):
- reg_users: cadastro do visitante (nome, cpf, email, celular, ativo) e idradcheck -> radcheck.id
- radcheck:  login (username) e senha do RADIUS. A senha fica num formato do FreeRADIUS, conforme a coluna
             "attribute": MD5-Password (hash MD5 em hexadecimal), SHA-Password, Cleartext-Password...

Trocar a senha = gravar a senha nova em radcheck no MESMO formato que ja esta la. O RADIUS le a tabela a cada
login, entao o visitante entra com a senha nova na hora.
"""
import hashlib
import logging
import re
import threading
import time
from contextlib import contextmanager

import pymysql
import pymysql.cursors

from . import ajustes

log = logging.getLogger("radius")

# Formatos de senha do FreeRADIUS que o sistema sabe gravar (attribute em minusculas -> como calcular)
FORMATOS = {
    "md5-password": ("MD5", lambda s: hashlib.md5(s.encode()).hexdigest()),
    "sha-password": ("SHA-1", lambda s: hashlib.sha1(s.encode()).hexdigest()),
    "sha1-password": ("SHA-1", lambda s: hashlib.sha1(s.encode()).hexdigest()),
    "sha2-password": ("SHA-256", lambda s: hashlib.sha256(s.encode()).hexdigest()),
    "sha256-password": ("SHA-256", lambda s: hashlib.sha256(s.encode()).hexdigest()),
    "cleartext-password": ("texto puro", lambda s: s),
    "user-password": ("texto puro", lambda s: s),
}
_NOME_TABELA = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class ErroRadius(Exception):
    pass


def _cfg() -> dict:
    c = {k: ajustes.obter(k) for k in ajustes.CAMPOS if k.startswith("RADIUS_")}
    c["ssh"] = str(c["RADIUS_SSH"]).lower() in ("sim", "true", "1")
    return c


def configurado() -> bool:
    c = _cfg()
    return bool(c["RADIUS_DB_HOST"] and c["RADIUS_DB_USUARIO"] and c["RADIUS_DB_NOME"])


def _tabelas(c: dict | None = None) -> tuple[str, str]:
    c = c or _cfg()
    usuarios, senhas = c["RADIUS_TABELA_USUARIOS"], c["RADIUS_TABELA_SENHAS"]
    for t in (usuarios, senhas):
        if not _NOME_TABELA.match(t or ""):
            raise ErroRadius(f"Nome de tabela inválido: '{t}'. Use só letras, números e _.")
    return f"`{usuarios}`", f"`{senhas}`"


# ------------------------------------------------------------------ conexao (direta ou por tunel SSH)
_tunel = {"obj": None, "chave": None}
_trava_tunel = threading.Lock()


_destino_bom: dict = {}   # (servidor SSH, porta do banco) -> endereco do MySQL que respondeu por dentro do servidor


def _destinos(c: dict) -> list[tuple[str, int]]:
    """Onde o MySQL e procurado por dentro do servidor SSH. No proprio servidor: primeiro 127.0.0.1 (o MySQL pode
    aceitar so conexao local) e depois o IP do servidor (o MySQL pode escutar so no IP da rede, ex.: 10.1.0.4)."""
    ssh_host = c["RADIUS_SSH_HOST"] or c["RADIUS_DB_HOST"]
    porta = int(c["RADIUS_DB_PORTA"])
    if ssh_host != c["RADIUS_DB_HOST"]:
        return [(c["RADIUS_DB_HOST"], porta)]
    opcoes = [("127.0.0.1", porta), (c["RADIUS_DB_HOST"], porta)]
    bom = _destino_bom.get((ssh_host, porta))
    return sorted(opcoes, key=lambda d: d != bom)


def _abrir_tunel(c: dict, destino: tuple[str, int]):
    """Tunel SSH reaproveitado entre as consultas (abrir SSH a cada busca deixaria a tela lenta)."""
    from sshtunnel import SSHTunnelForwarder
    ssh_host = c["RADIUS_SSH_HOST"] or c["RADIUS_DB_HOST"]
    chave = (ssh_host, c["RADIUS_SSH_PORTA"], c["RADIUS_SSH_USUARIO"], c["RADIUS_SSH_SENHA"], destino)
    with _trava_tunel:
        t = _tunel["obj"]
        if t is not None and _tunel["chave"] == chave and t.is_active:
            return t
        if t is not None:
            try:
                t.stop()
            except Exception:  # noqa: BLE001
                pass
        t = SSHTunnelForwarder((ssh_host, int(c["RADIUS_SSH_PORTA"])), ssh_username=c["RADIUS_SSH_USUARIO"],
                               ssh_password=c["RADIUS_SSH_SENHA"] or None, remote_bind_address=destino,
                               set_keepalive=30, allow_agent=False, host_pkey_directories=[])
        t.start()
        _tunel.update(obj=t, chave=chave)
        return t


def fechar_tunel() -> None:
    with _trava_tunel:
        if _tunel["obj"] is not None:
            try:
                _tunel["obj"].stop()
            except Exception:  # noqa: BLE001
                pass
        _tunel.update(obj=None, chave=None)


def _traduzir(erro: Exception, c: dict, destino: tuple | None = None) -> ErroRadius:
    texto = str(erro)
    codigo = erro.args[0] if isinstance(erro, pymysql.err.MySQLError) and erro.args else None
    alvo = f"{c['RADIUS_DB_HOST']}:{c['RADIUS_DB_PORTA']}"
    usuario = c["RADIUS_DB_USUARIO"]
    if codigo == 1698:
        return ErroRadius(f"O MySQL não deixa o usuário '{usuario}' entrar por programas: neste servidor ele só entra pelo terminal "
                          "do próprio servidor (auth_socket). Crie um usuário do MySQL só para a Gestão TI (o comando está em "
                          "Configurações → Wi-Fi Visitante → “Recomendado”) e use-o aqui.")
    if codigo == 1045:
        return ErroRadius(f"O MySQL recusou o usuário '{usuario}' ou a senha do banco. Confira em Configurações → Wi-Fi Visitante. "
                          "Se o usuário for o root, o mais garantido é criar um usuário só para a Gestão TI.")
    if codigo == 1044:
        return ErroRadius(f"O usuário '{usuario}' não tem acesso ao banco '{c['RADIUS_DB_NOME']}'.")
    if codigo == 1049:
        return ErroRadius(f"O banco '{c['RADIUS_DB_NOME']}' não existe nesse servidor.")
    if codigo == 1146:
        return ErroRadius(f"Tabela não encontrada: {texto}. Confira os nomes das tabelas em Configurações.")
    if codigo in (1142, 1143):
        return ErroRadius("O usuário do banco não tem permissão para essa operação (precisa de SELECT e UPDATE nas duas tabelas).")
    if codigo == 1130:
        return ErroRadius(f"O MySQL não aceita o usuário '{usuario}' vindo desta conexão. Use “Conectar por SSH” ou crie o usuário "
                          "para o endereço certo (veja o comando em “Recomendado”).")
    if codigo in (2003, 2013) or "timed out" in texto.lower():
        if c["ssh"]:
            return ErroRadius("O SSH conectou, mas o MySQL não respondeu por dentro do servidor (tentei 127.0.0.1 e o IP do servidor, "
                              f"porta {c['RADIUS_DB_PORTA']}). Confira se o MySQL está rodando e em qual porta.")
        return ErroRadius(f"Não foi possível conectar ao banco em {alvo}. Se o MySQL aceita só conexões locais, marque “Conectar por SSH”.")
    nome = type(erro).__name__
    if "Authentication" in nome or "authentication" in texto.lower():
        return ErroRadius("O servidor SSH recusou o usuário ou a senha do servidor.")
    if "SSHTunnel" in nome or "Could not establish" in texto or "ssh" in texto.lower():
        return ErroRadius(f"Não foi possível abrir o SSH em {c['RADIUS_SSH_HOST'] or c['RADIUS_DB_HOST']}:{c['RADIUS_SSH_PORTA']}: {texto}")
    return ErroRadius(f"Erro no banco do Wi-Fi Visitante ({nome}): {texto}")


def _conectar(c: dict):
    """Abre a conexao (direta ou pelo tunel). Pelo SSH, tenta 127.0.0.1 e depois o IP do servidor."""
    kw = dict(user=c["RADIUS_DB_USUARIO"], password=c["RADIUS_DB_SENHA"] or "", database=c["RADIUS_DB_NOME"],
              charset="utf8mb4", connect_timeout=8, read_timeout=20, write_timeout=20, autocommit=False,
              cursorclass=pymysql.cursors.DictCursor)
    if not c["ssh"]:
        return pymysql.connect(host=c["RADIUS_DB_HOST"], port=int(c["RADIUS_DB_PORTA"]), **kw)
    ultimo = None
    for destino in _destinos(c):
        t = _abrir_tunel(c, destino)
        try:
            conn = pymysql.connect(host="127.0.0.1", port=t.local_bind_port, **kw)
            _destino_bom[(c["RADIUS_SSH_HOST"] or c["RADIUS_DB_HOST"], int(c["RADIUS_DB_PORTA"]))] = destino
            return conn
        except pymysql.err.OperationalError as erro:
            if erro.args and erro.args[0] in (2003, 2013):   # MySQL nao respondeu nesse endereco: tenta o proximo
                log.info("MySQL nao respondeu em %s pelo SSH: %s", destino, erro)
                ultimo = erro
                fechar_tunel()
                continue
            raise
    raise ultimo


def _seguro(acao) -> None:
    try:
        acao()
    except Exception:  # noqa: BLE001 - conexao ja caida
        pass


@contextmanager
def conexao(c: dict | None = None):
    c = c or _cfg()
    if not (c["RADIUS_DB_HOST"] and c["RADIUS_DB_USUARIO"] and c["RADIUS_DB_NOME"]):
        raise ErroRadius("O banco do Wi-Fi Visitante ainda não foi configurado. Configure em Configurações → Wi-Fi Visitante.")
    try:
        conn = _conectar(c)
    except ErroRadius:
        raise
    except Exception as erro:  # noqa: BLE001 - erros do pymysql/paramiko viram mensagens claras
        log.warning("Conexao com o banco do Wi-Fi Visitante falhou: %r", erro)
        if c["ssh"]:
            fechar_tunel()
        raise _traduzir(erro, c) from erro
    try:
        yield conn
    except ErroRadius:
        _seguro(conn.rollback)
        raise
    except Exception as erro:  # noqa: BLE001 - qualquer falha vira mensagem na tela (nunca erro 500)
        log.exception("Erro no banco do Wi-Fi Visitante")
        _seguro(conn.rollback)
        if c["ssh"]:
            fechar_tunel()
        raise _traduzir(erro, c) from erro
    finally:
        _seguro(conn.close)


# ------------------------------------------------------------------ utilitarios de exibicao
def _digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def mascarar_cpf(cpf) -> str:
    d = _digitos(cpf)
    return f"***.{d[3:6]}.{d[6:9]}-**" if len(d) == 11 else (cpf or "")


def formatar_celular(cel) -> str:
    d = _digitos(cel)
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return cel or ""


def corrigir_texto(v):
    """Portais PHP antigos gravam texto UTF-8 em tabela latin1: "José" volta como "JosÃ©". Desfaz isso so na exibicao."""
    if not isinstance(v, str) or not any(ch in v for ch in "ÃÂ"):
        return v
    try:
        consertado = v.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return v
    return consertado if "Ã" not in consertado else v


def _corrigir(r: dict | None) -> dict | None:
    if r:
        for k in ("nome", "email"):
            if k in r:
                r[k] = corrigir_texto(r[k])
    return r


def _item(r: dict) -> dict:
    _corrigir(r)
    login = r.get("username") or ""
    login_mostrado = mascarar_cpf(login) if _digitos(login) == login and len(login) == 11 else login
    formato = FORMATOS.get((r.get("attribute") or "").lower(), (r.get("attribute") or None, None))[0]
    return {"id": r["id"], "nome": r["nome"], "cpf": mascarar_cpf(r["cpf"]), "email": r["email"],
            "celular": formatar_celular(r["celular"]), "ativo": bool(r["ativo"]), "login": login_mostrado,
            "tem_login": bool(login), "formato_senha": formato}


# ------------------------------------------------------------------ operacoes
_totais = {"quando": 0.0, "dados": None, "chave": None}
_TOTAIS_SEGUNDOS = 300


def totais(forcar: bool = False) -> dict:
    """Quantos visitantes ativos e inativos ha no portal (conta a tabela inteira: guardado por 5 minutos)."""
    c = _cfg()
    chave = (c["RADIUS_DB_HOST"], c["RADIUS_DB_NOME"], c["RADIUS_TABELA_USUARIOS"])
    if not forcar and _totais["dados"] and _totais["chave"] == chave and time.time() - _totais["quando"] < _TOTAIS_SEGUNDOS:
        return _totais["dados"]
    tu, _ = _tabelas()
    with conexao() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT SUM(ativo = 1) AS ativos, SUM(ativo <> 1) AS inativos FROM {tu}")
        r = cur.fetchone() or {}
    dados = {"ativos": int(r.get("ativos") or 0), "inativos": int(r.get("inativos") or 0)}
    _totais.update(quando=time.time(), dados=dados, chave=chave)
    return dados


def _ajustar_totais(de_ativo: bool, para_ativo: bool) -> None:
    """Depois de ativar/desativar: corrige o total guardado sem contar a tabela de novo."""
    d = _totais["dados"]
    if d and de_ativo != para_ativo:
        d["ativos"] += 1 if para_ativo else -1
        d["inativos"] += -1 if para_ativo else 1


MAX_RESULTADOS = 500   # a busca para aqui (refinar a busca se passar disso)


def _condicao_busca(q: str) -> tuple[str, list, str | None]:
    """Uma unica condicao simples conforme o que foi digitado (a tabela do portal nao tem indices nessas colunas,
    entao cada busca le a tabela uma vez: OR em muitas colunas + juncao + ORDER BY ficava lento demais).
    Devolve (condicao em reg_users, argumentos, login exato para procurar tambem na tabela de senhas)."""
    q = q.strip()
    d = _digitos(q)
    so_numeros = bool(d) and re.fullmatch(r"[\d\s.\-()/]+", q) is not None
    if so_numeros and len(d) == 11:                       # CPF ou celular completo
        return "r.cpf = %s OR r.celular = %s", [d, d], d
    if so_numeros:                                        # parte do CPF (inicio) ou do celular
        return "r.cpf LIKE %s OR r.celular LIKE %s", [f"{d}%", f"%{d}%"], None
    if "@" in q:                                          # e-mail (inicio) ou login por e-mail
        return "r.email LIKE %s", [f"{q}%"], q
    return "r.nome LIKE %s OR r.email LIKE %s", [f"%{q}%", f"{q}%"], None   # nome (qualquer parte) ou inicio do e-mail


def listar(q: str = "", situacao: str = "ativos", limite: int = 100) -> dict:
    tu, ts = _tabelas()
    cond, args, login = _condicao_busca(q or "")
    campos = "r.id, r.nome, r.cpf, r.email, r.celular, r.ativo, r.idradcheck"
    with conexao() as conn, conn.cursor() as cur:
        # 1) cadastros que batem (sem juncao e sem ORDER BY: o MySQL para assim que acha MAX_RESULTADOS+1)
        cur.execute(f"SELECT {campos} FROM {tu} r WHERE {cond} LIMIT %s", args + [MAX_RESULTADOS + 1])
        linhas = list(cur.fetchall())
        # 2) login exato na tabela de senhas (username tem indice no RADIUS): acha quem entra por e-mail/CPF diferente do cadastro
        if login:
            cur.execute(f"SELECT id FROM {ts} WHERE username = %s", (login,))
            ids_rc = [x["id"] for x in cur.fetchall()]
            if ids_rc:
                vistos = {x["id"] for x in linhas}
                marcas = ",".join(["%s"] * len(ids_rc))
                cur.execute(f"SELECT {campos} FROM {tu} r WHERE r.idradcheck IN ({marcas}) LIMIT 20", ids_rc)
                linhas += [x for x in cur.fetchall() if x["id"] not in vistos]
        # 3) logins so dos cadastros encontrados (pela chave primaria: rapido)
        ids = [x["idradcheck"] for x in linhas if x.get("idradcheck")]
        logins = {}
        for k in range(0, len(ids), 500):
            parte = ids[k:k + 500]
            cur.execute(f"SELECT id, username, attribute FROM {ts} WHERE id IN ({','.join(['%s'] * len(parte))})", parte)
            logins.update({x["id"]: x for x in cur.fetchall()})
    for x in linhas:
        rc = logins.get(x.get("idradcheck")) or {}
        x["username"], x["attribute"] = rc.get("username"), rc.get("attribute")
    mais = len(linhas) > MAX_RESULTADOS
    linhas = linhas[:MAX_RESULTADOS]
    contagem = {"ativos": sum(1 for x in linhas if x["ativo"]), "inativos": sum(1 for x in linhas if not x["ativo"])}
    da_aba = [x for x in linhas if bool(x["ativo"]) == (situacao != "inativos")] if situacao in ("ativos", "inativos") else linhas
    da_aba.sort(key=lambda x: (corrigir_texto(x["nome"]) or "").lower())
    return {"itens": [_item(x) for x in da_aba[:max(1, min(int(limite), 2000))]], "contagem": contagem,
            "total": len(da_aba), "mais_resultados": mais}


def _visitante(cur, id_: int) -> dict:
    tu, ts = _tabelas()
    cur.execute(f"SELECT r.id, r.nome, r.cpf, r.email, r.celular, r.ativo, r.idradcheck, c.username "
                f"FROM {tu} r LEFT JOIN {ts} c ON c.id = r.idradcheck WHERE r.id = %s", (id_,))
    r = cur.fetchone()
    if not r:
        raise ErroRadius("Visitante não encontrado no banco do Wi-Fi.")
    return _corrigir(r)


def obter(id_: int) -> dict:
    with conexao() as conn, conn.cursor() as cur:
        r = _visitante(cur, id_)
    return {**_item(r), "email_completo": r["email"]}


def trocar_senha(id_: int, nova: str, reativar: bool = False) -> dict:
    """Grava a nova senha em todas as linhas de senha do login, no formato que cada uma ja usa."""
    tu, ts = _tabelas()
    with conexao() as conn, conn.cursor() as cur:
        r = _visitante(cur, id_)
        if not r.get("username"):
            raise ErroRadius("Este cadastro não está ligado a um login no RADIUS (idradcheck sem correspondência).")
        cur.execute(f"SELECT id, attribute FROM {ts} WHERE username = %s", (r["username"],))
        linhas = cur.fetchall()
        de_senha = [x for x in linhas if (x["attribute"] or "").lower() in FORMATOS]
        desconhecidas = [x["attribute"] for x in linhas
                         if "password" in (x["attribute"] or "").lower() and (x["attribute"] or "").lower() not in FORMATOS]
        if desconhecidas:
            raise ErroRadius(f"A senha deste login usa um formato que o sistema não sabe gravar: {', '.join(desconhecidas)}.")
        if not de_senha:
            raise ErroRadius("Não há linha de senha para este login na tabela de senhas.")
        formatos = []
        for x in de_senha:
            nome, calcular = FORMATOS[x["attribute"].lower()]
            cur.execute(f"UPDATE {ts} SET value = %s WHERE id = %s", (calcular(nova), x["id"]))
            formatos.append(nome)
        if reativar and not r["ativo"]:
            cur.execute(f"UPDATE {tu} SET ativo = 1 WHERE id = %s", (id_,))
        conn.commit()
    if reativar and not r["ativo"]:
        _ajustar_totais(False, True)
    return {"visitante": r, "formatos": sorted(set(formatos)), "reativado": bool(reativar and not r["ativo"])}


def definir_ativo(id_: int, ativo: bool) -> dict:
    tu, _ = _tabelas()
    with conexao() as conn, conn.cursor() as cur:
        r = _visitante(cur, id_)
        cur.execute(f"UPDATE {tu} SET ativo = %s WHERE id = %s", (1 if ativo else 0, id_))
        conn.commit()
    _ajustar_totais(bool(r["ativo"]), bool(ativo))
    return r


def testar(c: dict | None = None) -> dict:
    """Diagnostico para a tela Configuracoes: conecta, conta os cadastros e mostra os formatos de senha usados."""
    tu, ts = _tabelas(c)
    with conexao(c) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total, SUM(ativo = 1) AS ativos FROM {tu}")
        u = cur.fetchone()
        cur.execute(f"SELECT attribute, COUNT(*) AS n FROM {ts} GROUP BY attribute ORDER BY n DESC")
        atributos = [{"atributo": a["attribute"], "linhas": a["n"],
                      "sabe_gravar": (a["attribute"] or "").lower() in FORMATOS,
                      "e_senha": "password" in (a["attribute"] or "").lower()} for a in cur.fetchall()]
        cur.execute(f"SELECT COUNT(*) AS n FROM {tu} r LEFT JOIN {ts} c ON c.id = r.idradcheck WHERE c.id IS NULL")
        sem_login = cur.fetchone()["n"]
    return {"cadastros": int(u["total"] or 0), "ativos": int(u["ativos"] or 0), "atributos": atributos,
            "sem_login": int(sem_login or 0)}


def conferir(login: str, senha: str, c: dict | None = None) -> dict:
    """Confere se uma senha conhecida bate com o que esta gravado: confirma o formato antes de usar o modulo."""
    _, ts = _tabelas(c)
    with conexao(c) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT attribute, value FROM {ts} WHERE username = %s", (login.strip(),))
        linhas = cur.fetchall()
    if not linhas:
        return {"encontrado": False}
    resultados = []
    for x in linhas:
        attr = (x["attribute"] or "")
        if attr.lower() not in FORMATOS:
            continue
        nome, calcular = FORMATOS[attr.lower()]
        resultados.append({"atributo": attr, "formato": nome, "bate": calcular(senha).lower() == (x["value"] or "").lower()})
    return {"encontrado": True, "resultados": resultados}
