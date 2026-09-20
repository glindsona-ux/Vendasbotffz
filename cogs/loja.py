"""
cogs/loja.py — Painel de loja voltado pro cliente: catálogo, carrinho e
checkout via Pix (QR Code + Copia e Cola gerados por pix_utils.py,
portado do Bot_ffz original).

Fluxo: /loja -> escolhe produto (Select) -> vai pro carrinho (pode aplicar
cupom de desconto) -> Finalizar compra -> QR Code gerado na hora com o
valor total já com desconto -> cliente paga e
clica "Já paguei" -> pedido fica visível pra admin confirmar com
`/pedido aprovar` (cogs/pedidos.py). Entrega automática (estoque) roda
sozinha assim que o admin aprova; entrega manual vira aviso pro admin
entregar na mão.
"""

import io

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from constants import MINUTOS_EXPIRAR_PEDIDO, PRODUTOS_POR_PAGINA, TIPO_ENTREGA_EMOJI, TipoEntrega
from pix_utils import gerar_qrcode_bytes, obter_logo_guild


def _linha_produto(p: dict) -> str:
    emoji = TIPO_ENTREGA_EMOJI[TipoEntrega(p["tipo_entrega"])]
    aviso_estoque = " — *sem estoque no momento*" if p["tipo_entrega"] == TipoEntrega.AUTOMATICA.value and p["estoque_disponivel"] == 0 else ""
    return f"**`#{p['id']}`** {p['nome']} {emoji} — R$ {p['preco']:.2f}{aviso_estoque}"


class SelectProduto(discord.ui.Select):
    def __init__(self, produtos: list[dict]):
        opcoes = [
            discord.SelectOption(
                label=f"{p['nome']} — R$ {p['preco']:.2f}",
                value=str(p["id"]),
                description=(p["descricao"] or "")[:100] or None,
            )
            for p in produtos[:25]
        ]
        super().__init__(placeholder="Escolha um produto pra adicionar ao carrinho...", options=opcoes)

    async def callback(self, interaction: discord.Interaction):
        produto_id = int(self.values[0])
        produto_atual = await db.obter_produto(produto_id)
        if not produto_atual or not produto_atual["ativo"]:
            await interaction.response.send_message("❌ Esse produto não está mais disponível.", ephemeral=True)
            return

        if produto_atual["tipo_entrega"] == TipoEntrega.AUTOMATICA.value:
            disponivel = await db.contar_estoque_disponivel(produto_id)
            if disponivel == 0:
                await interaction.response.send_message("❌ Esse produto está sem estoque no momento.", ephemeral=True)
                return

        await db.adicionar_ao_carrinho(interaction.guild.id, interaction.user.id, produto_id)
        await interaction.response.send_message(
            f"✅ **{produto_atual['nome']}** adicionado ao carrinho. Use `/carrinho` pra finalizar a compra.",
            ephemeral=True,
        )


class LojaView(discord.ui.View):
    def __init__(self, produtos: list[dict]):
        super().__init__(timeout=300)
        if produtos:
            self.add_item(SelectProduto(produtos))


class BotaoRemoverItem(discord.ui.Button):
    def __init__(self, produto_id: int, nome: str):
        super().__init__(label=f"Remover {nome[:60]}", style=discord.ButtonStyle.secondary, emoji="🗑️")
        self.produto_id = produto_id

    async def callback(self, interaction: discord.Interaction):
        await db.remover_do_carrinho(interaction.guild.id, interaction.user.id, self.produto_id)
        await _atualizar_carrinho(interaction)


class BotaoEsvaziarCarrinho(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Esvaziar carrinho", style=discord.ButtonStyle.danger, emoji="🧹")

    async def callback(self, interaction: discord.Interaction):
        await db.limpar_carrinho(interaction.guild.id, interaction.user.id)
        await _atualizar_carrinho(interaction)


class BotaoFinalizarCompra(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Finalizar compra (gerar Pix)", style=discord.ButtonStyle.success, emoji="💳")

    async def callback(self, interaction: discord.Interaction):
        config = await db.obter_config_loja(interaction.guild.id)
        if not config or not config.get("chave_pix"):
            await interaction.response.send_message(
                "❌ A loja ainda não tem uma chave Pix configurada. Peça pra um admin rodar `/configurarpagamento`.",
                ephemeral=True,
            )
            return

        # defer ANTES de mexer no banco: o checkout pode esperar a trava de
        # pedidos e o Discord só dá 3s pra primeira resposta.
        await interaction.response.defer(ephemeral=True, thinking=True)

        pedido, erro = await db.criar_pedido_do_carrinho(interaction.guild.id, interaction.user.id)
        if not pedido:
            await interaction.followup.send(f"❌ {erro}", ephemeral=True)
            return

        logo_bytes = await obter_logo_guild(interaction.guild)
        img_bytes, payload = gerar_qrcode_bytes(
            config["chave_pix"],
            config["nome_recebedor"] or "PAGAMENTO PIX",
            cidade=config.get("cidade") or "Sao Paulo",
            valor=pedido["valor_total"],
            logo_bytes=logo_bytes,
        )
        await db.salvar_payload_pix(pedido["id"], payload)

        resumo = "\n".join(f"• {i['quantidade']}x {i['nome']} — R$ {i['preco']:.2f}" for i in pedido["itens"])
        if pedido.get("desconto"):
            resumo += (
                f"\n\nSubtotal: R$ {pedido['valor_bruto']:.2f}"
                f"\n🎟️ Cupom `{pedido['cupom_codigo']}`: −R$ {pedido['desconto']:.2f}"
            )
        arquivo = discord.File(io.BytesIO(img_bytes.read()), filename="pix.png")

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"### 💳 Pedido `#{pedido['id']}` — R$ {pedido['valor_total']:.2f}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(resumo))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"**Pix Copia e Cola:**\n```\n{payload}\n```"))
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://pix.png")))
        linha_botoes = discord.ui.ActionRow()
        linha_botoes.add_item(BotaoJaPaguei(pedido["id"]))
        container.add_item(linha_botoes)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# FFZ VENDAS • Pague e clique em \"Já paguei\" pra avisar o vendedor • "
            f"O pedido expira em {MINUTOS_EXPIRAR_PEDIDO} min se não for pago"
        ))
        view.add_item(container)

        await interaction.followup.send(view=view, file=arquivo, ephemeral=True)


class ModalCupom(discord.ui.Modal, title="Usar cupom de desconto"):
    codigo = discord.ui.TextInput(
        label="Código do cupom",
        placeholder="Ex: BLACKFRIDAY",
        min_length=2,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild_id, user_id = interaction.guild.id, interaction.user.id
        itens = await db.obter_carrinho(guild_id, user_id)
        if not itens:
            await interaction.response.send_message("❌ Seu carrinho está vazio.", ephemeral=True)
            return

        calculo = await db.calcular_cupom(guild_id, user_id, str(self.codigo), itens)
        if not calculo["ok"]:
            await interaction.response.send_message(f"❌ {calculo['erro']}", ephemeral=True)
            return

        await db.definir_cupom_carrinho(guild_id, user_id, str(self.codigo))
        await _atualizar_carrinho(interaction)


class BotaoCupom(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Usar cupom", style=discord.ButtonStyle.secondary, emoji="🎟️")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModalCupom())


class BotaoRemoverCupom(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Tirar cupom", style=discord.ButtonStyle.secondary, emoji="❌")

    async def callback(self, interaction: discord.Interaction):
        await db.remover_cupom_carrinho(interaction.guild.id, interaction.user.id)
        await _atualizar_carrinho(interaction)


class BotaoJaPaguei(discord.ui.Button):
    def __init__(self, pedido_id: int):
        super().__init__(label="Já paguei", style=discord.ButtonStyle.primary, emoji="✅", custom_id=f"loja_ja_paguei_{pedido_id}")
        self.pedido_id = pedido_id

    async def callback(self, interaction: discord.Interaction):
        pedido = await db.obter_pedido(self.pedido_id)
        if not pedido or pedido["status"] != "aguardando_pagamento":
            await interaction.response.send_message("Esse pedido já não está mais aguardando pagamento.", ephemeral=True)
            return

        self.disabled = True
        self.label = "Aguardando confirmação..."
        await interaction.response.edit_message(view=self.view)

        await interaction.followup.send(
            f"📥 **Novo pagamento a confirmar** — Pedido `#{pedido['id']}` de {interaction.user.mention} "
            f"— R$ {pedido['valor_total']:.2f}\nUm admin precisa rodar `/pedido aprovar id:{pedido['id']}` pra liberar a entrega.",
        )


def _montar_view_carrinho(resumo: dict) -> discord.ui.LayoutView:
    """Tela do carrinho (usada tanto pelo /carrinho quanto pelos botões que
    atualizam a mesma mensagem) — uma função só pra os dois nunca divergirem."""
    itens = resumo["itens"]
    view = discord.ui.LayoutView(timeout=180)
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay("### 🛒 Seu carrinho"))
    container.add_item(discord.ui.Separator())

    if not itens:
        texto_vazio = "Vazio. Use `/loja` pra adicionar produtos."
        if resumo.get("aviso_cupom"):
            texto_vazio += f"\n\n⚠️ Seu cupom foi removido: {resumo['aviso_cupom']}"
        container.add_item(discord.ui.TextDisplay(texto_vazio))
    else:
        linhas = [f"• {i['quantidade']}x **{i['nome']}** — R$ {i['preco'] * i['quantidade']:.2f}" for i in itens]
        linhas.append("")
        if resumo["cupom"]:
            linhas.append(f"Subtotal: R$ {resumo['subtotal']:.2f}")
            linhas.append(f"🎟️ Cupom `{resumo['cupom']['codigo']}`: −R$ {resumo['desconto']:.2f}")
        linhas.append(f"**Total: R$ {resumo['total']:.2f}**")
        if resumo.get("aviso_cupom"):
            linhas.append(f"\n⚠️ Seu cupom foi removido: {resumo['aviso_cupom']}")
        container.add_item(discord.ui.TextDisplay("\n".join(linhas)))
        container.add_item(discord.ui.Separator())

        linha1 = discord.ui.ActionRow()
        for i in itens[:5]:
            linha1.add_item(BotaoRemoverItem(i["produto_id"], i["nome"]))
        container.add_item(linha1)

        linha2 = discord.ui.ActionRow()
        linha2.add_item(BotaoFinalizarCompra())
        linha2.add_item(BotaoRemoverCupom() if resumo["cupom"] else BotaoCupom())
        linha2.add_item(BotaoEsvaziarCarrinho())
        container.add_item(linha2)

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay("-# FFZ VENDAS • Carrinho"))
    view.add_item(container)
    return view


async def _atualizar_carrinho(interaction: discord.Interaction):
    resumo = await db.resumo_carrinho(interaction.guild.id, interaction.user.id)
    view = _montar_view_carrinho(resumo)

    if interaction.response.is_done():
        await interaction.edit_original_response(view=view)
    else:
        await interaction.response.edit_message(view=view)


class Loja(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="loja", description="Abre o catálogo de produtos da loja.")
    async def loja(self, interaction: discord.Interaction):
        produtos = await db.listar_produtos(interaction.guild.id, apenas_ativos=True)
        if not produtos:
            await interaction.response.send_message("A loja ainda não tem produtos cadastrados.", ephemeral=True)
            return

        linhas = [_linha_produto(p) for p in produtos[:PRODUTOS_POR_PAGINA * 5]]
        view = licenca.montar_view_licenca("🛒 Loja", linhas, client=interaction.client)

        select_view = LojaView(produtos)
        await interaction.response.send_message(view=view, ephemeral=True)
        await interaction.followup.send(view=select_view, ephemeral=True)

    @app_commands.command(name="carrinho", description="Mostra seu carrinho de compras.")
    async def carrinho(self, interaction: discord.Interaction):
        resumo = await db.resumo_carrinho(interaction.guild.id, interaction.user.id)
        await interaction.response.send_message(view=_montar_view_carrinho(resumo), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Loja(bot))
