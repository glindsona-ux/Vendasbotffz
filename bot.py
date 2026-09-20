import sys
import signal
import traceback
import logging
import hashlib
import json
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import database as db
import emojis_app
import os
from datetime import datetime, timezone
from aiohttp import web

# Como o bot usa bot.start() (não bot.run()), o discord.py não configura o
# logging sozinho — sem isso, todo logger.info/warning das cogs (fila.py
# etc) ficaria mudo. Formato com timestamp pra facilitar achar coisa nos
# logs da Discloud.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

# Acima desse tamanho, o ffz_esports.db local é considerado "banco real"
# (não o placeholder de 1 byte commitado no repo). Usado só pra decidir se
# vale a pena restaurar por cima com o backup da DM (ver
# restaurar_backup_do_discord). 4KB já é bem folgado - até um banco novinho
# com as tabelas criadas e vazias passa disso.
LIMITE_DB_PLACEHOLDER_BYTES = 4096

def excepthook(type, value, tb):
    print(''.join(traceback.format_exception(type, value, tb)))
sys.excepthook = excepthook

# === SERVIDOR WEB PRA UPTIMEROBOT ===
async def handle(request):
    return web.Response(text="FFZ E-SPORTS BOT ONLINE 🔥")


# === WEBHOOK DO MERCADO PAGO (renovação automática) ===
# O Mercado Pago manda um POST aqui toda vez que o status de um pagamento
# muda. NUNCA confiamos no corpo dessa notificação sozinho -- ela só diz
# "o pagamento X mudou", quem confirma de verdade é a re-consulta direta
# na API (mercadopago_utils.consultar_pagamento), senão qualquer um que
# souber essa URL podia forjar um POST fake e ganhar dias de graça.
async def handle_webhook_mercadopago(request):
    try:
        dados = await request.json()
    except Exception:
        return web.Response(status=400, text="corpo inválido")

    payment_id = None
    if dados.get("type") == "payment":
        payment_id = dados.get("data", {}).get("id")
    if not payment_id:
        # MP também manda notificações de outros tipos (ex: merchant_order)
        # que a gente ignora -- responde 200 mesmo assim pra ele não ficar
        # reenviando a mesma notificação sem parar.
        return web.Response(status=200, text="ignorado")

    try:
        payment_id = int(payment_id)
    except (TypeError, ValueError):
        return web.Response(status=200, text="id inválido, ignorado")

    try:
        import mercadopago_utils
        from cogs.renovacao import processar_pagamento_aprovado

        info = await mercadopago_utils.consultar_pagamento(payment_id)
        if info["status"] == "approved":
            await processar_pagamento_aprovado(bot, payment_id)
    except Exception as e:
        print(f"⚠️ Erro processando webhook do Mercado Pago (payment {payment_id}): {e}")
        traceback.print_exc()
        # Responde 200 mesmo em erro nosso -- devolver erro faz o MP ficar
        # reenviando a mesma notificação em loop; melhor logar e investigar
        # manualmente do que travar o webhook inteiro por causa de 1 pagamento.

    return web.Response(status=200, text="ok")


async def start_webserver():
    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_post('/webhook/mercadopago', handle_webhook_mercadopago)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Servidor web rodando na porta {port}")



# === RESTAURAÇÃO AUTOMÁTICA DE BACKUP ===
# O Discloud (hospedagem atual, via GitHub auto-deploy) parece clonar o
# repositório do zero a cada redeploy/restart, e o ffz_esports.db que fica
# commitado no GitHub é só um placeholder vazio (1 byte) — por isso o bot
# "zerava" tudo (ranking, configs, licenças) toda vez que reiniciava.
# (Esse comportamento foi observado antes no Render também, daí o nome da
# função — mas hoje o risco real é no Discloud.)
#
# O backup é mandado de hora em hora na PRÓPRIA DM do bot com o dono
# (veja cogs/painel_owner.py -> backup_horario). Essa função roda ANTES do
# banco de dados ser aberto pela primeira vez: ela entra nessa DM, acha
# a mensagem mais recente com um anexo .db, baixa e sobrescreve o
# arquivo local — daí sim o banco "de verdade" é aberto já restaurado.
#
# IMPORTANTE: só funciona ler a DM do PRÓPRIO bot (não dá pra ler DM de
# outro bot com você — o Discord não permite isso, cada bot só enxerga
# a própria conversa). Por isso o backup precisa ser mandado pelo FFZ
# E-SPORTS mesmo, não por um bot terceiro tipo o "Vulkan Filas".
#
# Usa um discord.Client TEMPORÁRIO e separado do bot principal só pra
# fazer login + buscar a mensagem, e fecha ele logo em seguida — assim
# não corre risco de logar duas vezes no mesmo `bot` (uma aqui, outra
# quando bot_start() chamar bot.start(token) mais adiante).
async def restaurar_backup_do_discord():
    token = os.getenv('TOKEN')
    if not token:
        print("⚠️ TOKEN não encontrado — pulando restauração automática de backup.")
        return

    # FIX (conflito com Litestream): em deploys que usam start.sh, o
    # Litestream já restaura o banco "de verdade" a partir do R2 ANTES do
    # `python bot.py` sequer começar a rodar — e ele é replicado quase em
    # tempo real, bem mais atualizado que o backup por DM (que só roda de
    # HORA EM HORA, ver cogs/painel_owner.py -> backup_diario).
    #
    # Sem essa checagem, a gente sempre baixava o último .db da DM e
    # SOBRESCREVIA o banco que o Litestream tinha acabado de restaurar,
    # podendo jogar fora até 1h de dados novos toda vez que o bot reiniciava.
    #
    # Agora só restaura pela DM se o banco local ainda parecer o
    # placeholder vazio (1 byte) que fica commitado no repo — ou seja,
    # exatamente o cenário original que esse código foi feito pra resolver
    # (deploy sem Litestream, tipo Discloud, ou Litestream sem réplica
    # ainda no R2). Se o arquivo já tem tamanho de banco real, quem
    # restaurou foi o Litestream e a gente não mexe.
    if os.path.exists(db.DB_PATH):
        tamanho_atual = os.path.getsize(db.DB_PATH)
        if tamanho_atual > LIMITE_DB_PLACEHOLDER_BYTES:
            print(
                f"ℹ️ Banco local já tem {tamanho_atual} bytes (provavelmente restaurado pelo "
                "Litestream) — pulando restauração por DM pra não sobrescrever com um backup "
                "mais antigo (o da DM só atualiza de hora em hora)."
            )
            return

    temp_client = discord.Client(intents=discord.Intents.default())
    try:
        await temp_client.login(token)

        from cogs.owner import OWNER_ID
        dono = await temp_client.fetch_user(OWNER_ID)
        canal_dm = await dono.create_dm()

        # FIX (backup corrompido "reciclado" pra sempre): antes só pegava o
        # ANEXO .db MAIS RECENTE e confiava cegamente nele. Problema: o
        # backup roda a cada 10min direto do arquivo ao vivo — se o banco
        # já tiver corrompido em algum momento (ex: Discloud matou o
        # processo no meio de uma escrita), TODO backup depois disso é só
        # uma cópia da mesma corrupção, e a gente ficava restaurando lixo
        # pra sempre sem perceber (setup_db() não pega esse tipo de erro,
        # só aparece numa query de verdade bem depois).
        #
        # Agora testa cada candidato (mais novo pro mais antigo) com
        # PRAGMA integrity_check antes de aceitar, e pula pro próximo se
        # estiver corrompido — até achar um saudável ou esgotar os
        # candidatos das últimas 50 mensagens.
        candidatos = []
        async for msg in canal_dm.history(limit=50):
            for anexo in msg.attachments:
                if anexo.filename.endswith(".db"):
                    candidatos.append(anexo)

        if not candidatos:
            print("⚠️ Nenhum backup .db encontrado nas últimas 50 mensagens da DM — mantendo banco local atual.")
            return

        caminho_tmp = "ffz_esports_candidato.db"
        for anexo in candidatos:
            await anexo.save(caminho_tmp)
            if await _db_integro(caminho_tmp):
                os.replace(caminho_tmp, "ffz_esports.db")
                print(f"✅ Backup restaurado com sucesso: {anexo.filename} ({anexo.size} bytes)")
                break
            print(f"⚠️ Backup {anexo.filename} está corrompido (falhou no integrity_check) — tentando o próximo mais antigo...")
            os.remove(caminho_tmp)
        else:
            print(
                "❌ TODOS os backups testados nas últimas 50 mensagens da DM estão corrompidos. "
                "Mantendo banco local atual (pode estar corrompido também — avise o dono pra checar backups mais antigos manualmente)."
            )

    except discord.Forbidden:
        print("❌ Sem permissão pra acessar a DM do dono do bot.")
    except Exception as e:
        print(f"❌ Erro ao restaurar backup automático: {e}")
        traceback.print_exc()
    finally:
        await temp_client.close()


async def _db_integro(caminho: str) -> bool:
    """Roda PRAGMA integrity_check num arquivo .db baixado, ANTES de
    aceitar ele como o banco de verdade. Síncrono por dentro (sqlite3 da
    stdlib), mas rodado numa thread separada (asyncio.to_thread) pra não
    travar o loop de eventos do bot enquanto verifica.

    FIX (loop de restauração vazia): um arquivo vazio/placeholder (1 byte)
    passa limpo no integrity_check -- pro SQLite isso é só um banco válido
    sem tabelas, não uma corrupção. Sem checar o tamanho antes, um backup
    vazio contaminado (ver _enviar_backup em cogs/painel_owner.py) era
    aceito igual a um banco de verdade e restaurado por cima no boot.
    """
    import sqlite3

    def _checar():
        try:
            if os.path.getsize(caminho) <= LIMITE_DB_PLACEHOLDER_BYTES:
                return False
            conn = sqlite3.connect(caminho)
            resultado = conn.execute("PRAGMA integrity_check;").fetchone()
            conn.close()
            return bool(resultado) and resultado[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    return await asyncio.to_thread(_checar)

# === BOT ===
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.presences = False  # desligado: aliviar tráfego do gateway (ligue de volta se algum recurso usar status online/offline)

# === BLINDAGEM GLOBAL DE LICENÇA (CommandTree customizada) ===
# FIX BLINDAGEM: até aqui, o bloqueio de licença era "opt-in" -- cada
# slash command precisava lembrar de colocar @requer_licenca() embaixo
# de @app_commands.command. Foi assim que /addcoin, /removercoins e
# /ia-configurar ficaram destrancados sem ninguém perceber (achado só
# numa auditoria manual). Essa CommandTree customizada roda ANTES de
# QUALQUER slash command, existente ou futuro -- inverte o padrão pra
# "bloqueado por padrão", só passa quem tá na exceção explícita definida
# em licenca.comando_isento_de_licenca (comandos do dono, /ativar,
# /licenca, /comandos, /debug_paineis). Um comando novo que ninguém
# lembrou de decorar agora fica travado por padrão, não liberado.
class FFZCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True  # autocomplete etc. não é bloqueado aqui

        if interaction.guild is None:
            return True  # comando usado em DM (ex: dono do bot) -- licença é por servidor, não se aplica

        import licenca
        if licenca.comando_isento_de_licenca(interaction.command):
            return True

        # Comandos com @requer_licenca() vão checar de novo aqui embaixo
        # (redundante, mas barato e inofensivo -- e cobre qualquer um que
        # não tenha o decorator). checar_licenca_view já manda o embed de
        # bloqueio pro usuário e retorna False se não tiver licença ativa.
        return await licenca.checar_licenca_view(interaction)


# === PREFIXO CUSTOMIZÁVEL POR SERVIDOR ===
# FEATURE: antes era um "." fixo em todo lugar. Agora cada servidor pode
# trocar pelo que tiver costume (ex: "!") em /configurar -> Editar Visual
# do Bot -> Prefixo (ver cogs/configurar.py -> ModalPrefixo). Guardado na
# coluna `prefixo` da tabela `configuracoes` (migração em
# cogs/configurar.py -> setup()).
PREFIXO_PADRAO = "."


async def get_prefix(bot_: commands.Bot, message: discord.Message):
    """Callable de command_prefix -- roda a CADA mensagem recebida, então
    precisa ser rápido e nunca estourar exceção (senão nenhum comando de
    prefixo funciona em lugar nenhum, nem o padrão).

    Em DM (message.guild is None) não existe servidor pra consultar -- usa
    sempre o "." padrão ali (comandos de dono como .sync, .painel etc.
    continuam funcionando igual de sempre por lá).

    db.obter_config() já tem cache em memória por guild_id (_cache_config),
    então isso só bate no banco de verdade na PRIMEIRA mensagem de cada
    servidor depois de um restart -- nas próximas é leitura de dict, mesmo
    custo de antes quando o prefixo era só uma string fixa.
    """
    if message.guild is None:
        return PREFIXO_PADRAO

    try:
        config = await db.obter_config(message.guild.id)
        prefixo = str(config.get("prefixo") or "").strip()
    except Exception as e:
        # BLINDAGEM: se o banco falhar bem no momento de ler o prefixo
        # (ex: "database is locked" passageiro), cai pro "." padrão em vez
        # de travar TODO comando de prefixo do servidor por causa disso.
        print(f"⚠️ Erro ao obter prefixo customizado (guild {message.guild.id}): {e} -- usando padrão '.'")
        return PREFIXO_PADRAO

    return prefixo or PREFIXO_PADRAO


bot = commands.Bot(command_prefix=get_prefix, intents=intents, tree_cls=FFZCommandTree)
bot.start_time = datetime.now()  # usado no painel de Estatísticas
startup_errors = []  # erros durante a inicialização, avisados por DM quando o bot conectar

@bot.event
async def on_connect():
    print("✅ Conectou no gateway do Discord")


# === ERROR HANDLER GLOBAL DO COMMAND TREE ===
# FIX: sem isso, qualquer check que falha (has_permissions, requer_licenca em
# algum caso de borda, cooldown, ou até um erro inesperado dentro do comando)
# nunca respondia a interação -> o usuário via "A interação falhou" sem
# nenhuma mensagem de erro de verdade. Isso cobre TODOS os slash commands
# do bot (existentes e os novos: SS, anúncios, convites, tickets).
def _bloqueio_de_licenca_recente(guild_id: int) -> bool:
    try:
        import licenca
        return licenca.bloqueio_recente(guild_id)
    except Exception:
        return False


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Se já tiver sido respondido (ex: o check já mandou uma msg de licença
    # bloqueada), não faz nada — já foi tratado.
    if interaction.response.is_done():
        pass
    elif (
        isinstance(error, app_commands.CheckFailure)
        and interaction.guild_id is not None
        and _bloqueio_de_licenca_recente(interaction.guild_id)
    ):
        # FIX BUG REAL (mensagem duplicada: 🔒 sem licença + ❌ genérica
        # juntas): o app mobile do Discord às vezes dispara a MESMA ação
        # como DUAS interações separadas (bug já documentado em
        # responder_seguro() e no diagnóstico [DIAG] acima). A 1ª
        # interação já mostrou o embed completo de "sem licença" com o
        # motivo certo; a 2ª (duplicada) cai aqui como CheckFailure só que
        # numa interação DIFERENTE, então o "interaction.response.is_done()"
        # dela é False e a gente mandava esse fallback genérico por cima,
        # empilhando duas mensagens confusas pra mesma ação. Se o mesmo
        # servidor já viu o bloqueio de licença há poucos segundos, não
        # repete -- só loga (ver bloco de log mais abaixo, roda sempre).
        pass
    else:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "❌ Você não tem permissão pra usar esse comando."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "❌ Você não pode usar esse comando agora."
            # FIX DIAGNÓSTICO: mesma ideia do bloco de erro genérico logo
            # abaixo -- só que aplicado ao CheckFailure (que antes só
            # mostrava a mensagem genérica pro dono também, sem pista
            # nenhuma). Mostra, só pro dono: se a interação já tinha sido
            # respondida antes de chegar aqui (o que não devia acontecer,
            # já que checamos isso lá em cima -- se aparecer mesmo assim é
            # sinal de condição de corrida), quanto tempo se passou desde
            # que o Discord criou a interação, e a causa original do erro
            # se tiver uma (ex: erro de rede/interação disfarçado de
            # CheckFailure, ver comentário mais abaixo sobre duas
            # instâncias do bot rodando com o mesmo token).
            try:
                from cogs.owner import eh_dono
                if await eh_dono(interaction.user.id):
                    delta = (datetime.now(timezone.utc) - interaction.created_at).total_seconds()
                    causa = error.__cause__
                    msg += (
                        f"\n\n🔧 DIAG: comando `/{interaction.command.name if interaction.command else '?'}` "
                        f"| guild `{interaction.guild_id}` | {delta:.2f}s desde a criação da interação "
                        f"| já respondida antes de chegar aqui? `{interaction.response.is_done()}` "
                        f"| causa original: `{type(causa).__name__ + ': ' + str(causa) if causa else '—'}`"
                    )
            except Exception:
                pass
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏱️ Calma! Tenta de novo em {error.retry_after:.0f}s."
        else:
            msg = "❌ Ocorreu um erro ao executar o comando. O dono do bot já foi avisado."
            # FIX DIAGNÓSTICO: pro dono do bot (só pra ele, nunca pra
            # clientes comuns) mostra o tipo/mensagem real do erro DIRETO
            # na resposta ephemeral -- antes só saía no console do Discloud,
            # e sem acesso fácil ao console não dava pra saber o motivo real
            # por trás do "Ocorreu um erro" genérico.
            try:
                from cogs.owner import eh_dono
                if await eh_dono(interaction.user.id):
                    causa = getattr(error, "original", error)
                    msg += f"\n\n🔧 `{type(causa).__name__}: {causa}`"
            except Exception:
                pass
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            try:
                await interaction.followup.send(msg, ephemeral=True)
            except discord.HTTPException:
                pass

    # FIX: CheckFailure/MissingPermissions não poluem a DM do dono (são
    # esperados no dia a dia), mas antes também não deixavam NENHUM rastro
    # nem no console -- se um check de licença falhasse de um jeito
    # inesperado (ex: sem mostrar o motivo real pro usuário), não tinha
    # como descobrir depois o que rolou. Agora sempre loga no console
    # (comando + guild + tipo do erro), só não manda DM pro dono.
    if isinstance(error, (app_commands.CheckFailure, app_commands.MissingPermissions, app_commands.CommandOnCooldown)):
        # FIX DIAGNÓSTICO: antes só logava "CheckFailure" genérico, sem a
        # mensagem original do discord.py -- impossível saber SE o bloqueio
        # foi de verdade (predicate retornou False de propósito) ou se foi
        # um erro de rede/interação (ex: "Unknown interaction" / "This
        # interaction has already been acknowledged") disfarçado de
        # CheckFailure. Essa segunda situação é o sintoma clássico de DUAS
        # instâncias do bot rodando com o mesmo token ao mesmo tempo (ex:
        # testando local enquanto o Discloud ainda está de pé, ou um
        # restart que não matou o processo antigo) -- o Discord manda a
        # interação pras duas conexões, uma responde e "ganha", a outra
        # cai aqui achando que foi bloqueio de permissão/licença quando na
        # verdade só perdeu a corrida. Agora o log mostra a causa raiz.
        print(
            f"[CHECK FAILED] /{interaction.command.name if interaction.command else '?'} "
            f"(guild `{interaction.guild_id}`, user `{interaction.user.id}`): "
            f"{type(error).__name__}: {error} | causa original: {type(error.__cause__).__name__ + ': ' + str(error.__cause__) if error.__cause__ else '—'}"
        )

    # Loga sempre, mesmo quando já foi respondido, e avisa o dono se for
    # um erro "de verdade" (não CheckFailure/MissingPermissions, que são
    # esperados no dia a dia e não merecem poluir a DM do dono).
    if not isinstance(error, (app_commands.CheckFailure, app_commands.CommandOnCooldown)):
        print(f"[TREE ERROR] /{interaction.command.name if interaction.command else '?'}: {error}")
        traceback.print_exc()
        try:
            from cogs.owner import OWNER_ID
            dono = await bot.fetch_user(OWNER_ID)
            await dono.send(
                f"⚠️ Erro no comando `/{interaction.command.name if interaction.command else '?'}` "
                f"(guild `{interaction.guild_id}`): `{type(error).__name__}: {error}`"
            )
        except Exception:
            pass

@bot.event
async def on_guild_join(guild: discord.Guild):
    """Sync INSTANTÂNEO só pra esse servidor específico assim que o bot
    entra nele. Isso resolve a demora do sync global (que pode levar até
    1h pra propagar) — o cliente compra, adiciona o bot, e os comandos
    slash (incluindo /ativar) já aparecem na hora pra ele."""
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ Sync instantâneo em '{guild.name}' ({guild.id}): {len(synced)} comandos")
    except Exception as e:
        print(f"❌ Erro no sync instantâneo de '{guild.name}': {e}")

    # 👋 BOAS-VINDAS — manda instrução de ativação pro dono do servidor que
    # acabou de adicionar o bot, e te avisa também que rolou uma instalação
    # nova (bom sinal de venda pra acompanhar).
    embed_boas_vindas = discord.Embed(
        title="👋 Valeu por adicionar o FFZ E-SPORTS SYSTEM!",
        description=(
            "Pra liberar o bot nesse servidor, ative sua licença com:\n"
            "`/ativar chave:SUA-KEY-AQUI`\n\n"
            "Não tem uma key ainda? Fala com quem te vendeu o bot."
        ),
        color=0x2ECC71
    )
    try:
        await guild.owner.send(embed=embed_boas_vindas)
    except (discord.HTTPException, discord.Forbidden, AttributeError):
        # DM fechada ou owner não resolvido em cache — tenta no canal do sistema
        try:
            if guild.system_channel:
                await guild.system_channel.send(embed=embed_boas_vindas)
        except discord.HTTPException:
            pass

    # 🔗 Tenta gerar um link de convite pra esse servidor novo, pra vc
    # conseguir entrar e dar uma olhada sem precisar pedir pro dono.
    # Só funciona se o bot tiver permissão de "Criar Convite" em pelo
    # menos um canal — nem todo servidor concede isso, então isso pode
    # falhar silenciosamente (não é bug, é permissão mesmo).
    link_convite = None
    try:
        canal_alvo = guild.system_channel
        if canal_alvo is None or not canal_alvo.permissions_for(guild.me).create_instant_invite:
            canal_alvo = None
            for canal in guild.text_channels:
                if canal.permissions_for(guild.me).create_instant_invite:
                    canal_alvo = canal
                    break
        if canal_alvo is not None:
            convite = await canal_alvo.create_invite(
                max_age=0, max_uses=0, unique=False,
                reason="Link automático pro dono do bot acompanhar a instalação"
            )
            link_convite = convite.url
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"⚠️ Não consegui gerar convite pra '{guild.name}': {e}")

    try:
        from cogs.owner import OWNER_ID
        dono_bot = await bot.fetch_user(OWNER_ID)

        dono_servidor = guild.owner
        if dono_servidor is None and guild.owner_id:
            try:
                dono_servidor = await bot.fetch_user(guild.owner_id)
            except (discord.NotFound, discord.HTTPException):
                dono_servidor = None

        embed_nova_instalacao = discord.Embed(
            title=f"{emojis_app.obter('ffz_estatisticas', '📈')} Nova instalação",
            description=f"O bot acabou de ser adicionado em **{guild.name}**.",
            color=0x2ECC71,
            timestamp=discord.utils.utcnow()
        )
        if guild.icon:
            embed_nova_instalacao.set_thumbnail(url=guild.icon.url)

        embed_nova_instalacao.add_field(name=f"{emojis_app.obter('ffz_etiqueta', '🏷️')} Servidor", value=guild.name, inline=True)
        embed_nova_instalacao.add_field(name=f"{emojis_app.obter('ffz_verificarid', '🆔')} ID", value=f"`{guild.id}`", inline=True)
        embed_nova_instalacao.add_field(name=f"{emojis_app.obter('ffz_participantes', '👥')} Membros", value=str(guild.member_count), inline=True)
        embed_nova_instalacao.add_field(
            name=f"{emojis_app.obter('ffz_coroa', '👑')} Dono",
            value=f"{dono_servidor.mention} (`{dono_servidor.id}`)" if dono_servidor else "Não encontrado",
            inline=True
        )
        embed_nova_instalacao.add_field(
            name="📅 Servidor criado em",
            value=discord.utils.format_dt(guild.created_at, style="D"),
            inline=True
        )
        embed_nova_instalacao.add_field(name="🌐 Total de servidores", value=str(len(bot.guilds)), inline=True)
        embed_nova_instalacao.add_field(
            name="🔗 Convite",
            value=link_convite if link_convite else "_Sem permissão de 'Criar Convite' em nenhum canal_",
            inline=False
        )
        embed_nova_instalacao.set_footer(text="FFZ E-SPORTS SYSTEM • Notificação automática")

        await dono_bot.send(embed=embed_nova_instalacao)
    except Exception as e:
        print(f"❌ Não consegui te avisar sobre a nova instalação: {e}")

def _calcular_hash_comandos(tree: app_commands.CommandTree) -> str:
    """Gera uma 'assinatura' de todos os comandos registrados na tree local
    (nomes, descrições, parâmetros). Se essa assinatura não mudou desde o
    último sync bem-sucedido, não faz sentido gastar chamada de API
    re-sincronizando -- é exatamente isso que causa os 429 quando o bot
    reinicia várias vezes seguidas (ex: durante testes/deploys rápidos)."""
    dados = []
    for cmd in sorted(tree.get_commands(), key=lambda c: c.name):
        params = sorted(getattr(cmd, "parameters", []) or [], key=lambda p: p.name)
        dados.append({
            "nome": cmd.name,
            "descricao": getattr(cmd, "description", ""),
            "params": [(p.name, str(p.type)) for p in params],
        })
    bruto = json.dumps(dados, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()

async def _ler_hash_salvo() -> dict:
    # Guardado no BANCO (não em arquivo), porque o filesystem do Discloud
    # não persiste entre deploys -- só o .db sobrevive (via backup por DM).
    bruto = await db.get_meta("sync_hash")
    if not bruto:
        return {}
    try:
        return json.loads(bruto)
    except Exception:
        return {}

async def _salvar_hash(hash_atual: str, guild_ids: list):
    try:
        await db.set_meta("sync_hash", json.dumps({"hash": hash_atual, "guild_ids": sorted(guild_ids)}))
    except Exception as e:
        print(f"⚠️ Não consegui salvar o hash de sync no banco: {e}")

async def sincronizar_comandos(forcar: bool = False) -> str:
    """Sincroniza os slash commands em todos os servidores, respeitando o
    cache de hash (pula se nada mudou) — a não ser que forcar=True, usado
    pelo comando manual .sync pra sincronizar na hora sem esperar um
    restart. Retorna uma mensagem curta com o resultado, pra tanto o
    on_ready (só printa) quanto o comando .sync (manda no chat) reusarem."""
    hash_atual = _calcular_hash_comandos(bot.tree)
    guild_ids_atuais = [g.id for g in bot.guilds]
    salvo = await _ler_hash_salvo()
    precisa_sync = forcar or (
        salvo.get("hash") != hash_atual
        or salvo.get("guild_ids") != sorted(guild_ids_atuais)
    )

    if not precisa_sync:
        msg = "⏭️ Comandos não mudaram desde o último sync — pulando (evita rate limit em restarts seguidos)."
        print(msg)
        return msg

    print("🔄 Limpando registro global antigo (evita comando duplicado)...")
    await bot.http.bulk_upsert_global_commands(bot.application_id, payload=[])
    print("✅ Registro global limpo.")

    print(f"🔄 Sincronizando comandos localmente em {len(bot.guilds)} servidor(es)...")
    semaforo = asyncio.Semaphore(10)

    async def sincronizar_guild(guild: discord.Guild):
        async with semaforo:
            try:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                return guild.id, len(synced), True
            except Exception as e:
                print(f"❌ Erro ao sincronizar em '{guild.name}': {e}")
                return guild.id, 0, False

    resultados = await asyncio.gather(*(sincronizar_guild(g) for g in bot.guilds))
    total = sum(qtd for _, qtd, _ in resultados)
    guild_ids_com_sucesso = [gid for gid, _, ok in resultados if ok]
    guild_ids_com_falha = [gid for gid, _, ok in resultados if not ok]

    msg = f"✅ Sincronizei comandos em {len(guild_ids_com_sucesso)}/{len(bot.guilds)} servidor(es) ({total} no total)"
    if guild_ids_com_falha:
        msg += f" — ⚠️ {len(guild_ids_com_falha)} falharam e vão tentar de novo no próximo restart: {guild_ids_com_falha}"
    print(msg)
    # Só marca como "sincronizado" quem realmente deu certo -- os que
    # falharam ficam de fora dessa lista salva, então na próxima checagem
    # de hash (bot.guilds vai continuar tendo o guild_id deles) a
    # comparação salvo['guild_ids'] != guild_ids_atuais vai bater diferente
    # e o sync tenta de novo sozinho, sem precisar de restart forçado nem
    # esperar o código mudar. Antes disso, um servidor que falhasse (ex:
    # 429 momentâneo) ficava com comandos faltando (tipo /configurar e
    # /criarfila) PRA SEMPRE, porque o hash era salvo como se tivesse dado
    # tudo certo mesmo assim.
    await _salvar_hash(hash_atual, guild_ids_com_sucesso)

    try:
        from cogs.painel_owner import _enviar_backup
        from cogs.owner import OWNER_ID
        destino_id = OWNER_ID
        if destino_id is None:
            donos = await db.listar_owners()
            destino_id = donos[0] if donos else None
        if destino_id:
            owner = await bot.fetch_user(destino_id)
            await _enviar_backup(owner)
    except Exception as e:
        print(f"⚠️ Backup imediato pós-sync falhou (não bloqueia o startup): {e}")

    return msg


@bot.event
async def on_ready():
    print(f'✅ BOT ONLINE: {bot.user} | ID: {bot.user.id}')

    await emojis_app.carregar(bot)
    emojis_app.iniciar_refresh_automatico(bot)

    try:
        await sincronizar_comandos()
    except Exception as e:
        print(f"❌ ERRO AO SINCRONIZAR: {e}")
        traceback.print_exc()
        startup_errors.append(f"Falha no sync: `{type(e).__name__}: {e}`")

@bot.command(name="sync")
async def sync_manual(ctx: commands.Context):
    """Comando de prefixo (.sync), só pro dono do bot. Força a
    sincronização dos slash commands na hora, ignorando o cache de hash."""
    from cogs.owner import eh_dono
    if not await eh_dono(ctx.author.id):
        return
    aviso = await ctx.reply("🔄 Sincronizando na hora (forçado, ignorando cache)...")
    try:
        resultado = await sincronizar_comandos(forcar=True)
        await aviso.edit(content=resultado)
    except Exception as e:
        await aviso.edit(content=f"❌ Erro ao sincronizar: `{e}`")
        traceback.print_exc()

    # 🚨 ALERTA POR DM — avisa o dono se algo deu errado na inicialização
    # (cog não carregou, banco falhou, sync falhou). Se o processo inteiro
    # cair/crashar antes de conseguir logar, o bot não consegue mandar DM
    # nenhuma (óbvio, não tá conectado) — pra esse caso, configure alerta
    # de downtime no próprio UptimeRobot que já bate no endpoint web do
    # bot (start_webserver), porque só um serviço externo consegue avisar
    # quando o processo cai de vez.
    try:
        from cogs.owner import OWNER_ID
        owner = await bot.fetch_user(OWNER_ID)
        if startup_errors:
            embed = discord.Embed(
                title="⚠️ Bot iniciou com problemas",
                description="\n".join(f"• {erro}" for erro in startup_errors[:15]),
                color=0xE74C3C
            )
            embed.set_footer(text=f"{bot.user} • {datetime.now().strftime('%d/%m %H:%M')}")
            await owner.send(embed=embed)
        else:
            print("✅ Sem erros na inicialização, DM de alerta não enviada.")
    except Exception as e:
        print(f"❌ Não consegui avisar o dono por DM: {e}")


@bot.command(name="refreshemojis")
async def refresh_emojis_manual(ctx: commands.Context):
    """Comando de prefixo (.refreshemojis), só pro dono do bot. Recarrega os
    emojis de aplicação na hora, sem esperar o próximo ciclo automático de
    30 minutos — usa isso logo depois de trocar/criar um emoji no Developer
    Portal pra não ficar tomando 'Invalid emoji' até o próximo ciclo.

    FIX: o cogs/fila.py tinha um cache PRÓPRIO, separado (EMOJIS_APP_CACHE),
    com fetch_application_emojis() próprio rodando em paralelo com o daqui
    -- 2 chamadas da mesma API disputando corrida a cada restart, e a do
    fila.py perdia direto (cache ficava em 0 até rodar esse comando na
    mão). Removido o cache duplicado: agora só existe o cache do
    emojis_app.py, e esse comando recarrega ele — só ele mesmo.
    """
    from cogs.owner import eh_dono
    if not await eh_dono(ctx.author.id):
        return
    await emojis_app.carregar(bot)
    await ctx.reply(f"✅ {emojis_app.total()} emoji(s) de aplicação recarregado(s).")


@bot.command(name="listaremojisapp")
async def listar_emojis_app(ctx: commands.Context):
    """.listaremojisapp — diagnóstico: mostra os nomes de emoji que o
    código PROCURA (constantes EMOJI_*_FIXO) x quais realmente estão
    carregados no cache agora. Usa pra descobrir na hora se um emoji
    "sumido" é falta de sincronizar cache ou se o nome cadastrado no
    Developer Portal não bate com o que o código espera (typo/grafia
    diferente) -- nesse 2º caso nenhum .refreshemojis resolve, só
    renomear o emoji no Developer Portal (ou ajustar a constante no
    código) pra ficar idêntico, caractere por caractere."""
    from cogs.owner import eh_dono
    if not await eh_dono(ctx.author.id):
        return

    from cogs import fila as cog_fila

    esperados = {
        nome: valor for nome, valor in vars(cog_fila).items()
        if nome.startswith("EMOJI_") and nome.endswith("_FIXO") and isinstance(valor, str)
    }

    linhas = []
    for nome_constante, nome_emoji in sorted(esperados.items(), key=lambda x: x[1]):
        achado = emojis_app._cache.get(nome_emoji)
        status = "✅" if achado else "❌ NÃO ENCONTRADO"
        linhas.append(f"{status} `{nome_emoji}` ({nome_constante})")

    texto = "\n".join(linhas) or "Nenhuma constante EMOJI_*_FIXO encontrada."
    embed = discord.Embed(
        title="🔍 Diagnóstico de Emojis de Aplicação",
        description=(
            f"Cache do emojis_app.py: **{emojis_app.total()}** emoji(s) carregado(s)\n\n"
            f"{texto}\n\n"
            "❌ = o nome que o código procura não existe (ainda) no cache. "
            "Se você já subiu um emoji parecido no Developer Portal mas o "
            "nome não é IDÊNTICO ao que tá entre crases acima, é por isso "
            "que ele nunca aparece — renomeia lá pra bater certinho."
        ),
        color=0x5865F2,
    )
    await ctx.reply(embed=embed)

async def load_cogs():
    print("🔄 Carregando cogs...")
    cogs = [
        'cogs.owner',        # sistema de keys/licença — carregar ANTES dos outros
        'cogs.painel_owner', # painel de controle via DM (!painel)
        'cogs.apagarpixadmin',
        'cogs.admin_coins',
        'cogs.configurar',
        'cogs.embedbuilder',
        'cogs.fila',
        'cogs.loja_produtos',  # Loja Digital: produto + estoque + entrega automática (conta/key)
        'cogs.cassino',   # Loja + Roleta + Caixa Premiada, tudo num arquivo só agora
        'cogs.paineis',
        'cogs.painelmediador',
        'cogs.pix',
        'cogs.pix_autolink',  # detecta chave PIX solta na thread (após sala liberada) e gera embed + QR sozinho
        'cogs.ranking',
        # --- integrados na consolidação SaaS ---
        'cogs.solicitarss',
        'cogs.solicitar_telador',  # espelho do solicitarss.py, pro cargo de Telador (fila/painel/W.O/ranking próprios)
        'cogs.auto_mensagem',
        'cogs.convites',  # inclui o anti-raid/anti-fake/anti-link/anti-palavrão (moderação embutida)
        'cogs.tickets',
        'cogs.ia_suporte',
        'cogs.painel_config',
        # --- fila de streamer (ex-VULKAN STORE), integrada nesta consolidação ---
        'cogs.streamer_fila',
        # --- bater ponto ---
        'cogs.ponto',
        # --- ranking e dashboard de mediadores ---
        'cogs.ranking_mediador',
        'cogs.dashboard_mediador',
        # --- /logs_mediador e /exportar_logs: histórico unificado de ações do mediador ---
        'cogs.logs_mediador',
        # --- gerenciador de emojis (add/remove em massa) ---
        'cogs.emojis',
        # --- foto de perfil do bot por servidor ---
        'cogs.avatar_manager',
        # --- blacklist de IDs por servidor ---
        'cogs.blacklist',
        # --- painel central UNIFICADO: .apostas vira o painel único
        # (Resumo das Filas com seletor Geral/Hoje/Ontem + dashboard geral
        # + diagnóstico técnico completo), substituindo as 3 partes que
        # existiam separadas: 'cogs.central_stats' (/central_dashboard,
        # /central_parar) e 'cogs.diagnostico' (.apostas antigo + botão
        # "Diagnóstico completo") -- por isso as duas SAÍRAM da lista.
        # Os dois arquivos continuam no projeto (suas funções auxiliares
        # são reaproveitadas de dentro de cogs/painel_central.py), só não
        # são mais carregados como extensão pra não duplicar comando.
        'cogs.painel_central',
        # --- /comandos e /comandosdono (lista de comandos em embed) ---
        'cogs.comandos',
        # --- painel de status da assinatura + renovação automática via Pix ---
        'cogs.painel_licenca',
        'cogs.renovacao',
        # --- .shop: cadastro manual de cliente (vincula comprador -> servidor
        # dele, pro Painel de Cliente saber de qual servidor mostrar a
        # assinatura mesmo fixado no servidor de suporte) ---
        'cogs.cadastro_cliente',
        # --- /painelmulta: fluxo completo de aplicar/cobrar/confirmar multa ---
        'cogs.painel_multa',
        # --- .logs @user: verificação de logs (filas + tickets) de um membro ---
        'cogs.logs_membro',
        # --- .filasativas: painel de filas abertas agora + últimas fechadas ---
        'cogs.filas_ativas',
        # --- .ticketativos: painel de tickets abertos agora + últimos fechados ---
        'cogs.ticket_ativos',
        # --- /logs-setup: cria a categoria + todos os canais de log do bot
        # de uma vez, organizados e já ligados na config (ver logs_partidas.py) ---
        'cogs.estrutura_logs',
        # --- !tela / !t / !config: verificação de tela via sala de Google Meet
        # (antes era o bot separado FFZ Call) -- self-contained: cria a própria
        # tabela (ffz_data.db, arquivo separado do banco principal), registra
        # sozinho o painel persistente de espectador no cog_load(). Precisa de
        # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN no
        # config.env (ver google_meet.py) -- sem isso, só esses 2 comandos
        # falham (com erro claro), o resto do bot funciona normal.
        'cogs.painel_tela',
    ]
    for cog in cogs:
        try:
            await bot.load_extension(cog)
            print(f'✅ Cog {cog} carregada')
        except Exception as e:
            print(f'❌ ERRO AO CARREGAR {cog}:')
            traceback.print_exc()
            startup_errors.append(f"Falha ao carregar `{cog}`: `{type(e).__name__}: {e}`")

ARQUIVO_HEALTHCHECK_DB = "healthcheck_ultimo.json"

# Tabelas monitoradas pelo healthcheck de boot — se alguma delas cair de
# forma suspeita (tinha dado, agora não tem mais) em relação ao boot
# anterior, é sinal forte de que o disco foi zerado/parcialmente perdido
# (ex: Discloud limpando storage num redeploy) e não só um restart normal.
TABELAS_HEALTHCHECK = ["filas", "configuracoes", "assinaturas"]


async def checar_saude_banco():
    """BLINDAGEM: roda logo depois do banco abrir (setup_db) e ANTES dos
    cogs carregarem, pra pegar dois cenários que antes só apareciam muito
    depois (ou nunca), na cara do cliente, sem ninguém saber a tempo:

    1) Banco CORROMPIDO (PRAGMA integrity_check falha) — antes só estourava
       erro na primeira query de verdade, já com o bot online e servidor
       recebendo erro_interno.
    2) Banco ESVAZIADO (ex: fila que tinha registro sumiu) — o cenário
       real que já aconteceu aqui: Discloud apaga o disco, o arquivo local
       volta a ser um banco válido só que ZERADO, e como não é o
       placeholder de 1 byte, a restauração automática por DM nem entra
       em ação (ver restaurar_backup_do_discord).

    Guarda a contagem de linhas das tabelas críticas num arquivo local
    pequeno (healthcheck_ultimo.json) a cada boot, e compara com o boot
    ANTERIOR. Se cair de forma suspeita, empilha em startup_errors --
    que já é mandado por DM pro dono no on_ready, sem precisar duplicar
    lógica de envio aqui.
    """
    contagem_atual = {}
    try:
        conn = await db.get_conn()

        cursor = await conn.execute("PRAGMA integrity_check")
        resultado = await cursor.fetchone()
        if not resultado or resultado[0] != "ok":
            startup_errors.append(
                f"🚨 `PRAGMA integrity_check` falhou no banco (`{resultado}`) — banco pode estar corrompido. "
                f"Considera restaurar um backup (`!restaurar` na DM ou `!backup` pra pegar o mais recente)."
            )

        for tabela in TABELAS_HEALTHCHECK:
            try:
                cursor = await conn.execute(f"SELECT COUNT(*) FROM {tabela}")
                linha = await cursor.fetchone()
                contagem_atual[tabela] = linha[0] if linha else 0
            except Exception:
                # Tabela pode não existir ainda em bancos bem antigos —
                # não é motivo de alerta por si só.
                pass
    except Exception as e:
        print(f"⚠️ Healthcheck do banco não rodou (não bloqueia o boot): {e}")
        return

    contagem_anterior = {}
    if os.path.exists(ARQUIVO_HEALTHCHECK_DB):
        try:
            with open(ARQUIVO_HEALTHCHECK_DB, "r") as f:
                contagem_anterior = json.load(f)
        except Exception:
            contagem_anterior = {}

    for tabela, atual in contagem_atual.items():
        anterior = contagem_anterior.get(tabela, 0)
        # Só alerta em queda BRUSCA (>= 50% sumindo) partindo de uma base
        # que já tinha dado real — evita alarme falso em banco novo
        # (anterior=0) ou em variações normais (ex: fila apagada de propósito
        # pelo próprio dono, que já dispara backup imediato à parte).
        if anterior >= 5 and atual <= anterior * 0.5:
            startup_errors.append(
                f"🚨 Tabela `{tabela}` caiu de {anterior} pra {atual} linha(s) desde o último boot — "
                f"parece perda de dado (disco zerado?). Considera restaurar um backup (`!restaurar` na DM)."
            )

    try:
        with open(ARQUIVO_HEALTHCHECK_DB, "w") as f:
            json.dump(contagem_atual, f)
    except Exception as e:
        print(f"⚠️ Não consegui salvar o healthcheck deste boot: {e}")


async def main():
    await start_webserver()

    # Restaura o backup mais recente do Discord ANTES de abrir o banco de
    # dados pela primeira vez — se não fizer isso antes, db.setup_db() já
    # abre e trava numa conexão com o arquivo vazio/placeholder.
    await restaurar_backup_do_discord()

    # FIX: setup_db() movido pra cá, antes de carregar os cogs.
    # Antes estava no on_ready(), que roda DEPOIS dos cogs já tentarem
    # usar o banco — causando "database is locked" e tabelas não encontradas.
    #
    # BLINDAGEM (erro_interno em massa): antes, se setup_db() falhasse
    # (disco lento, arquivo do restore ainda sendo liberado pelo SO etc.),
    # o erro era só logado e o boot CONTINUAVA -- o bot ficava online e
    # respondendo com o banco quebrado, e TODO servidor (até quem paga)
    # caía em "erro_interno" na checagem de licença. A grande maioria
    # desses casos é transitória e resolve sozinha em poucos segundos, então
    # agora tenta de novo (com pausa crescente) antes de desistir. Só depois
    # de esgotar as tentativas é que segue mesmo assim (loga pro dono via
    # startup_errors) -- nesse ponto realmente é uma falha séria, não
    # transitória, e não tem sentido travar o bot pra sempre sem nem
    # conseguir avisar o dono por DM (que só é possível depois de conectar).
    for tentativa in range(1, 4):
        try:
            await db.setup_db()
            print("✅ Banco de dados OK")
            break
        except Exception as e:
            print(f"❌ Erro no DB (tentativa {tentativa}/3): {e}")
            if tentativa < 3:
                await asyncio.sleep(2 * tentativa)  # 2s, depois 4s
                continue
            traceback.print_exc()
            startup_errors.append(f"Falha ao iniciar o banco de dados após 3 tentativas: `{type(e).__name__}: {e}`")

    await checar_saude_banco()

    await load_cogs()

    try:
        from cogs.ranking import RankingView
        bot.add_view(RankingView())
        print("✅ View ranking registrada")
    except Exception as e:
        print(f"❌ Erro view ranking: {e}")

    # View persistente do painel de blacklist — mesmo motivo dos outros:
    # sem registrar de novo a cada restart, os botões Verificar ID /
    # Adicionar Blacklist param de responder.
    try:
        from cogs.blacklist import PainelBlacklistView
        bot.add_view(PainelBlacklistView())
        print("✅ View painel de blacklist registrada")
    except Exception as e:
        print(f"❌ Erro view painel de blacklist: {e}")

    # Views persistentes do sistema de tickets — precisam ser registradas
    # de novo a cada restart, senão os botões/select param de funcionar
    # (mesmo bug que já existia no bot de tickets original).
    #
    # FIX (registro duplicado): o PainelTicketView(0, 0) NÃO é registrado
    # aqui mais -- isso já é feito 1x dentro do próprio cog, em
    # Tickets.cog_load() (cogs/tickets.py), que roda garantidamente antes
    # deste bloco (load_cogs() já subiu todos os cogs logo acima). Registrar
    # de novo aqui era redundante (a mesma view/custom_id sendo adicionada
    # duas vezes ao internal store do discord.py) e só atrapalhava debug
    # futuro. O que continua tendo que ser feito aqui é o resto, que
    # depende de dado do banco (lista de painéis) e não faz sentido morar
    # dentro do cog_load de um cog só.
    try:
        from cogs.painel_config import AbrirTicketView, AbrirTicketContainerView
        paineis = await db.listar_todos_paineis_ativos() if hasattr(db, "listar_todos_paineis_ativos") else await db.obter_todos_paineis()
        for p in paineis:
            if p.get('estilo_painel') == 'moderno':
                bot.add_view(AbrirTicketContainerView(p['id']))
            else:
                bot.add_view(AbrirTicketView(p['id']))
        print(f"✅ Views de tickets registradas ({len(paineis)} painel(is))")
    except Exception as e:
        print(f"❌ Erro views de tickets: {e}")

    # Views persistentes da fila de streamer — mesmo motivo dos tickets acima:
    # sem registrar de novo a cada restart, os botões Entrar/Sair/Próximo e
    # Confirmar/Cancelar param de responder.
    try:
        from cogs.streamer_fila import registrar_views_paineis, registrar_views_atendimentos
        registrar_views_paineis(bot)
        registrar_views_atendimentos(bot)
        print("✅ Views da fila de streamer registradas")
    except Exception as e:
        print(f"❌ Erro views da fila de streamer: {e}")

    # View persistente do painel de bater ponto — mesmo motivo dos outros:
    # sem registrar de novo a cada restart, os botões Iniciar/Fechar/etc
    # param de responder.
    try:
        from cogs.ponto import registrar_views_paineis as registrar_views_ponto
        registrar_views_ponto(bot)
        print("✅ View painel de ponto registrada")
    except Exception as e:
        print(f"❌ Erro view painel de ponto: {e}")

    # Views persistentes do sistema de solicitar SS — diferente das outras
    # acima, aqui cada mensagem tem dados PRÓPRIOS (jogador, solicitante,
    # analista), então não dá pra registrar 1 instância genérica igual ao
    # ranking/ponto/dashboard: cada view precisa ser recriada com os dados
    # certos e escopada à mensagem certa via message_id, senão o discord.py
    # não sabe qual instância usar quando o custom_id é o mesmo em várias
    # mensagens (ss_assumir / ss_limpo / ss_wo se repetem em cada painel).
    try:
        from cogs.solicitarss import ViewSSAssumir, ViewSSResultado
        pendentes = await db.obter_ss_pendentes()
        for p in pendentes:
            view = ViewSSAssumir(p['id'], p['modalidade'], p['jogador_id'], p['solicitante_id'], p['origem_id'])
            bot.add_view(view, message_id=p['message_id_solicitacao'])
        em_analise = await db.obter_ss_em_analise()
        for p in em_analise:
            view = ViewSSResultado(p['id'], p['modalidade'], p['jogador_id'], p['analista_id'], p['solicitante_id'])
            bot.add_view(view, message_id=p['message_id_resultado'])
        print(f"✅ Views de SS registradas ({len(pendentes)} pendente(s), {len(em_analise)} em análise)")
    except Exception as e:
        print(f"❌ Erro views de SS: {e}")

    # Views persistentes do sistema de Telador — espelho do bloco de SS
    # logo acima, mesmo motivo (tel_assumir / tel_limpo / tel_wo se repetem
    # em cada painel, cada mensagem tem dados próprios).
    try:
        from cogs.solicitar_telador import ViewTeladorAssumir, ViewTeladorResultado
        pendentes_tel = await db.obter_tel_pendentes()
        for p in pendentes_tel:
            view = ViewTeladorAssumir(p['id'], p['modalidade'], p['jogador_id'], p['solicitante_id'], p['origem_id'])
            bot.add_view(view, message_id=p['message_id_solicitacao'])
        em_analise_tel = await db.obter_tel_em_analise()
        for p in em_analise_tel:
            view = ViewTeladorResultado(p['id'], p['modalidade'], p['jogador_id'], p['analista_id'], p['solicitante_id'])
            bot.add_view(view, message_id=p['message_id_resultado'])
        print(f"✅ Views de Telador registradas ({len(pendentes_tel)} pendente(s), {len(em_analise_tel)} em análise)")
    except Exception as e:
        print(f"❌ Erro views de Telador: {e}")

    # View persistente do /dashboard_mediador — mesmo motivo dos outros:
    # os 4 botões (Perfil/Atividade/Emblemas/Comparativo) usam custom_id
    # fixo, então o Discord acha o callback certo em QUALQUER painel já
    # postado antes, independente do label/emoji configurado em cada guild
    # (isso é só cosmético e já fica salvo na mensagem original). Só
    # precisa de 1 instância registrada aqui pra "destravar" todos.
    try:
        from cogs.dashboard_mediador import ViewDashboardMediador
        from database import _DASHBOARD_MED_CONFIG_PADRAO
        bot.add_view(ViewDashboardMediador(_DASHBOARD_MED_CONFIG_PADRAO))
        print("✅ View dashboard de mediadores registrada")
    except Exception as e:
        print(f"❌ Erro view dashboard de mediadores: {e}")

    # Views persistentes do sistema de partidas (fila.py) e do Painel do
    # Mediador (painelmediador.py) — MESMO MOTIVO de todas as views acima,
    # mas essas duas nunca tinham sido registradas aqui. Na prática, toda
    # vez que o bot reiniciava com uma partida em "Esperando Confirmar" ou
    # com o select do mediador aberto, esses componentes morriam de vez
    # (clique dava "essa interação falhou", sem recuperação possível).
    # Os valores de init (0, 0, 0, 0, "") são só placeholder — as ações
    # de ambas as views já buscam a partida fresca no banco a partir de
    # interaction.channel.id, então funcionam pra QUALQUER thread mesmo
    # religadas de forma genérica assim.
    try:
        from cogs.fila import ViewPartida, ViewCopiarChavePix, ViewPagamentoLiberadoV2, ViewPagamentoLiberadoCompleto
        bot.add_view(ViewPartida())
        bot.add_view(ViewCopiarChavePix())
        # Botão "Jogar Contra" da fila de streamer (cogs/streamer_fila.py)
        # reaproveita esse mesmo fluxo de partida (Confirmar/Cancelar +
        # Definir Valor) -- mesmo motivo de tudo aqui: sem religar depois
        # de um restart, os botões dessas threads param de responder.
        from cogs.streamer_fila import ViewPartidaStreamer
        bot.add_view(ViewPartidaStreamer())
        # Card V2 "Pagamento Liberado" (Components V2, formato antigo) —
        # mantida só pra religar cards já enviados ANTES dessa troca pro
        # embed clássico continuarem funcionando depois de um restart.
        bot.add_view(ViewPagamentoLiberadoV2())
        # Card novo (embed clássico + select "Ações do mediador" + botão
        # "Copiar Chave Pix" juntos na mesma View/mensagem) — usado a
        # partir de agora em _revelar_pagamento_pix.
        bot.add_view(ViewPagamentoLiberadoCompleto())
        print("✅ Views de partida (Confirmar/Cancelar + Copiar Chave Pix + Pagamento Liberado V2 + Completo) registradas")
    except Exception as e:
        print(f"❌ Erro view de partida: {e}")

    # View persistente do painel "Sala Liberada" (Copiar ID / Trocar Valor)
    # — mesmo motivo das duas acima, mas essa NUNCA tinha sido registrada
    # aqui: depois de um restart com uma sala já lançada numa thread, os
    # botões morriam de vez (interação falhava sem recuperação). Os valores
    # de init são só placeholder — os callbacks já buscam id_sala/modo
    # frescos no banco a partir de interaction.channel_id, então essa 1
    # instância genérica destrava os botões de QUALQUER sala já lançada.
    try:
        from cogs.painelmediador import ViewSalaLiberada
        bot.add_view(ViewSalaLiberada(0, "", 0, "", ""))
        print("✅ View de sala liberada (Copiar ID/Trocar Valor) registrada")
    except Exception as e:
        print(f"❌ Erro view de sala liberada: {e}")

    try:
        from cogs.painelmediador import ViewPainelMediadorSelect, ViewPainelMediador
        bot.add_view(ViewPainelMediadorSelect(bot, 0, 0, 0, 0, ""))
        bot.add_view(ViewPainelMediador(bot, 0, 0, 0, 0, ""))
        print("✅ Views do Painel do Mediador registradas")
    except Exception as e:
        print(f"❌ Erro views do Painel do Mediador: {e}")

    await bot_start()

async def _shutdown_gracioso(nome_sinal: str):
    """Roda quando o processo recebe SIGTERM/SIGINT (redeploy, restart
    manual, `docker stop`, etc). Faz um checkpoint + backup de última hora
    ANTES de deixar o bot desconectar, cobrindo a janela entre o último
    backup do loop de 10min e o momento exato do desligamento -- sem
    isso, exatamente essa janela é o que já causou perda de fila/licença
    recém-criada em redeploys anteriores (ver comentários em
    cogs/owner.py -> _backup_apos_acao_critica). Best-effort: se algo
    falhar aqui, ainda assim deixa o bot fechar (não trava o processo
    preso tentando desligar)."""
    print(f"🛑 Sinal {nome_sinal} recebido -- shutdown gracioso (checkpoint + backup final)...")
    try:
        await db.checkpoint_wal()
    except Exception as e:
        print(f"⚠️ Checkpoint no shutdown falhou (não impede o desligamento): {e}")
    try:
        from cogs.owner import OWNER_ID
        from cogs.painel_owner import _enviar_backup
        destino_id = OWNER_ID
        if destino_id is None:
            donos = await db.listar_owners()
            destino_id = donos[0] if donos else None
        if destino_id is not None:
            owner = await bot.fetch_user(destino_id)
            await _enviar_backup(owner)
            print("✅ Backup final de shutdown enviado")
    except Exception as e:
        print(f"⚠️ Backup final de shutdown falhou (não impede o desligamento): {e}")
    await bot.close()


async def bot_start():
    token = os.getenv('TOKEN')
    if not token:
        print("❌ ERRO: TOKEN não encontrado nas Environment Variables do Render!")
        return

    # BLINDAGEM (perda de escritas recentes num redeploy): o Discloud
    # provavelmente manda SIGTERM pro processo antes de matá-lo/apagar o
    # disco num redeploy (é o sinal padrão de "encerre com calma" em
    # containers) -- mas sem um handler pra ele, o processo simplesmente
    # morre na hora, e qualquer escrita recente que ainda só existia no
    # -wal (não mesclada no .db principal) some, mesmo que o backup
    # automático de 10min ainda não tivesse rodado. Handler AQUI, e não
    # só confiar no loop de 10min: junta as duas camadas -- o loop cobre
    # perda por CRASH (sem aviso nenhum), esse handler cobre perda por
    # REDEPLOY/restart normal (COM aviso, que é o caso mais comum no dia
    # a dia). Só registra em loop de verdade (SO Unix -- Discloud é
    # Linux); em ambiente sem suporte a isso, só ignora e segue confiando
    # no loop de 10min sozinho, como era antes.
    try:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(_shutdown_gracioso(s.name)))
        print("✅ Handler de shutdown gracioso (SIGTERM/SIGINT) registrado")
    except (NotImplementedError, RuntimeError) as e:
        print(f"⚠️ Não consegui registrar handler de shutdown gracioso ({e}) -- seguindo só com o backup de 10min")

    # FIX (bot "zumbi" após instabilidade do Discord): antes, qualquer erro
    # fora de LoginFailure/PrivilegedIntentsRequired (ex: DiscordServerError
    # -- Discord respondendo 5xx no login, instabilidade passageira do lado
    # deles) fazia bot_start() desistir de vez. O webserver (start_webserver,
    # que roda antes disso no main()) continuava de pé, então o Discloud
    # mostrava "online" só pelo processo/porta responderem -- mas o bot
    # nunca mais tentava logar de novo, ficando offline no Discord até
    # alguém reiniciar manualmente. Agora tenta de novo com backoff pra
    # erros que são plausivelmente passageiros (rede/servidor do Discord),
    # e só desiste de vez pra erro de credencial/config (que retry nenhum
    # resolve).
    tentativa = 0
    max_tentativas = 5
    while tentativa < max_tentativas:
        tentativa += 1
        try:
            print(f"🔑 Token encontrado, conectando no Discord... (tentativa {tentativa}/{max_tentativas})")
            await bot.start(token)
            return  # bot.start() só retorna em close() normal -- sai do loop
        except discord.LoginFailure:
            print("❌ ERRO: TOKEN INVÁLIDO! (não adianta tentar de novo, abortando)")
            return
        except discord.PrivilegedIntentsRequired:
            print("❌ ERRO: INTENTS DESLIGADAS NO DEV PORTAL! (não adianta tentar de novo, abortando)")
            return
        except (discord.ConnectionClosed, discord.GatewayNotFound, discord.HTTPException, OSError) as e:
            # Cobre DiscordServerError (subclasse de HTTPException), timeout
            # de rede, gateway fora do ar etc -- tudo que é razoável esperar
            # que se resolva sozinho em alguns segundos/minutos.
            espera = min(30 * tentativa, 120)  # 30s, 60s, 90s, 120s, 120s
            print(f"⚠️ Falha transitória conectando no Discord ({type(e).__name__}: {e}). "
                  f"Tentando de novo em {espera}s...")
            if tentativa < max_tentativas:
                await asyncio.sleep(espera)
        except Exception as e:
            print(f"❌ ERRO FATAL NO BOT:")
            traceback.print_exc()
            return

    print(f"❌ Desisti de conectar no Discord após {max_tentativas} tentativas. "
          "Verifique a página de status do Discord (discordstatus.com) ou reinicie manualmente.")

if __name__ == "__main__":
    print("=== INICIANDO BOT ===")
    asyncio.run(main())

