"""Catalogo de modulos da Gestao TI e regras de permissao.

Qualquer usuario pode receber qualquer modulo, mas so o adm geral (e quem ele autorizar) concede permissoes.
A equipe da TI recebe os modulos padrao no primeiro acesso. Quem nao tem permissao nenhuma ve os modulos
marcados com aguarda=True (os de e-mail) bloqueados, ate a TI liberar.
"""
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
        "nome": "E-mails para usuários do AD",
        "descricao": "Disparo em lote para os e-mails dos usuários ativos do Active Directory, em blocos.",
        "url": "/m/email?origem=ad", "icone": "email", "disponivel": True, "aguarda": True,
    },
    "email_lista": {
        "nome": "E-mails por lista (TXT)",
        "descricao": "Disparo em lote para uma lista de e-mails enviada em arquivo TXT.",
        "url": "/m/email?origem=lista", "icone": "lista", "disponivel": True, "aguarda": True,
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

# Modulos dados automaticamente a quem da TI entra pela primeira vez
PADRAO_TI = [m for m, d in MODULOS.items() if d["disponivel"]]


def permitidos_para(ti: bool = True) -> list[str]:
    """Modulos que podem ser concedidos (todos os disponiveis; o controle e de quem concede)."""
    return [m for m, d in MODULOS.items() if d["disponivel"]]


def aguardando() -> list[str]:
    """Modulos que aparecem bloqueados para quem ainda nao tem permissao (os de e-mail)."""
    return [m for m, d in MODULOS.items() if d["disponivel"] and d.get("aguarda")]


def efetivos(ti: bool, concedidos) -> list[str]:
    """Modulos que o usuario realmente acessa: os concedidos que a regra permite, na ordem do catalogo."""
    pode = set(permitidos_para(ti))
    dados = set(concedidos)
    return [m for m in MODULOS if m in dados and m in pode]


def catalogo() -> list[dict]:
    return [{"id": m, **d, "grupo_nome": GRUPOS.get(d.get("grupo"), {}).get("nome")} for m, d in MODULOS.items()]


def grupos() -> dict:
    return GRUPOS
