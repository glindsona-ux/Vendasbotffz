"""
cogs/pedidos.py — Gestão de pedidos depois do checkout: aprovação manual
do pagamento (v1 não tem webhook automático de banco — mesmo motivo do
FFZ Salas estar pausado, dependência de API de terceiro), entrega
automática de estoque, entrega manual e consulta de status.
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from constants import STATUS_PEDIDO_LABEL, STATUS_PEDIDO_EMOJI
from permissoes import checar_admin_ou_avisar
from emojis_app import E


def _resumo_entrega(resultado: dict) -> list[str]:
    linhas = []
    for item in resultado.get("entregue_auto", []):
        conteudo = "\n".join(f"`{c}`" for c in item["itens"])
        linhas.append(f"{E.RAIO} **{item['nome']}** (entregue automaticamente):\n{conteudo}")
    for item in resultado.get("pendente_manual", []):
        motivo = "sem estoque no momento" if item["motivo"] == "sem_estoque" else "entrega manual"
        linhas.append(f"{E.TICKET} **{item['nome']}** x{item['quantidade']} — pendente ({motivo})")
    return linhas


class Pedidos(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    pedido = app_commands.Group(name="pedido", description="Gerenciar pedidos da loja (admin).")

    @pedido.command(name="aprovar", description="[ADMIN] Confirma o pagamento de um pedido e dispara a entrega.")
    @app_commands.describe(id="ID do pedido (veja no aviso de \"Já paguei\" ou em /pedido listar)")
    async def aprovar(self, interaction: discord.Interaction, id: int):
        if not await checar_admin_ou_avisar(interaction):
            return

        pedido_atual = await db.obter_pedido(id)
        if not pedido_atual or pedido_atual["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(f"{E.ERRO} Pedido não encontrado nesse servidor.", ephemeral=True)
            return
        if pedido_atual["status"] not in ("aguardando_pagamento",):
            await interaction.response.send_message(
                f"{E.ERRO} Esse pedido já está com status **{STATUS_PEDIDO_LABEL.get(pedido_atual['status'], pedido_atual['status'])}**.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # A transição AGUARDANDO -> PAGO é atômica: se outro admin aprovou o
        # mesmo pedido no mesmo instante, só UM chega aqui com True. Quem
        # perde não pode disparar a entrega, senão o estoque sai duas vezes.
        if not await db.marcar_pedido_pago(id, aprovado_por=interaction.user.id):
            await interaction.followup.send(
                f"{E.ERRO} Esse pedido acabou de ser aprovado (ou cancelado/expirado) por outra pessoa.", ephemeral=True
            )
            return
        resultado = await db.processar_entrega_automatica(id)
        pedido_atualizado = await db.obter_pedido(id)

        linhas_entrega = _resumo_entrega(resultado)
        status_final = STATUS_PEDIDO_LABEL.get(pedido_atualizado["status"], pedido_atualizado["status"])

        view = licenca.montar_view_licenca(
            f"{E.OK} Pedido `#{id}` aprovado — {status_final}",
            linhas_entrega or ["Nada a entregar automaticamente."],
            client=interaction.client,
        )
        await interaction.followup.send(view=view, ephemeral=True)

        # Avisa o cliente por DM com o que já foi entregue / o que falta.
        try:
            comprador = await interaction.client.fetch_user(pedido_atualizado["user_id"])
            texto_dm = f"{E.OK} Seu pedido `#{id}` em **{interaction.guild.name}** foi aprovado!\n\n" + "\n".join(linhas_entrega)
            if resultado.get("pendente_manual"):
                texto_dm += "\n\nUm admin vai te chamar pra finalizar a entrega manual em breve."
            await comprador.send(texto_dm)
        except (discord.Forbidden, discord.HTTPException):
            pass

        if resultado.get("pendente_manual"):
            await interaction.channel.send(
                f"{E.TICKET} Pedido `#{id}` de <@{pedido_atualizado['user_id']}> tem item(ns) pendente(s) de entrega manual — "
                f"quando entregar, rode `/pedido entregarmanual id:{id}`."
            )

    @pedido.command(name="entregarmanual", description="[ADMIN] Marca um pedido como totalmente entregue após entrega manual.")
    @app_commands.describe(id="ID do pedido")
    async def entregarmanual(self, interaction: discord.Interaction, id: int):
        if not await checar_admin_ou_avisar(interaction):
            return

        pedido_atual = await db.obter_pedido(id)
        if not pedido_atual or pedido_atual["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(f"{E.ERRO} Pedido não encontrado nesse servidor.", ephemeral=True)
            return
        if pedido_atual["status"] != "pago":
            await interaction.response.send_message(
                f"{E.ERRO} Esse pedido está com status **{STATUS_PEDIDO_LABEL.get(pedido_atual['status'], pedido_atual['status'])}**, não dá pra marcar como entregue agora.",
                ephemeral=True,
            )
            return

        await db.marcar_pedido_entregue_manual(id)
        await interaction.response.send_message(f"{E.OK} Pedido `#{id}` marcado como entregue.", ephemeral=True)

    @pedido.command(name="cancelar", description="[ADMIN] Cancela um pedido que ainda está aguardando pagamento.")
    @app_commands.describe(id="ID do pedido")
    async def cancelar(self, interaction: discord.Interaction, id: int):
        if not await checar_admin_ou_avisar(interaction):
            return

        pedido_atual = await db.obter_pedido(id)
        if not pedido_atual or pedido_atual["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(f"{E.ERRO} Pedido não encontrado nesse servidor.", ephemeral=True)
            return

        if not await db.cancelar_pedido(id):
            # Pedido já pago/entregue não pode ser cancelado aqui: o estoque
            # já saiu pro cliente, e "devolver" ele ao estoque o venderia de novo.
            await interaction.response.send_message(
                f"{E.ERRO} Só dá pra cancelar pedido que está **aguardando pagamento** — o `#{id}` está com status "
                f"**{STATUS_PEDIDO_LABEL.get(pedido_atual['status'], pedido_atual['status'])}**.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"{E.OK} Pedido `#{id}` cancelado.", ephemeral=True)

    @pedido.command(name="listar", description="[ADMIN] Lista os últimos pedidos do servidor.")
    async def listar(self, interaction: discord.Interaction):
        if not await checar_admin_ou_avisar(interaction):
            return

        pedidos = await db.listar_pedidos_guild(interaction.guild.id, limit=15)
        if not pedidos:
            await interaction.response.send_message("Nenhum pedido ainda.", ephemeral=True)
            return

        linhas = []
        for p in pedidos:
            emoji = STATUS_PEDIDO_EMOJI.get(p["status"], "•")
            label = STATUS_PEDIDO_LABEL.get(p["status"], p["status"])
            cupom = f" {E.CUPOM}{p['cupom_codigo']}" if p.get("cupom_codigo") else ""
            linhas.append(f"`#{p['id']}` <@{p['user_id']}> — R$ {p['valor_total']:.2f}{cupom} — {emoji} {label}")

        view = licenca.montar_view_licenca(f"{E.ESTOQUE} Últimos pedidos", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)

    @app_commands.command(name="meuspedidos", description="Mostra seus pedidos nesse servidor.")
    async def meuspedidos(self, interaction: discord.Interaction):
        meus = await db.listar_pedidos_usuario(interaction.guild.id, interaction.user.id, limit=15)
        if not meus:
            await interaction.response.send_message("Você ainda não tem pedidos nesse servidor.", ephemeral=True)
            return

        linhas = []
        for p in meus:
            emoji = STATUS_PEDIDO_EMOJI.get(p["status"], "•")
            label = STATUS_PEDIDO_LABEL.get(p["status"], p["status"])
            linhas.append(f"`#{p['id']}` — R$ {p['valor_total']:.2f} — {emoji} {label}")

        view = licenca.montar_view_licenca(f"{E.ESTOQUE} Seus pedidos", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Pedidos(bot))
