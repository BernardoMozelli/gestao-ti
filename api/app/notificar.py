"""Avisos para a TI quando chega uma solicitacao nova (Telegram, WhatsApp, e-mail)."""
import logging

import httpx

from . import config

log = logging.getLogger("notificar")


def _tempo(horas) -> str:
    if not horas:
        return "tempo indeterminado"
    if horas >= 24 and horas % 24 == 0:
        return f"{horas // 24} dia(s)"
    return f"{horas} hora(s)"


def _texto(sol) -> str:
    login = f" ({sol.usuario_login})" if getattr(sol, "usuario_login", None) else ""
    return (
        f"{config.APP_NOME} - nova solicitação nº {sol.id}\n"
        f"Computador: {sol.hostname}\n"
        f"Usuário: {sol.usuario}{login}\n"
        f"Tempo pedido: {_tempo(sol.horas_solicitadas)}\n"
        f"Motivo: {sol.motivo}\n\n"
        f"Liberar ou negar: {config.PAINEL_URL.rstrip('/')}/m/wifi"
    )


def _telegram(texto: str) -> None:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat in config.TELEGRAM_CHAT_ID.split(","):
        r = httpx.post(url, json={"chat_id": chat.strip(), "text": texto}, timeout=10)
        r.raise_for_status()


def _whatsapp(texto: str) -> None:
    if not (config.EVOLUTION_URL and config.EVOLUTION_INSTANCE and config.EVOLUTION_APIKEY and config.WHATSAPP_NUMEROS):
        return
    url = f"{config.EVOLUTION_URL}/message/sendText/{config.EVOLUTION_INSTANCE}"
    for numero in config.WHATSAPP_NUMEROS:
        r = httpx.post(url, headers={"apikey": config.EVOLUTION_APIKEY},
                       json={"number": numero, "text": texto}, timeout=15)
        r.raise_for_status()


def nova_solicitacao(sol) -> None:
    """Executado em segundo plano: uma falha num canal nao impede os outros."""
    texto = _texto(sol)
    for nome, envio in (("telegram", lambda: _telegram(texto)),
                        ("whatsapp", lambda: _whatsapp(texto))):
        try:
            envio()
        except Exception as erro:  # noqa: BLE001
            log.warning("Falha ao notificar por %s: %s", nome, erro)


def pedido_creditos(pedido_id: int, nome: str, login: str, quantidade: int, motivo: str) -> None:
    """Aviso de pedido de creditos de e-mail (mesmos canais da TI: Telegram e WhatsApp, se configurados)."""
    texto = (f"{config.APP_NOME} - pedido de créditos de e-mail nº {pedido_id}\n"
             f"Usuário: {nome} ({login})\n"
             f"Quantidade: {quantidade} crédito{'' if quantidade == 1 else 's'}\n"
             f"Motivo: {motivo}\n\n"
             f"Aprovar ou recusar: {config.PAINEL_URL.rstrip('/')}/m/creditos-email")
    for nome_canal, envio in (("telegram", lambda: _telegram(texto)), ("whatsapp", lambda: _whatsapp(texto))):
        try:
            envio()
        except Exception as erro:  # noqa: BLE001
            log.warning("Falha ao notificar pedido de créditos por %s: %s", nome_canal, erro)
