"""
cogs/vitrine.py — vitrines: posts públicos e personalizáveis no canal
(banner + produtos com thumbnail/preço) com um botão **Comprar** que abre
um tópico privado só entre o cliente e a equipe. Dentro do tópico, o
cliente escolhe produtos (igual ao /loja de hoje) e finaliza com Pix
manual ou, se o servidor tiver um gateway automático configurado, com
confirmação automática via webhook.

Isso substitui o fluxo "cliente entra no /loja ephemeral sozinho" por
"cliente vê a vitrine bonita no canal público e clica Comprar" — sem
tirar o `/loja` antigo, que continua funcionando pra quem já usa assim.
"""

from __future__ import annotations

import io

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from constants import MINUTOS_EXPIRAR_PEDIDO, TIPO_ENTREGA_EMOJI, TipoEntrega
from gateways import instanciar_gateway, obter_classe_gateway
from permissoes import checar_admin_ou_avisar
from pix_utils import gerar_qrcode_bytes, obter_logo_guild
from emojis_app import E

MAX_PRODUTOS_NA_VITRINE_VISUAL = 10


# ─── Montagem do post público da vitrine ───────────────────────────────────

def _custom_id_comprar(vitrine_id: int) -> str:
    return f"vitrine_comprar_{vitrine_id}"


def montar_view_vitrine_publica(vitrine: dict, produtos: list[dict]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_color=vitrine.get("cor"))

    container.add_item(discord.ui.TextDisplay(f"## {vitrine['titulo']}"))
    if vitrine.get("descricao"):
        container.add_item(discord.ui.TextDisplay(vitrine["descricao"]))

    if vitrine.get("banner_url"):
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media=vitrine["banner_url"])))

    container.add_item(discord.ui.Separator())

    if not produtos:
        container.add_item(discord.ui.TextDisplay("*Nenhum produto nessa vitrine ainda.*"))
    else:
        for p in produtos[:MAX_PRODUTOS_NA_VITRINE_VISUAL]:
            emoji = TIPO_ENTREGA_EMOJI[TipoEntrega(p["tipo_entrega"])]
            sem_estoque = p["tipo_entrega"] == TipoEntrega.AUTOMATICA.value and p["estoque_disponivel"] == 0
            aviso = " — *sem estoque*" if sem_estoque else ""
            texto = f"### {emoji} {p['nome']} — R$ {p['preco']:.2f}{aviso}"
            if p.get("descricao"):
                texto += f"\n{p['descricao']}"
            bloco = discord.ui.TextDisplay(texto)
            if p.get("imagem_url"):
                container.add_item(discord.ui.Section(bloco, accessory=discord.ui.Thumbnail(media=p["imagem_url"])))
            else:
                container.add_item(bloco)

    container.add_item(discord.ui.Separator())
    linha = discord.ui.ActionRow()
    linha.add_item(discord.ui.Button(
        label="Comprar", style=discord.ButtonStyle.success, emoji=E.CARRINHO,
        custom_id=_custom_id_comprar(vitrine["id"]),
    ))
    container.add_item(linha)
    container.add_item(discord.ui.TextDisplay("-# FFZ VENDAS • Clique em Comprar pra abrir seu atendimento privado"))

    view.add_item(container)
    return view


# ─── Tópico privado: escolher produtos + finalizar ─────────────────────────
# O botão "Comprar" do post público não usa uma View com timeout comum:
# ele é pego direto pelo listener genérico `Vitrine.on_interaction` (mais
# abaixo), casando pelo prefixo do custom_id (`vitrine_comprar_<id>`).
# Isso faz o botão funcionar pra sempre, mesmo depois do bot reiniciar,
# sem precisar re-registrar uma View por vitrine existente.

class SelectProdutoThread(discord.ui.Select):
    def __init__(self, produtos: list[dict]):
        opcoes = [
            discord.SelectOption(
                label=f"{p['nome']} — R$ {p['preco']:.2f}",
                value=str(p["id"]),
                description=(p["descricao"] or "")[:100] or None,
            )
            for p in produtos[:25]
        ]
        super().__init__(placeholder="Escolha um produto pra adicionar ao pedido...", options=opcoes)

    async def callback(self, interaction: discord.Interaction):
        produto_id = int(self.values[0])
        produto_atual = await db.obter_produto(produto_id)
        if not produto_atual or not produto_atual["ativo"]:
            await interaction.response.send_message(f"{E.ERRO} Esse produto não está mais disponível.", ephemeral=True)
            return
        if produto_atual["tipo_entrega"] == TipoEntrega.AUTOMATICA.value:
            disponivel = await db.contar_estoque_disponivel(produto_id)
            if disponivel == 0:
                await interaction.response.send_message(f"{E.ERRO} Esse produto está sem estoque no momento.", ephemeral=True)
                return

        await db.adicionar_ao_carrinho(interaction.guild.id, interaction.user.id, produto_id)
        produtos = await _produtos_do_topico(interaction)
        view = await _montar_tela_pedido_thread(interaction.guild.id, interaction.user.id, produtos)
        await interaction.response.edit_message(view=view)


def _texto_resumo(resumo: dict) -> str:
    itens = resumo["itens"]
    if not itens:
        return f"{E.CARRINHO} Seu pedido está vazio. Escolha um produto no menu abaixo."
    linhas = [f"• {i['quantidade']}x **{i['nome']}** — R$ {i['preco'] * i['quantidade']:.2f}" for i in itens]
    linhas.append("")
    linhas.append(f"**Total: R$ {resumo['total']:.2f}**")
    return "\n".join(linhas)


async def _montar_tela_pedido_thread(guild_id: int, user_id: int, produtos_sugeridos: list[dict]) -> discord.ui.LayoutView:
    resumo = await db.resumo_carrinho(guild_id, user_id)
    gateway_padrao = await _gateway_padrao_disponivel(guild_id)

    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container()
    container.add_item(discord.ui.TextDisplay(f"### {E.SACOLA} Seu pedido"))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_texto_resumo(resumo)))
    container.add_item(discord.ui.Separator())

    linha_select = discord.ui.ActionRow()
    linha_select.add_item(SelectProdutoThread(produtos_sugeridos))
    container.add_item(linha_select)

    if resumo["itens"]:
        linha_botoes = discord.ui.ActionRow()
        linha_botoes.add_item(BotaoFinalizarPixManual())
        if gateway_padrao:
            linha_botoes.add_item(BotaoFinalizarAutomatico(gateway_padrao))
        linha_botoes.add_item(BotaoEsvaziarPedido())
        container.add_item(linha_botoes)

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay("-# FFZ VENDAS • Atendimento privado"))
    view.add_item(container)
    return view


async def _gateway_padrao_disponivel(guild_id: int) -> str | None:
    config = await db.obter_config_loja(guild_id)
    padrao = config.get("gateway_padrao") if config else None
    if not padrao:
        return None
    credenciais = await db.obter_gateway_config(guild_id, padrao)
    if not credenciais:
        return None
    return padrao


class BotaoEsvaziarPedido(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Esvaziar", style=discord.ButtonStyle.danger, emoji=E.LIXO)

    async def callback(self, interaction: discord.Interaction):
        await db.limpar_carrinho(interaction.guild.id, interaction.user.id)
        produtos = await _produtos_do_topico(interaction)
        view = await _montar_tela_pedido_thread(interaction.guild.id, interaction.user.id, produtos)
        await interaction.response.edit_message(view=view)


async def _produtos_do_topico(interaction: discord.Interaction) -> list[dict]:
    """Descobre quais produtos oferecer no Select desse tópico: se o
    tópico nasceu de uma vitrine, só os dela; senão, o catálogo inteiro."""
    vitrine_id = getattr(interaction.client, "_vitrine_por_thread", {}).get(interaction.channel.id)
    if vitrine_id:
        produtos = await db.produtos_da_vitrine(vitrine_id)
        if produtos:
            return produtos
    return await db.listar_produtos(interaction.guild.id, apenas_ativos=True)


class BotaoJaPagueiThread(discord.ui.Button):
    def __init__(self, pedido_id: int):
        super().__init__(label="Já paguei", style=discord.ButtonStyle.primary, emoji=E.OK, custom_id=f"vitrine_ja_paguei_{pedido_id}")
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
            f"{E.RECEBIDO} **Novo pagamento a confirmar** — Pedido `#{pedido['id']}` de {interaction.user.mention} "
            f"— R$ {pedido['valor_total']:.2f}\nUm admin precisa rodar `/pedido aprovar id:{pedido['id']}` pra liberar a entrega."
        )


class BotaoFinalizarPixManual(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Pagar com Pix (manual)", style=discord.ButtonStyle.success, emoji=E.PIX)

    async def callback(self, interaction: discord.Interaction):
        config = await db.obter_config_loja(interaction.guild.id)
        if not config or not config.get("chave_pix"):
            await interaction.response.send_message(
                f"{E.ERRO} A loja ainda não tem uma chave Pix configurada. Peça pra um admin rodar `/configurarloja`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        pedido, erro = await db.criar_pedido_do_carrinho(interaction.guild.id, interaction.user.id)
        if not pedido:
            await interaction.followup.send(f"{E.ERRO} {erro}")
            return

        await db.definir_thread_pedido(pedido["id"], interaction.channel.id)

        logo_bytes = await obter_logo_guild(interaction.guild)
        img_bytes, payload = gerar_qrcode_bytes(
            config["chave_pix"], config["nome_recebedor"] or "PAGAMENTO PIX",
            cidade=config.get("cidade") or "Sao Paulo", valor=pedido["valor_total"], logo_bytes=logo_bytes,
        )
        await db.salvar_payload_pix(pedido["id"], payload)

        resumo_txt = "\n".join(f"• {i['quantidade']}x {i['nome']} — R$ {i['preco']:.2f}" for i in pedido["itens"])
        arquivo = discord.File(io.BytesIO(img_bytes.read()), filename="pix.png")

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"### {E.PIX} Pedido `#{pedido['id']}` — R$ {pedido['valor_total']:.2f}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(resumo_txt))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"**Pix Copia e Cola:**\n```\n{payload}\n```"))
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://pix.png")))
        linha = discord.ui.ActionRow()
        linha.add_item(BotaoJaPagueiThread(pedido["id"]))
        container.add_item(linha)
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# FFZ VENDAS • Pague e clique em \"Já paguei\" • Expira em {MINUTOS_EXPIRAR_PEDIDO} min"
        ))
        view.add_item(container)
        await interaction.followup.send(view=view, file=arquivo)


class BotaoFinalizarAutomatico(discord.ui.Button):
    def __init__(self, gateway_nome: str):
        classe = obter_classe_gateway(gateway_nome)
        super().__init__(label=f"Pagar automático ({classe.LABEL})", style=discord.ButtonStyle.primary, emoji=E.RAIO)
        self.gateway_nome = gateway_nome

    async def callback(self, interaction: discord.Interaction):
        credenciais = await db.obter_gateway_config(interaction.guild.id, self.gateway_nome)
        classe = obter_classe_gateway(self.gateway_nome)
        if not credenciais:
            await interaction.response.send_message(f"{E.ERRO} Esse gateway não está mais configurado nesse servidor.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        pedido, erro = await db.criar_pedido_do_carrinho(interaction.guild.id, interaction.user.id)
        if not pedido:
            await interaction.followup.send(f"{E.ERRO} {erro}")
            return
        await db.definir_thread_pedido(pedido["id"], interaction.channel.id)

        gateway = instanciar_gateway(self.gateway_nome, credenciais)
        base_url = _base_url_webhook(interaction.client)
        notification_url = f"{base_url}/webhook/{self.gateway_nome}" if base_url else None

        resultado = await gateway.criar_cobranca(
            valor=pedido["valor_total"], descricao=f"Pedido #{pedido['id']}",
            pedido_id=pedido["id"], notification_url=notification_url,
        )
        if not resultado.ok:
            await db.cancelar_pedido(pedido["id"])
            await interaction.followup.send(f"{E.ERRO} {resultado.erro}")
            return

        await db.definir_gateway_pedido(pedido["id"], self.gateway_nome, resultado.charge_id)

        resumo_txt = "\n".join(f"• {i['quantidade']}x {i['nome']} — R$ {i['preco']:.2f}" for i in pedido["itens"])

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container()
        container.add_item(discord.ui.TextDisplay(f"### {classe.EMOJI} Pedido `#{pedido['id']}` — R$ {pedido['valor_total']:.2f}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(resumo_txt))
        container.add_item(discord.ui.Separator())
        if resultado.copia_cola:
            container.add_item(discord.ui.TextDisplay(f"**Pix Copia e Cola:**\n```\n{resultado.copia_cola}\n```"))
        if resultado.checkout_url:
            container.add_item(discord.ui.TextDisplay(f"**Link de pagamento:** {resultado.checkout_url}"))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# FFZ VENDAS • Confirmação automática via {classe.LABEL} • Pode levar alguns segundos após o pagamento"
        ))
        view.add_item(container)

        arquivo = None
        if resultado.qr_base64:
            import base64
            arquivo = discord.File(io.BytesIO(base64.b64decode(resultado.qr_base64)), filename="pix.png")
            container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media="attachment://pix.png")))

        if arquivo:
            await interaction.followup.send(view=view, file=arquivo)
        else:
            await interaction.followup.send(view=view)


def _base_url_webhook(client: discord.Client) -> str | None:
    return getattr(client, "base_url_publica", None)


# ─── Comando principal: abre o tópico privado a partir do botão Comprar ───

async def abrir_topico_de_compra(interaction: discord.Interaction, vitrine_id: int | None):
    await interaction.response.defer(ephemeral=True, thinking=True)

    nome_topico = f"pedido-{interaction.user.name}"[:100]
    try:
        thread = await interaction.channel.create_thread(
            name=nome_topico, type=discord.ChannelType.private_thread,
            invitable=False, auto_archive_duration=1440,
        )
        await thread.add_user(interaction.user)
    except discord.Forbidden:
        await interaction.followup.send(
            f"{E.ERRO} Não tenho permissão pra criar tópicos privados nesse canal. Peça pra um admin me dar a permissão "
            "**Criar tópicos privados**.", ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(f"{E.ERRO} Não consegui abrir seu atendimento agora. Tente de novo.", ephemeral=True)
        return

    if vitrine_id:
        mapa = getattr(interaction.client, "_vitrine_por_thread", None)
        if mapa is None:
            mapa = {}
            interaction.client._vitrine_por_thread = mapa
        mapa[thread.id] = vitrine_id
        produtos = await db.produtos_da_vitrine(vitrine_id) or await db.listar_produtos(interaction.guild.id)
    else:
        produtos = await db.listar_produtos(interaction.guild.id, apenas_ativos=True)

    if not produtos:
        await thread.send(f"{interaction.user.mention} A loja ainda não tem produtos disponíveis aqui. Um admin já foi avisado.")
    else:
        view = await _montar_tela_pedido_thread(interaction.guild.id, interaction.user.id, produtos)
        await thread.send(content=f"{interaction.user.mention} bem-vindo(a)! Escolha o que quer comprar:", view=view)

    await interaction.followup.send(f"{E.OK} Abri seu atendimento privado: {thread.mention}", ephemeral=True)


# ─── Comandos de admin ──────────────────────────────────────────────────────

class Vitrine(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not hasattr(bot, "_vitrine_por_thread"):
            bot._vitrine_por_thread = {}

    async def cog_load(self):
        # Registra UM listener global (via on_interaction) pro botão
        # Comprar de qualquer vitrine — feito em bot.py/on_interaction
        # seria mais direto, mas manter aqui evita mexer no bot.py.
        pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("vitrine_comprar_"):
            return
        try:
            vitrine_id = int(custom_id.removeprefix("vitrine_comprar_"))
        except ValueError:
            return
        await abrir_topico_de_compra(interaction, vitrine_id)

    vitrine = app_commands.Group(name="vitrine", description="Gerenciar vitrines públicas da loja.")

    @vitrine.command(name="criar", description="[ADMIN] Cria uma vitrine nova pra postar no canal.")
    @app_commands.describe(
        nome="Nome curto/identificador (ex: promocao-natal)",
        titulo="Título grande mostrado no post",
        descricao="Texto de apresentação (opcional)",
        banner_url="Imagem grande no topo do post (opcional)",
        cor_hex="Cor de destaque em hexadecimal, ex: FF5500 (opcional)",
    )
    async def vitrine_criar(self, interaction: discord.Interaction, nome: str, titulo: str, descricao: str = None, banner_url: str = None, cor_hex: str = None):
        if not await checar_admin_ou_avisar(interaction):
            return
        cor = None
        if cor_hex:
            try:
                cor = int(cor_hex.strip().lstrip("#"), 16)
            except ValueError:
                await interaction.response.send_message(f"{E.ERRO} Cor inválida — use um hexadecimal tipo `FF5500`.", ephemeral=True)
                return
        banner_limpo = db.limpar_url_imagem(banner_url) if banner_url else None
        vitrine_id, erro = await db.criar_vitrine(interaction.guild.id, nome, titulo, descricao, banner_limpo, cor)
        if erro:
            await interaction.response.send_message(f"{E.ERRO} {erro}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{E.OK} Vitrine `#{vitrine_id}` criada. Agora adicione produtos com `/vitrine produto_add` e depois "
            f"publique com `/vitrine postar`.", ephemeral=True,
        )

    @vitrine.command(name="produto_add", description="[ADMIN] Adiciona um produto a uma vitrine.")
    @app_commands.describe(nome="Nome/slug da vitrine", produto_id="ID do produto (veja /produto listar)")
    async def vitrine_produto_add(self, interaction: discord.Interaction, nome: str, produto_id: int):
        if not await checar_admin_ou_avisar(interaction):
            return
        vitrine = await db.obter_vitrine_por_slug(interaction.guild.id, nome)
        if not vitrine:
            await interaction.response.send_message(f"{E.ERRO} Vitrine não encontrada.", ephemeral=True)
            return
        produto = await db.obter_produto(produto_id)
        if not produto or produto["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(f"{E.ERRO} Produto não encontrado nesse servidor.", ephemeral=True)
            return
        await db.adicionar_produto_vitrine(vitrine["id"], produto_id)
        await interaction.response.send_message(f"{E.OK} **{produto['nome']}** adicionado à vitrine **{vitrine['titulo']}**.", ephemeral=True)

    @vitrine.command(name="produto_remover", description="[ADMIN] Remove um produto de uma vitrine.")
    @app_commands.describe(nome="Nome/slug da vitrine", produto_id="ID do produto")
    async def vitrine_produto_remover(self, interaction: discord.Interaction, nome: str, produto_id: int):
        if not await checar_admin_ou_avisar(interaction):
            return
        vitrine = await db.obter_vitrine_por_slug(interaction.guild.id, nome)
        if not vitrine:
            await interaction.response.send_message(f"{E.ERRO} Vitrine não encontrada.", ephemeral=True)
            return
        await db.remover_produto_vitrine(vitrine["id"], produto_id)
        await interaction.response.send_message(f"{E.OK} Produto removido da vitrine.", ephemeral=True)

    @vitrine.command(name="listar", description="[ADMIN] Lista as vitrines desse servidor.")
    async def vitrine_listar(self, interaction: discord.Interaction):
        if not await checar_admin_ou_avisar(interaction):
            return
        vitrines = await db.listar_vitrines(interaction.guild.id)
        if not vitrines:
            await interaction.response.send_message("Nenhuma vitrine criada ainda. Use `/vitrine criar`.", ephemeral=True)
            return
        linhas = []
        for v in vitrines:
            produtos = await db.produtos_da_vitrine(v["id"], apenas_ativos=False)
            linhas.append(f"`{v['slug']}` — **{v['titulo']}** — {len(produtos)} produto(s)")
        view = licenca.montar_view_licenca(f"{E.SACOLA} Vitrines", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)

    @vitrine.command(name="remover", description="[ADMIN] Apaga uma vitrine (não apaga os produtos, só o post).")
    @app_commands.describe(nome="Nome/slug da vitrine")
    async def vitrine_remover(self, interaction: discord.Interaction, nome: str):
        if not await checar_admin_ou_avisar(interaction):
            return
        vitrine = await db.obter_vitrine_por_slug(interaction.guild.id, nome)
        if not vitrine:
            await interaction.response.send_message(f"{E.ERRO} Vitrine não encontrada.", ephemeral=True)
            return
        await db.remover_vitrine(vitrine["id"])
        await interaction.response.send_message(f"{E.OK} Vitrine **{vitrine['titulo']}** removida.", ephemeral=True)

    @vitrine.command(name="postar", description="[ADMIN] Publica a vitrine nesse canal com o botão Comprar.")
    @app_commands.describe(nome="Nome/slug da vitrine")
    async def vitrine_postar(self, interaction: discord.Interaction, nome: str):
        if not await checar_admin_ou_avisar(interaction):
            return
        vitrine = await db.obter_vitrine_por_slug(interaction.guild.id, nome)
        if not vitrine:
            await interaction.response.send_message(f"{E.ERRO} Vitrine não encontrada.", ephemeral=True)
            return
        produtos = await db.produtos_da_vitrine(vitrine["id"])
        view = montar_view_vitrine_publica(vitrine, produtos)
        await interaction.response.send_message(view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(Vitrine(bot))
