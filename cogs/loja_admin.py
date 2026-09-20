"""
cogs/loja_admin.py — Comandos de administração da loja (só admin do
servidor, via permissoes.checar_admin_ou_avisar — não confundir com
owner.is_owner(), que é o DONO DO SAAS FFZ Vendas).
"""

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import licenca
from constants import (
    COR_LOJA,
    MAX_BYTES_ARQUIVO_ESTOQUE,
    MAX_TAMANHO_ITEM_ESTOQUE,
    TIPO_ENTREGA_EMOJI,
    TIPO_ENTREGA_LABEL,
    TipoEntrega,
)
from estoque_utils import ArquivoEstoqueInvalido, itens_do_arquivo
from permissoes import checar_admin_ou_avisar
from pix_validators import TIPO_CHAVE_LABEL, detectar_tipo_chave
from emojis_app import E


async def _produto_da_guild_ou_avisar(interaction: discord.Interaction, produto_id: int, exigir_automatico: bool = False):
    """Busca o produto garantindo que é DESTE servidor (ID de outro servidor
    responde como "não encontrado"). Já responde o erro; se devolver None o
    comando deve só dar `return`."""
    produto = await db.obter_produto(produto_id)
    if not produto or produto["guild_id"] != interaction.guild.id:
        await interaction.response.send_message(f"{E.ERRO} Produto não encontrado nesse servidor.", ephemeral=True)
        return None
    if exigir_automatico and produto["tipo_entrega"] != TipoEntrega.AUTOMATICA.value:
        await interaction.response.send_message(
            f"{E.ERRO} Esse produto é de entrega **manual** — não usa estoque.", ephemeral=True
        )
        return None
    return produto


def _texto_resultado_estoque(nome: str, resultado: dict, total_disponivel: int) -> str:
    linhas = [f"{E.OK} **{resultado['adicionados']}** item(ns) adicionado(s) ao estoque de **{nome}**."]
    if resultado["duplicados"]:
        linhas.append(f"{E.VOLTAR} {resultado['duplicados']} repetido(s) ignorado(s) (já estavam no estoque ou apareceram duas vezes).")
    if resultado["invalidos"]:
        linhas.append(f"{E.AVISO} {resultado['invalidos']} linha(s) grande(s) demais ignorada(s) (máx. {MAX_TAMANHO_ITEM_ESTOQUE} caracteres).")
    linhas.append(f"Total disponível agora: **{total_disponivel}**.")
    return "\n".join(linhas)


class LojaAdmin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    produto = app_commands.Group(name="produto", description="Gerenciar produtos da loja.")

    # ─── Configuração de pagamento ──────────────────────────────────────

    @app_commands.command(name="configurarpagamento", description="[ADMIN] Configura a chave Pix que recebe os pagamentos da loja.")
    @app_commands.describe(
        chave="A chave Pix (CPF, telefone, e-mail ou chave aleatória) cadastrada no seu banco",
        nome_recebedor="Nome que aparece pro cliente no QR Code (ex: nome do titular da conta)",
        cidade="Cidade do titular da conta (aparece no QR Code, opcional)",
    )
    async def configurarpagamento(self, interaction: discord.Interaction, chave: str, nome_recebedor: str, cidade: str = "Sao Paulo"):
        if not await checar_admin_ou_avisar(interaction):
            return

        tipo = detectar_tipo_chave(chave)
        if tipo is None:
            await interaction.response.send_message(
                f"{E.ERRO} Essa chave não parece válida. Aceito CPF, telefone (com DDD), e-mail ou chave aleatória (UUID).",
                ephemeral=True,
            )
            return

        await db.definir_config_loja(interaction.guild.id, chave.strip(), tipo, nome_recebedor.strip(), cidade.strip())

        view = licenca.montar_view_licenca(
            f"{E.OK} Pagamento configurado",
            [
                f"**Chave Pix:** `{chave}` ({TIPO_CHAVE_LABEL[tipo]})",
                f"**Recebedor:** {nome_recebedor}",
                f"**Cidade:** {cidade}",
                "---",
                "Os QR Codes gerados na loja a partir de agora vão usar esses dados.",
            ],
            client=interaction.client,
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    # ─── Produtos ────────────────────────────────────────────────────────

    @produto.command(name="criar", description="[ADMIN] Cria um novo produto na loja.")
    @app_commands.describe(
        nome="Nome do produto",
        preco="Preço em reais (ex: 29.90)",
        tipo_entrega="Automática (puxa do estoque na hora) ou Manual (abre ticket pra entregar)",
        descricao="Descrição que aparece no card do produto",
        imagem_url="URL de uma imagem/banner do produto (opcional)",
    )
    @app_commands.choices(tipo_entrega=[
        app_commands.Choice(name="Automática (estoque)", value=TipoEntrega.AUTOMATICA.value),
        app_commands.Choice(name="Manual (ticket)", value=TipoEntrega.MANUAL.value),
    ])
    async def produto_criar(
        self,
        interaction: discord.Interaction,
        nome: str,
        preco: float,
        tipo_entrega: app_commands.Choice[str],
        descricao: str = None,
        imagem_url: str = None,
    ):
        if not await checar_admin_ou_avisar(interaction):
            return
        if preco <= 0:
            await interaction.response.send_message(f"{E.ERRO} O preço precisa ser maior que zero.", ephemeral=True)
            return

        imagem_limpa = db.limpar_url_imagem(imagem_url) if imagem_url else None
        produto_id = await db.criar_produto(interaction.guild.id, nome.strip(), descricao, preco, tipo_entrega.value, imagem_limpa)

        view = licenca.montar_view_licenca(
            f"{TIPO_ENTREGA_EMOJI[TipoEntrega(tipo_entrega.value)]} Produto criado — `#{produto_id}`",
            [
                f"**Nome:** {nome}",
                f"**Preço:** R$ {preco:.2f}",
                f"**Entrega:** {tipo_entrega.name}",
                "---",
                (
                    f"Cadastre o estoque com `/produto estoque produto_id:{produto_id}` antes de divulgar."
                    if tipo_entrega.value == TipoEntrega.AUTOMATICA.value
                    else "Entrega manual — sem estoque a cadastrar, você entrega no ticket após o pagamento."
                ),
            ],
            client=interaction.client,
        )
        await interaction.response.send_message(view=view, ephemeral=True)

    @produto.command(name="estoque", description="[ADMIN] Adiciona itens ao estoque de um produto de entrega automática.")
    @app_commands.describe(
        produto_id="ID do produto (veja em /produto listar)",
        itens="Um item por linha (ex: chaves/códigos) — cole vários de uma vez",
        permitir_repetidos="Aceitar itens repetidos (padrão: não — repetidos são ignorados)",
    )
    async def produto_estoque(
        self, interaction: discord.Interaction, produto_id: int, itens: str, permitir_repetidos: bool = False
    ):
        if not await checar_admin_ou_avisar(interaction):
            return
        produto_atual = await _produto_da_guild_ou_avisar(interaction, produto_id, exigir_automatico=True)
        if not produto_atual:
            return

        linhas = [linha.strip() for linha in itens.replace("\r", "").split("\n") if linha.strip()]
        if not linhas:
            await interaction.response.send_message(f"{E.ERRO} Nenhum item válido encontrado — separe um por linha.", ephemeral=True)
            return

        resultado = await db.adicionar_estoque_itens(produto_id, linhas, permitir_duplicados=permitir_repetidos)
        total = await db.contar_estoque_disponivel(produto_id)
        await interaction.response.send_message(
            _texto_resultado_estoque(produto_atual["nome"], resultado, total), ephemeral=True
        )

    @produto.command(
        name="estoque_arquivo",
        description="[ADMIN] Adiciona estoque em massa a partir de um arquivo .txt (um item por linha).",
    )
    @app_commands.describe(
        produto_id="ID do produto (veja em /produto listar)",
        arquivo="Arquivo .txt com um item por linha",
        permitir_repetidos="Aceitar itens repetidos (padrão: não — repetidos são ignorados)",
    )
    async def produto_estoque_arquivo(
        self,
        interaction: discord.Interaction,
        produto_id: int,
        arquivo: discord.Attachment,
        permitir_repetidos: bool = False,
    ):
        if not await checar_admin_ou_avisar(interaction):
            return
        produto_atual = await _produto_da_guild_ou_avisar(interaction, produto_id, exigir_automatico=True)
        if not produto_atual:
            return

        if not arquivo.filename.lower().endswith(".txt"):
            await interaction.response.send_message(f"{E.ERRO} Envie um arquivo **.txt** (um item por linha).", ephemeral=True)
            return
        if arquivo.size > MAX_BYTES_ARQUIVO_ESTOQUE:
            await interaction.response.send_message(
                f"{E.ERRO} Arquivo grande demais ({arquivo.size / 1_000_000:.1f} MB). O máximo é "
                f"{MAX_BYTES_ARQUIVO_ESTOQUE / 1_000_000:.0f} MB — divida em partes.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            itens = itens_do_arquivo(await arquivo.read())
        except ArquivoEstoqueInvalido as erro:
            await interaction.followup.send(f"{E.ERRO} {erro}", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send(f"{E.ERRO} Não consegui baixar o arquivo. Tente enviar de novo.", ephemeral=True)
            return

        resultado = await db.adicionar_estoque_itens(produto_id, itens, permitir_duplicados=permitir_repetidos)
        total = await db.contar_estoque_disponivel(produto_id)
        await interaction.followup.send(
            f"{E.ARQUIVO} Li **{len(itens)}** linha(s) de `{arquivo.filename}`.\n"
            + _texto_resultado_estoque(produto_atual["nome"], resultado, total),
            ephemeral=True,
        )

    @produto.command(name="limpar_estoque", description="[ADMIN] Apaga o estoque AINDA NÃO ENTREGUE de um produto.")
    @app_commands.describe(
        produto_id="ID do produto (veja em /produto listar)",
        confirmar="Marque True pra realmente apagar (sem isso só mostra quantos itens seriam apagados)",
    )
    async def produto_limpar_estoque(self, interaction: discord.Interaction, produto_id: int, confirmar: bool = False):
        if not await checar_admin_ou_avisar(interaction):
            return
        produto_atual = await _produto_da_guild_ou_avisar(interaction, produto_id, exigir_automatico=True)
        if not produto_atual:
            return

        disponivel = await db.contar_estoque_disponivel(produto_id)
        if not confirmar:
            await interaction.response.send_message(
                f"{E.AVISO} Isso apagaria **{disponivel}** item(ns) disponíveis de **{produto_atual['nome']}** "
                f"(o que já foi entregue não é tocado). Rode de novo com `confirmar: True` pra apagar.",
                ephemeral=True,
            )
            return

        apagados = await db.limpar_estoque_disponivel(produto_id)
        await interaction.response.send_message(
            f"{E.LIXO} {apagados} item(ns) apagado(s) do estoque de **{produto_atual['nome']}**.", ephemeral=True
        )

    @produto.command(name="editar", description="[ADMIN] Edita nome, preço, descrição, imagem ou liga/desliga um produto.")
    @app_commands.describe(
        produto_id="ID do produto (veja em /produto listar)",
        nome="Novo nome (opcional)",
        preco="Novo preço em reais (opcional) — pedidos já abertos mantêm o preço antigo",
        descricao="Nova descrição (opcional)",
        imagem_url="Nova imagem/banner (opcional)",
        ativo="False tira do catálogo sem apagar; True volta a vender (opcional)",
    )
    async def produto_editar(
        self,
        interaction: discord.Interaction,
        produto_id: int,
        nome: str = None,
        preco: float = None,
        descricao: str = None,
        imagem_url: str = None,
        ativo: bool = None,
    ):
        if not await checar_admin_ou_avisar(interaction):
            return
        produto_atual = await _produto_da_guild_ou_avisar(interaction, produto_id)
        if not produto_atual:
            return

        if preco is not None and preco <= 0:
            await interaction.response.send_message(f"{E.ERRO} O preço precisa ser maior que zero.", ephemeral=True)
            return
        imagem_limpa = None
        if imagem_url is not None:
            imagem_limpa = db.limpar_url_imagem(imagem_url)
            if imagem_limpa is None:
                await interaction.response.send_message(f"{E.ERRO} Essa URL de imagem não parece válida (precisa começar com http:// ou https://).", ephemeral=True)
                return

        mudancas = {
            "nome": nome.strip() if nome else None,
            "preco": preco,
            "descricao": descricao,
            "imagem_url": imagem_limpa,
            "ativo": None if ativo is None else int(ativo),
        }
        if all(valor is None for valor in mudancas.values()):
            await interaction.response.send_message("Nada pra mudar — preencha pelo menos um campo.", ephemeral=True)
            return

        await db.editar_produto(produto_id, **mudancas)
        novo = await db.obter_produto(produto_id)
        status_novo = f"{E.OK} ativo" if novo["ativo"] else f"{E.CANCELADO} inativo"
        await interaction.response.send_message(
            f"{E.OK} Produto `#{produto_id}` atualizado: **{novo['nome']}** — R$ {novo['preco']:.2f} — "
            f"{status_novo}.",
            ephemeral=True,
        )

    @produto.command(name="listar", description="[ADMIN] Lista os produtos cadastrados na loja.")
    async def produto_listar(self, interaction: discord.Interaction):
        if not await checar_admin_ou_avisar(interaction):
            return

        produtos = await db.listar_produtos(interaction.guild.id, apenas_ativos=False)
        if not produtos:
            await interaction.response.send_message("Nenhum produto cadastrado ainda. Use `/produto criar`.", ephemeral=True)
            return

        linhas = []
        usados = 0
        for i, p in enumerate(produtos):
            status = f"{E.CANCELADO} inativo" if not p["ativo"] else f"{E.OK} ativo"
            emoji_entrega = TIPO_ENTREGA_EMOJI[TipoEntrega(p["tipo_entrega"])]
            extra = f" — estoque: {p['estoque_disponivel']}" if p["tipo_entrega"] == TipoEntrega.AUTOMATICA.value else ""
            linha = f"`#{p['id']}` **{p['nome']}** — R$ {p['preco']:.2f} {emoji_entrega}{extra} — {status}"
            # O card de Components V2 aceita no máximo 4000 caracteres de texto e cada
            # emoji custom ocupa ~30 (o unicode ocupava 1-2), então corta a lista antes de estourar.
            if usados + len(linha) > 3400:
                linhas.append(f"\n… e mais {len(produtos) - i} produto(s).")
                break
            usados += len(linha) + 1
            linhas.append(linha)

        view = licenca.montar_view_licenca(f"{E.CARRINHO} Produtos da loja", linhas, client=interaction.client)
        await interaction.response.send_message(view=view, ephemeral=True)

    @produto.command(name="remover", description="[ADMIN] Remove (desativa) um produto do catálogo.")
    @app_commands.describe(produto_id="ID do produto (veja em /produto listar)")
    async def produto_remover(self, interaction: discord.Interaction, produto_id: int):
        if not await checar_admin_ou_avisar(interaction):
            return

        produto_atual = await db.obter_produto(produto_id)
        if not produto_atual or produto_atual["guild_id"] != interaction.guild.id:
            await interaction.response.send_message(f"{E.ERRO} Produto não encontrado nesse servidor.", ephemeral=True)
            return

        await db.remover_produto(produto_id)
        await interaction.response.send_message(f"{E.OK} Produto **{produto_atual['nome']}** removido do catálogo.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LojaAdmin(bot))
