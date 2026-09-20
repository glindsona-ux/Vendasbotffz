"""
permissoes.py — versão inicial simplificada. O bot original também aceita
um cargo customizado (cargo_admin, configurável em /configurar), mas esse
projeto ainda não tem o sistema genérico de config por servidor — por
enquanto só dono do servidor OU permissão "Administrador" do Discord.
Quando o /configurar desse V2 nascer, dá pra portar o resto igual.
"""

import discord
from emojis_app import E


async def eh_admin_do_bot(interaction: discord.Interaction) -> bool:
    """True se o usuário pode usar comandos administrativos do bot
    NESTE servidor (não confundir com o dono do bot/SaaS — ver owner.py)."""
    if interaction.guild is None:
        return False
    if interaction.user.id == interaction.guild.owner_id:
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    return False


async def checar_admin_ou_avisar(interaction: discord.Interaction) -> bool:
    """Faz a checagem de admin e já responde com o erro se não tiver
    permissão. Retorna True quando pode continuar, False quando já
    respondeu com a mensagem de erro (o comando deve dar `return`)."""
    if await eh_admin_do_bot(interaction):
        return True
    await interaction.response.send_message(
        f"{E.ERRO} Você não tem permissão para usar esse comando.", ephemeral=True
    )
    return False
