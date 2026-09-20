"""
cogs/cupons.py — Cupons de desconto da loja (comandos de admin).

O cliente aplica o cupom no próprio carrinho (botão "Usar cupom" em
cogs/loja.py); aqui é só a parte de criar, listar e ligar/desligar.

Regras que ficam no banco (database.calcular_cupom), não aqui:
  • o uso do cupom é reservado quando o pedido é CRIADO e devolvido se ele
    for cancelado/expirar — então não dá pra estourar o limite abrindo
    vários carrinhos;
  • o total do pedido nunca chega a zero (Pix sem valor = cliente paga o
    que quiser).
"""

from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from constants import TipoCupom
from permissoes import checar_admin_ou_avisar
from emojis_app import E

MAX_CUPONS_NA_LISTA = 15  # o card de Components V2 tem limite de texto (4000 caracteres)


def _formatar_desconto(cupom: dict) -> str:
    if cupom["tipo"] == TipoCupom.PERCENTUAL.value:
        valor = f"{cupom['valor']:g}%"
    else:
        valor = f"R$ {cupom['valor']:.2f}"
    return valor


def _formatar_data(valor: str | None) -> str:
    if not valor:
        return "sem validade"
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y")
    except ValueError:
        return valor


def _linha_cupom(cupom: dict) -> str:
    if not cupom["ativo"]:
        status = f"{E.CANCELADO} desativado"
    elif cupom["expirado"]:
        status = f"{E.EXPIRADO} expirado"
    else:
        status = f"{E.OK} ativo"

    limite = f"/{cupom['max_usos']}" if cupom["max_usos"] is not None else ""
    partes = [f"`{cupom['codigo']}` — **{_formatar_desconto(cupom)}**", f"{cupom['usos']}{limite} usos"]
    if cupom["max_por_usuario"] is not None:
        partes.append(f"{cupom['max_por_usuario']}x por pessoa")
    if cupom["valor_minimo"] is not None:
        partes.append(f"mín. R$ {cupom['valor_minimo']:.2f}")
    if cupom["produto_id"] is not None:
        partes.append(f"só produto #{cupom['produto_id']}")
    partes.append(_formatar_data(cupom["expira_em"]) if cupom["expira_em"] else "sem validade")
    partes.append(status)
    return " • ".join(partes)


class Cupons(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    cupom = app_commands.Group(name="cupom", description="Gerenciar cupons de desconto da loja (admin).")

    @cupom.command(name="criar", description="[ADMIN] Cria um cupom de desconto.")
    @app_commands.describe(
        codigo="O código que o cliente digita (ex: BLACKFRIDAY). Não diferencia maiúscula de minúscula.",
        tipo="Percentual (%) ou valor fixo em reais",
        valor="Quanto desconta: 10 = 10% (percentual) ou R$ 10,00 (fixo)",
        valor_minimo="Só vale se o carrinho tiver pelo menos esse valor em reais (opcional)",
        max_usos="Quantas vezes o cupom pode ser usado no total (opcional)",
        max_por_usuario="Quantas vezes CADA pessoa pode usar (opcional)",
        dias_validade="Expira depois de quantos dias (opcional)",
        produto_id="Vale só pra esse produto — veja o ID em /produto listar (opcional)",
    )
    @app_commands.choices(tipo=[
        app_commands.Choice(name="Percentual (%)", value=TipoCupom.PERCENTUAL.value),
        app_commands.Choice(name="Valor fixo (R$)", value=TipoCupom.FIXO.value),
    ])
    async def criar(
        self,
        interaction: discord.Interaction,
        codigo: str,
        tipo: app_commands.Choice[str],
        valor: float,
        valor_minimo: float = None,
        max_usos: int = None,
        max_por_usuario: int = None,
        dias_validade: int = None,
        produto_id: int = None,
    ):
        if not await checar_admin_ou_avisar(interaction):
            return

        if produto_id is not None:
            produto = await db.obter_produto(produto_id)
            if not produto or produto["guild_id"] != interaction.guild.id:
                await interaction.response.send_message(f"{E.ERRO} Produto não encontrado nesse servidor.", ephemeral=True)
                return

        try:
            await db.criar_cupom(
                interaction.guild.id, codigo, tipo.value, valor, interaction.user.id,
                valor_minimo=valor_minimo, max_usos=max_usos, max_por_usuario=max_por_usuario,
                produto_id=produto_id, dias_validade=dias_validade,
            )
        except ValueError as erro:
            await interaction.response.send_message(f"{E.ERRO} {erro}", ephemeral=True)
            return

        cupom = await db.obter_cupom(interaction.guild.id, codigo)
        view = licenca.montar_view_licenca(
            f"{E.CUPOM} Cupom criado — `{cupom['codigo']}`",
            [_linha_cupom({**cupom, "usos": 0}), "---", "O cliente aplica no carrinho pelo botão **Usar cupom**."],
            client=interaction.client,
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    @cupom.command(name="listar", description="[ADMIN] Lista os cupons do servidor e quantas vezes cada um foi usado.")
    async def listar(self, interaction: discord.Interaction):
        if not await checar_admin_ou_avisar(interaction):
            return

        cupons = await db.listar_cupons(interaction.guild.id)
        if not cupons:
            await interaction.response.send_message("Nenhum cupom ainda. Use `/cupom criar`.", ephemeral=True)
            return

        linhas = [_linha_cupom(c) for c in cupons[:MAX_CUPONS_NA_LISTA]]
        if len(cupons) > MAX_CUPONS_NA_LISTA:
            linhas.append(f"\n… e mais {len(cupons) - MAX_CUPONS_NA_LISTA} cupom(ns) mais antigo(s).")
        view = licenca.montar_view_licenca(f"{E.CUPOM} Cupons da loja", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)

    @cupom.command(name="desativar", description="[ADMIN] Desativa um cupom (quem já pagou com ele não é afetado).")
    @app_commands.describe(codigo="Código do cupom")
    async def desativar(self, interaction: discord.Interaction, codigo: str):
        if not await checar_admin_ou_avisar(interaction):
            return
        if await db.definir_cupom_ativo(interaction.guild.id, codigo, False):
            await interaction.response.send_message(
                f"{E.OK} Cupom `{db.normalizar_codigo_cupom(codigo)}` desativado.", ephemeral=True
            )
        else:
            await interaction.response.send_message(f"{E.ERRO} Cupom não encontrado nesse servidor.", ephemeral=True)

    @cupom.command(name="ativar", description="[ADMIN] Reativa um cupom desativado.")
    @app_commands.describe(codigo="Código do cupom")
    async def ativar(self, interaction: discord.Interaction, codigo: str):
        if not await checar_admin_ou_avisar(interaction):
            return
        if await db.definir_cupom_ativo(interaction.guild.id, codigo, True):
            await interaction.response.send_message(
                f"{E.OK} Cupom `{db.normalizar_codigo_cupom(codigo)}` ativado.", ephemeral=True
            )
        else:
            await interaction.response.send_message(f"{E.ERRO} Cupom não encontrado nesse servidor.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Cupons(bot))
