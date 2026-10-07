"""Login do painel pelo Active Directory.

O usuario entra com o login e a senha do dominio. A aplicacao faz o bind no AD
com essas credenciais (nao precisa de conta de servico) e depois confere se a
conta esta dentro da OU permitida e/ou e membro do grupo permitido.
"""
import logging
import ssl

from ldap3 import SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPCommunicationError, LDAPException, LDAPResponseTimeoutError
from ldap3.utils.conv import escape_filter_chars

from . import config

log = logging.getLogger("ad")

# Codigos que o AD devolve junto com o erro 49 (invalidCredentials)
_MOTIVOS = {
    "52e": "Usuário ou senha incorretos.",
    "525": "Usuário ou senha incorretos.",
    "530": "Este usuário não pode entrar neste horário.",
    "531": "Este usuário não pode entrar a partir deste computador.",
    "532": "A senha do domínio expirou. Troque a senha no Windows e tente de novo.",
    "533": "A conta está desativada no AD.",
    "701": "A conta expirou no AD.",
    "773": "É preciso trocar a senha do domínio antes de entrar.",
    "775": "A conta está bloqueada no AD. Aguarde ou peça o desbloqueio.",
}


class ErroAD(Exception):
    """Falha de login. `credencial=True` quando o AD recusou a senha ou a conta."""

    def __init__(self, mensagem: str, credencial: bool = False):
        super().__init__(mensagem)
        self.credencial = credencial


def unidade_do_dn(dn: str) -> str | None:
    """'CN=Fulano,OU=Infra,OU=TI,DC=...' -> 'Infra' (a OU onde a conta esta)."""
    for parte in dn.split(","):
        chave, _, valor = parte.strip().partition("=")
        if chave.upper() == "OU" and valor:
            return valor
    return None


def normalizar_login(usuario: str) -> str:
    """Aceita 'joao', 'SANTA-LUZIA\\joao' ou 'joao@santaluzia.int' e devolve 'joao'."""
    usuario = (usuario or "").strip()
    if "\\" in usuario:
        usuario = usuario.split("\\", 1)[1]
    if "@" in usuario:
        usuario = usuario.split("@", 1)[0]
    return usuario.lower()


def _modos() -> list[str]:
    if config.AD_SEGURANCA in ("ldaps", "starttls", "nenhuma"):
        return [config.AD_SEGURANCA]
    modos = ["ldaps", "starttls"]
    if config.AD_PERMITIR_SEM_TLS:
        modos.append("nenhuma")
    return modos


# Rotas (servidor, modo) que funcionaram ou falharam: evita esperar o tempo limite de um DC/modo que nao responde
# (ex.: LDAPS 636 fechado faz cada tentativa esperar AD_TIMEOUT segundos antes de cair no StartTLS).
_rota_boa: dict[str, tuple[str, str]] = {}
_rota_ruim: dict[tuple[str, str], float] = {}
_ESQUECER_FALHA = 300  # segundos


def rotas(modos: list[str], chave: str = "padrao") -> list[tuple[str, str]]:
    """(servidor, modo) na ordem de tentativa: o que funcionou por ultimo primeiro; os que falharam ha pouco, por ultimo."""
    import time
    todas = [(h, m) for h in config.AD_SERVIDORES for m in modos]
    agora_ = time.time()
    boa = _rota_boa.get(chave)
    # 1) a rota que funcionou por ultimo; 2) as que nao falharam ha pouco; 3) no outro DC, o mesmo modo que funcionou
    return sorted(todas, key=lambda r: (r != boa, agora_ - _rota_ruim.get(r, 0) < _ESQUECER_FALHA,
                                        bool(boa) and r[1] != boa[1]))


def lembrar(rota: tuple[str, str], ok: bool, chave: str = "padrao", host_fora: bool | None = None) -> None:
    """Guarda o resultado de uma tentativa. Falha na porta 389 (StartTLS/sem TLS) ou no meio de uma consulta
    = o DC esta fora do ar: todas as rotas dele vao para o fim da fila por 5 minutos."""
    import time
    if ok:
        _rota_boa[chave] = rota
        for r in [r for r in _rota_ruim if r[0] == rota[0]]:
            _rota_ruim.pop(r, None)
        return
    if host_fora is None:
        host_fora = rota[1] != "ldaps"   # LDAPS recusado pode ser so falta de certificado; 389 fora = DC fora
    agora_ = time.time()
    for r in ([(rota[0], m) for m in ("ldaps", "starttls", "nenhuma")] if host_fora else [rota]):
        _rota_ruim[r] = agora_
    for k, v in list(_rota_boa.items()):
        if v == rota or (host_fora and v[0] == rota[0]):
            _rota_boa.pop(k, None)
    if host_fora:
        log.warning("DC %s fora do ar: usando o outro servidor do AD pelos proximos minutos.", rota[0])


def executar(conectar, operacao, chave: str = "padrao"):
    """Roda operacao(conn) numa conexao aberta por conectar(). Se o DC cair no meio da consulta,
    marca o DC como fora e repete no outro servidor do AD."""
    tentativas = max(2, len(config.AD_SERVIDORES))
    for n in range(1, tentativas + 1):
        conn = conectar()
        try:
            return operacao(conn)
        except (LDAPCommunicationError, LDAPResponseTimeoutError, OSError) as erro:
            rota = getattr(conn, "rota_gt", None)
            log.warning("O AD parou de responder durante a consulta (%s): %s", rota, erro)
            if rota:
                lembrar(rota, False, chave, host_fora=True)
            if n == tentativas:
                raise ErroAD("Os servidores do AD não responderam. Tente de novo em instantes.")
        finally:
            try:
                conn.unbind()
            except Exception:  # noqa: BLE001 - conexao ja caida
                pass


def _tls() -> Tls:
    return Tls(validate=ssl.CERT_REQUIRED if config.AD_VALIDAR_CERTIFICADO else ssl.CERT_NONE)


def _abrir(host: str, modo: str, usuario_bind: str, senha: str) -> Connection:
    """Abre a conexao e faz o bind. Separado para poder ser substituido nos testes."""
    if modo == "ldaps":
        servidor = Server(host, port=636, use_ssl=True, tls=_tls(), connect_timeout=config.AD_TIMEOUT)
    else:
        servidor = Server(host, port=389, use_ssl=False, tls=_tls(), connect_timeout=config.AD_TIMEOUT)
    conn = Connection(servidor, user=usuario_bind, password=senha, authentication=SIMPLE,
                      receive_timeout=config.AD_TIMEOUT, raise_exceptions=False)
    conn.open()
    if modo == "starttls" and not conn.start_tls():
        conn.unbind()
        raise LDAPException(f"StartTLS recusado por {host}")
    conn.bind()
    return conn


def _conectar(login: str, senha: str) -> Connection:
    usuario_bind = f"{login}@{config.AD_DOMINIO}"
    ultimo_erro = "nenhum servidor do AD configurado"
    for host, modo in rotas(_modos()):
            try:
                conn = _abrir(host, modo, usuario_bind, senha)
            except (LDAPException, OSError) as erro:
                ultimo_erro = f"{host} ({modo}): {erro}"
                log.warning("AD indisponivel em %s", ultimo_erro)
                lembrar((host, modo), False)
                continue
            if conn.bound:
                lembrar((host, modo), True)
                conn.rota_gt = (host, modo)
                return conn
            resultado = conn.result or {}
            conn.unbind()
            if resultado.get("result") == 49:
                # Senha recusada: para aqui para nao gastar tentativas e bloquear a conta
                mensagem = str(resultado.get("message", "")).lower()
                motivo = next((texto for codigo, texto in _MOTIVOS.items() if f"data {codigo}" in mensagem),
                              "Usuário ou senha incorretos.")
                raise ErroAD(motivo, credencial=True)
            ultimo_erro = f"{host} ({modo}): {resultado.get('description')} {resultado.get('message')}"
            log.warning("Bind recusado em %s", ultimo_erro)
    raise ErroAD("Não foi possível falar com o Active Directory. Tente de novo ou use o login local.")


def autenticar(usuario: str, senha: str, exigir_admin: bool = False) -> dict:
    """Valida no AD e devolve {'usuario', 'nome', 'email', 'dn'} ou levanta ErroAD.

    exigir_admin=False: qualquer conta ativa do dominio (portal de solicitacao).
    exigir_admin=True:  so contas dentro da OU e/ou do grupo permitido (Painel Administrativo).
    """
    if not config.AD_HABILITADO:
        raise ErroAD("O login pelo AD está desativado no servidor.")
    login = normalizar_login(usuario)
    if not login or not senha:
        raise ErroAD("Informe usuário e senha.", credencial=True)
    if exigir_admin and not (config.AD_OU_PERMITIDA or config.AD_GRUPO_PERMITIDO):
        log.error("AD_OU_PERMITIDA e AD_GRUPO_PERMITIDO vazios: ninguem do AD pode entrar no painel.")
        raise ErroAD("O acesso da TI pelo AD ainda não foi configurado no servidor.")

    conn = _conectar(login, senha)
    try:
        filtro_usuario = f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter_chars(login)}))"
        conn.search(config.AD_BASE_DN, filtro_usuario, SUBTREE,
                    attributes=["displayName", "mail", "distinguishedName", "userAccountControl"])
        if not conn.entries:
            raise ErroAD("Usuário não encontrado no AD.", credencial=True)
        entrada = conn.entries[0]
        dn = str(entrada.entry_dn)
        uac = int(entrada.userAccountControl.value or 0) if "userAccountControl" in entrada else 0
        if uac & 2:
            raise ErroAD("A conta está desativada no AD.", credencial=True)

        ti = _eh_ti(conn, login, dn)
        if exigir_admin and not ti:
            log.info("Painel negado para %s (fora da OU/grupo permitido): %s", login, dn)
            raise ErroAD("Seu usuário não tem permissão para acessar o Painel Administrativo.", credencial=True)

        nome = str(entrada.displayName.value) if "displayName" in entrada and entrada.displayName.value else login
        email = str(entrada.mail.value) if "mail" in entrada and entrada.mail.value else None
        return {"usuario": login, "nome": nome, "email": email, "dn": dn, "unidade": unidade_do_dn(dn), "ti": ti}
    finally:
        conn.unbind()


def _eh_ti(conn, login: str, dn: str) -> bool:
    """A conta esta na OU da TI (AD_OU_PERMITIDA, inclui sub-OUs) ou no grupo AD_GRUPO_PERMITIDO?"""
    if config.AD_OU_PERMITIDA and dn.lower().endswith("," + config.AD_OU_PERMITIDA.strip().lower()):
        return True
    if config.AD_GRUPO_PERMITIDO:
        # 1.2.840.113556.1.4.1941 = LDAP_MATCHING_RULE_IN_CHAIN (considera grupos dentro de grupos)
        filtro_grupo = (f"(&(sAMAccountName={escape_filter_chars(login)})"
                        f"(memberOf:1.2.840.113556.1.4.1941:={escape_filter_chars(config.AD_GRUPO_PERMITIDO)}))")
        conn.search(config.AD_BASE_DN, filtro_grupo, SUBTREE, attributes=["sAMAccountName"])
        return bool(conn.entries)
    return False


def _conectar_servico() -> Connection:
    """Conexao com a conta de servico (AD_BIND_USUARIO), usada para buscar e listar usuarios."""
    if not (config.AD_HABILITADO and config.AD_BIND_USUARIO and config.AD_BIND_SENHA):
        raise ErroAD("A conta de serviço do AD (AD_BIND_USUARIO e AD_BIND_SENHA) não está configurada no servidor.")
    bind = config.AD_BIND_USUARIO if "@" in config.AD_BIND_USUARIO or "=" in config.AD_BIND_USUARIO \
        else f"{config.AD_BIND_USUARIO}@{config.AD_DOMINIO}"
    ultimo = "nenhum servidor do AD configurado"
    for host, modo in rotas(_modos()):
            try:
                conn = _abrir(host, modo, bind, config.AD_BIND_SENHA)
            except (LDAPException, OSError) as erro:
                ultimo = f"{host} ({modo}): {erro}"
                lembrar((host, modo), False)
                continue
            if conn.bound:
                lembrar((host, modo), True)
                conn.rota_gt = (host, modo)
                return conn
            ultimo = f"{host} ({modo}): {(conn.result or {}).get('description')}"
            conn.unbind()
    log.warning("Conta de servico do AD indisponivel: %s", ultimo)
    raise ErroAD("Não foi possível conectar ao AD com a conta de serviço. Verifique AD_BIND_USUARIO e AD_BIND_SENHA.")




def listar_usuarios_com_email() -> list[dict]:
    """Todos os usuarios ativos do AD que tem e-mail (busca paginada, funciona com dezenas de milhares)."""
    def ler(conn):
        filtro = "(&(objectCategory=person)(objectClass=user)(mail=*))"
        resultado = []
        for e in conn.extend.standard.paged_search(config.AD_BASE_DN, filtro, SUBTREE,
                                                   attributes=["sAMAccountName", "displayName", "mail", "userAccountControl"],
                                                   paged_size=500, generator=True):
            if e.get("type") != "searchResEntry":
                continue
            a = e.get("attributes", {})
            uac = a.get("userAccountControl")
            uac = uac[0] if isinstance(uac, list) and uac else uac
            if int(uac or 0) & 2:  # bit 2 do userAccountControl = conta desativada
                continue
            email = a.get("mail")
            email = (email[0] if isinstance(email, list) and email else email) or ""
            if not email or "@" not in str(email):
                continue
            login = a.get("sAMAccountName")
            login = str(login[0] if isinstance(login, list) else login or "").lower()
            nome = a.get("displayName")
            nome = str(nome[0] if isinstance(nome, list) and nome else nome or login)
            dn = str(e.get("dn", ""))
            resultado.append({"login": login, "nome": nome, "email": str(email).strip().lower(),
                              "unidade": unidade_do_dn(dn) or ""})
        resultado.sort(key=lambda u: u["nome"].lower())
        return resultado
    return executar(_conectar_servico, ler)


def aliases_de(proxy_addresses, email_principal: str | None) -> list[str]:
    """Enderecos extras da conta (proxyAddresses 'smtp:alias@dominio'), sem repetir o e-mail principal.
    E onde ficam os aliases que a pessoa recebe e pode usar como remetente (ex.: geral@, informatica@)."""
    resultado = []
    for valor in proxy_addresses or []:
        tipo, _, endereco = str(valor).partition(":")
        endereco = endereco.strip().lower()
        if tipo.lower() == "smtp" and "@" in endereco and endereco != (email_principal or "") and endereco not in resultado:
            resultado.append(endereco)
    return resultado


def consultar_usuario(login: str) -> dict | None:
    """Dados de uma conta pelo login (nome, e-mail, OU e se e da TI). None se nao existir."""
    login = normalizar_login(login)
    def ler(conn):
        conn.search(config.AD_BASE_DN,
                    f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter_chars(login)}))",
                    SUBTREE, attributes=["displayName", "mail", "userAccountControl", "proxyAddresses"])
        if not conn.entries:
            return None
        e = conn.entries[0]
        dn = str(e.entry_dn)
        email = str(e.mail.value).strip().lower() if "mail" in e and e.mail.value else None
        return {"usuario": login, "dn": dn, "unidade": unidade_do_dn(dn), "ti": _eh_ti(conn, login, dn),
                "nome": str(e.displayName.value) if "displayName" in e and e.displayName.value else login,
                "email": email,
                "aliases": aliases_de(e.proxyAddresses.values if "proxyAddresses" in e else [], email),
                "ativo": not (int(e.userAccountControl.value or 0) & 2) if "userAccountControl" in e else True}
    return executar(_conectar_servico, ler)


def buscar_usuarios(termo: str, limite: int = 20) -> list[dict]:
    """Busca pessoas no AD por nome ou login. Exige a conta de servico (AD_BIND_USUARIO)."""
    if not (config.AD_HABILITADO and config.AD_BIND_USUARIO and config.AD_BIND_SENHA):
        return []
    termo = escape_filter_chars(termo.strip())
    if len(termo) < 2:
        return []

    def ler(conn):
        filtro = (f"(&(objectCategory=person)(objectClass=user)"
                  f"(|(sAMAccountName=*{termo}*)(displayName=*{termo}*)(mail=*{termo}*)))")
        conn.search(config.AD_BASE_DN, filtro, SUBTREE, attributes=["sAMAccountName", "displayName", "mail"],
                    size_limit=limite)
        return [{"login": str(e.sAMAccountName.value).lower(),
                 "nome": str(e.displayName.value) if e.displayName.value else str(e.sAMAccountName.value),
                 "email": str(e.mail.value) if e.mail.value else None} for e in conn.entries]
    try:
        return executar(_conectar_servico, ler)
    except ErroAD as erro:
        log.warning("Busca no AD falhou: %s", erro)
        return []


def diagnosticar(usuario: str, senha: str) -> None:
    """Uso: docker exec -it gestao-ti-api python -m app.ad usuario
    Mostra qual servidor e modo de seguranca funcionam e se o usuario seria aceito."""
    login = normalizar_login(usuario)
    print(f"Testando login de '{login}@{config.AD_DOMINIO}'")
    for host in config.AD_SERVIDORES:
        for modo in ("ldaps", "starttls", "nenhuma"):
            try:
                conn = _abrir(host, modo, f"{login}@{config.AD_DOMINIO}", senha)
                estado = "OK" if conn.bound else f"recusado: {conn.result.get('description')} {conn.result.get('message')}"
                conn.unbind()
            except (LDAPException, OSError) as erro:
                estado = f"sem conexao: {erro}"
            print(f"  {host:<15} {modo:<9} {estado}")
    print(f"\nModos usados pelo painel (AD_SEGURANCA={config.AD_SEGURANCA}): {', '.join(_modos())}")
    for rotulo, admin in (("Login comum", False), ("Equipe da TI (OU/grupo)", True)):
        try:
            dados = autenticar(usuario, senha, exigir_admin=admin)
            print(f"{rotulo}: ACEITO como {dados['nome']} - {dados.get('unidade')} ({dados['dn']})")
        except ErroAD as erro:
            print(f"{rotulo}: NEGADO - {erro}")


if __name__ == "__main__":
    import getpass
    import sys

    nome_usuario = sys.argv[1] if len(sys.argv) > 1 else input("Usuario do AD: ")
    diagnosticar(nome_usuario, getpass.getpass("Senha: "))
