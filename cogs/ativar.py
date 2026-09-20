import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from owner import is_owner


class Ativar(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ativar", description="Ativa a licença do FFZ Vendas nesse servidor com uma key.")
    @app_commands.describe(chave="A chave de ativação que você recebeu na compra")
    async def ativar(self, interaction: discord.Interaction, chave: str):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só funciona dentro de um servidor.", ephemeral=True)
            return

        sucesso, motivo, dados = await db.ativar_chave(chave, interaction.guild.id, interaction.user.id)

        if sucesso:
            view = licenca.montar_view_licenca(
                f"{licenca.emoji_plano(dados['plano'])} Licença ativada com sucesso!",
                [
                    f"**Plano:** {dados['plano']}",
                    f"**Duração:** {dados['dias']} dias",
                    f"**Vence em:** `{dados['vence']}`",
                    "---",
                    "Use `/licenca` a qualquer momento pra conferir o status.",
                ],
                client=interaction.client,
            )
            await licenca.responder_seguro(interaction, view=view, ephemeral=True)
            return

        mensagens = {
            "nao_encontrada": "❌ Essa chave não existe. Confere se copiou certinho.",
            "cancelada": "❌ Essa chave foi cancelada e não pode mais ser usada.",
            "ja_usada": "❌ Essa chave já foi usada em outro servidor — cada chave só serve pra um servidor.",
        }
        await licenca.responder_seguro(
            interaction,
            content=mensagens.get(motivo, "❌ Não foi possível ativar essa chave."),
            ephemeral=True,
        )

    @app_commands.command(name="licenca", description="Mostra o status da licença deste servidor.")
    async def licenca_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Esse comando só funciona dentro de um servidor.", ephemeral=True)
            return

        status = await licenca.verificar_licenca(interaction.guild.id)

        if not status["ativo"]:
            view = licenca.montar_view_licenca(
                "🔒 Sem licença ativa",
                ["Use `/ativar chave:SUA-KEY-AQUI` pra ativar."],
                client=interaction.client,
            )
            await interaction.response.send_message(view=view, ephemeral=True)
            return

        tempo_txt, dias_restantes = licenca.formatar_tempo_restante(status["vence"])
        dias_totais = licenca.PLANOS.get((status["plano"] or "").upper(), 30)
        barra = licenca.barra_progresso(dias_restantes, dias_totais)

        view = licenca.montar_view_licenca(
            f"{licenca.emoji_plano(status['plano'])} Licença ativa — {status['plano']}",
            [
                f"**Tempo restante:** {tempo_txt}",
                barra,
                f"**Vence em:** `{status['vence']}`",
            ],
            client=interaction.client,
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="gerarkey", description="[DONO] Gera uma nova key de ativação pra vender.")
    @app_commands.describe(plano="Plano da key", dias="Quantos dias de acesso essa key concede")
    @app_commands.choices(plano=[
        app_commands.Choice(name="Básico", value="BASICO"),
        app_commands.Choice(name="Premium", value="PREMIUM"),
        app_commands.Choice(name="Vitalício", value="VITALICIO"),
    ])
    @is_owner()
    async def gerarkey(self, interaction: discord.Interaction, plano: app_commands.Choice[str], dias: int = None):
        dias_final = dias if dias is not None else licenca.PLANOS.get(plano.value, 30)
        nova_key = await db.criar_chave(plano.value, dias_final, interaction.user.id)

        view = licenca.montar_view_licenca(
            f"{licenca.emoji_plano(plano.value)} Key gerada — {plano.name}",
            [
                f"**Duração:** {dias_final} dias",
                "---",
                "Copie e envie pro cliente. Essa é a ÚNICA vez que a key aparece em texto puro.",
            ],
            client=interaction.client,
            chave=nova_key,
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="listarkeys", description="[DONO] Lista as últimas keys geradas.")
    @is_owner()
    async def listarkeys(self, interaction: discord.Interaction):
        chaves = await db.listar_chaves(limit=15)
        if not chaves:
            await interaction.response.send_message("Nenhuma key gerada ainda.", ephemeral=True)
            return

        linhas = []
        for c in chaves:
            status = "🚫 cancelada" if c["cancelada"] else ("✅ usada" if c["usado_por"] else "⬜ disponível")
            linhas.append(f"`#{c['id']}` {c['chave_mascarada']} — {c['plano']} ({c['dias']}d) — {status}")

        view = licenca.montar_view_licenca("🔑 Últimas keys geradas", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="revogarlicenca", description="[DONO] Revoga a licença de um servidor (chargeback, calote, etc).")
    @is_owner()
    async def revogarlicenca(self, interaction: discord.Interaction, guild_id: str):
        await db.revogar_licenca(int(guild_id))
        await interaction.response.send_message(f"✅ Licença do servidor `{guild_id}` revogada.", ephemeral=True)

    @app_commands.command(name="estenderlicenca", description="[DONO] Estende a licença de um servidor.")
    @is_owner()
    async def estenderlicenca(self, interaction: discord.Interaction, guild_id: str, dias: int):
        nova_vence = await db.estender_licenca(int(guild_id), dias)
        await interaction.response.send_message(f"✅ Licença estendida até `{nova_vence}`.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ativar(bot))
