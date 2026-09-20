"""
========================================================================
 LICENCA.PY — SISTEMA CENTRAL DE LICENCIAMENTO (FFZ VENDAS V2)
========================================================================
Portado quase sem alteração do bot FFZ E-Sports original — é o mesmo
sistema já validado em produção lá, só trocando o nome do produto no
rodapé dos embeds/cards.

Único ponto de verdade pra saber se um servidor pode usar o bot. Todo
comando, painel, botão e embed do bot deve passar por aqui.

Regra de negócio: SEM registro de assinatura = BLOQUEADO. Não existe modo
grátis. Servidor só usa o bot depois de ativar uma key com /ativar.
========================================================================
"""

import time
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands

import database as db
import emojis_app

logger = logging.getLogger("ffzvendas.licenca")

_ultimo_bloqueio_mostrado: dict[int, float] = {}


def bloqueio_recente(guild_id: int, janela_segundos: float = 4.0) -> bool:
    instante = _ultimo_bloqueio_mostrado.get(guild_id)
    return instante is not None and (time.monotonic() - instante) < janela_segundos


# Planos fixos disponíveis (dias de duração).
PLANOS = {
    "BASICO": 30,
    "PREMIUM": 30,
    "VITALICIO": 36500,  # ~100 anos, efetivamente sem vencimento
}

CORES_PLANO = {
    "BASICO": 0xD9D9D9,
    "PREMIUM": 0xFFFFFF,
    "VITALICIO": 0xF5F5F5,
}

PLANO_EMOJIS = {
    "BASICO": "🔘",
    "PREMIUM": "⚪",
    "VITALICIO": "💎",
}


def emoji_plano(plano: str) -> str:
    p = (plano or "").upper()
    return emojis_app.obter(f"ffz_{p.lower()}", PLANO_EMOJIS.get(p, "🔑"))


def barra_progresso(dias_restantes: float, dias_totais: float, tamanho: int = 12) -> str:
    if not dias_totais or dias_totais <= 0:
        dias_totais = 1
    proporcao = max(0.0, min(1.0, dias_restantes / dias_totais))
    preenchido = round(proporcao * tamanho)
    preenchido = max(0, min(tamanho, preenchido))
    return "⬜" * preenchido + "⬛" * (tamanho - preenchido)


def formatar_tempo_restante(vence_str: str) -> tuple[str, float]:
    if not vence_str:
        return "—", 0.0
    try:
        vence = datetime.strptime(vence_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return vence_str, 0.0

    delta = vence - datetime.now()
    dias_restantes = delta.total_seconds() / 86400

    if dias_restantes <= 0:
        return "Vencida", 0.0

    dias = int(dias_restantes)
    horas = int((dias_restantes - dias) * 24)
    if dias >= 1:
        texto = f"{dias} dia{'s' if dias != 1 else ''}"
        if horas > 0:
            texto += f" e {horas}h"
    else:
        texto = f"{horas}h restantes" if horas > 0 else "menos de 1h"
    return texto, dias_restantes


class BotaoCopiarKey(discord.ui.Button):
    def __init__(self, chave: str):
        super().__init__(
            label="Copiar Key",
            emoji=emojis_app.obter("ffz_copiar", "📋"),
            style=discord.ButtonStyle.secondary,
            custom_id="licenca_copiar_key",
        )
        self.chave = chave

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"`{self.chave}`", ephemeral=True)


def montar_view_licenca(
    titulo: str,
    linhas: list[str],
    client: discord.Client = None,
    chave: str = None,
) -> discord.ui.LayoutView:
    """Card branco (Components V2, sem accent_color) compartilhado por
    todo o sistema de licença."""
    view = discord.ui.LayoutView(timeout=180 if chave else None)
    container = discord.ui.Container()

    cabecalho = discord.ui.TextDisplay(f"### {titulo}")
    if client and client.user:
        container.add_item(discord.ui.Section(cabecalho, accessory=discord.ui.Thumbnail(media=client.user.display_avatar.url)))
    else:
        container.add_item(cabecalho)

    container.add_item(discord.ui.Separator())

    bloco_atual = []
    for linha in linhas:
        if linha == "---":
            if bloco_atual:
                container.add_item(discord.ui.TextDisplay("\n".join(bloco_atual)))
                bloco_atual = []
            container.add_item(discord.ui.Separator())
        else:
            bloco_atual.append(linha)
    if bloco_atual:
        container.add_item(discord.ui.TextDisplay("\n".join(bloco_atual)))

    if chave:
        linha_botoes = discord.ui.ActionRow()
        linha_botoes.add_item(BotaoCopiarKey(chave))
        container.add_item(linha_botoes)

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay("-# FFZ VENDAS • Licenciamento"))

    view.add_item(container)
    return view


async def verificar_licenca(guild_id: int, usar_cache: bool = False) -> dict:
    """Fonte única de verdade sobre o status da licença de um servidor.
    Nunca levanta exceção. Se a licença venceu, já auto-desativa (self-
    healing)."""
    try:
        assinatura = None
        ultimo_erro = None
        for tentativa in range(1, 4):
            try:
                assinatura = await (db.obter_assinatura(guild_id) if usar_cache else db.obter_assinatura_fresca(guild_id))
                ultimo_erro = None
                break
            except Exception as e:
                ultimo_erro = e
                if tentativa < 3:
                    import asyncio
                    await asyncio.sleep(0.15 * tentativa)
        if ultimo_erro is not None:
            raise ultimo_erro

        if not assinatura:
            return {"ativo": False, "motivo": "sem_licenca", "plano": None, "vence": None}

        if not assinatura["ativo"]:
            return {"ativo": False, "motivo": "revogado", "plano": assinatura["plano"], "vence": assinatura["vence"]}

        vence_str = assinatura["vence"]
        if vence_str:
            vence = datetime.strptime(vence_str, "%Y-%m-%d %H:%M:%S")
            if vence < datetime.now():
                await db.desativar_assinatura(guild_id)
                return {"ativo": False, "motivo": "expirado", "plano": assinatura["plano"], "vence": vence_str}

        return {"ativo": True, "motivo": "ok", "plano": assinatura["plano"], "vence": vence_str}
    except Exception as e:
        logger.error(f"Erro ao verificar licença da guild {guild_id}: {e}", exc_info=True)
        return {"ativo": False, "motivo": "erro_interno", "plano": None, "vence": None}


def _embed_bloqueio(status: dict, client: discord.Client = None) -> discord.Embed:
    motivo = status["motivo"]
    plano_txt = f"{emoji_plano(status['plano'])} {status['plano']}" if status.get("plano") else None

    if motivo == "sem_licenca":
        desc = (
            "Este servidor **não possui uma licença ativa** do FFZ Vendas.\n\n"
            "Fale com quem vendeu o bot pra você pra receber uma **key** e ativar com:\n"
            "`/ativar chave:SUA-KEY-AQUI`"
        )
    elif motivo == "expirado":
        desc = (
            f"A licença deste servidor **venceu em `{status['vence']}`** (plano {plano_txt}).\n\n"
            "Peça uma nova key pra renovar com:\n"
            "`/ativar chave:SUA-KEY-AQUI`"
        )
    elif motivo == "erro_interno":
        desc = (
            "Não foi possível verificar a licença deste servidor agora (erro interno).\n\n"
            "Tenta de novo em alguns segundos."
        )
    else:  # revogado
        desc = (
            f"A licença deste servidor (plano {plano_txt}) foi **revogada** pelo dono do bot.\n\n"
            "Entre em contato pra regularizar."
        )

    embed = discord.Embed(title="🔒 Servidor sem acesso", description=desc, color=0xE0E0E0, timestamp=datetime.now())
    if client and client.user:
        embed.set_thumbnail(url=client.user.display_avatar.url)
    embed.set_footer(text="FFZ VENDAS • Licenciamento")
    return embed


async def checar_licenca_view(interaction: discord.Interaction, guild_id: int = None) -> bool:
    """Usar dentro de interaction_check() de Views persistentes (botões).
    Aceita guild_id explícito pra views que respondem em DM."""
    guild_id = guild_id if guild_id is not None else interaction.guild_id

    status = await verificar_licenca(guild_id)

    if status["ativo"]:
        return True

    embed = _embed_bloqueio(status, client=interaction.client)
    _ultimo_bloqueio_mostrado[guild_id] = time.monotonic()
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException as e:
        logger.warning(f"Falha ao enviar embed de bloqueio de licença (guild {guild_id}): {type(e).__name__}: {e}")
    except Exception as e:
        logger.error(f"Erro inesperado em checar_licenca_view (guild {guild_id}): {e}", exc_info=True)
    return False


async def responder_seguro(interaction: discord.Interaction, **kwargs) -> bool:
    """Envia a resposta final sem quebrar se a interação já tiver sido
    "gasta" (ex: cliente mobile reenviando a mesma interação)."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(**kwargs)
            return True
    except (discord.InteractionResponded, discord.HTTPException) as e:
        logger.warning(f"1ª tentativa de resposta falhou (guild {interaction.guild_id}): {type(e).__name__}: {e} — tentando via followup.")

    try:
        await interaction.followup.send(**kwargs)
        return True
    except discord.HTTPException as e:
        logger.error(f"Não consegui entregar a resposta final (guild {interaction.guild_id}): {type(e).__name__}: {e}")
        return False


def requer_licenca():
    """Decorator pra slash commands. Colocar IMEDIATAMENTE abaixo de
    @app_commands.command."""
    async def predicate(interaction: discord.Interaction) -> bool:
        return await checar_licenca_view(interaction)
    return app_commands.check(predicate)


# Comandos que PRECISAM funcionar mesmo sem licença nenhuma.
COMANDOS_SEM_LICENCA = {"ativar", "licenca", "comandos"}


def comando_isento_de_licenca(command) -> bool:
    """True se esse comando não precisa passar pelo check de licença:
    comandos do dono do bot (@is_owner(), detectado automaticamente) ou
    os poucos comandos "públicos" da lista acima."""
    if command is None:
        return False
    if command.qualified_name in COMANDOS_SEM_LICENCA:
        return True
    for check in getattr(command, "checks", []):
        if getattr(check, "__qualname__", "") == "is_owner.<locals>.predicate":
            return True
    return False
