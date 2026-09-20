"""
cogs/configurarloja.py — `/configurarloja`: painel único de admin que
reúne a configuração de pagamento (Pix manual + gateways automáticos) e
aponta pros comandos de produtos/vitrine, em vez de espalhar tudo em
comandos soltos que ninguém lembra de cor.

Os comandos antigos (`/configurarpagamento`, `/produto ...`) continuam
funcionando do mesmo jeito — esse painel só organiza o ponto de entrada,
sem quebrar nada que já estava em produção.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from gateways import GATEWAYS, obter_classe_gateway
from permissoes import checar_admin_ou_avisar
from pix_validators import TIPO_CHAVE_LABEL, detectar_tipo_chave
from emojis_app import E


# ─── Modal: Pix manual ──────────────────────────────────────────────────────

class ModalPixManual(discord.ui.Modal, title="Configurar Pix manual"):
    chave = discord.ui.TextInput(label="Chave Pix", placeholder="CPF, telefone, e-mail ou chave aleatória", max_length=140)
    nome_recebedor = discord.ui.TextInput(label="Nome do recebedor (aparece no QR)", max_length=60)
    cidade = discord.ui.TextInput(label="Cidade do titular", default="Sao Paulo", required=False, max_length=40)

    async def on_submit(self, interaction: discord.Interaction):
        tipo = detectar_tipo_chave(str(self.chave))
        if tipo is None:
            await interaction.response.send_message(
                f"{E.ERRO} Essa chave não parece válida. Aceito CPF, telefone (com DDD), e-mail ou chave aleatória (UUID).",
                ephemeral=True,
            )
            return
        await db.definir_config_loja(
            interaction.guild.id, str(self.chave).strip(), tipo, str(self.nome_recebedor).strip(),
            str(self.cidade).strip() or "Sao Paulo",
        )
        await interaction.response.send_message(
            f"{E.OK} Pix manual configurado — chave `{self.chave}` ({TIPO_CHAVE_LABEL[tipo]}).", ephemeral=True
        )


# ─── Modal genérico: credenciais de um gateway automático ──────────────────

class ModalGateway(discord.ui.Modal):
    def __init__(self, gateway_nome: str):
        classe = obter_classe_gateway(gateway_nome)
        super().__init__(title=f"Configurar {classe.LABEL}")
        self.gateway_nome = gateway_nome
        self.classe = classe
        self._inputs = {}
        for campo in classe.CAMPOS_CONFIG:
            item = discord.ui.TextInput(
                label=campo.label[:45],
                placeholder=campo.placeholder[:100] if campo.placeholder else None,
                required=campo.obrigatorio,
                max_length=campo.max_length,
                style=discord.TextStyle.paragraph if campo.max_length > 100 else discord.TextStyle.short,
            )
            self._inputs[campo.chave] = item
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        credenciais = {chave: str(item.value).strip() for chave, item in self._inputs.items()}
        await db.definir_gateway_config(interaction.guild.id, self.gateway_nome, credenciais)

        aviso_extra = ""
        if not self.classe.IMPLEMENTADO:
            aviso_extra = (
                f"\n\n{E.AVISO} As credenciais já ficam salvas (criptografadas), mas a integração automática do "
                f"**{self.classe.LABEL}** ainda não está pronta — pedidos com esse gateway caem pro aviso "
                f"de \"em breve\" até eu terminar a implementação real."
            )
        await interaction.response.send_message(
            f"{E.OK} **{self.classe.LABEL}** configurado nesse servidor.{aviso_extra}", ephemeral=True
        )


# ─── Sub-painel: gateways automáticos ───────────────────────────────────────

class BotaoConfigurarGateway(discord.ui.Button):
    def __init__(self, gateway_nome: str, configurado: bool):
        classe = obter_classe_gateway(gateway_nome)
        # custom emoji não entra no texto do label — o "configurado" agora aparece
        # como botão verde (o emoji do gateway continua no campo `emoji=`).
        estilo = discord.ButtonStyle.success if configurado else discord.ButtonStyle.secondary
        super().__init__(label=f"{classe.LABEL}", style=estilo, emoji=classe.EMOJI)
        self.gateway_nome = gateway_nome

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModalGateway(self.gateway_nome))


class SelectGatewayPadrao(discord.ui.Select):
    def __init__(self, gateways_configurados: list[str], padrao_atual: str | None):
        opcoes = [
            discord.SelectOption(
                label=obter_classe_gateway(g).LABEL,
                value=g,
                default=(g == padrao_atual),
                emoji=obter_classe_gateway(g).EMOJI,
            )
            for g in gateways_configurados
        ]
        super().__init__(placeholder="Gateway padrão pro botão \"pagar automático\"...", options=opcoes)

    async def callback(self, interaction: discord.Interaction):
        await db.definir_gateway_padrao(interaction.guild.id, self.values[0])
        await interaction.response.send_message(
            f"{E.OK} **{obter_classe_gateway(self.values[0]).LABEL}** definido como gateway padrão do checkout automático.",
            ephemeral=True,
        )


class ViewGateways(discord.ui.View):
    def __init__(self, guild_id: int, configurados: list[str], padrao: str | None):
        super().__init__(timeout=180)
        for nome in GATEWAYS:
            self.add_item(BotaoConfigurarGateway(nome, configurado=nome in configurados))
        if configurados:
            self.add_item(SelectGatewayPadrao(configurados, padrao))


class BotaoAbrirGateways(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Gateways automáticos", style=discord.ButtonStyle.primary, emoji=E.RAIO)

    async def callback(self, interaction: discord.Interaction):
        configurados = await db.listar_gateways_configurados(interaction.guild.id)
        config_loja = await db.obter_config_loja(interaction.guild.id)
        padrao = config_loja.get("gateway_padrao") if config_loja else None

        linhas = ["Escolha um gateway abaixo pra cadastrar (ou trocar) a chave de API dele."]
        for nome, classe in GATEWAYS.items():
            status = f"{E.ATIVO} configurado" if nome in configurados else f"{E.INATIVO} não configurado"
            pronto = "" if classe.IMPLEMENTADO else " *(integração ainda não implementada)*"
            marca_padrao = " — **padrão atual**" if nome == padrao else ""
            linhas.append(f"{classe.EMOJI} **{classe.LABEL}** — {status}{pronto}{marca_padrao}")

        view = ViewGateways(interaction.guild.id, configurados, padrao)
        embed_view = licenca.montar_view_licenca(f"{E.RAIO} Gateways de pagamento automático", linhas, client=interaction.client)
        await interaction.response.send_message(view=embed_view, ephemeral=True)
        await interaction.followup.send(view=view, ephemeral=True)


class BotaoAbrirPixManual(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Pix manual", style=discord.ButtonStyle.secondary, emoji=E.PIX)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModalPixManual())


class BotaoInfoVitrines(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Vitrines", style=discord.ButtonStyle.secondary, emoji=E.SACOLA)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"{E.SACOLA} **Vitrines** — use:\n"
            "• `/vitrine criar` — cria uma vitrine nova (título, descrição, banner)\n"
            "• `/vitrine produto_add` / `produto_remover` — escolhe quais produtos aparecem nela\n"
            "• `/vitrine postar` — publica a vitrine nesse canal com o botão **Comprar**\n"
            "• `/vitrine listar` — vê as vitrines já criadas",
            ephemeral=True,
        )


class BotaoInfoProdutos(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Produtos", style=discord.ButtonStyle.secondary, emoji=E.ESTOQUE)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"{E.ESTOQUE} **Produtos** — use:\n"
            "• `/produto criar` — cadastra um produto novo (nome, preço, tipo de entrega)\n"
            "• `/produto estoque` / `estoque_arquivo` — carrega o estoque (entrega automática)\n"
            "• `/produto editar` — muda nome, preço, descrição, imagem ou ativo/inativo\n"
            "• `/produto listar` — vê todos os produtos cadastrados",
            ephemeral=True,
        )


class ViewPainelPrincipal(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(BotaoAbrirPixManual())
        self.add_item(BotaoAbrirGateways())
        self.add_item(BotaoInfoProdutos())
        self.add_item(BotaoInfoVitrines())


class ConfigurarLoja(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="configurarloja", description="[ADMIN] Painel único pra configurar pagamento, gateways, produtos e vitrines.")
    async def configurarloja(self, interaction: discord.Interaction):
        if not await checar_admin_ou_avisar(interaction):
            return

        config = await db.obter_config_loja(interaction.guild.id)
        gateways_ativos = await db.listar_gateways_configurados(interaction.guild.id)

        linhas = []
        if config and config.get("chave_pix"):
            linhas.append(f"{E.PIX} Pix manual: **configurado** ({config['nome_recebedor']})")
        else:
            linhas.append(f"{E.PIX} Pix manual: {E.INATIVO} não configurado")

        if gateways_ativos:
            nomes = ", ".join(obter_classe_gateway(g).LABEL for g in gateways_ativos)
            linhas.append(f"{E.RAIO} Gateways automáticos ativos: **{nomes}**")
        else:
            linhas.append(f"{E.RAIO} Gateways automáticos: {E.INATIVO} nenhum configurado")

        linhas.append("---")
        linhas.append("Escolha abaixo o que quer configurar:")

        view = licenca.montar_view_licenca(f"{E.FERRAMENTA} Configurar loja", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)
        await interaction.followup.send(view=ViewPainelPrincipal(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigurarLoja(bot))
