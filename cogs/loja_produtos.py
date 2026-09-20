"""
Loja Digital — produto + estoque + entrega automática (conta/key/código).

Sistema NOVO, independente da "Loja" de coins do cassino.py (aquela vende
cargo/cosmético com a moeda interna do cassino; essa aqui entrega um
produto de verdade pro cliente — conta, key, código de ativação, etc).

Por enquanto essa é só a PRIMEIRA peça (produto + estoque + entrega): o
botão "Comprar" já entrega o item na hora, sem cobrar nada sozinho — serve
pra quem cobra por fora (Pix manual) e só ativa o produto depois de
confirmar o pagamento, ou pra brindes/testes. O carrinho + checkout com
Pix automático é a próxima peça, ainda não implementada aqui.

Fluxo:
1. Admin cria o produto (/loja_admin -> Criar Novo)
2. Admin cola o estoque (1 conta/key por linha, quantas quiser de uma vez)
3. Admin publica a vitrine (/loja) no canal desejado
4. Cliente clica "Comprar" -> pega 1 unidade do estoque, marca como
   entregue, manda por DM (ephemeral se a DM falhar), loga a venda.
"""
import discord
from discord import app_commands, ui
from discord.ext import commands
import database as db
import emojis_app
import re
from licenca import requer_licenca, checar_licenca_view
from permissoes import checar_admin_ou_avisar

_EMOJIS_PADRAO = {
    "ffz_produto": "📦",
    "ffz_estoque": "📥",
    "ffz_comprar": "🛒",
    "ffz_preco": "💰",
    "ffz_entregue": "✅",
    "ffz_vazio": "⛔",
    "ffz_editar": "✏️",
    "ffz_apagar": "🗑️",
    "ffz_ativar": "🟢",
    "ffz_desativar": "🔴",
    "ffz_loja": "🏪",
    "ffz_historico": "📋",
    "ffz_voltar": "↩️",
}


def _e(nome: str) -> str:
    return emojis_app.obter(nome, _EMOJIS_PADRAO.get(nome, "•"))


async def _eh_admin_loja(interaction: discord.Interaction) -> bool:
    """Mesma régua de admin do resto do bot (dono do server, Admin nativo
    do Discord, ou cargo_admin configurado em /configurar)."""
    return await checar_admin_ou_avisar(interaction)


# ============================================================================
# HUB ADMIN — listar/escolher produto (mesmo padrão do HubPontoView em
# cogs/ponto.py: um Select pra escolher um produto existente ou criar um
# novo, e uma "tela de config" por produto com botões de gerenciamento)
# ============================================================================
async def montar_embed_hub(guild: discord.Guild, produtos: list) -> discord.Embed:
    embed = discord.Embed(
        title=f"{_e('ffz_loja')} Loja Digital — Produtos",
        description="Escolha um produto existente pra gerenciar, ou crie um novo pelo menu abaixo.",
        color=0x2ECC71,
    )
    if not produtos:
        embed.add_field(name="Produtos deste servidor", value="_Nenhum produto criado ainda._", inline=False)
    else:
        linhas = []
        for p in produtos[:24]:
            status = _e("ffz_ativar") if p["ativo"] else _e("ffz_desativar")
            qtd = await db.contar_estoque_disponivel(p["id"])
            linhas.append(f"{status} **{p['nome']}** — R$ {p['preco']:.2f} — estoque: `{qtd}` — `#{p['id']}`")
        if len(produtos) > 24:
            linhas.append(f"_...e mais {len(produtos) - 24} produto(s). Mostrando os 24 primeiros._")
        embed.add_field(name=f"Produtos deste servidor ({len(produtos)})", value="\n".join(linhas), inline=False)
    return embed


class ModalCriarProduto(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="📦 Novo Produto")
        self.nome = discord.ui.TextInput(max_length=80, placeholder="Ex: Conta Free Fire Diamante")
        self.add_item(discord.ui.Label(text="Nome do produto", component=self.nome))
        self.preco = discord.ui.TextInput(max_length=10, placeholder="Ex: 9.90", default="0")
        self.add_item(discord.ui.Label(text="Preço (R$)", component=self.preco))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_valor = float(str(self.preco.value).replace(",", ".").strip() or "0")
        except ValueError:
            return await interaction.response.send_message(
                f"{_e('ffz_vazio')} Preço inválido — manda só número, tipo `9.90`.", ephemeral=True
            )
        if preco_valor < 0:
            return await interaction.response.send_message(f"{_e('ffz_vazio')} Preço não pode ser negativo.", ephemeral=True)

        produto_id = await db.criar_produto(
            interaction.guild_id, str(self.nome.value).strip(), "", preco_valor, None, interaction.user.id
        )
        produto = await db.obter_produto(produto_id)
        await interaction.response.edit_message(
            content=f"{_e('ffz_entregue')} Produto **{self.nome.value}** criado! Agora cola o estoque e edita os detalhes pelos botões abaixo.",
            embed=await montar_embed_config(interaction.guild, produto),
            view=PainelConfigProdutoView(produto_id),
        )


class EscolherProdutoSelect(discord.ui.Select):
    def __init__(self, produtos: list):
        opcoes = [
            discord.SelectOption(
                label=p["nome"][:100],
                description=f"#{p['id']} • R$ {p['preco']:.2f} • {'Ativo' if p['ativo'] else 'Desativado'}",
                emoji=_e("ffz_ativar") if p["ativo"] else _e("ffz_desativar"),
                value=str(p["id"]),
            )
            for p in produtos[:24]
        ]
        opcoes.append(discord.SelectOption(label="➕ Criar Novo Produto", description="Dar um nome e configurar do zero", emoji="🆕", value="_criar_novo"))
        super().__init__(placeholder="Escolha um produto pra gerenciar, ou crie um novo...", options=opcoes, row=0)

    async def callback(self, interaction: discord.Interaction):
        escolha = self.values[0]
        if escolha == "_criar_novo":
            await interaction.response.send_modal(ModalCriarProduto())
            return

        produto = await db.obter_produto(int(escolha))
        if not produto:
            produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=False)
            embed = await montar_embed_hub(interaction.guild, produtos)
            await interaction.response.edit_message(content="⚠️ Esse produto não existe mais.", embed=embed, view=HubLojaView(produtos))
            return

        await interaction.response.edit_message(
            content=None, embed=await montar_embed_config(interaction.guild, produto), view=PainelConfigProdutoView(int(escolha))
        )


class HubLojaView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await checar_licenca_view(interaction)

    def __init__(self, produtos: list):
        super().__init__(timeout=300)
        self.add_item(EscolherProdutoSelect(produtos))


# ============================================================================
# TELA DE CONFIG DE 1 PRODUTO
# ============================================================================
async def montar_embed_config(guild: discord.Guild, produto: dict) -> discord.Embed:
    qtd = await db.contar_estoque_disponivel(produto["id"])
    status = "🟢 Ativo (aparece na vitrine)" if produto["ativo"] else "🔴 Desativado (some da vitrine)"
    embed = discord.Embed(
        title=f"{produto['emoji'] or _e('ffz_produto')} {produto['nome']}",
        description=produto["descricao"] or "_Sem descrição ainda._",
        color=0x2ECC71 if produto["ativo"] else 0x95A5A6,
    )
    embed.add_field(name=f"{_e('ffz_preco')} Preço", value=f"R$ {produto['preco']:.2f}", inline=True)
    embed.add_field(name=f"{_e('ffz_estoque')} Estoque disponível", value=f"`{qtd}`", inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.set_footer(text=f"Produto #{produto['id']}")
    return embed


class ModalEditarProduto(discord.ui.Modal):
    def __init__(self, produto: dict):
        titulo = f"Editar {produto['nome']}"
        if len(titulo) > 45:
            titulo = titulo[:44] + "…"
        super().__init__(title=titulo)
        self.produto_id = produto["id"]

        self.nome = discord.ui.TextInput(default=produto["nome"], max_length=80)
        self.add_item(discord.ui.Label(text="Nome do produto", component=self.nome))

        self.descricao = discord.ui.TextInput(
            default=produto["descricao"] or "", required=False, max_length=500,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(discord.ui.Label(text="Descrição", component=self.descricao))

        self.preco = discord.ui.TextInput(default=f"{produto['preco']:.2f}", max_length=10)
        self.add_item(discord.ui.Label(text="Preço (R$)", component=self.preco))

        self.emoji = discord.ui.TextInput(default=produto["emoji"] or "", required=False, max_length=50)
        self.add_item(discord.ui.Label(text="Emoji (opcional)", component=self.emoji))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            preco_valor = float(str(self.preco.value).replace(",", ".").strip() or "0")
        except ValueError:
            return await interaction.response.send_message(f"{_e('ffz_vazio')} Preço inválido.", ephemeral=True)
        if preco_valor < 0:
            return await interaction.response.send_message(f"{_e('ffz_vazio')} Preço não pode ser negativo.", ephemeral=True)

        await db.editar_produto(
            self.produto_id,
            nome=str(self.nome.value).strip(),
            descricao=str(self.descricao.value or "").strip(),
            preco=preco_valor,
            emoji=str(self.emoji.value or "").strip() or None,
        )
        produto = await db.obter_produto(self.produto_id)
        await interaction.response.edit_message(
            content=f"{_e('ffz_entregue')} Produto atualizado!",
            embed=await montar_embed_config(interaction.guild, produto),
            view=PainelConfigProdutoView(self.produto_id),
        )


class ModalAdicionarEstoque(discord.ui.Modal):
    def __init__(self, produto: dict):
        titulo = f"Estoque — {produto['nome']}"
        if len(titulo) > 45:
            titulo = titulo[:44] + "…"
        super().__init__(title=titulo)
        self.produto_id = produto["id"]
        self.linhas = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=4000,
            placeholder="usuario1:senha1\nusuario2:senha2\nKEY-ABCD-1234\n...",
        )
        self.add_item(discord.ui.Label(text="1 item por linha (conta/key/código)", component=self.linhas))

    async def on_submit(self, interaction: discord.Interaction):
        linhas_lista = str(self.linhas.value).split("\n")
        qtd = await db.adicionar_estoque(self.produto_id, linhas_lista, interaction.user.id)
        produto = await db.obter_produto(self.produto_id)
        await interaction.response.edit_message(
            content=f"{_e('ffz_estoque')} **{qtd}** unidade(s) adicionada(s) ao estoque.",
            embed=await montar_embed_config(interaction.guild, produto),
            view=PainelConfigProdutoView(self.produto_id),
        )


class ConfirmarApagarProdutoView(discord.ui.View):
    def __init__(self, produto_id: int, produto_nome: str):
        super().__init__(timeout=60)
        self.produto_id = produto_id
        self.produto_nome = produto_nome

    @discord.ui.button(label="Apagar de vez", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db.excluir_produto(self.produto_id)
        produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=False)
        await interaction.response.edit_message(
            content=f"{_e('ffz_apagar')} Produto **{self.produto_nome}** apagado (o histórico de vendas continua guardado).",
            embed=await montar_embed_hub(interaction.guild, produtos),
            view=HubLojaView(produtos),
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produto = await db.obter_produto(self.produto_id)
        if not produto:
            produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=False)
            return await interaction.response.edit_message(
                content=None, embed=await montar_embed_hub(interaction.guild, produtos), view=HubLojaView(produtos)
            )
        await interaction.response.edit_message(
            content=None, embed=await montar_embed_config(interaction.guild, produto), view=PainelConfigProdutoView(self.produto_id)
        )


class PainelConfigProdutoView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await checar_licenca_view(interaction)

    def __init__(self, produto_id: int):
        super().__init__(timeout=300)
        self.produto_id = produto_id

    @discord.ui.button(label="Editar", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produto = await db.obter_produto(self.produto_id)
        if not produto:
            return await interaction.response.send_message("⚠️ Esse produto não existe mais.", ephemeral=True)
        await interaction.response.send_modal(ModalEditarProduto(produto))

    @discord.ui.button(label="Adicionar Estoque", style=discord.ButtonStyle.secondary, emoji="📥", row=0)
    async def estoque(self, interaction: discord.Interaction, button: discord.ui.Button):
        produto = await db.obter_produto(self.produto_id)
        if not produto:
            return await interaction.response.send_message("⚠️ Esse produto não existe mais.", ephemeral=True)
        await interaction.response.send_modal(ModalAdicionarEstoque(produto))

    @discord.ui.button(label="Ativar/Desativar", style=discord.ButtonStyle.secondary, emoji="🔁", row=0)
    async def alternar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produto = await db.obter_produto(self.produto_id)
        if not produto:
            return await interaction.response.send_message("⚠️ Esse produto não existe mais.", ephemeral=True)
        novo_status = 0 if produto["ativo"] else 1
        await db.editar_produto(self.produto_id, ativo=novo_status)
        produto = await db.obter_produto(self.produto_id)
        await interaction.response.edit_message(embed=await montar_embed_config(interaction.guild, produto), view=self)

    @discord.ui.button(label="Apagar", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def apagar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produto = await db.obter_produto(self.produto_id)
        if not produto:
            return await interaction.response.send_message("⚠️ Esse produto não existe mais.", ephemeral=True)
        await interaction.response.edit_message(
            content=f"⚠️ Tem certeza que quer apagar **{produto['nome']}**? O estoque ainda não entregue vai junto (o histórico de vendas continua).",
            embed=None,
            view=ConfirmarApagarProdutoView(self.produto_id, produto["nome"]),
        )

    @discord.ui.button(label="Voltar", style=discord.ButtonStyle.secondary, emoji="↩️", row=1)
    async def voltar(self, interaction: discord.Interaction, button: discord.ui.Button):
        produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=False)
        await interaction.response.edit_message(
            content=None, embed=await montar_embed_hub(interaction.guild, produtos), view=HubLojaView(produtos)
        )


# ============================================================================
# VITRINE (público, Components V2) + ENTREGA AUTOMÁTICA
# ============================================================================
class BotaoComprarProduto(ui.DynamicItem[ui.Button], template=r"loja:comprar:(?P<produto_id>[0-9]+)"):
    def __init__(self, produto_id: int):
        super().__init__(
            ui.Button(
                label="Comprar", style=discord.ButtonStyle.success,
                emoji=_e("ffz_comprar"),
                custom_id=f"loja:comprar:{produto_id}",
            )
        )
        self.produto_id = produto_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Item, match: re.Match, /):
        return cls(int(match["produto_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not await checar_licenca_view(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        produto = await db.obter_produto(self.produto_id)
        if not produto or not produto["ativo"]:
            return await interaction.followup.send(f"{_e('ffz_vazio')} Esse produto não está mais disponível.", ephemeral=True)

        item_entregue = await db.entregar_um_item(self.produto_id, interaction.user.id)
        if not item_entregue:
            return await interaction.followup.send(
                f"{_e('ffz_vazio')} **{produto['nome']}** está sem estoque no momento. Fala com a staff.",
                ephemeral=True,
            )

        await db.registrar_entrega_log(
            interaction.guild_id, self.produto_id, item_entregue["id"], interaction.user.id, produto["nome"]
        )

        embed_entrega = discord.Embed(
            title=f"{_e('ffz_entregue')} {produto['nome']}",
            description=f"```{item_entregue['conteudo']}```",
            color=0x2ECC71,
        )
        embed_entrega.set_footer(text=f"Comprado em {interaction.guild.name}")

        # Tenta mandar por DM primeiro (fica só com o cliente, ninguém mais
        # vê); se a DM estiver fechada, cai pro ephemeral aqui mesmo --
        # nunca posta a conta/key num canal público de jeito nenhum.
        entregue_onde = "na sua DM"
        try:
            await interaction.user.send(embed=embed_entrega)
        except (discord.Forbidden, discord.HTTPException):
            entregue_onde = "abaixo (sua DM está fechada)"
            await interaction.followup.send(embed=embed_entrega, ephemeral=True)

        await interaction.followup.send(
            f"{_e('ffz_entregue')} Compra confirmada! Seu **{produto['nome']}** foi entregue {entregue_onde}.",
            ephemeral=True,
        )

        # Atualiza a vitrine pra refletir o novo número de estoque (se a
        # mensagem ainda existir e o painel ainda tiver esse produto).
        try:
            produtos_atuais = await db.listar_produtos(interaction.guild_id, somente_ativos=True)
            await interaction.message.edit(view=await montar_view_vitrine(produtos_atuais))
        except Exception:
            pass


async def montar_view_vitrine(produtos: list) -> ui.LayoutView:
    """Card V2 com 1 seção por produto ativo (nome, descrição, preço,
    estoque) + botão Comprar. Sistema novo e sem nenhum botão concorrendo
    por posição (like a fila), então V2 aqui é só a forma mais bonita e
    direta de fazer -- product + botão colados, sem limitação nenhuma do
    Discord envolvida."""
    view = ui.LayoutView(timeout=None)
    container = ui.Container(accent_color=discord.Colour(0x2ECC71))
    container.add_item(ui.TextDisplay(f"# {_e('ffz_loja')} Loja"))
    container.add_item(ui.Separator())

    if not produtos:
        container.add_item(ui.TextDisplay("_Nenhum produto disponível no momento._"))
        view.add_item(container)
        return view

    for i, p in enumerate(produtos):
        qtd = await db.contar_estoque_disponivel(p["id"])
        emoji_produto = p["emoji"] or _e("ffz_produto")
        linhas = [f"## {emoji_produto} {p['nome']}"]
        if p["descricao"]:
            linhas.append(p["descricao"])
        linhas.append(f"{_e('ffz_preco')} **R$ {p['preco']:.2f}**  •  {_e('ffz_estoque')} `{qtd}` em estoque")
        texto = ui.TextDisplay("\n".join(linhas))

        botao = BotaoComprarProduto(p["id"])
        if qtd <= 0:
            botao.item.disabled = True
            botao.item.label = "Esgotado"
            botao.item.style = discord.ButtonStyle.secondary
            botao.item.emoji = _e("ffz_vazio")

        container.add_item(texto)
        container.add_item(ui.ActionRow(botao))
        if i < len(produtos) - 1:
            container.add_item(ui.Separator())

    view.add_item(container)
    return view


# ============================================================================
# COG
# ============================================================================
class LojaProdutos(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="loja_admin", description="Gerencia os produtos da loja digital (criar, estoque, ativar/desativar, apagar)")
    @requer_licenca()
    async def loja_admin(self, interaction: discord.Interaction):
        if not await _eh_admin_loja(interaction):
            return
        produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=False)
        await interaction.response.send_message(
            embed=await montar_embed_hub(interaction.guild, produtos), view=HubLojaView(produtos), ephemeral=True
        )

    @app_commands.command(name="loja", description="Publica a vitrine da loja digital neste canal")
    @requer_licenca()
    async def loja(self, interaction: discord.Interaction):
        if not await _eh_admin_loja(interaction):
            return
        produtos = await db.listar_produtos(interaction.guild_id, somente_ativos=True)
        await interaction.response.send_message("✅ Vitrine publicada!", ephemeral=True)
        await interaction.channel.send(view=await montar_view_vitrine(produtos))

    @app_commands.command(name="loja_vendas", description="Mostra as últimas vendas da loja digital")
    @requer_licenca()
    async def loja_vendas(self, interaction: discord.Interaction):
        if not await _eh_admin_loja(interaction):
            return
        vendas = await db.historico_entregas(interaction.guild_id, limite=15)
        if not vendas:
            return await interaction.response.send_message(f"{_e('ffz_historico')} Nenhuma venda registrada ainda.", ephemeral=True)
        linhas = [
            f"{_e('ffz_entregue')} **{v['nome_produto']}** — <@{v['usuario_id']}> — `{v['entregue_em']}`"
            for v in vendas
        ]
        embed = discord.Embed(title=f"{_e('ffz_historico')} Últimas vendas", description="\n".join(linhas), color=0x2ECC71)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    # DynamicItem: o botão "Comprar" da vitrine sobrevive a restart/redeploy
    # sem precisar recriar a mensagem -- discord.py roteia qualquer
    # custom_id que bata com "loja:comprar:<id>" de volta pro callback certo.
    bot.add_dynamic_items(BotaoComprarProduto)
    await bot.add_cog(LojaProdutos(bot))
