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
from gateways import obter_classe_gateway, instanciar_gateway
from emojis_app import E

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

# URL pública onde ESSE bot está hospedado (Render/Discloud...) — usada só
# pra montar a notification_url que os gateways de pagamento chamam de
# volta quando um Pix é pago. Sem isso, o webhook automático não funciona
# e o admin tem que usar `/pedido aprovar` na mão mesmo pra pedidos feitos
# num gateway automático (a cobrança ainda é criada normalmente).
bot.base_url_publica = os.getenv("BASE_URL", "").rstrip("/") or None

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
            await interaction.response.send_message(f"{E.ERRO} Você não pode usar esse comando agora.", ephemeral=True)
        return

    logger.error(f"Erro não tratado num app command: {error}", exc_info=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"{E.ERRO} Ocorreu um erro ao executar o comando.", ephemeral=True)
        else:
            await interaction.followup.send(f"{E.ERRO} Ocorreu um erro ao executar o comando.", ephemeral=True)
    except discord.HTTPException:
        pass


# ─── Servidor web (keep-alive Render/Discloud + futuro webhook de pagamento) ──

async def handle_home(request):
    return web.Response(text="FFZ Vendas está online ✅")


async def handle_health(request):
    return web.json_response({"ok": True, "bot": str(bot.user) if bot.user else None})


async def _confirmar_pagamento_automatico(pedido_id: int):
    """Mesma lógica de `/pedido aprovar`, só que disparada pelo webhook em
    vez de um admin — reaproveita as mesmas garantias de atomicidade
    (marcar_pedido_pago só deixa UM caminho vencer a corrida)."""
    if not await db.marcar_pedido_pago(pedido_id, aprovado_por=None):
        return  # já tinha sido aprovado/cancelado por outro caminho

    resultado = await db.processar_entrega_automatica(pedido_id)
    pedido = await db.obter_pedido(pedido_id)

    linhas = []
    for item in resultado.get("entregue_auto", []):
        conteudo = "\n".join(f"`{c}`" for c in item["itens"])
        linhas.append(f"{E.RAIO} **{item['nome']}** (entregue automaticamente):\n{conteudo}")
    for item in resultado.get("pendente_manual", []):
        linhas.append(f"{E.TICKET} **{item['nome']}** x{item['quantidade']} — pendente de entrega manual")

    texto = f"{E.OK} **Pagamento confirmado automaticamente!** Pedido `#{pedido_id}`\n\n" + "\n".join(linhas)

    if pedido.get("canal_thread_id"):
        try:
            canal = bot.get_channel(pedido["canal_thread_id"]) or await bot.fetch_channel(pedido["canal_thread_id"])
            await canal.send(texto)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(f"Não consegui avisar o tópico do pedido #{pedido_id} sobre o pagamento confirmado.")

    try:
        comprador = await bot.fetch_user(pedido["user_id"])
        await comprador.send(texto)
    except (discord.Forbidden, discord.HTTPException):
        pass

    if resultado.get("pendente_manual") and pedido.get("canal_thread_id"):
        try:
            canal = bot.get_channel(pedido["canal_thread_id"]) or await bot.fetch_channel(pedido["canal_thread_id"])
            await canal.send(f"{E.TICKET} Item(ns) pendente(s) de entrega manual — rode `/pedido entregarmanual id:{pedido_id}` quando entregar.")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass


async def handle_webhook_gateway(request):
    """Endpoint genérico `/webhook/<gateway>` — cada gateway tem seu
    próprio formato de payload, interpretado por `processar_webhook`
    daquela classe. Sempre responde 200 pra evitar retries infinitos do
    lado do gateway, mesmo quando o evento é ignorado (não é pagamento,
    pedido não encontrado, etc.) — só logamos o motivo."""
    gateway_nome = request.match_info.get("gateway")
    classe = obter_classe_gateway(gateway_nome)
    if classe is None:
        return web.json_response({"ok": False, "erro": "gateway desconhecido"}, status=404)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    evento = classe.processar_webhook(payload, dict(request.headers))
    if not evento:
        return web.json_response({"ok": True})

    charge_id = evento["charge_id"]
    pedido = await db.obter_pedido_por_charge(gateway_nome, charge_id)
    if not pedido:
        logger.warning(f"Webhook {gateway_nome}: charge {charge_id} não corresponde a nenhum pedido conhecido.")
        return web.json_response({"ok": True})

    status = evento["status"]
    if status == "verificar":
        # O gateway só avisou "algo mudou" (ex: Mercado Pago) — precisa
        # consultar o status real usando a credencial DESSE servidor.
        credenciais = await db.obter_gateway_config(pedido["guild_id"], gateway_nome)
        if not credenciais:
            return web.json_response({"ok": True})
        gateway = instanciar_gateway(gateway_nome, credenciais)
        status = await gateway.verificar_pagamento(charge_id)

    if status == "pago":
        await _confirmar_pagamento_automatico(pedido["id"])
    elif status == "cancelado":
        await db.cancelar_pedido(pedido["id"])

    return web.json_response({"ok": True})


async def start_webserver():
    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/webhook/{gateway}", handle_webhook_gateway)
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
