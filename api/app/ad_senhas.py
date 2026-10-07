"""Reset de senha e desbloqueio de contas do AD (modulo Gestao de Usuarios).

Usa a conta AD_RESET_USUARIO (ou, se vazia, a conta de servico AD_BIND_USUARIO). Essa conta precisa da
permissao delegada "Redefinir senha" (e "Ler/gravar lockoutTime" e "pwdLastSet") nas OUs dos usuarios.
O AD so aceita troca de senha por conexao criptografada (LDAPS 636 ou StartTLS) - sem isso ele recusa.
"""
import logging
import re
import secrets
import string
from datetime import datetime, timezone

from ldap3 import MODIFY_REPLACE, SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from . import ad, config
from .ad import ErroAD, normalizar_login, unidade_do_dn

log = logging.getLogger("ad.senhas")

_ATRIBUTOS = ["sAMAccountName", "displayName", "mail", "userAccountControl", "lockoutTime", "pwdLastSet",
              "lastLogonTimestamp", "adminCount", "msDS-User-Account-Control-Computed", "description",
              "employeeID", "initials", "whenCreated"]   # employeeID = CPF, initials = matricula (padrao da prefeitura)

# Palavras curtas e sem acento para montar senhas temporarias faceis de ditar por telefone
_PALAVRAS = ["Sol", "Rio", "Mar", "Lua", "Pato", "Gato", "Bola", "Casa", "Flor", "Mesa", "Livro", "Verde", "Azul",
             "Norte", "Pedra", "Ponte", "Praia", "Serra", "Trigo", "Campo", "Vento", "Barco", "Folha", "Tigre",
             "Lago", "Ninho", "Nuvem", "Ouro", "Prata", "Fogo", "Cedro", "Ipe", "Arara", "Onda", "Trem"]


def gerar_senha() -> str:
    """Senha temporaria forte e facil de ditar: Palavra-Palavra-1234! (maiuscula, minuscula, numero, simbolo)."""
    p1, p2 = secrets.choice(_PALAVRAS), secrets.choice(_PALAVRAS)
    while p2 == p1:
        p2 = secrets.choice(_PALAVRAS)
    return f"{p1}-{p2}-{secrets.randbelow(9000) + 1000}{secrets.choice('!@#$%*')}"


def _usuario_reset() -> tuple[str, str]:
    usuario = config.AD_RESET_USUARIO or config.AD_BIND_USUARIO
    senha = config.AD_RESET_SENHA if config.AD_RESET_USUARIO else config.AD_BIND_SENHA
    if not (config.AD_HABILITADO and usuario and senha):
        raise ErroAD("A conta do AD para redefinir senhas não está configurada (AD_RESET_USUARIO/AD_RESET_SENHA "
                     "ou AD_BIND_USUARIO/AD_BIND_SENHA no .env).")
    bind = usuario if "@" in usuario or "=" in usuario else f"{usuario}@{config.AD_DOMINIO}"
    return bind, senha


def _conectar(somente_criptografada: bool):
    bind, senha = _usuario_reset()
    modos = [m for m in ad._modos() if m != "nenhuma"] if somente_criptografada else ad._modos()
    if somente_criptografada and not modos:
        modos = ["ldaps", "starttls"]
    ultimo = "nenhum servidor do AD configurado"
    chave = "criptografada" if somente_criptografada else "padrao"
    for host, modo in ad.rotas(modos, chave):
            try:
                conn = ad._abrir(host, modo, bind, senha)
            except (LDAPException, OSError) as erro:
                ultimo = f"{host} ({modo}): {erro}"
                ad.lembrar((host, modo), False, chave)
                continue
            if conn.bound:
                ad.lembrar((host, modo), True, chave)
                conn.rota_gt = (host, modo)
                return conn
            ultimo = f"{host} ({modo}): {(conn.result or {}).get('description')}"
            conn.unbind()
    log.warning("Conexao para reset de senha falhou: %s", ultimo)
    if somente_criptografada:
        raise ErroAD("Não foi possível abrir uma conexão criptografada com o AD (LDAPS 636 ou StartTLS). "
                     "O AD só aceita troca de senha assim: os controladores de domínio precisam ter certificado.")
    raise ErroAD("Não foi possível conectar ao AD com a conta de reset de senha.")


def _data(valor) -> datetime | None:
    """Datas do AD (FILETIME ou datetime); 0 / 1601 = nunca."""
    if valor in (None, "", 0, "0"):
        return None
    if isinstance(valor, datetime):
        if valor.year <= 1601:
            return None
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    if isinstance(valor, str) and len(valor) >= 14 and valor[:14].isdigit():   # GeneralizedTime: 20260101120000.0Z
        try:
            return datetime.strptime(valor[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return datetime.fromtimestamp((n - 116444736000000000) / 10_000_000, tz=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _attr(e, nome):
    return e[nome].value if nome in e and e[nome].value not in (None, []) else None


def so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def mascarar_cpf(cpf) -> str | None:
    """'12345678900' -> '***.456.789-**' (o CPF completo nao vai para a tela)."""
    d = so_digitos(cpf)
    if len(d) == 11:
        return f"***.{d[3:6]}.{d[6:9]}-**"
    return ("*" * max(len(d) - 4, 0) + d[-4:]) if d else None


def _conta(e) -> dict:
    return _montar(str(e.entry_dn), lambda nome: _attr(e, nome))


# Bits do userAccountControl que tiram a conta do padrao (512 = conta normal): bolinha laranja
_FLAGS_ATENCAO = {
    0x20: "não exige senha",
    0x80: "senha com criptografia reversível",
    0x10000: "senha nunca expira",
    0x40000: "exige smart card",
    0x80000: "confiável para delegação",
    0x100000: "não pode ser delegada",
    0x400000: "não exige pré-autenticação Kerberos",
}
_BITS_CONHECIDOS = 0x2 | 0x10 | 0x200 | 0x800000 | sum(_FLAGS_ATENCAO)


def situacao_conta(uac: int, ativo: bool, bloqueado: bool, expirada: bool, trocar: bool) -> tuple[str, str]:
    """Bolinha de status, como no Gestao TI antigo (que olhava so o userAccountControl):
    vermelho = desativada ou bloqueada; verde = conta normal (512); laranja = ativa com algo fora do padrao."""
    if not ativo:
        return "vermelho", "Desativada no AD"
    if bloqueado:
        return "vermelho", "Bloqueada no AD (senha errada várias vezes)"
    motivos = [texto for bit, texto in _FLAGS_ATENCAO.items() if uac & bit]
    if uac & ~_BITS_CONHECIDOS:
        motivos.append(f"configuração fora do padrão (userAccountControl {uac})")
    if expirada:
        motivos.append("senha expirada")
    if trocar:
        motivos.append("deve trocar a senha no próximo logon")
    if motivos:
        return "laranja", "Ativa, com atenção: " + "; ".join(motivos)
    return "verde", "Ativa e normal"


def _montar(dn: str, ler) -> dict:
    """Situacao da conta a partir de um leitor de atributos (Entry do ldap3 ou resultado paginado)."""
    uac = int(ler("userAccountControl") or 0)
    calculado = ler("msDS-User-Account-Control-Computed")
    calculado = int(calculado) if calculado is not None else None
    lockout = _data(ler("lockoutTime"))
    pwd = ler("pwdLastSet")
    login = str(ler("sAMAccountName") or "").lower()
    cpf = ler("employeeID")
    ativo = not (uac & 2)
    bloqueado = (bool(calculado & 0x10) if calculado is not None else lockout is not None) or bool(uac & 0x10)
    expirada = (bool(calculado & 0x800000) if calculado is not None else False) or bool(uac & 0x800000)
    trocar = pwd in (0, "0") or (isinstance(pwd, datetime) and pwd.year <= 1601)
    status, status_motivo = situacao_conta(uac, ativo, bloqueado, expirada, trocar)
    return {
        "login": login, "nome": str(ler("displayName") or login), "email": ler("mail"),
        "unidade": unidade_do_dn(dn) or "", "dn": dn, "descricao": ler("description"),
        "cpf": mascarar_cpf(cpf), "matricula": str(ler("initials") or "").strip() or None,
        "ativo": ativo,
        "bloqueado": bloqueado,
        "senha_expirada": expirada,
        "senha_nunca_expira": bool(uac & 0x10000),
        "deve_trocar_senha": trocar,
        "status": status, "status_motivo": status_motivo,
        "senha_trocada_em": _iso(_data(pwd)),
        "ultimo_logon": _iso(_data(ler("lastLogonTimestamp"))),
        "protegida": int(ler("adminCount") or 0) == 1,
        **_uso(ler, uac),
    }


def _uso(ler, uac: int) -> dict:
    """Ha quantos dias a conta nao e usada (ultimo logon; se nunca entrou, desde a criacao).
    lastLogonTimestamp replica entre os DCs com ate ~14 dias de atraso: serve bem para a regra dos 90 dias."""
    referencia = _data(ler("lastLogonTimestamp")) or _data(ler("whenCreated"))
    dias = (datetime.now(timezone.utc) - referencia).days if referencia else None
    exige = bool(uac & 2) and (dias is None or dias > config.DIAS_REATIVACAO_SEI)
    return {"dias_sem_uso": dias, "exige_sei": exige}


def _ler_paginado(atributos: dict):
    def ler(nome):
        v = atributos.get(nome)
        if isinstance(v, list):
            v = v[0] if v else None
        return None if v in ("", []) else v
    return ler


# ------------------------------------------------------------------ cache da lista do AD
# Ler o AD inteiro e pesado. A lista fica em memoria e e entregue na hora; quando passa de _CACHE_SEGUNDOS,
# quem pede recebe a lista atual e uma atualizacao roda em segundo plano. O sistema ja carrega ao subir.
import threading
import time as _time
import unicodedata

_cache: dict = {"quando": 0.0, "contas": None, "por_login": {}, "ti": set(), "ti_quando": 0.0, "erro": None}
_CACHE_SEGUNDOS = 120
_GRUPO_TI_SEGUNDOS = 600
_trava = threading.Lock()
_atualizando = threading.Event()


def _sem_acento(texto) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or "").lower()) if unicodedata.category(c) != "Mn")


def _chave_busca(c: dict) -> str:
    return _sem_acento(" ".join(str(c.get(k) or "") for k in ("login", "nome", "email", "matricula", "unidade")))


def _carregar() -> None:
    inicio = _time.time()
    contas = listar_todos()
    for c in contas:
        c["_busca"], c["_ordem"] = _chave_busca(c), _sem_acento(c["nome"])
    if not _cache["ti_quando"] or _time.time() - _cache["ti_quando"] > _GRUPO_TI_SEGUNDOS:
        _cache["ti"], _cache["ti_quando"] = logins_do_grupo_ti(), _time.time()
    with _trava:
        _cache.update(contas=contas, por_login={c["login"]: c for c in contas}, quando=_time.time(), erro=None)
    log.info("Lista do AD carregada: %d contas em %.1fs", len(contas), _time.time() - inicio)


def _carregar_em_segundo_plano() -> None:
    if _atualizando.is_set():
        return
    _atualizando.set()

    def rodar():
        try:
            _carregar()
        except Exception as erro:  # noqa: BLE001 - mantem a lista antiga e tenta de novo na proxima
            _cache["erro"] = str(erro)
            log.warning("Atualizacao da lista do AD falhou: %s", erro)
        finally:
            _atualizando.clear()
    threading.Thread(target=rodar, daemon=True, name="lista-ad").start()


def aquecer() -> None:
    """Chamado ao subir o sistema: deixa a lista pronta antes de alguem abrir a tela."""
    if config.AD_HABILITADO and (config.AD_RESET_USUARIO or config.AD_BIND_USUARIO):
        _carregar_em_segundo_plano()


def limpar_cache() -> None:
    """Forca a proxima leitura a buscar o AD de novo (sem apagar a lista atual)."""
    _cache["quando"] = 0.0


def atualizar_conta(conta: dict) -> None:
    """Depois de uma acao (senha, desbloqueio, reativacao), troca so aquela conta na lista em memoria."""
    with _trava:
        if _cache["contas"] is None:
            return
        anterior = _cache["por_login"].get(conta["login"])
        nova = {**(anterior or {}), **{k: v for k, v in conta.items() if k != "dias_sem_uso_antes"}}
        nova["_busca"], nova["_ordem"] = _chave_busca(nova), _sem_acento(nova["nome"])
        nova.setdefault("cpf_digitos", "")
        _cache["por_login"][conta["login"]] = nova
        _cache["contas"] = [nova if c["login"] == conta["login"] else c for c in _cache["contas"]] if anterior \
            else _cache["contas"] + [nova]


def diretorio(atualizar: bool = False) -> tuple[list[dict], set[str]]:
    """Contas do AD + logins do grupo da TI. Responde na hora com a lista em memoria."""
    if atualizar or _cache["contas"] is None:
        _carregar()                      # primeira vez ou botao "Atualizar": espera a leitura
    elif _time.time() - _cache["quando"] > _CACHE_SEGUNDOS:
        _carregar_em_segundo_plano()     # lista antiga: entrega a atual e atualiza por tras
    return _cache["contas"], _cache["ti"]


def listar_todos() -> list[dict]:
    """Todas as contas de pessoas do AD, ativas e desativadas (busca paginada).
    Cada item traz 'cpf_digitos' so para a busca no servidor; a tela recebe o CPF mascarado."""
    def _op(conn):
        atributos = [a for a in _ATRIBUTOS if a != "msDS-User-Account-Control-Computed"]  # atributo calculado: so em busca de 1 conta
        contas = []
        for e in conn.extend.standard.paged_search(config.AD_BASE_DN, "(&(objectCategory=person)(objectClass=user))", SUBTREE,
                                                   attributes=atributos, paged_size=1000, generator=True):
            if e.get("type") != "searchResEntry":
                continue
            ler = _ler_paginado(e.get("attributes", {}))
            conta = _montar(str(e.get("dn", "")), ler)
            if not conta["login"]:
                continue
            conta["cpf_digitos"] = so_digitos(ler("employeeID"))
            contas.append(conta)
        return contas
    return ad.executar(lambda: _conectar(somente_criptografada=False), _op)


def logins_do_grupo_ti() -> set[str]:
    """Membros (inclusive por grupos dentro de grupos) do AD_GRUPO_PERMITIDO, para marcar a equipe da TI na lista."""
    if not config.AD_GRUPO_PERMITIDO:
        return set()

    def _op(conn):
        filtro = f"(&(objectCategory=person)(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(config.AD_GRUPO_PERMITIDO)}))"
        conn.search(config.AD_BASE_DN, filtro, SUBTREE, attributes=["sAMAccountName"])
        return {str(e.sAMAccountName.value).lower() for e in conn.entries}
    try:
        return ad.executar(lambda: _conectar(somente_criptografada=False), _op)
    except (ErroAD, LDAPException) as erro:
        log.warning("Nao foi possivel listar o grupo da TI: %s", erro)
        return set()


def buscar(termo: str, limite: int = 25) -> list[dict]:
    termo = escape_filter_chars(termo.strip())
    if len(termo) < 2:
        return []
    def _op(conn):
        filtro = (f"(&(objectCategory=person)(objectClass=user)"
                  f"(|(sAMAccountName=*{termo}*)(displayName=*{termo}*)(mail=*{termo}*)"
                  f"(employeeID=*{termo}*)(initials={termo})))")
        conn.search(config.AD_BASE_DN, filtro, SUBTREE, attributes=_ATRIBUTOS, size_limit=limite)
        return sorted((_conta(e) for e in conn.entries), key=lambda c: c["nome"].lower())
    return ad.executar(lambda: _conectar(somente_criptografada=False), _op)


def _localizar(conn, login: str):
    conn.search(config.AD_BASE_DN,
                f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter_chars(login)}))",
                SUBTREE, attributes=_ATRIBUTOS)
    if not conn.entries:
        raise ErroAD(f"A conta '{login}' não foi encontrada no AD.", credencial=True)
    return conn.entries[0]


def consultar(login: str) -> dict:
    def _op(conn):
        return _conta(_localizar(conn, normalizar_login(login)))
    return ad.executar(lambda: _conectar(somente_criptografada=False), _op)


def _erro_ldap(conn, acao: str, protegida: bool = False) -> ErroAD:
    r = conn.result or {}
    msg = str(r.get("message", ""))
    codigo = r.get("result")
    if protegida and (codigo == 50 or "INSUFF_ACCESS" in msg.upper()):
        # Contas de administradores (adminCount=1) usam as permissoes do AdminSDHolder, nao as delegadas na OU
        return ErroAD(f"O AD não deixou {acao}: esta é uma conta de administrador (protegida pelo AdminSDHolder) e a conta "
                      "de serviço da Gestão TI não tem permissão nela. Delegue \"Redefinir senha\" (e \"Gravar "
                      "userAccountControl\"/\"lockoutTime\") à conta de serviço no objeto AdminSDHolder "
                      "(CN=AdminSDHolder,CN=System,DC=santaluzia,DC=int).", credencial=True)
    if codigo == 19 or "0000052D" in msg.upper():
        return ErroAD("A senha não atende à política do domínio: tamanho mínimo, complexidade "
                      "(maiúsculas, minúsculas, números, símbolos) ou ela já foi usada antes.", credencial=True)
    if codigo == 50 or "INSUFF_ACCESS" in msg.upper():
        return ErroAD(f"A conta do sistema não tem permissão para {acao} desta conta no AD. "
                      "Peça para delegar a permissão na OU, ou a conta é protegida (administradores).", credencial=True)
    if codigo == 53:
        return ErroAD("O AD recusou a operação (a conexão precisa ser criptografada ou a senha não é aceita).")
    return ErroAD(f"O AD recusou: {r.get('description')} {msg}".strip())


def redefinir(login: str, nova_senha: str, exigir_troca: bool, desbloquear: bool) -> dict:
    """Troca a senha (reset administrativo, sem precisar da senha antiga)."""
    login = normalizar_login(login)
    conn = _conectar(somente_criptografada=True)
    try:
        conta = _conta(_localizar(conn, login))
        dn = conta["dn"]
        if not conn.extend.microsoft.modify_password(dn, nova_senha):
            raise _erro_ldap(conn, "trocar a senha", conta["protegida"])
        if exigir_troca and not conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]}):
            raise _erro_ldap(conn, "exigir a troca de senha")
        desbloqueou = bool(desbloquear and conta["bloqueado"])
        if desbloqueou and not conn.modify(dn, {"lockoutTime": [(MODIFY_REPLACE, [0])]}):
            raise _erro_ldap(conn, "desbloquear")
        final = _conta(_localizar(conn, login))
        final["desbloqueada_agora"] = desbloqueou
        atualizar_conta(final)
        return final
    finally:
        conn.unbind()


def desbloquear_conta(login: str) -> dict:
    login = normalizar_login(login)
    conn = _conectar(somente_criptografada=False)
    try:
        conta = _conta(_localizar(conn, login))
        if not conn.modify(conta["dn"], {"lockoutTime": [(MODIFY_REPLACE, [0])]}):
            raise _erro_ldap(conn, "desbloquear")
        final = _conta(_localizar(conn, login))
        atualizar_conta(final)
        return final
    finally:
        conn.unbind()


def reativar(login: str, nova_senha: str | None, exigir_troca: bool, ciente_sei: bool = False) -> dict:
    """Reativa uma conta desativada no AD (tira o bit 2 do userAccountControl).
    Com nova_senha, troca a senha antes: o AD recusa reativar conta sem senha valida pela politica."""
    login = normalizar_login(login)
    conn = _conectar(somente_criptografada=bool(nova_senha))
    try:
        e = _localizar(conn, login)
        conta = _conta(e)
        if conta["exige_sei"] and not ciente_sei:
            raise ErroAD(f"Esta conta está sem uso há mais de {config.DIAS_REATIVACAO_SEI} dias. A reativação precisa de "
                         "processo SEI: confirme na tela que o SEI existe.", credencial=True)
        dn = conta["dn"]
        if nova_senha:
            if not conn.extend.microsoft.modify_password(dn, nova_senha):
                raise _erro_ldap(conn, "trocar a senha", conta["protegida"])
            if exigir_troca and not conn.modify(dn, {"pwdLastSet": [(MODIFY_REPLACE, [0])]}):
                raise _erro_ldap(conn, "exigir a troca de senha")
        uac = int(_attr(e, "userAccountControl") or 512)
        if uac & 2 and not conn.modify(dn, {"userAccountControl": [(MODIFY_REPLACE, [uac & ~2])]}):
            r = conn.result or {}
            if r.get("result") == 53 or "0000052D" in str(r.get("message", "")).upper():
                raise ErroAD("O AD não deixou reativar porque a conta está sem uma senha válida. "
                             "Marque “Definir uma nova senha” e tente de novo.", credencial=True)
            raise _erro_ldap(conn, "reativar", conta["protegida"])
        if conta["bloqueado"]:
            conn.modify(dn, {"lockoutTime": [(MODIFY_REPLACE, [0])]})
        final = _conta(_localizar(conn, login))
        atualizar_conta(final)
        final["dias_sem_uso_antes"] = conta["dias_sem_uso"]
        return final
    finally:
        conn.unbind()
