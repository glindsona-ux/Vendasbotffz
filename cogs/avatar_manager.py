"""
========================================================================
 AVATAR_MANAGER.PY — FOTO DE PERFIL DO BOT POR SERVIDOR
========================================================================
Permite trocar a foto de perfil do bot DENTRO de um servidor específico
(guild avatar), sem afetar o avatar global da conta do bot em nenhum
outro lugar. Usa PATCH /guilds/{guild_id}/members/@me da API do Discord
via bot.http.request, porque discord.py não expõe um método de alto
nível pra isso.

Guarda em banco (tabela avatar_bot_config) só o timestamp da última
troca por servidor, pra aplicar um cooldown e evitar 429 (rate limit)
do Discord — a imagem em si não fica salva, ela já vive no CDN do
Discord assim que é aplicada.
========================================================================
"""

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.http import Route
from datetime import datetime, timedelta
import database as db
from licenca import requer_licenca, checar_licenca_view
from permissoes import checar_admin_ou_avisar
from emojis_app import E

# Discord deixa trocar avatar (global ou de servidor) só poucas vezes
# em um intervalo curto antes de começar a devolver 429. Cooldown
# conservador pra nunca bater nesse limite mesmo em uso normal.
COOLDOWN_MINUTOS = 10


async def _trocar_avatar_guild(bot: commands.Bot, guild_id: int, image_bytes: bytes | None):
    """image_bytes=None remove o avatar customizado do servidor (volta pro global)."""
    avatar_data = discord.utils._bytes_to_base64_data(image_bytes) if image_bytes else None
    route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
    return await bot.http.request(route, json={"avatar": avatar_data})


async def _trocar_banner_guild(bot: commands.Bot, guild_id: int, image_bytes: bytes | None):
    """image_bytes=None remove o banner customizado do servidor.

    Mesmo endpoint do avatar (PATCH /guilds/{id}/members/@me), que
    também aceita o campo "banner" segundo a documentação oficial do
    Discord — não é gambiarra, é um campo real do "Modify Current
    Member". Não tenho 100% de certeza que esse campo específico já
    está liberado pra CONTA DE BOT (o rollout desse recurso foi
    gradual), mas o avatar já funciona pro bot nesse mesmo endpoint, e
    se o Discord recusar o erro vem tratado (403/400) com mensagem
    clara pro usuário, não quebra silencioso.
    """
    banner_data = discord.utils._bytes_to_base64_data(image_bytes) if image_bytes else None
    route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
    return await bot.http.request(route, json={"banner": banner_data})


async def _checar_cooldown(guild_id: int, tipo: str = "avatar") -> tuple[bool, int]:
    """Retorna (pode_trocar, minutos_restantes). tipo: "avatar" ou "banner"
    — cooldowns independentes, trocar um não trava o outro."""
    coluna = "ultima_troca" if tipo == "avatar" else "ultima_troca_banner"
    conn = await db.get_conn()
    cursor = await conn.execute(
        f"SELECT {coluna} FROM avatar_bot_config WHERE guild_id = ?", (guild_id,)
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return True, 0

    ultima_troca = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    liberado_em = ultima_troca + timedelta(minutes=COOLDOWN_MINUTOS)
    agora = datetime.now()
    if agora >= liberado_em:
        return True, 0

    minutos_restantes = int((liberado_em - agora).total_seconds() // 60) + 1
    return False, minutos_restantes


async def _registrar_troca(guild_id: int, tipo: str = "avatar"):
    coluna = "ultima_troca" if tipo == "avatar" else "ultima_troca_banner"
    conn = await db.get_conn()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    await conn.execute(
        f"INSERT INTO avatar_bot_config (guild_id, {coluna}) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {coluna} = excluded.{coluna}",
        (guild_id, agora)
    )
    await conn.commit()


async def _aplicar_novo_avatar(interaction: discord.Interaction, image_bytes: bytes) -> bool:
    """Valida cooldown + tamanho, aplica o avatar e responde a interação.
    Retorna True se aplicou com sucesso, False se bloqueou/errou (a
    mensagem de erro já foi enviada, o chamador só precisa parar).
    Espera que a interação já esteja deferida (ephemeral)."""
    pode_trocar, minutos_restantes = await _checar_cooldown(interaction.guild_id, "avatar")
    if not pode_trocar:
        await interaction.followup.send(
            f"{E.TEMPO} Calma! Você já trocou a foto recentemente. Tenta de novo em **{minutos_restantes} min**.",
            ephemeral=True
        )
        return False

    if len(image_bytes) > 10 * 1024 * 1024:
        await interaction.followup.send(
            f"{E.ERRO} Imagem muito grande (máx 10MB pro Discord aceitar).", ephemeral=True
        )
        return False

    try:
        await _trocar_avatar_guild(interaction.client, interaction.guild_id, image_bytes)
        await _registrar_troca(interaction.guild_id, "avatar")
    except discord.HTTPException as e:
        if e.status == 429:
            await interaction.followup.send(
                f"{E.ERRO} O Discord recusou por limite de trocas (429). Aguarda alguns minutos e tenta de novo.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"{E.ERRO} Erro do Discord ao trocar o avatar: {e}", ephemeral=True)
        return False
    except Exception as e:
        await interaction.followup.send(f"{E.ERRO} Erro inesperado: {e}", ephemeral=True)
        return False

    return True


async def _aplicar_novo_banner(interaction: discord.Interaction, image_bytes: bytes) -> bool:
    """Mesma lógica de _aplicar_novo_avatar, só que pro banner."""
    pode_trocar, minutos_restantes = await _checar_cooldown(interaction.guild_id, "banner")
    if not pode_trocar:
        await interaction.followup.send(
            f"{E.TEMPO} Calma! Você já trocou o banner recentemente. Tenta de novo em **{minutos_restantes} min**.",
            ephemeral=True
        )
        return False

    if len(image_bytes) > 10 * 1024 * 1024:
        await interaction.followup.send(
            f"{E.ERRO} Imagem muito grande (máx 10MB pro Discord aceitar).", ephemeral=True
        )
        return False

    try:
        await _trocar_banner_guild(interaction.client, interaction.guild_id, image_bytes)
        await _registrar_troca(interaction.guild_id, "banner")
    except discord.HTTPException as e:
        if e.status == 429:
            await interaction.followup.send(
                f"{E.ERRO} O Discord recusou por limite de trocas (429). Aguarda alguns minutos e tenta de novo.",
                ephemeral=True
            )
        elif e.status in (400, 403):
            await interaction.followup.send(
                f"{E.ERRO} O Discord recusou a troca de banner (esse recurso pode ainda não estar liberado pra "
                "conta de bot). O avatar continua funcionando normal — só o banner que não colou dessa vez.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"{E.ERRO} Erro do Discord ao trocar o banner: {e}", ephemeral=True)
        return False
    except Exception as e:
        await interaction.followup.send(f"{E.ERRO} Erro inesperado: {e}", ephemeral=True)
        return False

    return True


def _embed_painel(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title=f"{E.IMAGEM} Foto de Perfil & Banner do Bot — Este Servidor",
        description=(
            "Troque a foto de perfil **e o banner** do bot só neste servidor, sem mexer "
            "no que aparece no resto do Discord.\n\n"
            "**Foto de perfil:**\n"
            "• Rode `/avatarbot` de novo anexando uma imagem direto (galeria/câmera), **ou**\n"
            "• Clique em **Enviar Foto por Link** abaixo\n\n"
            "**Banner:**\n"
            "• Clique em **Enviar Banner por Link** abaixo (recomendado 16:9)\n\n"
            f"{E.TEMPO} Cooldown entre trocas: **{COOLDOWN_MINUTOS} minutos** (limite do próprio Discord, "
            "cada um com o cooldown independente)."
        ),
        color=0xFFFFFF
    )
    if guild.me.display_avatar:
        embed.set_thumbnail(url=guild.me.display_avatar.url)
    if guild.me.banner:
        embed.set_image(url=guild.me.banner.url)
    embed.set_footer(text=f"{guild.name}")
    return embed


class ModalNovoAvatar(ui.Modal, title="Nova Foto de Perfil"):
    url_imagem = ui.TextInput(
        label="Link direto da imagem",
        placeholder="https://exemplo.com/imagem.png",
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        url = db.limpar_url_imagem(self.url_imagem.value)
        if not url:
            return await interaction.response.send_message(
                f"{E.ERRO} Link inválido. Precisa ser um link direto começando com http:// ou https://",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            f"{E.ERRO} Não consegui baixar essa imagem (status {resp.status}). Verifica o link.",
                            ephemeral=True
                        )
                    image_bytes = await resp.read()
        except Exception as e:
            return await interaction.followup.send(f"{E.ERRO} Erro ao baixar a imagem: {e}", ephemeral=True)

        if not await _aplicar_novo_avatar(interaction, image_bytes):
            return

        embed = _embed_painel(interaction.guild)
        await interaction.followup.send(f"{E.OK} Foto de perfil atualizada só neste servidor!", ephemeral=True)
        try:
            await interaction.message.edit(embed=embed, view=PainelAvatar())
        except (discord.HTTPException, AttributeError):
            pass


class ModalNovoBanner(ui.Modal, title="Novo Banner"):
    url_imagem = ui.TextInput(
        label="Link direto da imagem (recomendado 16:9)",
        placeholder="https://exemplo.com/banner.png",
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        url = db.limpar_url_imagem(self.url_imagem.value)
        if not url:
            return await interaction.response.send_message(
                f"{E.ERRO} Link inválido. Precisa ser um link direto começando com http:// ou https://",
                ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return await interaction.followup.send(
                            f"{E.ERRO} Não consegui baixar essa imagem (status {resp.status}). Verifica o link.",
                            ephemeral=True
                        )
                    image_bytes = await resp.read()
        except Exception as e:
            return await interaction.followup.send(f"{E.ERRO} Erro ao baixar a imagem: {e}", ephemeral=True)

        if not await _aplicar_novo_banner(interaction, image_bytes):
            return

        embed = _embed_painel(interaction.guild)
        await interaction.followup.send(f"{E.OK} Banner atualizado só neste servidor!", ephemeral=True)
        try:
            await interaction.message.edit(embed=embed, view=PainelAvatar())
        except (discord.HTTPException, AttributeError):
            pass


class PainelAvatar(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await checar_licenca_view(interaction)

    @ui.button(label="Enviar Foto por Link", style=discord.ButtonStyle.blurple, emoji=E.LINK, custom_id="avatar_enviar", row=0)
    async def btn_enviar(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ModalNovoAvatar())

    @ui.button(label="Restaurar Foto Padrão", style=discord.ButtonStyle.grey, emoji=E.VOLTAR, custom_id="avatar_resetar", row=0)
    async def btn_resetar(self, interaction: discord.Interaction, button: ui.Button):
        pode_trocar, minutos_restantes = await _checar_cooldown(interaction.guild_id, "avatar")
        if not pode_trocar:
            return await interaction.response.send_message(
                f"{E.TEMPO} Calma! Tenta de novo em **{minutos_restantes} min**.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            await _trocar_avatar_guild(interaction.client, interaction.guild_id, None)
            await _registrar_troca(interaction.guild_id, "avatar")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"{E.ERRO} Erro do Discord: {e}", ephemeral=True)

        embed = _embed_painel(interaction.guild)
        await interaction.followup.send(f"{E.OK} Avatar deste servidor restaurado pro padrão global.", ephemeral=True)
        try:
            await interaction.message.edit(embed=embed, view=PainelAvatar())
        except (discord.HTTPException, AttributeError):
            pass

    @ui.button(label="Enviar Banner por Link", style=discord.ButtonStyle.blurple, emoji=E.PALETA, custom_id="banner_enviar", row=1)
    async def btn_enviar_banner(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ModalNovoBanner())

    @ui.button(label="Restaurar Banner Padrão", style=discord.ButtonStyle.grey, emoji=E.VOLTAR, custom_id="banner_resetar", row=1)
    async def btn_resetar_banner(self, interaction: discord.Interaction, button: ui.Button):
        pode_trocar, minutos_restantes = await _checar_cooldown(interaction.guild_id, "banner")
        if not pode_trocar:
            return await interaction.response.send_message(
                f"{E.TEMPO} Calma! Tenta de novo em **{minutos_restantes} min**.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        try:
            await _trocar_banner_guild(interaction.client, interaction.guild_id, None)
            await _registrar_troca(interaction.guild_id, "banner")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"{E.ERRO} Erro do Discord: {e}", ephemeral=True)

        embed = _embed_painel(interaction.guild)
        await interaction.followup.send(f"{E.OK} Banner deste servidor restaurado pro padrão.", ephemeral=True)
        try:
            await interaction.message.edit(embed=embed, view=PainelAvatar())
        except (discord.HTTPException, AttributeError):
            pass


class AvatarManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="avatarbot", description="Troca ou abre o painel da foto de perfil e do banner do bot neste servidor")
    @app_commands.describe(
        imagem="Anexe uma imagem pra trocar a FOTO DE PERFIL direto. Deixe vazio pra abrir o painel.",
        banner="Anexe uma imagem pra trocar o BANNER direto (pode usar junto com 'imagem')."
    )
    @requer_licenca()
    async def avatarbot(self, interaction: discord.Interaction, imagem: discord.Attachment = None, banner: discord.Attachment = None):
        if not await checar_admin_ou_avisar(interaction):
            return
        if imagem is None and banner is None:
            embed = _embed_painel(interaction.guild)
            return await interaction.response.send_message(embed=embed, view=PainelAvatar(), ephemeral=True)

        if imagem is not None and not (imagem.content_type or "").startswith("image/"):
            return await interaction.response.send_message(
                f"{E.ERRO} O anexo de foto de perfil não parece ser uma imagem. Anexa um png, jpg ou webp.", ephemeral=True
            )
        if banner is not None and not (banner.content_type or "").startswith("image/"):
            return await interaction.response.send_message(
                f"{E.ERRO} O anexo de banner não parece ser uma imagem. Anexa um png, jpg ou webp.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)
        resultados = []

        if imagem is not None:
            image_bytes = await imagem.read()
            if await _aplicar_novo_avatar(interaction, image_bytes):
                resultados.append(f"{E.OK} Foto de perfil atualizada")

        if banner is not None:
            banner_bytes = await banner.read()
            if await _aplicar_novo_banner(interaction, banner_bytes):
                resultados.append(f"{E.OK} Banner atualizado")

        if resultados:
            await interaction.followup.send(" e ".join(resultados) + " só neste servidor!", ephemeral=True)


async def setup(bot):
    conn = await db.get_conn()
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS avatar_bot_config (
            guild_id INTEGER PRIMARY KEY,
            ultima_troca TEXT,
            ultima_troca_banner TEXT
        )
    """)
    # Migração pra quem já tinha a tabela antes do banner existir.
    try:
        await conn.execute("ALTER TABLE avatar_bot_config ADD COLUMN ultima_troca_banner TEXT")
    except Exception:
        pass  # coluna já existe
    await conn.commit()

    bot.add_view(PainelAvatar())
    await bot.add_cog(AvatarManager(bot))
