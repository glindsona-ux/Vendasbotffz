import logging
import os
import sys
import traceback
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from aiohttp import web

import database as db
import licenca
from constants import MINUTOS_EXPIRAR_PEDIDO

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ffzvendas")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("❌ Variável de ambiente DISCORD_TOKEN ausente.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class FFZCommandTree(app_commands.CommandTree):
    """Bloqueado por padrão: qualquer slash command novo que ninguém
    lembrou de proteger já nasce travado, em vez de liberado por engano."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True

        if interaction.guild is None:
            return True  # DM (ex: dono do bot) — licença é por servidor

        if licenca.comando_isento_de_licenca(interaction.command):
            return True

        return await licenca.checar_licenca_view(interaction)


bot = commands.Bot(command_prefix="!", intents=intents, tree_cls=FFZCommandTree)
bot.start_time = datetime.now()

EXTENSIONS_DIR = os.path.join(os.path.dirname(__file__), "cogs")


async def carregar_cogs():
    for arquivo in os.listdir(EXTENSIONS_DIR):
        if arquivo.endswith(".py") and not arquivo.startswith("_"):
            extensao = f"cogs.{arquivo[:-3]}"
            try:
                await bot.load_extension(extensao)
                logger.info(f"✅ Carregado: {extensao}")
            except Exception:
                logger.error(f"❌ Erro ao carregar {extensao}:\n{traceback.format_exc()}")


@tasks.loop(minutes=5)
async def expirar_pedidos_task():
    try:
        total = await db.expirar_pedidos_antigos(MINUTOS_EXPIRAR_PEDIDO)
        if total:
            logger.info(f"🕓 {total} pedido(s) expirado(s) por falta de pagamento — estoque liberado de volta.")
    except Exception:
        logger.error(f"❌ Erro ao expirar pedidos antigos:\n{traceback.format_exc()}")


@bot.event
async def on_ready():
    logger.info(f"🔥 FFZ Vendas online como {bot.user}")
    logger.info(f"📡 Conectado em {len(bot.guilds)} servidor(es)")
    try:
        synced = await bot.tree.sync()
        logger.info(f"🔄 {len(synced)} slash command(s) sincronizado(s)")
    except Exception:
        logger.error(f"❌ Erro ao sincronizar comandos:\n{traceback.format_exc()}")

    if not expirar_pedidos_task.is_running():
        expirar_pedidos_task.start()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # Provavelmente já foi tratado (embed de bloqueio de licença já
        # enviado pelo CommandTree/decorator) — evita mensagem duplicada.
        if interaction.guild and licenca.bloqueio_recente(interaction.guild.id):
            return
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Você não pode usar esse comando agora.", ephemeral=True)
        return

    logger.error(f"Erro não tratado num app command: {error}", exc_info=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Ocorreu um erro ao executar o comando.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Ocorreu um erro ao executar o comando.", ephemeral=True)
    except discord.HTTPException:
        pass


# ─── Servidor web (keep-alive Render/Discloud + futuro webhook de pagamento) ──

async def handle_home(request):
    return web.Response(text="FFZ Vendas está online ✅")


async def handle_health(request):
    return web.json_response({"ok": True, "bot": str(bot.user) if bot.user else None})


async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 3000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Webserver rodando na porta {port}")


async def main():
    await db.setup_db()
    await carregar_cogs()
    await start_webserver()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
