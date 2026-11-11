"""Catalogo de modulos da Gestao TI e regras de permissao.

Qualquer usuario pode receber qualquer modulo, mas so o adm geral (e quem ele autorizar) concede permissoes.
A equipe da TI recebe os modulos padrao no primeiro acesso. Os modulos marcados com para_todos=True (os de e-mail)
ficam liberados para qualquer pessoa que entra, sem precisar de permissao (o envio depende de creditos), enquanto
a opcao EMAIL_PARA_TODOS (Configuracoes) estiver ligada. Da para bloquear esses modulos para uma pessoa (linha
"-modulo" na tabela de permissoes) ou desligar a liberacao geral: ai eles voltam a depender de permissao.
"""
from . import ajustes

MODULOS: dict[str, dict] = {
    "wifi": {
        "nome": "Wi-Fi no computador",
        "descricao": "Pedidos de liberação do Wi-Fi nos computadores, bloqueios, computadores e registro de atividades.",
        "url": "/m/wifi", "icone": "wifi", "disponivel": True, "grupo": "rede_sem_fio",
    },
    "wifi_visitante": {
        "nome": "Wi-Fi Visitante",
        "descricao": "Visitantes cadastrados no portal do Wi-Fi: trocar a senha, ativar e desativar o cadastro.",
        "url": "/m/wifi-visitante", "icone": "visitante", "disponivel": True, "grupo": "rede_sem_fio",
    },
    "email_ad": {
        "nome": "E-mails para servidores da Prefeitura",
        "descricao": "Envie um e-mail para os servidores da Prefeitura, escolhendo as pessoas pelo nome ou pelo setor.",
        "url": "/m/email?origem=ad", "icone": "email", "disponivel": True, "para_todos": True,
    },
    "email_lista": {
        "nome": "E-mails por lista",
        "descricao": "Envie um e-mail para uma lista de endereços que você tem num arquivo de texto (um e-mail por linha).",
        "url": "/m/email?origem=lista", "icone": "lista", "disponivel": True, "para_todos": True,
    },
    "creditos_email": {
        "nome": "Créditos de e-mail",
        "descricao": "Pedidos de créditos para os disparos de e-mail: aprovar, recusar e liberar créditos aos usuários.",
        "url": "/m/creditos-email", "icone": "moeda", "disponivel": True, "padrao_ti": False,
    },
    "configuracoes": {
        "nome": "Configurações",
        "descricao": "Servidor de e-mail (SMTP), remetente e limites dos disparos.",
        "url": "/m/configuracoes", "icone": "engrenagem", "disponivel": True,
    },
    "logs": {
        "nome": "Logs",
        "descricao": "Tudo o que foi feito na Gestão TI, em todos os módulos: quem, o quê, quando e de onde.",
        "url": "/m/logs", "icone": "registro", "disponivel": True,
    },
    "usuarios": {
        "nome": "Gestão de Usuários",
        "descricao": "Usuários do AD e locais: permissões, redefinir senha, desbloquear e reativar contas.",
        "url": "/m/usuarios", "icone": "usuarios", "disponivel": True,
    },
}

# Grupos: modulos que aparecem juntos em um unico card da tela inicial
GRUPOS = {
    "rede_sem_fio": {"nome": "Rede Sem Fio", "descricao": "Wi-Fi nos computadores da prefeitura e visitantes do portal do Wi-Fi.",
                     "url": "/m/rede-sem-fio", "icone": "wifi"},
}

# Modulos dados automaticamente a quem da TI entra pela primeira vez.
# Creditos de e-mail fica de fora: quem tem esse modulo envia sem limite e libera creditos para os outros.
PADRAO_TI = [m for m, d in MODULOS.items() if d["disponivel"] and d.get("padrao_ti", True)]


def permitidos_para(ti: bool = True) -> list[str]:
    """Modulos que podem ser concedidos (todos os disponiveis; o controle e de quem concede)."""
    return [m for m, d in MODULOS.items() if d["disponivel"]]


def aguardando() -> list[str]:
    """Modulos que aparecem como "aguardando liberacao" para quem nao tem permissao: os de e-mail, quando a
    liberacao geral esta desligada."""
    return [] if liberacao_geral() else liberaveis()


def liberaveis() -> list[str]:
    """Modulos que podem ficar liberados para todos (os de e-mail)."""
    return [m for m, d in MODULOS.items() if d["disponivel"] and d.get("para_todos")]


def liberacao_geral() -> bool:
    return str(ajustes.obter("EMAIL_PARA_TODOS")).lower() == "sim"


def para_todos() -> list[str]:
    """Modulos liberados agora para qualquer pessoa, sem permissao (vazio se a liberacao geral estiver desligada)."""
    return liberaveis() if liberacao_geral() else []


def bloqueados(concedidos) -> list[str]:
    """Modulos bloqueados para a pessoa (linhas "-modulo" nas permissoes)."""
    return [c[1:] for c in concedidos if c.startswith("-")]


def linhas_permissao(lista: list[str], atuais=()) -> set[str]:
    """O que gravar nas permissoes: os modulos marcados e, para os liberaveis desmarcados, um bloqueio ("-modulo").
    Com a liberacao geral desligada, desmarcar so tira a permissao; bloqueios que ja existiam ficam guardados."""
    if liberacao_geral():
        return set(lista) | {"-" + m for m in liberaveis() if m not in lista}
    return set(lista) | {"-" + m for m in bloqueados(atuais) if m not in lista}


def efetivos(ti: bool, concedidos) -> list[str]:
    """Modulos que o usuario realmente acessa: os concedidos que a regra permite e os liberados para todos,
    menos os bloqueados para a pessoa, na ordem do catalogo."""
    pode = set(permitidos_para(ti))
    concedidos = list(concedidos)
    dados = ({c for c in concedidos if not c.startswith("-")} | set(para_todos())) - set(bloqueados(concedidos))
    return [m for m in MODULOS if m in dados and m in pode]


def catalogo() -> list[dict]:
    geral = liberacao_geral()
    return [{"id": m, **d, "liberavel": bool(d.get("para_todos")), "para_todos": bool(d.get("para_todos")) and geral,
             "grupo_nome": GRUPOS.get(d.get("grupo"), {}).get("nome")} for m, d in MODULOS.items()]


def grupos() -> dict:
    return GRUPOS
