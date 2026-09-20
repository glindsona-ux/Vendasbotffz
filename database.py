import aiosqlite
import asyncio
import logging
import os
from datetime import datetime, timedelta

# Texto padrão de abertura de ticket (fonte única, usada tanto na migração
# de painéis antigos quanto na criação de painéis novos — ver
# criar_painel_ticket). Só existe aqui em cima pra não depender do DEFAULT
# gravado na coluna `mensagem_abertura` da tabela `ticket_paineis`: esse
# DEFAULT fica fixo no arquivo .db desde quando a tabela foi criada, e não
# se atualiza sozinho só porque esse texto mudou aqui no código -- por
# isso painéis novos criados num banco antigo continuavam nascendo com uma
# versão de fábrica desatualizada (a "resumida"), mesmo em servidores
# novos, mesmo depois do texto "certo" já estar no código há tempos.
MENSAGEM_ABERTURA_PADRAO = (
    "{ffz_diamante} Bem-vindo ao canal oficial de atendimento do servidor **{servidor}**\n\n"
    "{ffz_setabaixo} Categoria selecionada: `{categoria}`\n\n"
    "{ffz_escudo} Todos os responsáveis já estão cientes do seu chamado. Descreva com "
    "o máximo de detalhes possível o motivo do contato para agilizar o atendimento.\n\n"
    "{ffz_alerta} Evite chamar alguém via DM, apenas aguarde que a equipe irá te atender."
)
import json
import re

DB_PATH = "ffz_esports.db"

# Caracteres invisíveis comuns quando o link de imagem é colado pelo celular
# (espaço non-breaking, zero-width space/joiner, BOM, etc.). Sozinhos eles
# fazem o link "parecer" válido pro olho humano mas quebram o
# .startswith('http'), fazendo thumb_url/banner_url virar None silenciosamente
# e a imagem simplesmente não aparecer (sem erro, sem ícone quebrado).
_CARACTERES_INVISIVEIS = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff\u00a0]')


def limpar_url_imagem(valor):
    """Valida e limpa uma URL de imagem salva em config (campos 'aviso' e
    'aviso_banner'). Único ponto de verdade — TODO lugar que for usar essas
    URLs (embed clássico, Components V2, painel de configuração, prévia)
    deve chamar esta função em vez de repetir a checagem na mão.

    Remove caracteres invisíveis de copiar-colar, tira espaços nas bordas e
    aceita http/https em qualquer capitalização. Retorna a URL limpa, ou
    None se não for uma URL válida (e loga o motivo, pra nunca mais falhar
    em silêncio)."""
    if not valor:
        return None
    limpo = _CARACTERES_INVISIVEIS.sub('', str(valor)).strip()
    if not limpo:
        return None
    if not limpo.lower().startswith(('http://', 'https://')):
        print(f"[IMAGEM] URL de imagem ignorada (nao comeca com http/https apos limpeza): {limpo!r}")
        return None
    return limpo

_COR_PADRAO = 0xFFFF00  # amarelo, mesmo default usado em todo o bot


def parse_cor_embed(valor, padrao: int = _COR_PADRAO) -> int:
    """Converte o texto salvo em 'cor_embed' (campo do /configurar) num inteiro
    de cor pro discord.Embed. Único ponto de verdade — TODO lugar que exibe
    um embed colorido deve chamar esta função em vez de repetir o parse na mão.

    ANTES: cada cog tinha seu próprio `int(cor_hex.replace('0x', ''), 16)` (ou
    variações). Isso só aceitava exatamente o formato "0xFF0000" — qualquer
    outra forma que o usuário digitasse no modal (com "#", sem prefixo, com
    espaço, minúsculo/maiúsculo misturado, colado do celular com caractere
    invisível) estourava ValueError e caía SEM AVISO no amarelo padrão. Por
    isso a cor "não ia": não existia erro nenhum, o valor simplesmente nunca
    era salvo/lido do jeito certo.

    Aceita: "0xFF0000", "#FF0000", "FF0000", "ff0000", " FF0000 ", e o atalho
    de 3 dígitos "F00" (vira "FF0000"). Retorna `padrao` se o valor for vazio
    ou não puder ser interpretado como cor."""
    if not valor:
        return padrao
    limpo = _CARACTERES_INVISIVEIS.sub('', str(valor)).strip()
    limpo = limpo.replace('#', '').replace('0x', '').replace('0X', '').strip()
    if not limpo:
        return padrao
    if len(limpo) == 3 and all(c in '0123456789abcdefABCDEF' for c in limpo):
        limpo = ''.join(c * 2 for c in limpo)
    try:
        cor = int(limpo, 16)
    except ValueError:
        return padrao
    if not (0 <= cor <= 0xFFFFFF):
        return padrao
    return cor


def formatar_cor_embed(cor: int) -> str:
    """Formata um inteiro de cor de volta pro formato salvo em config
    ('0xRRGGBB'), pra normalizar o que fica gravado no banco assim que o
    usuário confirma uma cor válida no modal do /configurar."""
    return f"0x{cor:06X}"


_conn: aiosqlite.Connection | None = None
_conn_lock = asyncio.Lock()

# Conexão SEPARADA, só-leitura, dedicada exclusivamente ao check de licença
# (obter_assinatura_fresca). MOTIVO: essa função roda em TODO comando
# protegido (60+ comandos) + todo clique de botão via checar_licenca_view(),
# e é a ÚNICA leitura quente do bot que não pode usar cache (precisa sempre
# bater no banco de verdade, ver docstring dela). Se ela usasse a MESMA
# conexão principal (_conn), ficaria enfileirada atrás de toda escrita do
# automod (anti-spam/anti-link rodando em CADA mensagem de TODOS os
# servidores) -- em picos de tráfego isso empurra o check de licença pra
# perto (ou além) do limite de 3s que o Discord dá pra reconhecer uma
# interação, e o Discord mostra a falha nativa dele SEM passar pelo nosso
# error handler (por isso não sobrava log nenhum). Com uma segunda conexão
# em modo WAL, essa leitura roda em paralelo de verdade com a conexão de
# escrita, sem fila.
_conn_licenca: aiosqlite.Connection | None = None
_conn_licenca_lock = asyncio.Lock()

# Locks por guild só pra trechos que fazem "ler um contador -> incrementar
# -> gravar" em mais de uma instrução SQL (ex: criar_caso_moderacao). O
# aiosqlite serializa as instruções na mesma conexão, mas NÃO torna essas
# sequências de várias instruções atômicas entre si — duas coroutines podem
# intercalar entre elas. Sem o lock, dois casos de moderação criados quase
# ao mesmo tempo (ex: anti-raid banindo vários membros do mesmo pico de
# entrada) podem ler o mesmo "próximo número" e gerar Caso #N duplicado.
_locks_guild: dict[int, asyncio.Lock] = {}


def _lock_guild(guild_id: int) -> asyncio.Lock:
    lock = _locks_guild.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks_guild[guild_id] = lock
    return lock


# FIX (perfil dessincronizado do ranking): add_vitoria/add_vitoria_wo faziam
# INSERT em partidas_finalizadas + 2 INSERTs em rankings + commit SEM
# nenhum lock. Como a conexão é única pro bot inteiro e não usa BEGIN
# explícito, se QUALQUER outra coroutine (outro comando, outra partida
# sendo finalizada, cassino, etc.) desse um commit() no meio dessa
# sequência, ela commitava o INSERT em partidas_finalizadas sozinho -- e se
# a sequência de add_vitoria fosse interrompida logo depois (exceção,
# concorrência), o rankings nunca recebia o UPDATE. Resultado: usuário
# aparece no /ranking (lê de partidas_finalizadas) mas o /perfil (lê de
# rankings) mostra 0. Esse lock global serializa toda a sequência
# partida->rankings, garantindo que ela sempre commita (ou nunca commita)
# como uma unidade só, mesmo com várias partidas finalizando ao mesmo tempo
# em guilds diferentes.
_lock_vitoria = asyncio.Lock()


# Cache em memória pra configs lidas em todo evento de mensagem/entrada
# (automod do convites.py roda em CADA mensagem de CADA servidor; moderação
# consulta a config em todo warn/mute/etc). Sem cache, isso é 1 SELECT no
# SQLite por mensagem — na conexão única, isso serializa o processamento de
# mensagens de TODOS os servidores atrás desses SELECTs. Invalidado sempre
# que a config correspondente é atualizada, então nunca fica desatualizado.
_cache_convites_config: dict[int, dict] = {}
_cache_moderacao_config: dict[int, dict] = {}
# Mesma lógica de cache acima, aplicada aos dois pontos mais quentes do
# bot: obter_config() é chamada em ~76 lugares diferentes (cor do embed,
# canais, emojis...) e obter_assinatura() roda em TODO comando protegido
# por @requer_licenca() (60 comandos) + todo clique de botão que passa
# por checar_licenca_view(). Config e licença mudam raríssimo perto do
# volume de leituras, então cachear em memória e invalidar só quando
# alguém realmente escreve evita 1 SELECT no SQLite por interação.
_cache_config: dict[int, dict] = {}
_cache_assinaturas: dict[int, dict | None] = {}


async def get_conn() -> aiosqlite.Connection:
    """Conexão única e compartilhada com o SQLite (correto pro SQLite —
    não é caso de usar pool). O lock evita que duas chamadas concorrentes
    (ex: dois eventos do Discord processados "ao mesmo tempo" no startup)
    criem duas conexões e uma delas vaze sem ser fechada."""
    global _conn
    if _conn is None:
        async with _conn_lock:
            if _conn is None:  # outra corrotina pode ter criado enquanto esperávamos o lock
                conn = await aiosqlite.connect(DB_PATH, timeout=30)
                # WAL: leitores não bloqueiam escritor e vice-versa
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA foreign_keys=ON")
                # NORMAL é seguro com WAL ligado (só perde durabilidade em
                # caso de crash do SO, não de crash do processo) e evita
                # fsync a cada commit — ganho grande de velocidade em escrita
                await conn.execute("PRAGMA synchronous=NORMAL")
                # Timeout do aiosqlite.connect() nem sempre cobre todo lock
                # interno; o PRAGMA garante o mesmo comportamento em ms
                await conn.execute("PRAGMA busy_timeout=30000")
                # Cache maior (~16MB) e tabelas temporárias em RAM em vez
                # de disco — ambos gratuitos pro tamanho desse banco
                await conn.execute("PRAGMA cache_size=-16000")
                await conn.execute("PRAGMA temp_store=MEMORY")
                # Row factory setado UMA VEZ aqui pra toda a vida da conexão.
                # aiosqlite.Row ainda suporta acesso por índice (row[0]),
                # então nenhum código existente quebra — mas agora dá pra
                # acessar por nome de coluna (row['campo']) em qualquer
                # lugar sem precisar repetir "conn.row_factory = aiosqlite.Row"
                # em cada função que chama get_conn().
                conn.row_factory = aiosqlite.Row
                _conn = conn
    return _conn


async def _get_conn_licenca() -> aiosqlite.Connection:
    """Segunda conexão com o MESMO arquivo de banco, dedicada só ao check
    de licença. Ver comentário em cima de `_conn_licenca` pro motivo. Só
    faz leitura (SELECT), então PRAGMA query_only=ON como blindagem extra
    -- se algum dia alguém acidentalmente tentar escrever usando essa
    conexão por engano, falha na hora em vez de silenciosamente competir
    com a conexão principal."""
    global _conn_licenca
    if _conn_licenca is None:
        async with _conn_licenca_lock:
            if _conn_licenca is None:
                conn = await aiosqlite.connect(DB_PATH, timeout=30)
                await conn.execute("PRAGMA journal_mode=WAL")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("PRAGMA query_only=ON")
                conn.row_factory = aiosqlite.Row
                _conn_licenca = conn
    return _conn_licenca


async def fechar_conn():
    global _conn, _conn_licenca
    if _conn is not None:
        await _conn.close()
        _conn = None
    if _conn_licenca is not None:
        await _conn_licenca.close()
        _conn_licenca = None


async def checkpoint_wal():
    """Força o SQLite a mesclar tudo que tá pendente no ffz_esports.db-wal
    de volta pro arquivo principal ffz_esports.db.

    FIX BUG REAL (backups sempre "vazios", ~4KB): em modo WAL, escritas
    recentes (ativar licença, configs, tudo) ficam só no arquivo -wal até
    o SQLite decidir sozinho fazer um checkpoint (só acontece depois de
    bastante volume de escrita acumulado). O backup horário
    (painel_owner._enviar_backup) manda o ffz_esports.db cru como anexo
    -- sem chamar isso antes, ele sempre pegava a foto do ÚLTIMO
    checkpoint automático, nunca o estado atual de verdade. Resultado:
    todo backup saía do mesmo tamanho pequeno, sem os dados recentes --
    e se um restart algum dia precisasse restaurar por esse backup (ver
    bot.restaurar_backup_do_discord), voltava o banco pra um estado de
    horas/dias atrás, "desativando" licenças que na verdade estavam
    ativas.

    TRUNCATE (em vez de PASSIVE, que é o padrão implícito e pode não
    conseguir mesclar tudo se tiver leitor com transação aberta) força o
    checkpoint mais agressivo disponível e zera o -wal depois -- garante
    que o .db sozinho, sem os arquivos auxiliares, já é uma foto completa
    e válida do banco no momento do backup.
    """
    db = await get_conn()
    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def otimizar_query_planner():
    """PRAGMA optimize -- atualiza as estatísticas internas que o SQLite
    usa pra decidir entre usar um índice ou fazer table scan. Diferente
    de ANALYZE puro (que sempre reescaneia tudo) ou VACUUM (que reescreve
    o arquivo inteiro e trava escritas), esse PRAGMA é leve por design:
    só mexe nas tabelas que mudaram o suficiente desde a última vez pra
    valer a pena, e a doc oficial do SQLite recomenda rodar isso
    periodicamente em bancos de longa duração (não é algo que só faz
    sentido 1x). Chamado no loop de 10min do backup automático."""
    db = await get_conn()
    await db.execute("PRAGMA optimize")


async def get_meta(chave: str) -> str | None:
    """Lê um valor da tabela bot_meta (key-value global e persistente)."""
    db = await get_conn()
    async with db.execute("SELECT valor FROM bot_meta WHERE chave = ?", (chave,)) as cursor:
        linha = await cursor.fetchone()
        return linha[0] if linha else None


async def set_meta(chave: str, valor: str):
    """Grava/atualiza um valor na tabela bot_meta."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO bot_meta (chave, valor) VALUES (?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
        (chave, valor)
    )
    await db.commit()


async def setup_db():
    db = await get_conn()

    # META (key-value simples e global). Usado por ex. pra guardar o hash
    # do último sync de slash commands (bot.py -> on_ready), já que o
    # filesystem NÃO persiste entre deploys no Discloud -- só o banco
    # sobrevive, via o sistema de backup por DM.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_meta (
            chave TEXT PRIMARY KEY, valor TEXT
        )
    """)

    # CONFIGURAÇÕES
    await db.execute("""
        CREATE TABLE IF NOT EXISTS configuracoes (
            guild_id INTEGER PRIMARY KEY, cargo_admin INTEGER, cargo_mediador INTEGER, cargo_streamer INTEGER,
            emoji_entrar TEXT DEFAULT '✅', emoji_sair TEXT DEFAULT '❌', emoji_fechar TEXT DEFAULT '🔒',
            cor_embed TEXT DEFAULT '0xFFFF00', taxa REAL DEFAULT 0.20, simbolo_moeda TEXT DEFAULT 'R$', aviso TEXT,
            canal_fila_med INTEGER, canal_pix_med INTEGER, categoria_tickets INTEGER, canal_invite_log INTEGER,
            canal_painel_med INTEGER, msg_painel_med INTEGER, canal_painel_pix INTEGER, msg_painel_pix INTEGER,
            canal_threads INTEGER, canal_logs INTEGER, canal_ranking INTEGER, thumbnail_url TEXT,
            emoji_mobile TEXT DEFAULT NULL, emoji_emulador TEXT DEFAULT NULL, emoji_misto TEXT DEFAULT NULL,
            emoji_tatico TEXT DEFAULT NULL, emoji_fullsoco TEXT DEFAULT NULL, emoji_girl TEXT DEFAULT NULL,
            emoji_fullsemtela TEXT DEFAULT NULL,
            emoji_valor TEXT DEFAULT NULL, emoji_players TEXT DEFAULT NULL, emoji_botao_entrar TEXT DEFAULT '✅',
            emoji_botao_sair TEXT DEFAULT '❌', cargo_extra1 INTEGER, cargo_extra2 INTEGER,
            texto_fila_aguardando TEXT DEFAULT 'aguardando{numero}', texto_fila_confirmada TEXT DEFAULT 'fila{numero}',
            texto_fila_sala TEXT DEFAULT '🏆 • Prêmio {valor}'
        )
    """)

    # FILAS
    await db.execute("""
        CREATE TABLE IF NOT EXISTS filas (
            message_id INTEGER PRIMARY KEY, guild_id INTEGER, canal_id INTEGER, modo TEXT, valor REAL,
            jogador1 INTEGER, jogador2 INTEGER, mediador_id INTEGER, subfilas TEXT DEFAULT '{}'
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS botoes_fila (
            guild_id INTEGER, modo TEXT, botoes TEXT, PRIMARY KEY (guild_id, modo)
        )
    """)
    # A PK de `filas` é message_id (cada fila é 1 mensagem) -- mas todo
    # /listarfilas, .filasativas, painel_central e a checagem de quantas
    # filas o servidor já tem filtram por guild_id sozinho, e sem índice
    # próprio isso é table scan. Hoje passa despercebido (poucas filas
    # abertas por vez), mas é SaaS multi-org: sem esse índice, cada guild
    # nova que entra faz TODA guild pagar o custo de escanear a tabela
    # inteira nessas telas.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_filas_guild
        ON filas (guild_id)
    """)
    # Associação canal -> tipo/modo (detectado pelo nome na 1ª vez, depois
    # sempre lido/atualizado por canal_id, sem precisar reler o nome).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS canais_fila_config (
            canal_id INTEGER PRIMARY KEY, guild_id INTEGER, tipo TEXT, modo TEXT
        )
    """)

    # PREÇOS CONFIGURADOS -- a lista de valores que o admin monta no painel
    # de criar fila (via "Adicionar Preço" ou "Vários de Uma Vez") é
    # persistida aqui. Antes ela só existia na memória da sessão do painel
    # (config_atual['precos']), então: 1) fechava o painel e a lista sumia,
    # tinha que digitar tudo de novo; 2) o resumo do /configurar mostrava os
    # valores da tabela `filas` (filas já POSTADAS) em vez do que o admin
    # realmente configurou -- os dois nunca foram a mesma coisa.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS precos_configurados (
            guild_id INTEGER, valor REAL, PRIMARY KEY (guild_id, valor)
        )
    """)

    # APOSTAS + PIX + MED
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tabela_apostas (
            guild_id INTEGER, valor REAL, paga REAL, premio REAL, PRIMARY KEY (guild_id, valor)
        )
    """)
    # FIX: faltava essa tabela -- cogs/diagnostico.py (.apostas) lê dela pra
    # montar os contadores diários (Criadas/Iniciadas/Salas Criadas), mas
    # como ela nunca era criada, todo .apostas caía em
    # "OperationalError: no such table: apostas_eventos".
    await db.execute("""
        CREATE TABLE IF NOT EXISTS apostas_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            tipo TEXT,
            med_id INTEGER,
            valor REAL,
            modo TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_apostas_eventos_guild_tipo ON apostas_eventos (guild_id, tipo, data)"
    )
    # Registro de FALHA ao gravar um evento em apostas_eventos (INSERT que
    # caiu no except em cogs/fila.py ou cogs/painelmediador.py). Essas
    # gravações são "blindadas" de propósito -- não podem travar a
    # criação de fila/partida -- mas isso também significava que uma
    # falha silenciosa nunca aparecia em lugar nenhum, e os números do
    # .apostas ficavam levemente errados sem ninguém perceber. Agora cada
    # falha vira uma linha aqui, e o Diagnóstico Técnico do .apostas
    # mostra a contagem dos últimos 7 dias.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eventos_falhos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            tipo TEXT,
            erro TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_eventos_falhos_guild_data ON eventos_falhos (guild_id, data)"
    )
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pix (
            guild_id INTEGER,
            user_id INTEGER,
            chave_pix TEXT,
            nome TEXT,
            qr_code TEXT,
            banco TEXT,
            cidade TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    # Auditoria do PIX: cada criação/atualização/remoção fica registrada,
    # com quem executou a ação. Importante numa área que envolve dinheiro
    # de verdade — em caso de disputa, dá pra saber exatamente o que
    # mudou, quando e por quem.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pix_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            acao TEXT,
            nome TEXT,
            chave_pix TEXT,
            banco TEXT,
            cidade TEXT,
            qr_code TEXT,
            executado_por INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_pix_historico_lookup
        ON pix_historico (guild_id, user_id, criado_em)
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fila_mediadores (
            guild_id INTEGER, user_id INTEGER, posicao INTEGER, PRIMARY KEY (guild_id, user_id)
        )
    """)

    # BLACKLIST — lista de IDs (jogadores) banidos do servidor. id_alvo fica
    # como TEXT pra aceitar tanto ID numérico do Discord quanto outros
    # formatos que o dono do servidor queira registrar (ex: ID de outro
    # jogo/plataforma), sem perder o valor por overflow de INTEGER.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            guild_id INTEGER,
            id_alvo TEXT,
            motivo TEXT,
            adicionado_por INTEGER,
            adicionado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, id_alvo)
        )
    """)

    # ========================================================================
    # MODERAÇÃO — warns, mutes, kicks, bans com case ID e log unificado
    # ========================================================================
    await db.execute("""
        CREATE TABLE IF NOT EXISTS moderacao_config (
            guild_id INTEGER PRIMARY KEY,
            canal_log INTEGER,
            warns_limite INTEGER DEFAULT 3,
            warns_acao TEXT DEFAULT 'mute',
            warns_acao_duracao INTEGER DEFAULT 3600
        )
    """)
    # Contador separado por servidor pra gerar o "Caso #N" sequencial
    # (começando em 1) sem depender do id global do autoincrement, que
    # ficaria com buracos/números gigantes assim que tiver mais de 1 guild.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS moderacao_contador (
            guild_id INTEGER PRIMARY KEY,
            proximo INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS moderacao_casos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            numero INTEGER,
            tipo TEXT,
            user_id INTEGER,
            moderador_id INTEGER,
            motivo TEXT,
            duracao_segundos INTEGER,
            expira_em TIMESTAMP,
            ativo INTEGER DEFAULT 1,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_moderacao_casos_usuario
        ON moderacao_casos (guild_id, user_id, ativo)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_moderacao_casos_numero
        ON moderacao_casos (guild_id, numero)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_moderacao_casos_expira
        ON moderacao_casos (tipo, ativo, expira_em)
    """)

    # PARTIDAS EM ANDAMENTO
    await db.execute("""
        CREATE TABLE IF NOT EXISTS partidas (
            thread_id INTEGER PRIMARY KEY, guild_id INTEGER, cap1_id INTEGER, cap2_id INTEGER,
            med_id INTEGER, valor REAL, confirmado1 INTEGER DEFAULT 0, confirmado2 INTEGER DEFAULT 0,
            cancelado INTEGER DEFAULT 0, vencedor INTEGER, finalizado INTEGER DEFAULT 0
        )
    """)
    # Mesmo caso de `filas`: painel_central e .filasativas fazem
    # `WHERE guild_id=? AND finalizado=0 AND cancelado=0` repetidamente
    # -- só tinha índice automático no thread_id (PK), que não serve pra
    # filtrar por guild_id. Tabela pequena hoje (só partidas em
    # andamento), mas mesmo raciocínio do SaaS multi-org acima.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_partidas_guild_status
        ON partidas (guild_id, finalizado, cancelado)
    """)

    # Contador do "número da fila/partida" por servidor -- usado só pra dar
    # nome ao tópico (aguardando1, fila1, ...) enquanto a partida ainda
    # está em andamento. Sobe 1 por partida criada, nunca reseta sozinho.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS numeracao_partidas (
            guild_id INTEGER PRIMARY KEY,
            ultimo INTEGER DEFAULT 0
        )
    """)

    # ========================================================================
    # LOJA DIGITAL (produto + estoque + entrega automática) — sistema novo,
    # independente da "Loja" de coins do cassino.py (aquela é cosmético
    # comprado com moeda interna; essa aqui é produto de verdade — conta,
    # key, código — entregue de verdade pro cliente). Ver cogs/loja_produtos.py.
    # ========================================================================
    await db.execute("""
        CREATE TABLE IF NOT EXISTS produtos_digitais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            nome TEXT NOT NULL,
            descricao TEXT,
            preco REAL DEFAULT 0,
            emoji TEXT,
            ativo INTEGER DEFAULT 1,
            criado_por INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_produtos_guild
        ON produtos_digitais (guild_id, ativo)
    """)

    # Cada LINHA aqui é uma unidade entregável (uma conta, uma key, um
    # código...). "entregue=0" = ainda no estoque, disponível pra vender;
    # "entregue=1" = já foi mandado pra alguém, fica guardado como
    # histórico (pra rastrear "qual conta foi pro fulano" se der suporte).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS produtos_estoque (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id INTEGER NOT NULL,
            conteudo TEXT NOT NULL,
            entregue INTEGER DEFAULT 0,
            entregue_para INTEGER,
            entregue_em TIMESTAMP,
            adicionado_por INTEGER,
            adicionado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (produto_id) REFERENCES produtos_digitais(id)
        )
    """)
    # A query mais comum é "me dá 1 item disponível desse produto" --
    # WHERE produto_id=? AND entregue=0 LIMIT 1. Sem esse índice ela varre
    # a tabela inteira toda vez que alguém compra.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_estoque_disponivel
        ON produtos_estoque (produto_id, entregue)
    """)

    # Log de entregas -- separado do estoque (que pode ser limpo/reorganizado)
    # pra manter um histórico permanente de "quem comprou o quê e quando",
    # útil pra suporte e pra métricas de vendas.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS produtos_entregas_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            produto_id INTEGER NOT NULL,
            estoque_id INTEGER,
            usuario_id INTEGER NOT NULL,
            nome_produto TEXT,
            entregue_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_entregas_guild
        ON produtos_entregas_log (guild_id, entregue_em)
    """)

    # RANKING
    await db.execute("""
        CREATE TABLE IF NOT EXISTS rankings (
            user_id INTEGER, guild_id INTEGER, vitorias INTEGER DEFAULT 0, derrotas INTEGER DEFAULT 0,
            wins_wo INTEGER DEFAULT 0, losses_wo INTEGER DEFAULT 0,
            sequencia INTEGER DEFAULT 0, maior_sequencia INTEGER DEFAULT 0, ultima_vitoria TIMESTAMP,
            maior_premio REAL DEFAULT 0, coins_gastas REAL DEFAULT 0, PRIMARY KEY (user_id, guild_id)
        )
    """)
    # A PK é (user_id, guild_id) -- nessa ordem, o índice automático da PK
    # não serve pra buscas que filtram só por guild_id (ranking geral,
    # /perfil), que é o caso mais comum. Sem esse índice extra, essas
    # queries faziam table scan completo.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_rankings_guild
        ON rankings (guild_id)
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS partidas_finalizadas (
            thread_id INTEGER PRIMARY KEY, guild_id INTEGER, vencedor_id INTEGER, perdedor_id INTEGER,
            valor REAL, data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # BUG RAIZ do ranking "errado até rodar /recalcularranking": a PK era
    # thread_id sozinho, assumindo 1 thread = 1 partida. Só que a Revanche
    # (RV, ver ModalAlterarValor em painelmediador.py) REAPROVEITA o mesmo
    # thread_id pra segunda partida -- na hora de gravar a 2ª vitória,
    # add_vitoria tentava inserir de novo com o mesmo thread_id e o SQLite
    # recusava (UNIQUE constraint), derrubando a transação inteira ANTES de
    # tocar em `rankings`. Resultado: a vitória da revanche nunca era
    # gravada em lugar nenhum -- nem /recalcularranking resolvia, porque ele
    # reconstrói a partir dessa própria tabela (a vitória nem chegava aqui).
    # Mesma técnica de migração já usada em clientes_registrados acima:
    # SQLite não deixa trocar a PRIMARY KEY com ALTER TABLE, então recria a
    # tabela com `id` autoincrementado (thread_id vira coluna normal,
    # indexada) só quando detecta o formato antigo.
    cursor = await db.execute("PRAGMA table_info(partidas_finalizadas)")
    _colunas_partidas_fin = [row[1] for row in await cursor.fetchall()]
    if _colunas_partidas_fin and "id" not in _colunas_partidas_fin:
        await db.execute("ALTER TABLE partidas_finalizadas RENAME TO partidas_finalizadas_old_migracao")
        await db.execute("""
            CREATE TABLE partidas_finalizadas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id INTEGER, guild_id INTEGER, vencedor_id INTEGER, perdedor_id INTEGER,
                valor REAL, data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO partidas_finalizadas (thread_id, guild_id, vencedor_id, perdedor_id, valor, data)
            SELECT thread_id, guild_id, vencedor_id, perdedor_id, valor, data FROM partidas_finalizadas_old_migracao
        """)
        await db.execute("DROP TABLE partidas_finalizadas_old_migracao")
        await db.commit()
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_partidas_finalizadas_thread
        ON partidas_finalizadas (thread_id)
    """)
    # Só tinha índice no thread_id (PK). Todo /ranking e o check de MVP
    # semanal filtram por guild_id (+ intervalo de data) e agrupam por
    # vencedor_id/perdedor_id -- sem índice, isso é table scan completo,
    # e piora conforme o histórico de partidas cresce.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_partidas_finalizadas_guild_data
        ON partidas_finalizadas (guild_id, data)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_partidas_finalizadas_guild_vencedor
        ON partidas_finalizadas (guild_id, vencedor_id)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_partidas_finalizadas_guild_perdedor
        ON partidas_finalizadas (guild_id, perdedor_id)
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS coins (
            user_id INTEGER, guild_id INTEGER, saldo INTEGER DEFAULT 0, PRIMARY KEY (user_id, guild_id)
        )
    """)

    # ========== RANKING DE MEDIADORES ==========
    # 1 linha por partida finalizada (win ou w.o) que passou pelo mediador.
    # 'lucro' já vem calculado e CONGELADO no momento do registro (taxa
    # cobrada de cada jogador x2), pra não mudar depois se a taxa do
    # /configurar for alterada. Sem isso não dava pra confiar no histórico,
    # já que a taxa é modificável a qualquer momento.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mediador_partidas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            thread_id INTEGER,
            med_id INTEGER,
            valor_aposta REAL,
            taxa_unitaria REAL,
            lucro REAL,
            tipo TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_mediador_partidas_lookup
        ON mediador_partidas (guild_id, med_id, data)
    """)

    # Cancelamentos ("Encerrar Aposta"): antes a partida era só deletada,
    # sem deixar rastro nenhum. Agora grava 1 linha aqui ANTES de deletar,
    # só pra alimentar a "taxa de cancelamento" do dashboard.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mediador_cancelamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            thread_id INTEGER,
            med_id INTEGER,
            valor_aposta REAL,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Mesma família de tabela que mediador_partidas (linha por evento do
    # mediador), mas essa aqui tinha ficado sem índice -- contagem de
    # cancelamentos por mediador era table scan.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_mediador_cancelamentos_lookup
        ON mediador_cancelamentos (guild_id, med_id)
    """)

    # Histórico de multas: o cargo_multa é aplicado/removido manualmente
    # pelo ADM direto no Discord (não existe comando pra isso), então um
    # listener de on_member_update detecta a troca do cargo e grava aqui.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS mediador_multas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            evento TEXT,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Colunas do fluxo completo do /painelmulta (aplicada por painel, com
    # motivo/valor/quem multou/pra quem pagar, ciclo de pagamento com
    # confirmação do ADM). As linhas antigas (só evento 'aplicada'/'removida'
    # gravadas pelo listener manual) continuam existindo com essas colunas
    # em NULL — não quebra o histórico anterior.
    for sql in [
        "ALTER TABLE mediador_multas ADD COLUMN motivo TEXT",
        "ALTER TABLE mediador_multas ADD COLUMN valor REAL",
        "ALTER TABLE mediador_multas ADD COLUMN admin_id INTEGER",
        "ALTER TABLE mediador_multas ADD COLUMN chave_pagamento TEXT",
        # status: 'ativa' -> 'aguardando_confirmacao' -> 'paga'
        # (multa removida na mão pelo botão "Remover Multa" fica com
        # evento='removida' e status permanece o que estava, só sai do
        # cargo — não é um status próprio pra não confundir com pagamento).
        "ALTER TABLE mediador_multas ADD COLUMN status TEXT DEFAULT 'ativa'",
        "ALTER TABLE mediador_multas ADD COLUMN pago_em TIMESTAMP",
        "ALTER TABLE mediador_multas ADD COLUMN confirmado_por INTEGER",
    ]:
        try:
            await db.execute(sql)
        except Exception:
            pass
    # Idem acima: histórico de multas por usuário (checagem de multa ativa
    # + listagem paginada), sem índice era table scan.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_mediador_multas_lookup
        ON mediador_multas (guild_id, user_id, data)
    """)

    # Log unificado de ações do mediador (Win / W.O / Encerrar) num lugar
    # só. Antes cada tipo de ação ficava espalhado (mediador_partidas pra
    # Win/WO, mediador_cancelamentos pro Encerrar), o que dificultava
    # puxar "tudo que o mediador X fez" numa query só. Essa tabela não
    # substitui as outras (que continuam alimentando lucro/taxa de
    # cancelamento) -- ela é só o histórico cronológico unificado, pro
    # /logs_mediador e /exportar_logs.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS log_acoes_mediador (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            thread_id INTEGER,
            thread_nome TEXT,
            med_id INTEGER,
            tipo TEXT,
            jogador1_id INTEGER,
            jogador2_id INTEGER,
            vencedor_id INTEGER,
            valor REAL,
            duracao_segundos INTEGER,
            data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Link direto pro transcript da partida (mensagem com o .html enviada
    # no canal_logs por gerar_e_enviar_transcript, em painelmediador.py) --
    # sem isso, o .logs @user (cogs/logs_membro.py) linkava pra THREAD que
    # já tinha sido deletada (Win/W.O/Encerrar sempre apagam a thread
    # depois de logar), um link sempre morto. Linhas antigas (de antes
    # dessa coluna existir) ficam com transcript_url NULL -- o .logs cai
    # pro texto sem link nesse caso, não quebra o histórico anterior.
    try:
        await db.execute("ALTER TABLE log_acoes_mediador ADD COLUMN transcript_url TEXT")
    except Exception:
        pass
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_log_acoes_mediador_med
        ON log_acoes_mediador (guild_id, med_id, data)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_log_acoes_mediador_tipo
        ON log_acoes_mediador (guild_id, tipo, data)
    """)

    # Personalização do painel /dashboard_mediador (título, descrição, cor
    # e label+emoji de cada botão), editável pelo /personalizar_dashboard_mediador.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS dashboard_med_config (
            guild_id INTEGER PRIMARY KEY,
            titulo TEXT DEFAULT '📊 Dashboard de Mediadores',
            descricao TEXT DEFAULT 'Acompanhe as estatísticas completas dos mediadores.',
            cor TEXT DEFAULT '0099FF',
            rodape TEXT DEFAULT '',
            label_perfil TEXT DEFAULT 'Meu Perfil', emoji_perfil TEXT DEFAULT '👤',
            label_atividade TEXT DEFAULT 'Atividade', emoji_atividade TEXT DEFAULT '📈',
            label_emblemas TEXT DEFAULT 'Emblemas', emoji_emblemas TEXT DEFAULT '🏅',
            label_comparativo TEXT DEFAULT 'Comparativo', emoji_comparativo TEXT DEFAULT '⚖️'
        )
    """)

    # ========== LOJA (100% customizável pelo /configurar, nada mais fixo no código) ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS loja_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            nome TEXT,
            emoji TEXT DEFAULT '🛒',
            descricao TEXT,
            preco INTEGER,
            tipo TEXT,              -- 'cargo' | 'buff' | 'saque'
            cargo_nome TEXT,        -- usado se tipo = cargo
            cargo_cor TEXT,         -- hex opcional, usado se tipo = cargo
            buff_chave TEXT,        -- usado se tipo = buff (ex: 'ap_gratis')
            valor_pix REAL,         -- usado se tipo = saque
            ativo INTEGER DEFAULT 1,
            ordem INTEGER DEFAULT 0
        )
    """)
    # Visual da loja (título/descrição/banner), separado dos itens — mesmo
    # padrão de roleta_config/caixa_config, só que faltava pra loja (por
    # isso "titulo_loja" nunca funcionava de verdade antes).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS loja_config (
            guild_id INTEGER PRIMARY KEY,
            titulo TEXT DEFAULT '🛒 LOJA',
            descricao TEXT DEFAULT '🪙 Ganhe coins jogando\n🛍️ Gaste em cargos e buffs\n💸 Ou troque por PIX',
            banner TEXT DEFAULT '',
            cor TEXT DEFAULT '5865F2'
        )
    """)

    # ========== ROLETA (itens sorteáveis, com peso/probabilidade configurável) ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS roleta_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            nome TEXT,
            emoji TEXT DEFAULT '🎁',
            tipo TEXT,              -- 'cargo' | 'buff' | 'coins' | 'nada'
            cargo_nome TEXT,
            cargo_cor TEXT,
            buff_chave TEXT,
            coins_qtd INTEGER,      -- usado se tipo = coins
            peso INTEGER DEFAULT 10,  -- quanto maior, mais chance de sair
            estoque INTEGER DEFAULT NULL,  -- NULL = infinito. Ex: 1 = só pode sair 1 vez no total, igual a caixa
            ativo INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS roleta_config (
            guild_id INTEGER PRIMARY KEY,
            custo_giro INTEGER DEFAULT 10,
            titulo TEXT DEFAULT '🎰 Roleta',
            emoji_girar TEXT DEFAULT '🎰',
            cor1 TEXT DEFAULT 'B01E1E',
            cor2 TEXT DEFAULT '781212',
            descricao TEXT DEFAULT 'Gire a roleta e concorra a prêmios!',
            banner TEXT DEFAULT ''
        )
    """)

    # ========== CAIXA PREMIADA (mystery box — mesma lógica da roleta, com estoque opcional) ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS caixa_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            nome TEXT,
            emoji TEXT DEFAULT '🎁',
            tipo TEXT,              -- 'cargo' | 'buff' | 'coins' | 'saque' | 'nada'
            cargo_nome TEXT,
            cargo_cor TEXT,
            buff_chave TEXT,
            coins_qtd INTEGER,      -- usado se tipo = coins
            valor_pix REAL,         -- usado se tipo = saque
            peso INTEGER DEFAULT 10,   -- quanto maior, mais chance de sair
            estoque INTEGER DEFAULT NULL,  -- NULL = infinito. Ex: 1 = só pode ser ganho 1 vez no total
            ativo INTEGER DEFAULT 1
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS caixa_config (
            guild_id INTEGER PRIMARY KEY,
            custo_abrir INTEGER DEFAULT 10,
            titulo TEXT DEFAULT '🎁 Caixa Premiada',
            emoji_abrir TEXT DEFAULT '🎁',
            descricao TEXT DEFAULT 'Tente sua sorte! Clique no botão abaixo pra abrir sua caixa e ver o que ganha!',
            cor TEXT DEFAULT 'FFD700',
            banner TEXT DEFAULT ''
        )
    """)

    # ========== BUFFS (efeitos temporários que o jogador compra/ganha, tipo 'ap_gratis') ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS buffs (
            guild_id INTEGER,
            user_id INTEGER,
            chave TEXT,
            quantidade INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, chave)
        )
    """)

    # ========== SISTEMA DE LICENÇA (KEYS) ==========
    # chaves: cada key gerada pelo dono. dias fica gravado aqui, mas o "vence"
    # só é calculado na ATIVAÇÃO (não na criação) — assim uma key não perde
    # validade parada no estoque esperando ser vendida.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS chaves (
            chave TEXT PRIMARY KEY,
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            criada_por INTEGER,
            usado_por INTEGER,
            usado_em TIMESTAMP,
            vence TIMESTAMP,
            cancelada INTEGER DEFAULT 0
        )
    """)
    # assinaturas: 1 linha por servidor. SEM linha = bloqueado (sem free tier).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS assinaturas (
            guild_id INTEGER PRIMARY KEY,
            plano TEXT,
            ativo INTEGER DEFAULT 1,
            vence TIMESTAMP,
            chave TEXT,
            ativado_por INTEGER,
            ativado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ========== SISTEMA DE RENOVAÇÃO (PAGAMENTO AUTOMÁTICO VIA MERCADO PAGO) ==========
    # planos_precos: preço em R$ pra cada combinação (plano, dias) que
    # aparece no menu de renovação. Editável em runtime via /configurarprecos
    # -- sem precisar redeploy pra mudar valor.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS planos_precos (
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            preco REAL NOT NULL,
            PRIMARY KEY (plano, dias)
        )
    """)
    # pagamentos_pix: 1 linha por cobrança Pix criada no Mercado Pago.
    # id = payment_id retornado pela API do MP (é com esse ID que o
    # webhook identifica qual pagamento foi aprovado, e é por ele que
    # sabemos pra qual guild/quantos dias creditar).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pagamentos_pix (
            id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            user_id INTEGER,
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            valor REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            canal_id INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processado_em TIMESTAMP
        )
    """)
    # pagamentos_manuais: renovação via Pix MANUAL (chave Pix do próprio
    # dono, sem API nenhuma) -- diferente de pagamentos_pix (Mercado
    # Pago), aqui não existe payment_id de terceiro pra confiar; o id é
    # gerado pelo próprio SQLite e fica 'pendente' até o dono aprovar ou
    # recusar manualmente (ver cogs/renovacao.py -> AprovacaoManualView).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pagamentos_manuais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            valor REAL NOT NULL,
            status TEXT DEFAULT 'pendente',
            canal_id INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processado_em TIMESTAMP,
            processado_por INTEGER
        )
    """)

    # clientes_registrados: vincula comprador(es) (user_id) ao(s)
    # servidor(es) dele (guild_id) -- cadastrado manualmente pelo
    # dono/suporte via .shop depois de confirmar a venda. É esse vínculo
    # que deixa o Painel de Cliente (cogs/painel_licenca.py) saber de qual
    # servidor mostrar a assinatura quando o cliente clica "Consultar
    # Plano"/"Renovar Plano" no servidor de SUPORTE, em vez de sempre
    # olhar pro interaction.guild_id (que ali é o servidor de suporte,
    # não o dele).
    #
    # 1 usuário pode ter VÁRIOS servidores vinculados ao mesmo tempo (ex:
    # cliente com múltiplas licenças) -- por isso a chave é composta
    # (user_id, guild_id), não mais só user_id. Cadastrar de novo pro
    # MESMO par (user_id, guild_id) atualiza esse vínculo; cadastrar pra
    # um guild_id diferente ADICIONA um vínculo novo, sem apagar o
    # anterior.
    #
    # MIGRAÇÃO: versões antigas dessa tabela tinham `user_id` como
    # PRIMARY KEY sozinho (1 servidor por cliente só). SQLite não deixa
    # trocar a PRIMARY KEY com ALTER TABLE, então recria a tabela no
    # formato novo preservando os dados existentes quando detecta o
    # formato antigo (ausência da coluna `id`).
    cursor = await db.execute("PRAGMA table_info(clientes_registrados)")
    _colunas_clientes = [row[1] for row in await cursor.fetchall()]
    if _colunas_clientes and "id" not in _colunas_clientes:
        await db.execute("ALTER TABLE clientes_registrados RENAME TO clientes_registrados_old_migracao")
        await db.execute("""
            CREATE TABLE clientes_registrados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_nome TEXT,
                plano TEXT,
                cadastrado_por INTEGER,
                cadastrado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, guild_id)
            )
        """)
        await db.execute("""
            INSERT INTO clientes_registrados
                (user_id, guild_id, guild_nome, plano, cadastrado_por, cadastrado_em)
            SELECT user_id, guild_id, guild_nome, plano, cadastrado_por, cadastrado_em
            FROM clientes_registrados_old_migracao
        """)
        await db.execute("DROP TABLE clientes_registrados_old_migracao")
    else:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clientes_registrados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                guild_nome TEXT,
                plano TEXT,
                cadastrado_por INTEGER,
                cadastrado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, guild_id)
            )
        """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_clientes_registrados_user
        ON clientes_registrados (user_id)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_clientes_registrados_guild
        ON clientes_registrados (guild_id)
    """)


    # bot_owners: lista dinâmica de donos (substitui o antigo ID fixo no código).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_owners (
            user_id INTEGER PRIMARY KEY,
            adicionado_por INTEGER,
            adicionado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # owner_senha: senha persistente do painel (hash + salt, nunca texto puro). 1 linha só.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS owner_senha (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            senha_hash TEXT NOT NULL,
            senha_salt TEXT NOT NULL,
            definida_por INTEGER,
            definida_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # owner_codigos_reset: código de 6 dígitos pra recuperação de senha (/esqueciasenha).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS owner_codigos_reset (
            codigo TEXT PRIMARY KEY,
            expira_em TIMESTAMP NOT NULL,
            criado_por INTEGER,
            usado INTEGER DEFAULT 0,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Personalização visual do painel de ranking (título/descrição/cor/emojis)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ranking_config (
            guild_id INTEGER PRIMARY KEY,
            titulo TEXT,
            descricao TEXT,
            rodape TEXT,
            cor INTEGER,
            emoji_ouro TEXT,
            emoji_prata TEXT,
            emoji_bronze TEXT
        )
    """)

    # ========== SS (SOLICITAR ANÁLISE) — integrado do Bot de solicitar SS ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ss_config (
            guild_id INTEGER PRIMARY KEY,
            org_name TEXT DEFAULT 'FFZ E-SPORTS',
            embed_color INTEGER DEFAULT 3447003,
            thumbnail_url TEXT DEFAULT '',
            emoji_analise TEXT DEFAULT '🔍',
            emoji_limpo TEXT DEFAULT '✅',
            emoji_wo TEXT DEFAULT '❌',
            canal_solicitacoes INTEGER,
            cargo_ss_mobile INTEGER,
            cargo_ss_emu INTEGER,
            cargo_adm INTEGER,
            painel_titulo TEXT DEFAULT '🏆 Ranking de Análises SS',
            painel_descricao TEXT,
            painel_rodape TEXT DEFAULT 'FFZ E-SPORTS SYSTEM'
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ss_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            analista_id INTEGER,
            solicitante_id INTEGER,
            jogador_id INTEGER,
            modalidade TEXT,
            resultado TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decidido_em TIMESTAMP
        )
    """)
    # Ranking de analistas (/ss_ranking, stats individuais) filtra por
    # guild_id (+ analista_id) e essa tabela ganha 1 linha por solicitação
    # de análise -- mesmo padrão do partidas_finalizadas, sem índice era
    # table scan completo e só piora com o tempo.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_logs_guild_analista
        ON ss_logs (guild_id, analista_id)
    """)
    # Fila de analistas SS (mesmo modelo da fila_mediadores): quem clica
    # "Entrar" no painel de fila fica na ordem, e o /solicitar puxa o
    # primeiro da fila da MODALIDADE pedida (mobile e emulador têm filas
    # separadas, já que um analista pode ter só um dos dois cargos).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fila_ss (
            guild_id INTEGER, user_id INTEGER, modalidade TEXT, posicao INTEGER,
            PRIMARY KEY (guild_id, user_id, modalidade)
        )
    """)

    # DEDUPE PERSISTENTE (cross-processo): dedupe.py já evitava duplicidade
    # em memória (mesma instância, evento reentregue pelo Discord), mas
    # duas INSTÂNCIAS do bot rodando ao mesmo tempo (mesmo token, ex: um
    # restart do Discloud que não matou o processo antigo a tempo) têm
    # cada uma seu próprio dicionário vazio -- as duas processam o mesmo
    # evento e a ação duplica de verdade (ex: .wo/.ssmob/.ssemu mandando
    # msg 2x). Essa tabela usa a PRIMARY KEY como trava atômica
    # compartilhada entre processos: quem inserir primeiro "ganha", o
    # segundo toma erro de chave duplicada e sabe que já foi processado.
    # Genérica, não amarrada só ao sistema SS -- qualquer cog pode usar
    # via database.evento_ja_processado().
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eventos_processados (
            evento_id INTEGER PRIMARY KEY,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Registro de W.O com provas (motivo + fotos/vídeos), pra consulta via
    # .wo (mediador/analista, obrigatório) e pelo botão W.O do painel de
    # análise (analista, opcional). log_id é nullable pois nem todo W.O vem
    # de uma análise em andamento (o .wo do mediador é avulso).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ss_wo_registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            alvo_id INTEGER,
            registrado_por_id INTEGER,
            origem TEXT,
            log_id INTEGER,
            motivo TEXT,
            provas TEXT DEFAULT '[]',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ss_wo_guild_alvo
        ON ss_wo_registros (guild_id, alvo_id)
    """)

    # ========== Telador (SOLICITAR ANÁLISE) — integrado do Bot de solicitar Telador ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tel_config (
            guild_id INTEGER PRIMARY KEY,
            org_name TEXT DEFAULT 'FFZ E-SPORTS',
            embed_color INTEGER DEFAULT 3447003,
            thumbnail_url TEXT DEFAULT '',
            emoji_analise TEXT DEFAULT '🔍',
            emoji_limpo TEXT DEFAULT '✅',
            emoji_wo TEXT DEFAULT '❌',
            canal_solicitacoes INTEGER,
            cargo_tel_mobile INTEGER,
            cargo_tel_emu INTEGER,
            cargo_adm INTEGER,
            painel_titulo TEXT DEFAULT '🏆 Ranking de Análises Telador',
            painel_descricao TEXT,
            painel_rodape TEXT DEFAULT 'FFZ E-SPORTS SYSTEM'
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tel_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            analista_id INTEGER,
            solicitante_id INTEGER,
            jogador_id INTEGER,
            modalidade TEXT,
            resultado TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decidido_em TIMESTAMP
        )
    """)
    # Ranking de analistas (/tel_ranking, stats individuais) filtra por
    # guild_id (+ analista_id) e essa tabela ganha 1 linha por solicitação
    # de análise -- mesmo padrão do partidas_finalizadas, sem índice era
    # table scan completo e só piora com o tempo.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tel_logs_guild_telador
        ON tel_logs (guild_id, analista_id)
    """)
    # Fila de analistas Telador (mesmo modelo da fila_mediadores): quem clica
    # "Entrar" no painel de fila fica na ordem, e o /solicitar puxa o
    # primeiro da fila da MODALIDADE pedida (mobile e emulador têm filas
    # separadas, já que um analista pode ter só um dos dois cargos).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fila_tel (
            guild_id INTEGER, user_id INTEGER, modalidade TEXT, posicao INTEGER,
            PRIMARY KEY (guild_id, user_id, modalidade)
        )
    """)

    # (dedupe cross-processo pro .telwo/.telmob/.telemu reaproveita a
    # tabela genérica `eventos_processados`, já criada mais acima junto
    # com o sistema SS -- ver database.evento_ja_processado().)

    # Registro de W.O com provas (motivo + fotos/vídeos), pra consulta via
    # .wo (mediador/analista, obrigatório) e pelo botão W.O do painel de
    # análise (analista, opcional). log_id é nullable pois nem todo W.O vem
    # de uma análise em andamento (o .wo do mediador é avulso).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tel_wo_registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            alvo_id INTEGER,
            registrado_por_id INTEGER,
            origem TEXT,
            log_id INTEGER,
            motivo TEXT,
            provas TEXT DEFAULT '[]',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tel_wo_guild_alvo
        ON tel_wo_registros (guild_id, alvo_id)
    """)

    # ========== ANÚNCIOS — integrado do Bot de anúncios ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS anuncios (
            guild_id INTEGER,
            nome TEXT,
            canal_id INTEGER,
            tipo TEXT DEFAULT 'embed',
            cor TEXT DEFAULT '2b2d31',
            titulo TEXT,
            descricao TEXT,
            imagem_url TEXT,
            tempo_renovacao INTEGER,
            msg_id INTEGER,
            ativo INTEGER DEFAULT 1,
            proxima_renovacao TIMESTAMP,
            ordem INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, nome)
        )
    """)

    # Multi-canal: um mesmo anúncio agora pode ser postado em VÁRIOS canais
    # ao mesmo tempo (antes era só 1 canal_id fixo na tabela anuncios acima).
    # Cada linha aqui é "esse anúncio, nesse canal, com essa mensagem postada".
    # A coluna canal_id da tabela anuncios acima fica só de legado/histórico
    # (não é mais lida nem escrita pelo código novo) — quem manda é essa tabela.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS anuncios_canais (
            guild_id INTEGER,
            nome TEXT,
            canal_id INTEGER,
            msg_id INTEGER,
            msg_id_mencao INTEGER,
            PRIMARY KEY (guild_id, nome, canal_id)
        )
    """)

    # ========== CONVITES (INVITES) — integrado do Bot de invites ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_config (
            guild_id INTEGER PRIMARY KEY,
            join_channel_id INTEGER, leave_channel_id INTEGER, log_channel_id INTEGER,
            join_title TEXT DEFAULT '🔵 NOVO RECRUTA NA ÁREA',
            join_body TEXT DEFAULT '👤 **Membro**\n{member}\n`{username}`\n\n🎯 **Recrutado por**\n{inviter}\n\n📊 **Total de convites**\n`{total}`',
            join_color TEXT DEFAULT '5865F2', join_banner TEXT DEFAULT '',
            leave_title TEXT DEFAULT '😔 RECRUTA ABANDONOU O POSTO',
            leave_body TEXT DEFAULT '👤 **Membro**\n`{username}`\n\n🎯 **Foi recrutado por**\n{inviter}',
            leave_color TEXT DEFAULT 'e74c3c', leave_banner TEXT DEFAULT '',
            log_title TEXT DEFAULT '📋 LOG DE CONVITES', log_color TEXT DEFAULT '5865F2',
            emoji_join TEXT DEFAULT '🔵', emoji_leave TEXT DEFAULT '😔', emoji_inviter TEXT DEFAULT '🎯',
            emoji_stats TEXT DEFAULT '📊', emoji_member TEXT DEFAULT '👤',
            footer_text TEXT DEFAULT 'FFZ E-SPORTS | {count} membros',
            msgs_formato_embed INTEGER DEFAULT 1,
            antilink_enabled INTEGER DEFAULT 0, antilink_msg TEXT DEFAULT '🚫 {member}, links não são permitidos aqui!',
            antilink_log_channel_id INTEGER,
            bad_words_enabled INTEGER DEFAULT 0, bad_words_list TEXT DEFAULT '',
            bad_words_msg TEXT DEFAULT '⚠️ {member}, esse tipo de linguagem não é permitida!',
            antiraid_enabled INTEGER DEFAULT 0, antiraid_joins INTEGER DEFAULT 5,
            antiraid_seconds INTEGER DEFAULT 10, antiraid_action TEXT DEFAULT 'kick',
            antiraid_log_channel_id INTEGER, mod_log_channel_id INTEGER,
            antifake_enabled INTEGER DEFAULT 0, antifake_horas INTEGER DEFAULT 24
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_data (
            guild_id INTEGER, user_id INTEGER, inviter_id INTEGER,
            invite_code TEXT DEFAULT '', joined_at TEXT DEFAULT '', fake INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_auto_roles (
            guild_id INTEGER, role_id INTEGER, PRIMARY KEY (guild_id, role_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_sorteios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
            titulo TEXT, descricao TEXT, premio TEXT, emoji TEXT DEFAULT '🎉', cor TEXT DEFAULT '5865F2',
            banner TEXT DEFAULT '', vencedores INTEGER DEFAULT 1, encerrado INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, message_id INTEGER,
            titulo TEXT, descricao TEXT, data_evento TEXT, local TEXT, emoji TEXT DEFAULT '📅', encerrado INTEGER DEFAULT 0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_ranking_live (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER
        )
    """)
    # Participantes de sorteio via botão (substitui o antigo sistema de reação)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_sorteio_participantes (
            sorteio_id INTEGER, user_id INTEGER, entrou_em TEXT DEFAULT '',
            PRIMARY KEY (sorteio_id, user_id)
        )
    """)
    # Confirmação de presença (RSVP) de eventos via botão
    await db.execute("""
        CREATE TABLE IF NOT EXISTS convites_evento_rsvp (
            evento_id INTEGER, user_id INTEGER, status TEXT DEFAULT 'sim',
            PRIMARY KEY (evento_id, user_id)
        )
    """)

    # ========== TICKETS — integrado do Bot de ticket (sem a parte de loja) ==========
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_paineis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            nome TEXT,
            titulo TEXT DEFAULT '🎫 Central de Atendimento',
            descricao TEXT DEFAULT 'Selecione uma categoria abaixo para abrir um ticket.',
            cor INTEGER DEFAULT 5793266,
            banner_url TEXT DEFAULT '',
            footer_text TEXT DEFAULT '',
            estilo_painel TEXT DEFAULT 'classico',
            canal_envio_id INTEGER, message_id INTEGER,
            canal_logs_id INTEGER, canal_transcricoes_id INTEGER, categoria_discord_id INTEGER,
            cargo_suporte_id INTEGER, cargo_suporte_id_2 INTEGER,
            mensagem_abertura TEXT DEFAULT '{ffz_diamante} Bem-vindo ao canal oficial de atendimento do servidor **{servidor}**

{ffz_setabaixo} Categoria selecionada: `{categoria}`

{ffz_escudo} Todos os responsáveis já estão cientes do seu chamado. Descreva com o máximo de detalhes possível o motivo do contato para agilizar o atendimento.

{ffz_alerta} Evite chamar alguém via DM, apenas aguarde que a equipe irá te atender.',
            notificar_dm INTEGER DEFAULT 1,
            max_tickets_usuario INTEGER DEFAULT 1
        )
    """)
    # NOVO: cargos de suporte deixaram de ser limitados a 2 (cargo_suporte_id
    # / cargo_suporte_id_2, mantidos só por compatibilidade). Agora cada
    # painel pode ter quantos cargos quiser cadastrados aqui — o único
    # limite é o do próprio componente de seleção do Discord (25 por vez).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_painel_cargos_suporte (
            painel_id INTEGER,
            cargo_id INTEGER,
            PRIMARY KEY (painel_id, cargo_id)
        )
    """)
    # Migração: qualquer painel que já tinha cargo_suporte_id/_2 preenchido
    # (de antes dessa tabela existir) tem esses cargos copiados pra cá.
    # INSERT OR IGNORE + roda toda vez no startup, então é seguro repetir.
    await db.execute("""
        INSERT OR IGNORE INTO ticket_painel_cargos_suporte (painel_id, cargo_id)
        SELECT id, cargo_suporte_id FROM ticket_paineis WHERE cargo_suporte_id IS NOT NULL
    """)
    await db.execute("""
        INSERT OR IGNORE INTO ticket_painel_cargos_suporte (painel_id, cargo_id)
        SELECT id, cargo_suporte_id_2 FROM ticket_paineis WHERE cargo_suporte_id_2 IS NOT NULL
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            painel_id INTEGER,
            nome TEXT,
            descricao TEXT,
            emoji TEXT DEFAULT '🎫'
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            thread_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            numero INTEGER,
            usuario_id INTEGER,
            painel_id INTEGER,
            categoria TEXT,
            assumido_por INTEGER,
            transcript_url TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            fechado_em TIMESTAMP,
            fechado_por INTEGER
        )
    """)
    # Só tinha índice no thread_id (PK). Histórico de tickets por usuário
    # (/meustickets), busca por número, média de avaliação por painel e as
    # duas tasks em loop (SLA e auto-close, que filtram tickets abertos)
    # todas faziam table scan completo -- e a tabela só cresce com o tempo.
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_guild_usuario
        ON tickets (guild_id, usuario_id, criado_em)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_guild_numero
        ON tickets (guild_id, numero)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_painel_usuario
        ON tickets (painel_id, usuario_id)
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_tickets_abertos
        ON tickets (fechado_em)
        WHERE fechado_em IS NULL
    """)
    # Ranking de suporte (/ranking_suporte): 1 linha por staff por servidor,
    # contando quantos tickets assumiu, quantos fechou, e quantos fechou
    # SEM ter assumido antes (indicador de qualidade do atendimento).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_staff_stats (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            assumidos INTEGER DEFAULT 0,
            fechados INTEGER DEFAULT 0,
            fechados_sem_assumir INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS cargos_absolutos (
            guild_id INTEGER NOT NULL,
            cargo_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, cargo_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_logs_acoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER, painel_id INTEGER, thread_id INTEGER, numero INTEGER,
            acao TEXT, autor_id INTEGER, alvo_id INTEGER, detalhes TEXT,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ticket_contador (
            guild_id INTEGER PRIMARY KEY, ultimo_numero INTEGER DEFAULT 0
        )
    """)

    try:
        await db.execute("ALTER TABLE convites_data ADD COLUMN saiu INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_data ADD COLUMN saiu_em TEXT")
    except Exception:
        pass
    # --- anti-raid mais robusto: quarentena, timeout, gate de idade e lockdown ---
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_quarentena_role_id INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_timeout_minutos INTEGER DEFAULT 60")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_idade_mode TEXT DEFAULT 'nunca'")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_idade_dias INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_lockdown_minutos INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN antiraid_alerta_role_id INTEGER")
    except Exception:
        pass

    # --- competição periódica de convites: reset automático + anúncio do vencedor ---
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN competicao_enabled INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN competicao_frequencia TEXT DEFAULT 'mensal'")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN competicao_canal_id INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_config ADD COLUMN competicao_periodo_inicio TEXT")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN termina_em TIMESTAMP")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN criado_por INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_eventos ADD COLUMN criado_por INTEGER")
    except Exception:
        pass
    # --- reforma do sorteio: reroll, cargo de anúncio e requisitos pra participar ---
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN vencedores_atuais TEXT")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN cargo_anuncio INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN cargo_necessario INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN conta_min_dias INTEGER")
    except Exception:
        pass
    # --- expectativa do sorteio: flags pra não repetir os avisos de "tá acabando" ---
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN aviso_10min INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_sorteios ADD COLUMN aviso_1min INTEGER DEFAULT 0")
    except Exception:
        pass
    # --- reforma do evento: data/hora estruturada pra lembrete e encerramento automático ---
    try:
        await db.execute("ALTER TABLE convites_eventos ADD COLUMN data_hora TIMESTAMP")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_eventos ADD COLUMN lembrete_enviado INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE convites_eventos ADD COLUMN lembrete_minutos INTEGER DEFAULT 60")
    except Exception:
        pass

    try:
        await db.execute("ALTER TABLE ticket_paineis ADD COLUMN estilo_painel TEXT DEFAULT 'classico'")
    except Exception:
        pass
    # NOVO: cargo específico por categoria — quando definido, é pingado
    # NO LUGAR dos cargos gerais de suporte do painel (cargo_suporte_id/_2).
    # Se ficar vazio, continua caindo no comportamento antigo (cargos do painel).
    try:
        await db.execute("ALTER TABLE ticket_categorias ADD COLUMN cargo_ping_id INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE ticket_categorias ADD COLUMN cargo_ping_id_2 INTEGER")
    except Exception:
        pass
    # NOVO: canal de destino específico por categoria — quando definido, o
    # tópico do ticket é criado NESSE canal em vez do canal onde o painel
    # foi enviado. Se ficar vazio, continua caindo no canal do painel (comportamento antigo).
    try:
        await db.execute("ALTER TABLE ticket_categorias ADD COLUMN canal_destino_id INTEGER")
    except Exception:
        pass
    # NOVO: marca quando a categoria veio de um template padrão (ver
    # categorias_padrao.py). Fica NULL pra categoria criada na mão (modal
    # "➕ Add Categoria"). Além de evitar repetir o mesmo preset duas vezes
    # no mesmo painel, alimenta a detecção automática de canal por nome.
    try:
        await db.execute("ALTER TABLE ticket_categorias ADD COLUMN slug TEXT")
    except Exception:
        pass
    # NOVO: canal público opcional pra onde vai um card de feedback toda vez
    # que um cliente avalia o atendimento (nota + comentário). emoji_feedback
    # permite usar um emoji customizado do servidor no lugar da ⭐ padrão.
    try:
        await db.execute("ALTER TABLE ticket_paineis ADD COLUMN canal_feedback_id INTEGER")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE ticket_paineis ADD COLUMN emoji_feedback TEXT DEFAULT '⭐'")
    except Exception:
        pass

    # NOVO: painéis criados ANTES dessa atualização já têm 'mensagem_abertura'
    # gravada com algum texto anterior (não é NULL, então o DEFAULT da coluna
    # não se aplica retroativamente). Aqui a gente troca quem ainda está
    # EXATAMENTE em qualquer uma das versões antigas de fábrica pelo modelo
    # final (fixo, com emoji unicode normal, sem depender de categoria
    # cadastrada nem de emoji de aplicação) — quem já editou a própria
    # mensagem não é tocado, o WHERE só bate nos textos de fábrica antigos,
    # ipsis litteris.
    _MENSAGEM_ABERTURA_ANTIGA = (
        "Olá, {usuario}.\n\n"
        "Seu atendimento foi registrado com sucesso sob o número **#{numero}**, "
        "na categoria **{categoria}**.\n\n"
        "Nossa equipe já foi notificada e dará início ao suporte em breve. "
        "Para agilizar o processo, descreva com o máximo de detalhes possível "
        "o motivo do seu contato."
    )
    # Versão intermediária 1: emoji unicode fixo + descrição por categoria.
    _MENSAGEM_ABERTURA_V2 = (
        "💎 Bem-vindo ao canal oficial de atendimento do servidor **{servidor}**\n\n"
        "↳ Categoria selecionada: `{categoria}`\n"
        "📌 {descricao_categoria}\n\n"
        "🛡️ Todos os responsáveis já estão cientes do seu chamado. Descreva com "
        "o máximo de detalhes possível o motivo do contato para agilizar o atendimento.\n\n"
        "⚠️ Evite chamar alguém via DM, apenas aguarde que a equipe irá te atender."
    )
    # Versão intermediária 2: placeholder de emoji de aplicação + descrição por categoria.
    _MENSAGEM_ABERTURA_V3 = (
        "{ffz_diamante} Bem-vindo ao canal oficial de atendimento do servidor **{servidor}**\n\n"
        "{ffz_setabaixo} Categoria selecionada: `{categoria}`\n"
        "📌 {descricao_categoria}\n\n"
        "{ffz_escudo} Todos os responsáveis já estão cientes do seu chamado. Descreva com "
        "o máximo de detalhes possível o motivo do contato para agilizar o atendimento.\n\n"
        "{ffz_alerta} Evite chamar alguém via DM, apenas aguarde que a equipe irá te atender."
    )
    # Versão final: fixa, emoji unicode normal, sem depender de descrição de
    # categoria nem de emoji de aplicação cadastrado.
    _MENSAGEM_ABERTURA_V4_UNICODE_SEM_CATEGORIA = (
        "💎 Bem-vindo ao canal oficial de atendimento do servidor **{servidor}**\n\n"
        "↳ Categoria selecionada: `{categoria}`\n\n"
        "🛡️ Todos os responsáveis já estão cientes do seu chamado. Descreva com "
        "o máximo de detalhes possível o motivo do contato para agilizar o atendimento.\n\n"
        "⚠️ Evite chamar alguém via DM, apenas aguarde que a equipe irá te atender."
    )
    # Versão final de verdade: mesmo texto, mas puxando os emojis
    # customizados que você cadastrar no Developer Portal (com fallback pro
    # unicode normal se ainda não tiver cadastrado, ver emojis_app.obter).
    _MENSAGEM_ABERTURA_FINAL = MENSAGEM_ABERTURA_PADRAO
    # Versão "card resumido": bem mais curta que as outras, sem os avisos
    # completos -- só confirmava a abertura e a categoria.
    _MENSAGEM_ABERTURA_CARD_RESUMIDO = (
        "Olá {usuario}, seu ticket #{numero} foi criado na categoria "
        "**{categoria}**. Aguarde o suporte."
    )
    for _versao_antiga in (
        _MENSAGEM_ABERTURA_ANTIGA, _MENSAGEM_ABERTURA_V2, _MENSAGEM_ABERTURA_V3,
        _MENSAGEM_ABERTURA_V4_UNICODE_SEM_CATEGORIA, _MENSAGEM_ABERTURA_CARD_RESUMIDO
    ):
        try:
            await db.execute(
                "UPDATE ticket_paineis SET mensagem_abertura = ? WHERE mensagem_abertura = ?",
                (_MENSAGEM_ABERTURA_FINAL, _versao_antiga)
            )
        except Exception:
            pass

    # ========== BATER PONTO ==========
    # registros: cada linha é UMA sessão de ponto (aberta ou fechada), ou um
    # PONTO: agora suporta MÚLTIPLOS painéis independentes por servidor.
    # ponto_paineis = 1 linha por painel, cada um com config própria (cargo,
    # logs, metas, canal/mensagem publicada, status ativo/pausado).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ponto_paineis (
            painel_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            titulo TEXT DEFAULT 'Bate Ponto',
            canal_id INTEGER,
            msg_painel_id INTEGER,
            cargo_ponto INTEGER,
            meta_diaria_minutos INTEGER DEFAULT 0,
            meta_semanal_minutos INTEGER DEFAULT 0,
            canal_log_inicio INTEGER,
            canal_log_concluido INTEGER,
            canal_log_fechado_admin INTEGER,
            canal_log_gerencia INTEGER,
            status TEXT DEFAULT 'ativo',
            criado_por INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # status da SESSÃO: 'aberto' | 'fechado' | 'fechado_admin' | 'cancelado'
    # | 'ajuste'. Agora vinculada a um painel_id específico (não só ao
    # servidor), pra cada painel contar as horas separadamente.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ponto_registros (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            painel_id INTEGER,
            guild_id INTEGER,
            user_id INTEGER,
            inicio TIMESTAMP,
            fim TIMESTAMP,
            duracao_segundos INTEGER,
            status TEXT DEFAULT 'aberto',
            fechado_por INTEGER,
            motivo TEXT
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ponto_painel_user_status
        ON ponto_registros (painel_id, user_id, status)
    """)
    # Tabela antiga (config única por servidor) mantida só pra permitir a
    # migração automática pro modelo multi-painel; o app não lê mais direto
    # daqui depois de migrado (ver _migrar_ponto_paineis).
    # Painel Central unificado (.apostas): guarda em qual canal/mensagem
    # cada servidor tem o painel "ao vivo" (auto-atualização) + qual
    # período do "Resumo das Filas" (geral/hoje/ontem) está selecionado
    # ali agora. Precisa existir ANTES do loop de ALTER TABLE logo abaixo
    # (que adiciona a coluna `periodo` em quem já tinha essa tabela de uma
    # versão anterior) -- por isso mora aqui no init central, e não mais
    # dentro do setup() de um cog específico.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS central_stats_live (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            message_id INTEGER,
            intervalo_minutos INTEGER DEFAULT 5,
            periodo TEXT DEFAULT 'geral'
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS ponto_config (
            guild_id INTEGER PRIMARY KEY,
            canal_ponto INTEGER,
            msg_painel_ponto INTEGER,
            cargo_ponto INTEGER,
            meta_diaria_minutos INTEGER DEFAULT 0,
            meta_semanal_minutos INTEGER DEFAULT 0,
            canal_log_inicio INTEGER,
            canal_log_concluido INTEGER,
            canal_log_fechado_admin INTEGER,
            canal_log_gerencia INTEGER
        )
    """)

    # Migrações seguras (ADD COLUMN ignora se já existir)
    for sql in [
        "ALTER TABLE rankings ADD COLUMN wins_wo INTEGER DEFAULT 0",
        "ALTER TABLE rankings ADD COLUMN losses_wo INTEGER DEFAULT 0",
        "ALTER TABLE partidas ADD COLUMN vencedor INTEGER",
        "ALTER TABLE partidas ADD COLUMN finalizado INTEGER DEFAULT 0",
        "ALTER TABLE configuracoes ADD COLUMN cargo_extra1 INTEGER",
        "ALTER TABLE configuracoes ADD COLUMN cargo_extra2 INTEGER",
        # Cores dos cards do painel .apostas (Confirmadas/Canceladas/Salas
        # Criadas/Finalizadas) -- antes fixas no código (verde/vermelho/
        # amarelo), agora editáveis pelo botão "🎨 Cores dos Cards" direto
        # no painel .apostas, no mesmo padrão de cor_embed (guarda '0xRRGGBB').
        "ALTER TABLE configuracoes ADD COLUMN cor_card_confirmadas TEXT DEFAULT '0x2ECC71'",
        "ALTER TABLE configuracoes ADD COLUMN cor_card_canceladas TEXT DEFAULT '0xE74C3C'",
        "ALTER TABLE configuracoes ADD COLUMN cor_card_salas TEXT DEFAULT '0xF1C40F'",
        "ALTER TABLE configuracoes ADD COLUMN cor_card_finalizadas TEXT DEFAULT '0x3498DB'",
        # Toggles pra ligar/desligar pelo /configurar as regras que antes
        # eram fixas no código: mediador não pode jogar, e conta com menos
        # de 7 dias não pode jogar. Default 1 (ativado) pra manter o
        # comportamento atual em servidores que já estão rodando.
        "ALTER TABLE configuracoes ADD COLUMN bloquear_mediador_jogar INTEGER DEFAULT 1",
        "ALTER TABLE configuracoes ADD COLUMN bloquear_conta_nova INTEGER DEFAULT 1",
        # Cargo de multa: ADM com esse cargo não pode entrar na fila de
        # mediadores até pagar/remover a multa. Configurável pelo /configurar.
        "ALTER TABLE configuracoes ADD COLUMN cargo_multa INTEGER",
        # Cargos que podem mexer nas AÇÕES do painel de multa (Add Multa,
        # Remover Multa, Confirmar Pagamento, .bloquear, .desbloquear) --
        # configurados em /painelmulta -> "Cargos do Painel". Colunas
        # faltando aqui fazia o UPDATE de cada seleção falhar (sqlite3.
        # OperationalError: no such column: cargo_multa_staff1) e a
        # escolha nunca era salva de verdade -- por isso o painel nunca
        # obedecia os cargos selecionados e continuava liberado pra
        # qualquer Admin (fallback de eh_staff_multa_membro).
        "ALTER TABLE configuracoes ADD COLUMN cargo_multa_staff1 INTEGER",
        "ALTER TABLE configuracoes ADD COLUMN cargo_multa_staff2 INTEGER",
        # Substitui o limite fixo de 2 cargos (staff1/staff2) por uma
        # LISTA (até 25, o máximo que um único RoleSelect do Discord
        # aceita de uma vez). Guarda os IDs separados por vírgula.
        "ALTER TABLE configuracoes ADD COLUMN cargo_multa_staff_ids TEXT",
        # Canal onde o painel /painelmulta fica fixado (a mensagem é
        # editada in-place a cada personalização, igual outros painéis).
        "ALTER TABLE configuracoes ADD COLUMN canal_painel_multa INTEGER",
        "ALTER TABLE configuracoes ADD COLUMN msg_painel_multa INTEGER",
        # Canal de logs dedicado do sistema de multa: cada ação (aplicada,
        # aguardando confirmação de pagamento, confirmada, removida na mão)
        # gera um embed aqui. É também onde o botão "Confirmar Pagamento"
        # aparece pro ADM — por isso o próprio ADM deve restringir a
        # visibilidade desse canal no Discord (mesmo padrão do canal_logs_pix,
        # o bot não mexe em permissão de canal sozinho).
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_multa INTEGER",
        # Textos personalizáveis do painel /painelmulta (título/descrição),
        # pra ficar no mesmo padrão de personalização dos outros painéis.
        "ALTER TABLE configuracoes ADD COLUMN painel_multa_titulo TEXT",
        "ALTER TABLE configuracoes ADD COLUMN painel_multa_desc TEXT",
        # Canal PÚBLICO de avisos de multa (diferente do canal_logs_multa,
        # que é só pra ADM ver) -- toda multa aplicada manda um card visual
        # aqui, pra transparência geral.
        "ALTER TABLE configuracoes ADD COLUMN canal_avisos_multa INTEGER",
        # Emoji do título do card público de aviso de multa -- escolhido
        # por select em /painelmulta (ver EMOJIS_AVISO_MULTA em
        # painel_multa.py). Guarda o emoji cru (unicode ou <:nome:id>).
        "ALTER TABLE configuracoes ADD COLUMN emoji_aviso_multa TEXT",
        # Guarda o modo/formato da fila (ex: "3x3 Mobile") direto na partida,
        # já que depois que a thread é criada não dá mais pra saber de qual
        # fila ela veio. Usado pelo painel de sala lançada (vem do chat).
        "ALTER TABLE partidas ADD COLUMN modo TEXT",
        # Congela a taxa (por jogador) vigente no momento em que o
        # pagamento da partida foi liberado. Usada depois pra calcular o
        # lucro do mediador com o valor REAL cobrado naquela partida, não
        # a taxa atual do /configurar (que pode já ter mudado).
        "ALTER TABLE partidas ADD COLUMN taxa_cobrada REAL",
        # Data de criação da partida — usada pra calcular o "tempo médio de
        # mediação" (do momento que a thread foi criada até finalizada).
        # Partidas antigas (de antes dessa coluna existir) ficam com o
        # timestamp de quando essa migração rodou, então entram como NULL
        # na prática pro cálculo de tempo médio (ver stats_completo_mediador).
        "ALTER TABLE partidas ADD COLUMN criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Duração da mediação dessa partida específica, em segundos —
        # gravada junto no INSERT de mediador_partidas (registrar_partida_mediada).
        "ALTER TABLE mediador_partidas ADD COLUMN duracao_segundos INTEGER",
        # Evita mandar o aviso de "licença vencendo em 3 dias" repetido
        # todo dia — só avisa uma vez por vencimento.
        "ALTER TABLE assinaturas ADD COLUMN avisado_vencimento INTEGER DEFAULT 0",
        # Personalização do embed "PARTIDA CRIADA" (SaaS: cada servidor
        # customiza título, emojis e o nome que aparece no lugar de
        # "Capitães" — ex: "Players", "Duelistas", etc — pelo /configurar.
        "ALTER TABLE configuracoes ADD COLUMN titulo_partida TEXT DEFAULT 'Partida Criada'",
        "ALTER TABLE configuracoes ADD COLUMN emoji_titulo_partida TEXT DEFAULT '⚔️'",
        "ALTER TABLE configuracoes ADD COLUMN emoji_capitaes TEXT DEFAULT '👑'",
        "ALTER TABLE configuracoes ADD COLUMN label_capitaes TEXT DEFAULT 'Players'",
        "ALTER TABLE configuracoes ADD COLUMN emoji_mediador TEXT DEFAULT '🕴️'",
        "ALTER TABLE configuracoes ADD COLUMN emoji_financeiro TEXT DEFAULT '💸'",
        # Emojis dos campos "Modo" e "Valor" do card de partida — antes
        # eram fixos no código (🏆 e 💰), agora personalizáveis por
        # servidor igual aos outros emojis do card (Players/Mediador).
        "ALTER TABLE configuracoes ADD COLUMN emoji_modo_partida TEXT DEFAULT '🏆'",
        "ALTER TABLE configuracoes ADD COLUMN emoji_valor_partida TEXT DEFAULT '💰'",
        # Separa banco e cidade da chave Pix, que antes vinham tudo
        # concatenado numa string só (ex: "email | Nubank | Cidade"),
        # poluindo a chave mostrada pros players na hora de pagar.
        "ALTER TABLE pix ADD COLUMN banco TEXT",
        "ALTER TABLE pix ADD COLUMN cidade TEXT",
        # Banner grande (embed.set_image) separado do thumbnail (embed.set_thumbnail,
        # que já existe na coluna 'aviso') — dá pra ter os dois ao mesmo tempo.
        "ALTER TABLE configuracoes ADD COLUMN aviso_banner TEXT DEFAULT ''",
        # Quantos coins o vencedor ganha por vitória (usado no botão "Dar
        # Win" / "Win por W.O" do painel do mediador). Antes o embed dizia
        # "+1 Coin" mas não existia NENHUM código creditando esse coin de
        # verdade — era só texto, sem ação por trás.
        "ALTER TABLE configuracoes ADD COLUMN coins_por_vitoria INTEGER DEFAULT 1",
        # Emojis personalizados dos formatos Tático e Full Soco — antes só
        # Mobile/Emulador/Misto tinham coluna própria, então esses dois
        # sempre caíam no emoji fixo do código, sem opção de trocar.
        "ALTER TABLE configuracoes ADD COLUMN emoji_tatico TEXT DEFAULT NULL",
        "ALTER TABLE configuracoes ADD COLUMN emoji_fullsoco TEXT DEFAULT NULL",
        # Personalização do painel de ranking de convites ao vivo (paginado,
        # com botão de atualizar), configurado pelo /painelconvites.
        "ALTER TABLE convites_config ADD COLUMN rank_title TEXT DEFAULT '🏆 RANKING DE CONVITES'",
        "ALTER TABLE convites_config ADD COLUMN rank_color TEXT DEFAULT '5865F2'",
        "ALTER TABLE convites_config ADD COLUMN rank_banner TEXT DEFAULT ''",
        "ALTER TABLE convites_config ADD COLUMN rank_footer TEXT DEFAULT 'FFZ E-SPORTS'",
        "ALTER TABLE convites_config ADD COLUMN rank_per_page INTEGER DEFAULT 10",
        # Coluna que faltava: o painel de anúncios (@everyone/@here/cargo)
        # já salvava esse campo desde a integração, mas a tabela nunca tinha
        # sido criada com ele — sem isso, qualquer anúncio com menção quebra
        # ao salvar com "no such column: mencao".
        "ALTER TABLE anuncios ADD COLUMN mencao TEXT",
        # ID da mensagem separada que carrega SÓ a menção (@everyone/@here/
        # cargo), postada logo abaixo do anúncio principal (embed) — assim
        # dá pra apagar ela também quando o anúncio é reenviado/apagado.
        "ALTER TABLE anuncios ADD COLUMN msg_id_mencao INTEGER",
        # Personalização do "Destaque do Dia" (ranking automático): nome da
        # marca configurável (SaaS - cada servidor tem o seu, não fica preso
        # a "FFZ E-SPORTS"), liga/desliga o post automático, horário
        # configurável (HH:MM, considerado no fuso America/Sao_Paulo) e a
        # data do último post feito (evita postar 2x se o loop rodar de novo
        # no mesmo minuto/dia por qualquer motivo).
        "ALTER TABLE ranking_config ADD COLUMN nome_marca TEXT DEFAULT 'E-SPORTS'",
        "ALTER TABLE ranking_config ADD COLUMN destaque_ativo INTEGER DEFAULT 1",
        "ALTER TABLE ranking_config ADD COLUMN destaque_horario TEXT DEFAULT '00:00'",
        "ALTER TABLE ranking_config ADD COLUMN ultimo_destaque TEXT",
        # Emojis customizáveis dos botões "Seu Perfil" e "Ranking" do painel
        # principal, configuráveis pelo /personalizarranking (antes fixos
        # em 👤 e 🏆 no código, sem opção de customizar por servidor).
        "ALTER TABLE ranking_config ADD COLUMN emoji_perfil TEXT DEFAULT '👤'",
        "ALTER TABLE ranking_config ADD COLUMN emoji_ranking TEXT DEFAULT '🏆'",
        # Cargo dado automaticamente pro Top 1 do "Destaque do Dia" (igual ao
        # bot da FAC7: quem fica em 1º ganha um cargo de recompensa até o
        # próximo destaque). Guarda o ID do cargo; None = recurso desligado.
        "ALTER TABLE ranking_config ADD COLUMN cargo_premio_top1 INTEGER",
        # Vincula cada registro de ponto a um painel específico (multi-painel
        # independente por servidor, em vez de 1 config global por guild).
        "ALTER TABLE ponto_registros ADD COLUMN painel_id INTEGER",

        # ===== UPGRADE TICKET SYSTEM: prioridade, avaliação, auto-close =====
        # Prioridade escolhida pelo usuário ao abrir o ticket (baixa/media/alta/urgente).
        "ALTER TABLE tickets ADD COLUMN prioridade TEXT DEFAULT 'media'",
        # Avaliação por DM depois do ticket fechado (1 a 5 estrelas + comentário opcional).
        "ALTER TABLE tickets ADD COLUMN avaliacao INTEGER",
        "ALTER TABLE tickets ADD COLUMN avaliacao_comentario TEXT",
        # Última atividade (qualquer mensagem) na thread — usado pelo auto-close
        # por inatividade, pra saber há quanto tempo o ticket tá parado.
        "ALTER TABLE tickets ADD COLUMN ultima_atividade TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        # Evita mandar o aviso de "vou fechar por inatividade" repetido — só uma vez
        # até alguém voltar a mandar mensagem (o que reseta essa flag).
        "ALTER TABLE tickets ADD COLUMN aviso_inatividade_enviado INTEGER DEFAULT 0",
        # Config de auto-close por painel: 0 = desativado. Horas de inatividade até
        # avisar, e horas até fechar de fato (contadas a partir do aviso).
        "ALTER TABLE ticket_paineis ADD COLUMN aviso_inatividade_horas INTEGER DEFAULT 0",
        "ALTER TABLE ticket_paineis ADD COLUMN auto_close_horas INTEGER DEFAULT 0",
        # Liga/desliga o pedido de avaliação por DM ao fechar o ticket.
        "ALTER TABLE ticket_paineis ADD COLUMN pedir_avaliacao INTEGER DEFAULT 1",
        # Guarda o ID da mensagem do painel INTERNO do ticket (Assumir/Prioridade/etc)
        # pra dar edit nela quando algo muda (responsável, prioridade), sem reenviar.
        "ALTER TABLE tickets ADD COLUMN painel_message_id INTEGER",

        # ===== BOTÃO DE FAQ OPCIONAL NO PAINEL PÚBLICO =====
        # Liga/desliga o Link Button de FAQ mostrado junto ao painel de abrir
        # ticket. Destino pode ser um canal do servidor OU uma URL externa
        # (só um dos dois fica preenchido por vez — o outro é limpo no código).
        "ALTER TABLE ticket_paineis ADD COLUMN faq_ativado INTEGER DEFAULT 0",
        "ALTER TABLE ticket_paineis ADD COLUMN faq_canal_id INTEGER",
        "ALTER TABLE ticket_paineis ADD COLUMN faq_url TEXT",

        # ===== PERSONALIZAÇÃO DO PAINEL MODERNO: POSIÇÃO DO MENU E FAQ =====
        # Posição do menu de categorias em relação ao texto no estilo Moderno
        # (Components V2): 'cima' ou 'baixo' (padrão, mantém comportamento antigo).
        "ALTER TABLE ticket_paineis ADD COLUMN select_posicao TEXT DEFAULT 'baixo'",
        "ALTER TABLE ticket_paineis ADD COLUMN banner_posicao TEXT DEFAULT 'baixo'",
        # ===== MODO DE ABERTURA DO TICKET: SELECT (padrão) OU BOTÃO =====
        # 'select' = comportamento antigo (menu dropdown com as categorias).
        # 'botao' = 1 botão cinza por categoria (ideal quando o painel tem só
        # 1 categoria, ex: painel de vendas — clica e já abre o ticket, sem
        # passo intermediário de escolher no menu).
        "ALTER TABLE ticket_paineis ADD COLUMN tipo_abertura TEXT DEFAULT 'select'",
        # Nome, emoji e texto de introdução do botão de FAQ, personalizáveis
        # em vez de fixos ("FAQ" / 📖 / texto padrão) no código.
        "ALTER TABLE ticket_paineis ADD COLUMN faq_label TEXT",
        "ALTER TABLE ticket_paineis ADD COLUMN faq_emoji TEXT",
        "ALTER TABLE ticket_paineis ADD COLUMN faq_texto TEXT",

        # ===== LOG VIVO POR TICKET (embed única editada, em vez de várias soltas) =====
        # Guarda o ID da mensagem de log (no canal_logs) desse ticket, pra dar
        # edit nela toda vez que algo acontece (assumir, prioridade, add/remove
        # user, fechar) em vez de mandar um embed novo pra cada ação.
        "ALTER TABLE tickets ADD COLUMN log_message_id INTEGER",

        # ===== MELHORIAS: SLA de 1ª resposta, cargo extra p/ urgente, reabertura =====
        # Se ninguém assumir em X minutos, o bot pinga o cargo de suporte de novo.
        # 0 = desativado.
        "ALTER TABLE ticket_paineis ADD COLUMN sla_assumir_minutos INTEGER DEFAULT 0",
        # Evita repetir o aviso de SLA — só dispara uma vez por ticket (reseta
        # quando o ticket é reaberto).
        "ALTER TABLE tickets ADD COLUMN sla_aviso_enviado INTEGER DEFAULT 0",
        # Cargo extra (opcional) pingado quando a prioridade vira "urgente",
        # além dos cargos de suporte normais.
        "ALTER TABLE ticket_paineis ADD COLUMN cargo_urgente_extra_id INTEGER",
        # Canal opcional pra receber log de erros/exceções do sistema de
        # ticket (bugs reais, não falhas esperadas tipo Forbidden).
        "ALTER TABLE ticket_paineis ADD COLUMN canal_erros_id INTEGER",

        # ===== MENU DO SELECT PERSONALIZÁVEL (EMOJI + TEXTO) =====
        # Emoji na frente do placeholder do menu de categorias.
        # NULL = nunca configurado (usa o padrão 🎫 no código).
        # '' (string vazia) = usuário escolheu remover o emoji.
        "ALTER TABLE ticket_paineis ADD COLUMN select_emoji TEXT",
        # Texto do placeholder do menu de categorias. NULL/vazio = usa o
        # texto padrão ("Selecione uma categoria para abrir ticket...").
        "ALTER TABLE ticket_paineis ADD COLUMN select_texto TEXT",

        # ===== PERSISTÊNCIA DO SISTEMA SS APÓS RESTART =====
        # Guarda o id da mensagem do painel "Assumir análise" e o id da
        # mensagem de resultado "Limpo/W.O", além do canal/thread de origem
        # de cada solicitação. Sem isso não dá pra reconstruir os botões
        # depois que o bot reinicia (ver obter_ss_pendentes/obter_ss_em_analise
        # em bot.py) — eles ficavam "mortos" até alguém reenviar a solicitação.
        "ALTER TABLE ss_logs ADD COLUMN message_id_solicitacao INTEGER",
        "ALTER TABLE ss_logs ADD COLUMN message_id_resultado INTEGER",
        "ALTER TABLE ss_logs ADD COLUMN origem_id INTEGER",

        # Canal de logs do sistema SS: 1 embed por solicitação, editado ao
        # longo do ciclo de vida (pendente -> em análise -> limpo/W.O), no
        # mesmo padrão do log vivo de tickets (log_message_id).
        "ALTER TABLE ss_config ADD COLUMN canal_logs_ss INTEGER",
        "ALTER TABLE ss_logs ADD COLUMN message_id_log INTEGER",

        # Painel de fila dos analistas SS (Entrar/Sair, igual o painel de
        # mediadores) — guarda onde ele foi postado pra dar pra editar/
        # reconstruir depois de um restart.
        "ALTER TABLE ss_config ADD COLUMN canal_painel_ss INTEGER",
        "ALTER TABLE ss_config ADD COLUMN msg_painel_ss INTEGER",

        # Canal de logs DEDICADO ao sistema de W.O -- antes o W.O usava o
        # mesmo canal_logs_ss dos analistas (log de análise/solicitação),
        # misturando prova de W.O (fotos/vídeos sensíveis) com log
        # operacional do dia a dia. NULL = continua caindo no
        # canal_logs_ss de sempre (compatibilidade com quem já tinha
        # configurado, ver _canal_logs_wo() em solicitarss.py).
        "ALTER TABLE ss_config ADD COLUMN canal_logs_wo INTEGER",

        # Timestamp de quando a análise foi assumida (manual ou puxada da
        # fila) -- já existiam criado_em e decidido_em, faltava o meio do
        # caminho. Usado pra montar a linha do tempo completa no log vivo
        # do canal_logs_ss (pendente -> assumida -> resultado).
        "ALTER TABLE ss_logs ADD COLUMN assumido_em TIMESTAMP",

        # Posição customizável de cada anúncio na lista do painel (Mover
        # ⬆️/⬇️ no /anuncios) — sem essa coluna todo mundo ficava só em
        # ordem alfabética, sem jeito de reorganizar manualmente.
        "ALTER TABLE anuncios ADD COLUMN ordem INTEGER DEFAULT 0",

        # ===== IMAGENS DO PAINEL DE TICKET (thumbnail além do banner) =====
        # banner_url já existia; thumbnail_url é a imagem pequena que entra
        # "grudada" do lado do texto no estilo Moderno (Components V2) e como
        # set_thumbnail no estilo Clássico.
        "ALTER TABLE ticket_paineis ADD COLUMN thumbnail_url TEXT DEFAULT ''",

        # ===== IA DE SUPORTE NOS TICKETS =====
        # Liga/desliga o atendimento automático por IA nesse painel, e o
        # texto de FAQ/base de conhecimento que ela usa pra responder. A IA
        # só responde enquanto ninguém assumiu o ticket (assumido_por NULL).
        "ALTER TABLE ticket_paineis ADD COLUMN ia_ativada INTEGER DEFAULT 0",
        "ALTER TABLE ticket_paineis ADD COLUMN ia_conhecimento TEXT DEFAULT ''",

        # ===== CASSINO: estoque na roleta (igual já tinha na caixa) e cores
        # customizáveis da roleta (antes fixas em vermelho/preto no código) =====
        "ALTER TABLE roleta_itens ADD COLUMN estoque INTEGER DEFAULT NULL",
        "ALTER TABLE roleta_config ADD COLUMN cor1 TEXT DEFAULT 'B01E1E'",
        "ALTER TABLE roleta_config ADD COLUMN cor2 TEXT DEFAULT '781212'",

        # ===== CASSINO: descrição/banner editáveis na Roleta e na Caixa
        # Premiada, igual já existia na Loja (antes eram textos fixos no
        # código e a Caixa nem tinha cor própria, ficava sempre dourada) =====
        "ALTER TABLE roleta_config ADD COLUMN descricao TEXT DEFAULT 'Gire a roleta e concorra a prêmios!'",
        "ALTER TABLE roleta_config ADD COLUMN banner TEXT DEFAULT ''",
        "ALTER TABLE caixa_config ADD COLUMN descricao TEXT DEFAULT 'Tente sua sorte! Clique no botão abaixo pra abrir sua caixa e ver o que ganha!'",
        "ALTER TABLE caixa_config ADD COLUMN cor TEXT DEFAULT 'FFD700'",
        "ALTER TABLE caixa_config ADD COLUMN banner TEXT DEFAULT ''",

        # ===== REAÇÃO AUTOMÁTICA NA MENSAGEM DE BOAS-VINDAS =====
        # Emoji que o bot reage na própria mensagem de entrada, escolhido
        # visualmente em /painelconvites (emoji custom do servidor ou
        # digitado à mão). '' / NULL = não reage em nada (comportamento
        # de antes, sem mudança pra quem não configurar).
        "ALTER TABLE convites_config ADD COLUMN join_reaction_emoji TEXT DEFAULT ''",

        # ===== CASSINO: cargo DE VERDADE (RoleSelect) em vez de digitar nome =====
        # cargo_id guarda o ID do cargo real escolhido no seletor do Discord.
        # cargo_nome continua existindo só pra prêmios antigos (criados antes
        # dessa mudança) — o código passa a preferir cargo_id quando presente
        # e só cai pro nome digitado como fallback pra não quebrar o que já
        # tinha sido cadastrado.
        "ALTER TABLE loja_itens ADD COLUMN cargo_id INTEGER",
        "ALTER TABLE roleta_itens ADD COLUMN cargo_id INTEGER",
        "ALTER TABLE caixa_itens ADD COLUMN cargo_id INTEGER",

        # Cargo "dono da lojinha": além do jogador e do bot, esse cargo
        # também recebe acesso ao ticket privado de prêmio/saque em PIX, e
        # é o único (fora Administrador) que pode apertar "Finalizar" pra
        # fechar o ticket depois de registrar o pagamento.
        "ALTER TABLE configuracoes ADD COLUMN cargo_dono_loja INTEGER",

        # Canal DEDICADO só pro cassino (Loja + Roleta + Caixa Premiada) —
        # separado de propósito do canal_logs genérico (que já é usado por
        # outras partes do bot), pra não misturar avisos de compra/prêmio
        # com o resto dos logs do servidor.
        "ALTER TABLE configuracoes ADD COLUMN canal_anuncios_cassino INTEGER",

        # Canal onde o auto-detect da blacklist funciona: se alguém mandar
        # só um número (a "cara" de um ID) nesse canal, o bot já responde
        # Detectado/Não Detectado sozinho, sem precisar digitar comando.
        # Fora desse canal o auto-detect fica desligado (evita disparar em
        # qualquer número aleatório digitado no chat geral do servidor).
        "ALTER TABLE configuracoes ADD COLUMN canal_blacklist INTEGER",

        # ===== ANTI-PALAVRÃO: canal de log próprio =====
        # Antes reaproveitava à força o canal do anti-link (cfg antilink_log_channel_id),
        # então dava pra ligar o anti-palavrão sem log nenhum ir pro lugar certo, ou
        # misturar os dois tipos de alerta no mesmo canal sem opção de separar.
        "ALTER TABLE convites_config ADD COLUMN bad_words_log_channel_id INTEGER",

        # ===== AUTOMOD NOVO: anti-invite, anti-menções, anti-caps, anti-spam =====
        # Canal de log compartilhado entre os 4 (fallback: canal geral de
        # moderação em /configmoderacao, se nem esse for configurado).
        "ALTER TABLE convites_config ADD COLUMN automod_log_channel_id INTEGER",

        # Anti-invite: bloqueia SÓ link de convite discord.gg/discord.com/invite,
        # funciona independente do anti-link geral estar ligado ou não.
        "ALTER TABLE convites_config ADD COLUMN antiinvite_enabled INTEGER DEFAULT 0",
        "ALTER TABLE convites_config ADD COLUMN antiinvite_msg TEXT DEFAULT '🚫 Convites de outros servidores não são permitidos!'",

        # Anti-menções em massa: apaga e loga se a mensagem tiver N+ menções de
        # usuário/cargo (comum em spam/raid — "@everyone fake" via menções soltas).
        "ALTER TABLE convites_config ADD COLUMN antimencoes_enabled INTEGER DEFAULT 0",
        "ALTER TABLE convites_config ADD COLUMN antimencoes_limite INTEGER DEFAULT 5",
        "ALTER TABLE convites_config ADD COLUMN antimencoes_msg TEXT DEFAULT '🚫 Muitas menções de uma vez!'",

        # Anti-caps lock: só considera mensagens com pelo menos N letras (evita
        # marcar "OK" ou "KKKK" curtos) e dispara quando X% delas são maiúsculas.
        "ALTER TABLE convites_config ADD COLUMN anticaps_enabled INTEGER DEFAULT 0",
        "ALTER TABLE convites_config ADD COLUMN anticaps_min_chars INTEGER DEFAULT 10",
        "ALTER TABLE convites_config ADD COLUMN anticaps_percentual INTEGER DEFAULT 70",
        "ALTER TABLE convites_config ADD COLUMN anticaps_msg TEXT DEFAULT '🚫 Evita escrever em CAPS LOCK!'",

        # Anti-spam/flood: X mensagens em Y segundos = apaga a mensagem, timeout
        # automático (se o autor não for staff) e loga. Buffer fica em memória
        # (self.msg_buffer no cog), não no banco — não faz sentido persistir
        # timestamp de flood entre restarts do bot.
        "ALTER TABLE convites_config ADD COLUMN antispam_enabled INTEGER DEFAULT 0",
        "ALTER TABLE convites_config ADD COLUMN antispam_msgs INTEGER DEFAULT 5",
        "ALTER TABLE convites_config ADD COLUMN antispam_seconds INTEGER DEFAULT 5",
        "ALTER TABLE convites_config ADD COLUMN antispam_timeout_minutos INTEGER DEFAULT 10",
        "ALTER TABLE convites_config ADD COLUMN antispam_msg TEXT DEFAULT '🚫 Calma com as mensagens! Você foi silenciado.'",

        # ===== AVISO PERSONALIZADO NA FILA (opcional, liga/desliga por servidor) =====
        # Card extra que aparece embaixo dos botões Entrar/Sair de toda fila
        # quando ativado — título, texto (aceita link em markdown) e cor da
        # barra lateral totalmente customizáveis pelo /configurar. Fica
        # OFF por padrão pra não mudar o visual de quem já tem fila rodando.
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_ativo INTEGER DEFAULT 0",
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_titulo TEXT DEFAULT '🚨 AVISO IMPORTANTE!'",
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_texto TEXT DEFAULT 'Edite esse texto em /configurar → Aviso Fila.'",
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_cor TEXT DEFAULT '0xFF0000'",

        # ===== MOTIVO OBRIGATÓRIO AO FECHAR TICKET =====
        # Texto digitado pelo staff no modal que agora aparece antes de
        # fechar (ou preenchido automaticamente como "Fechado por
        # inatividade" no auto-close). Fica salvo no ticket pra aparecer
        # no embed de log e no cabeçalho da transcrição em HTML.
        "ALTER TABLE tickets ADD COLUMN fechado_motivo TEXT",

        # ===== REESTRUTURAÇÃO DO SISTEMA DE KEYS (hash em vez de texto puro) =====
        # A partir de agora, 'assinaturas' guarda uma REFERÊNCIA à key usada
        # (id na tabela 'chaves', já reestruturada — ver _migrar_chaves_para_hash)
        # e uma versão MASCARADA pra exibir em painéis, nunca mais a key
        # completa em texto puro. Motivo: o banco inteiro sai como anexo cru
        # no backup horário pra DM do dono (ver painel_owner._enviar_backup)
        # -- se esse arquivo vazar (DM comprometida, anexo compartilhado por
        # engano, etc), toda key gerada até então (usada ou ainda em
        # estoque) ficava exposta em texto puro pra quem pegasse o arquivo.
        # A coluna antiga 'chave' continua existindo por compatibilidade com
        # registros anteriores à migração, mas não é mais escrita.
        "ALTER TABLE assinaturas ADD COLUMN chave_id INTEGER",
        "ALTER TABLE assinaturas ADD COLUMN chave_mascarada TEXT",

        # Canal DEDICADO só pra hospedar foto de QR Code do PIX — separado
        # de propósito do canal_logs genérico (que já é usado por logs de
        # mediação/fila/tickets), pra não misturar imagem de QR Code com o
        # resto dos logs do servidor. Mesmo padrão do canal_anuncios_cassino.
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_pix INTEGER",

        # Canal DEDICADO só pra hospedar upload de Thumbnail/Banner do
        # /configurar (aba Visual) — mesmo motivo do canal_logs_pix acima:
        # antes usava o canal_logs genérico das filas, misturando imagem
        # de config com log de partida/mediação.
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_visual INTEGER",

        # NOVO: canal de feedback onde o Bot reage automaticamente em toda
        # mensagem postada (ex: 👍/🤝/👎 pra galera votar rápido sem precisar
        # digitar nada). reacao_feedback_emojis guarda até 3 emojis
        # separados por vírgula (unicode ou custom <:nome:id>).
        "ALTER TABLE configuracoes ADD COLUMN canal_reacao_feedback_id INTEGER",
        "ALTER TABLE configuracoes ADD COLUMN reacao_feedback_emojis TEXT DEFAULT '👍,🤝,👎'",

        # FIX PERSISTÊNCIA: o painel "Sala Liberada" (ID + senha, ver
        # painelmediador._lancar_sala) guardava id_sala/senha_sala só na
        # instância da View em memória — depois de QUALQUER restart do
        # bot, o botão "Copiar ID" passava a copiar string vazia (e
        # "Trocar Valor" perdia o modo certo), porque a partida nunca
        # salvava esses dados em lugar nenhum pra reconstruir na volta.
        # Agora fica salvo na própria partida, e a View busca sempre o
        # valor fresco do banco por thread_id (mesmo esquema de
        # ViewPartida/ViewCopiarChavePix), então sobrevive a qualquer
        # quantidade de restarts.
        "ALTER TABLE partidas ADD COLUMN sala_id TEXT",
        "ALTER TABLE partidas ADD COLUMN sala_senha TEXT",
        # Número sequencial da partida dentro do servidor (ver
        # numeracao_partidas) -- usado pro nome do tópico nas fases
        # "aguardando{n}" / "fila{n}" antes do ID/senha sair.
        "ALTER TABLE partidas ADD COLUMN numero INTEGER",
        # ===== MEDIADOR PREMIUM: pega 2 filas antes de rodar pro fim =====
        # Cargo configurável (/configurar) que dá direito a pegar 2 filas
        # seguidas em 1º lugar, em vez de 1 (mediador normal), antes de ir
        # pro fim da fila de rotação.
        "ALTER TABLE configuracoes ADD COLUMN cargo_mediador_premium INTEGER",
        # Contador de quantas filas o mediador (se Premium) já pegou desde
        # a última vez que foi pro fim da fila. Fica na própria linha da
        # fila_mediadores, então some sozinho se ele sair da fila (a linha
        # é deletada em sair_fila_med) -- reentrar sempre volta com 0.
        "ALTER TABLE fila_mediadores ADD COLUMN contador_premium INTEGER DEFAULT 0",
        # ===== AVISO PIX LIBERADO / AVISO SALA LIBERADA =====
        # Mesmo modelo do "Aviso Fila" (aviso_fila_*) já existente, só que
        # disparado em outros 2 momentos: quando o mediador libera o Pix
        # (ViewLiberarPix) e quando ele lança ID+senha da sala (_lancar_sala).
        # Cada um com seu próprio liga/desliga, título, texto e cor.
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_ativo INTEGER DEFAULT 0",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_titulo TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_texto TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_cor TEXT DEFAULT '0xFF0000'",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_ativo INTEGER DEFAULT 0",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_titulo TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_texto TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_cor TEXT DEFAULT '0xFF0000'",

        # ===== "2º BLOCO" DOS 3 AVISOS (Fila/Pix/Sala) =====
        # Pedido do usuário: mandou print de outro bot (Complexo E-Sports)
        # onde aparecem 2 embeds diferentes empilhados na MESMA mensagem
        # (ex: "🚨 REGRAS PADRÕES!" em branco + "🎥 análises rolando" em
        # cinza, logo abaixo do card de Confirmar/Cancelar). Cada aviso
        # (aviso_fila/aviso_pix/aviso_sala) ganha um 2º título/texto/cor
        # opcional -- se o texto2 estiver vazio, esse 2º bloco simplesmente
        # não aparece (comportamento 100% igual a antes pra quem não
        # configurar nada). Cor padrão cinza escuro (bem diferente do
        # vermelho padrão do bloco 1), pra já sair parecido com o print
        # de referência sem precisar configurar nada na cor.
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_titulo2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_texto2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_fila_cor2 TEXT DEFAULT '0x2F3136'",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_titulo2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_texto2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_pix_cor2 TEXT DEFAULT '0x2F3136'",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_titulo2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_texto2 TEXT",
        "ALTER TABLE configuracoes ADD COLUMN aviso_sala_cor2 TEXT DEFAULT '0x2F3136'",
        # ===== NOVO TIPO DE FILA: GIRL 🎀 =====
        # Mesmo esquema de emoji configurável que Mobile/Emulador/Misto/
        # Tático/Full Soco já tinham (emoji_mobile, emoji_emulador, etc.) --
        # tipo novo, reaproveitando os mesmos botões/emojis fixos de
        # Mobile (1x1: Gel Normal/Gel Infinito | 2x2-4x4: Normal/Full Ump e
        # Xm8), só que com formato próprio configurável (padrão 🎀).
        "ALTER TABLE configuracoes ADD COLUMN emoji_girl TEXT DEFAULT NULL",
        # ===== NOVO TIPO DE FILA: FULL SEM TELA =====
        # Mesmo esquema de emoji configurável que os outros tipos já têm --
        # tipo novo, reaproveitando os mesmos botões do Full Soco (só
        # Entrar cinza / Sair vermelho), sem ícone fixo próprio (cai no
        # genérico 🎮 até o servidor personalizar em /configurar).
        "ALTER TABLE configuracoes ADD COLUMN emoji_fullsemtela TEXT DEFAULT NULL",
        # Trava da fila de mediadores (menu de Opções do painel): quando
        # ligada, ninguém consegue clicar em "Mediar" até um ADM destravar.
        "ALTER TABLE configuracoes ADD COLUMN fila_med_travada INTEGER DEFAULT 0",

        # ===== BLACKLIST: canal de logs + cargos com permissão =====
        # Canal único onde vai todo log de ID adicionado E removido da
        # blacklist (um só canal pros dois eventos, por escolha do dono).
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_blacklist INTEGER",
        # Até 3 cargos (IDs separados por vírgula) que têm permissão de
        # adicionar/remover ID na blacklist pelo painel — além de quem já
        # tinha acesso antes (dono do servidor, owner global, cargo_admin,
        # Administrador do Discord).
        "ALTER TABLE configuracoes ADD COLUMN cargos_blacklist TEXT",
        # Provas (URLs de anexos enviados no chat) anexadas na hora de
        # adicionar um ID na blacklist, guardadas como lista JSON.
        "ALTER TABLE blacklist ADD COLUMN provas TEXT",

        # ===== PAINEL CENTRAL UNIFICADO (.apostas + /central_dashboard +
        # diagnóstico completo, tudo num painel só) =====
        # Período do "Resumo das Filas" (geral/hoje/ontem) escolhido no
        # select -- salvo pra sobreviver ao ciclo de auto-atualização (o
        # loop de 1 em 1 minuto precisa saber qual período reconstruir a
        # cada edição) e a um restart do bot.
        "ALTER TABLE central_stats_live ADD COLUMN periodo TEXT DEFAULT 'geral'",

        # ===== BLACKLIST: emojis customizados nos LOGS (não no painel — o
        # painel principal voltou a ser embed normal) =====
        # Emoji (unicode ou <:nome:id> do servidor) que aparece no cabeçalho
        # do log quando um ID é ADICIONADO na blacklist. NULL = usa o
        # padrão 🔨 do código.
        "ALTER TABLE configuracoes ADD COLUMN emoji_log_blacklist_add TEXT",
        # Mesma ideia, pro log de ID REMOVIDO da blacklist. NULL = usa o
        # padrão 🗑️ do código.
        "ALTER TABLE configuracoes ADD COLUMN emoji_log_blacklist_remove TEXT",

        # ===== SISTEMA DE LOGS DAS FILAS (ciclo de vida completo da
        # partida, cada etapa no seu canal dedicado) + CATEGORIA AUTOMÁTICA
        # (ver cogs/estrutura_logs.py) =====
        # Fila Lançada: disparado assim que os 2 players são pareados e a
        # thread da partida é criada (mesmo instante do "Aguardando", só
        # que num canal separado -- pensado pra quem quer ver só a "vitrine"
        # das apostas abertas, sem o resto do ruído de log).
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_fila_lancada INTEGER",
        # Fila Aguardando: mesmo instante do "Lançada", mas no canal onde
        # ADM/staff acompanha o que está rolando aguardando confirmação/
        # resultado.
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_fila_aguardando INTEGER",
        # Fila Confirmada: partida finalizada com vencedor definido (Win ou
        # Win por W.O) -- ver ViewDeclararVencedor/ViewDeclararVencedorWO
        # em painelmediador.py.
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_fila_confirmada INTEGER",
        # Fila Cancelada: partida cancelada pelos players (ViewPartida) OU
        # encerrada pelo mediador sem declarar vencedor (_acao_encerrar_aposta).
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_fila_cancelada INTEGER",
        # Salas Criadas: disparado quando o mediador lança ID+senha da sala
        # (_lancar_sala) -- é o momento em que a partida "sai do papel" de
        # verdade dentro do jogo.
        "ALTER TABLE configuracoes ADD COLUMN canal_logs_salas_criadas INTEGER",
        # ID da categoria criada pelo /logs-setup (estrutura automática de
        # canais de log) -- guardado pra rodar o comando de novo depois
        # (ex: recriar um canal apagado sem duplicar a categoria inteira).
        "ALTER TABLE configuracoes ADD COLUMN categoria_logs_id INTEGER",

        # ===== PERSISTÊNCIA DO SISTEMA TELADOR APÓS RESTART =====
        # Mesmo problema que o SS tinha (ver ALTER TABLE ss_logs acima):
        # tel_logs foi criada só com CREATE TABLE IF NOT EXISTS, então quem
        # já tinha o banco criado antes dessas colunas existirem nunca
        # ganhou elas -- ficava dando "no such column" em qualquer
        # UPDATE/SELECT que usasse esses campos (obter_tel_pendentes,
        # definir_tel_message_id_solicitacao, etc. em database.py).
        "ALTER TABLE tel_logs ADD COLUMN message_id_solicitacao INTEGER",
        "ALTER TABLE tel_logs ADD COLUMN message_id_resultado INTEGER",
        "ALTER TABLE tel_logs ADD COLUMN origem_id INTEGER",
        "ALTER TABLE tel_logs ADD COLUMN message_id_log INTEGER",
        "ALTER TABLE tel_logs ADD COLUMN assumido_em TIMESTAMP",

        # Mesma lacuna em tel_config: canal de logs (+ canal dedicado de
        # W.O, com fallback pro de cima -- ver _canal_logs_wo() em
        # solicitar_telador.py) e canal/mensagem do painel de fila dos
        # analistas (Entrar/Sair), pra sobreviver a um restart do bot.
        "ALTER TABLE tel_config ADD COLUMN canal_logs_tel INTEGER",
        "ALTER TABLE tel_config ADD COLUMN canal_logs_wo INTEGER",
        "ALTER TABLE tel_config ADD COLUMN canal_painel_tel INTEGER",
        "ALTER TABLE tel_config ADD COLUMN msg_painel_tel INTEGER",
        # Liga/desliga os emojis decorativos (título, Modo, Valor, Players,
        # Mediador) do card de partida -- pedido de quem prefere o card
        # mais "limpo", só com o texto. 1 = mostra (comportamento de
        # sempre), 0 = esconde. Não mexe em QUAL emoji está configurado em
        # cada campo (emoji_modo_partida etc.), só se ele aparece ou não.
        "ALTER TABLE configuracoes ADD COLUMN mostrar_emojis_partida INTEGER DEFAULT 1",

        # ===== ATUALIZAÇÃO DO SISTEMA DE TICKET (trazido do bot de ticket) =====
        "ALTER TABLE ticket_paineis ADD COLUMN emoji_reacao_feedback TEXT DEFAULT '🎉'",
        "ALTER TABLE ticket_paineis ADD COLUMN estilo_ticket TEXT DEFAULT 'v2'",
        "ALTER TABLE configuracoes ADD COLUMN cargo_absoluto_ticket INTEGER",
        "ALTER TABLE ticket_paineis ADD COLUMN botao_assumir_emoji TEXT",
        "ALTER TABLE ticket_paineis ADD COLUMN botao_fechar_emoji TEXT",
        "ALTER TABLE ticket_paineis ADD COLUMN botao_painelstaff_emoji TEXT",

        # ===== RESET DOS EMOJIS "DE FÁBRICA" DA FILA (bug: fallback pro emoji
        # fixo do Developer Portal nunca funcionava) =====
        # emoji_valor/emoji_players/emoji_mobile/etc nasciam com DEFAULT
        # '💰'/'👥'/'📱'... na criação da linha, então config.get(chave,
        # fallback) NUNCA caía no fallback -- a chave sempre existia com um
        # valor. Resultado: o emoji fixo (ffz_valorr, ffz_jogadores,
        # ffz_controlle etc.) nunca aparecia pra ninguém, nem pra quem
        # nunca mexeu no botão de /configurar. Isso zera (NULL) só quem
        # ainda está no valor padrão de fábrica -- quem já personalizou
        # manualmente pra outro emoji continua com a escolha dele intacta.
        "UPDATE configuracoes SET emoji_valor = NULL WHERE emoji_valor = '💰'",
        "UPDATE configuracoes SET emoji_players = NULL WHERE emoji_players = '👥'",
        "UPDATE configuracoes SET emoji_mobile = NULL WHERE emoji_mobile = '📱'",
        "UPDATE configuracoes SET emoji_emulador = NULL WHERE emoji_emulador = '💻'",
        "UPDATE configuracoes SET emoji_misto = NULL WHERE emoji_misto = '🔀'",
        "UPDATE configuracoes SET emoji_tatico = NULL WHERE emoji_tatico = '🎯'",
        "UPDATE configuracoes SET emoji_fullsoco = NULL WHERE emoji_fullsoco = '👊'",
        "UPDATE configuracoes SET emoji_girl = NULL WHERE emoji_girl = '🎀'",
        "UPDATE configuracoes SET emoji_fullsemtela = NULL WHERE emoji_fullsemtela = '🎮'",

        # ===== NOME DA FILA CUSTOMIZÁVEL (texto do tópico/thread) =====
        # Antes "aguardando{n}" e "fila{n}" eram fixos no código -- agora dá
        # pra personalizar em /configurar → Editar Visual → Nome da Fila.
        # "{numero}" no texto salvo é onde o número da partida entra.
        "ALTER TABLE configuracoes ADD COLUMN texto_fila_aguardando TEXT DEFAULT 'aguardando{numero}'",
        "ALTER TABLE configuracoes ADD COLUMN texto_fila_confirmada TEXT DEFAULT 'fila{numero}'",
        # "{valor}" aqui já vem PRONTO com símbolo + prêmio formatado
        # (ex: "R$ 4.00") -- diferente de "{numero}" acima, não dá pra
        # pedir o admin digitar o símbolo da moeda, porque ele já é
        # configurável em outro lugar (Economia → Símbolo da Moeda).
        "ALTER TABLE configuracoes ADD COLUMN texto_fila_sala TEXT DEFAULT '🏆 • Prêmio {valor}'",
    ]:
        try:
            await db.execute(sql)
        except Exception:
            pass

    await db.execute(
        "CREATE TABLE IF NOT EXISTS contadores_logs ("
        "guild_id INTEGER NOT NULL, tipo TEXT NOT NULL, contador INTEGER DEFAULT 0, "
        "PRIMARY KEY (guild_id, tipo))"
    )

    await db.commit()
    await _migrar_ponto_paineis(db)
    await _migrar_anuncios_canais(db)
    await _migrar_chaves_para_hash(db)
    await _migrar_cargos_staff_multa(db)

    # Pré-abre a conexão dedicada do check de licença aqui (startup),
    # em vez de deixar a PRIMEIRA checagem de licença pagar o custo de
    # abrir a conexão na hora. Assim, quando o primeiro comando/botão
    # chegar depois do bot ficar pronto, a conexão já tá quente.
    await _get_conn_licenca()


async def _migrar_anuncios_canais(db):
    """Migração automática pro multi-canal: todo anúncio que já existia (1
    canal_id fixo na tabela anuncios) ganha uma linha correspondente em
    anuncios_canais, virando o "primeiro/único canal" dele — ninguém perde
    o anúncio já configurado, e dá pra ir lá depois e adicionar mais canais.
    Roda toda vez que o bot inicia, mas só migra quem ainda não tem nenhuma
    linha em anuncios_canais (não duplica em restarts seguintes)."""
    cursor = await db.execute("""
        SELECT guild_id, nome, canal_id, msg_id, msg_id_mencao
        FROM anuncios
        WHERE canal_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM anuncios_canais ac
              WHERE ac.guild_id = anuncios.guild_id AND ac.nome = anuncios.nome
          )
    """)
    pendentes = await cursor.fetchall()
    for row in pendentes:
        await db.execute(
            "INSERT OR IGNORE INTO anuncios_canais (guild_id, nome, canal_id, msg_id, msg_id_mencao) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["guild_id"], row["nome"], row["canal_id"], row["msg_id"], row["msg_id_mencao"])
        )
    if pendentes:
        await db.commit()


async def _migrar_ponto_paineis(db):
    """Migração única e idempotente: converte a antiga 'ponto_config' (1
    config por servidor) num painel #1 em 'ponto_paineis', e vincula os
    registros antigos (painel_id NULL) a esse painel. Servidores que ainda
    não tinham nenhum ponto configurado simplesmente não geram painel — a
    tabela ponto_config fica vazia/parada, sem custo nenhum."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ponto_config")
    antigas = await cursor.fetchall()

    for cfg in antigas:
        guild_id = cfg["guild_id"]
        # já migrado nesse servidor? não cria de novo.
        cursor2 = await db.execute("SELECT painel_id FROM ponto_paineis WHERE guild_id = ? LIMIT 1", (guild_id,))
        ja_existe = await cursor2.fetchone()
        if ja_existe:
            continue

        cursor3 = await db.execute(
            "INSERT INTO ponto_paineis (guild_id, titulo, canal_id, msg_painel_id, cargo_ponto, "
            "meta_diaria_minutos, meta_semanal_minutos, canal_log_inicio, canal_log_concluido, "
            "canal_log_fechado_admin, canal_log_gerencia, status) "
            "VALUES (?, 'Bate Ponto', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ativo')",
            (
                guild_id, cfg["canal_ponto"], cfg["msg_painel_ponto"], cfg["cargo_ponto"],
                cfg["meta_diaria_minutos"], cfg["meta_semanal_minutos"], cfg["canal_log_inicio"],
                cfg["canal_log_concluido"], cfg["canal_log_fechado_admin"], cfg["canal_log_gerencia"],
            )
        )
        novo_painel_id = cursor3.lastrowid

        await db.execute(
            "UPDATE ponto_registros SET painel_id = ? WHERE guild_id = ? AND painel_id IS NULL",
            (novo_painel_id, guild_id)
        )

    await db.commit()
async def obter_config(guild_id):
    if guild_id in _cache_config:
        return _cache_config[guild_id]

    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM configuracoes WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO configuracoes (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM configuracoes WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    resultado = dict(row) if row else {}
    _cache_config[guild_id] = resultado
    return resultado


async def atualizar_config(guild_id, campo, valor=None):
    """Atualiza um ou mais campos da tabela `configuracoes` e SEMPRE mantém
    o cache em memória (_cache_config) coerente — é a única forma "oficial"
    de escrever nessa tabela.

    Uso normal, um campo por vez (compatível com todo o código já existente):
        await atualizar_config(guild_id, "taxa", 0.20)

    Uso com vários campos de uma vez, no lugar de escrever um UPDATE manual
    (ex: texto + cor do aviso da fila juntos):
        await atualizar_config(guild_id, {"rodape_texto": "...", "cor_embed": "..."})

    BLINDAGEM: antes, vários pontos do bot (principalmente cogs/configurar.py)
    faziam `UPDATE configuracoes ...` direto na conexão, ignorando essa
    função — e sempre que alguém esquecia de também limpar o cache na mão, o
    painel ficava mostrando o valor antigo até o bot reiniciar (foi o bug do
    "cor/taxa/GIF não muda em tempo real"). Centralizando toda escrita aqui
    dentro, fica estruturalmente impossível esquecer: quem quiser gravar
    configuração PRECISA passar por essa função, e ela invalida o cache
    sozinha sempre, sem depender de ninguém lembrar.
    """
    db = await get_conn()
    await db.execute("INSERT OR IGNORE INTO configuracoes (guild_id) VALUES (?)", (guild_id,))
    campos = campo if isinstance(campo, dict) else {campo: valor}
    set_clause = ", ".join(f"{c} = ?" for c in campos)
    params = list(campos.values()) + [guild_id]
    await db.execute(f"UPDATE configuracoes SET {set_clause} WHERE guild_id = ?", params)
    await db.commit()
    _cache_config.pop(guild_id, None)


def invalidar_cache_config(guild_id):
    """Limpa o cache em memória de obter_config() para esse guild.

    FIX (cache desatualizado no painel de configuração): qualquer código que
    faça UPDATE direto na tabela `configuracoes` SEM passar por
    atualizar_config() precisa chamar isso logo depois do commit, senão
    obter_config() continua devolvendo o valor antigo (o SELECT nem roda,
    porque o guild_id já "existe" no cache). Foi exatamente o que fazia
    cor/taxa/gif/banner etc. parecerem "não mudar" no painel: o banco salvava
    certo, só a leitura seguinte é que vinha do cache velho.
    """
    _cache_config.pop(guild_id, None)


# ========== SUB-FILAS E BOTÕES ==========
async def salvar_botoes_fila(guild_id, modo, botoes):
    db = await get_conn()
    await db.execute(
        "INSERT OR REPLACE INTO botoes_fila (guild_id, modo, botoes) VALUES (?, ?, ?)",
        (guild_id, modo, json.dumps(botoes))
    )
    await db.commit()


async def _migrar_cargos_staff_multa(db):
    """O painel de multa (/painelmulta -> Cargos do Painel) usava só 2
    colunas fixas (cargo_multa_staff1/2). Isso virou uma lista única
    (cargo_multa_staff_ids, até 25 cargos -- limite do próprio RoleSelect
    do Discord). Quem já tinha staff1/staff2 preenchidos (em servidores
    onde essas 2 colunas já existiam) tem os valores copiados pra lista
    nova, sem perder a configuração. Servidores que nunca conseguiram
    salvar nada (por causa do bug das colunas faltando) simplesmente
    começam com a lista vazia, igual antes."""
    try:
        cursor = await db.execute(
            "SELECT guild_id, cargo_multa_staff1, cargo_multa_staff2, cargo_multa_staff_ids "
            "FROM configuracoes WHERE cargo_multa_staff1 IS NOT NULL OR cargo_multa_staff2 IS NOT NULL"
        )
        rows = await cursor.fetchall()
        for guild_id, staff1, staff2, ids_atual in rows:
            if ids_atual:
                continue  # já migrado antes, não sobrescreve escolha mais recente
            ids = [str(v) for v in (staff1, staff2) if v]
            if ids:
                await db.execute(
                    "UPDATE configuracoes SET cargo_multa_staff_ids = ? WHERE guild_id = ?",
                    (",".join(ids), guild_id),
                )
        await db.commit()
    except Exception as e:
        logging.getLogger("ffz").warning(f"Erro ao migrar cargos staff multa: {e}")


async def obter_botoes_fila(guild_id, modo):
    db = await get_conn()
    cursor = await db.execute("SELECT botoes FROM botoes_fila WHERE guild_id = ? AND modo = ?", (guild_id, modo))
    row = await cursor.fetchone()
    return json.loads(row[0]) if row and row[0] else None


async def salvar_canal_fila_config(canal_id, guild_id, tipo, modo):
    """Grava/atualiza a associação canal_id -> (tipo, modo) pro /criarfila."""
    db = await get_conn()
    await db.execute(
        "INSERT OR REPLACE INTO canais_fila_config (canal_id, guild_id, tipo, modo) VALUES (?, ?, ?, ?)",
        (canal_id, guild_id, tipo, modo)
    )
    await db.commit()


async def obter_canal_fila_config(canal_id):
    """Retorna (tipo, modo) já associados a esse canal, ou (None, None)."""
    db = await get_conn()
    cursor = await db.execute("SELECT tipo, modo FROM canais_fila_config WHERE canal_id = ?", (canal_id,))
    row = await cursor.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def obter_subfilas(message_id):
    """Reconstrói {chave_subfila: [user_id, ...]} direto da tabela
    fila_timeouts, ordenado por quem entrou primeiro (importante: código
    que pega os 2 primeiros de uma subfila pra formar partida depende dessa
    ordem FIFO). Essa tabela virou a ÚNICA fonte de verdade sobre quem está
    em cada subfila — a antiga coluna JSON `subfilas` na tabela `filas`
    não é mais escrita nem lida, ficou só como coluna morta pra não exigir
    uma migração de ALTER TABLE DROP COLUMN."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT chave_subfila, user_id FROM fila_timeouts WHERE message_id = ? ORDER BY ts ASC",
        (message_id,)
    )
    linhas = await cursor.fetchall()
    subfilas = {}
    for linha in linhas:
        subfilas.setdefault(linha['chave_subfila'], []).append(linha['user_id'])
    return subfilas


# ========== PREÇOS CONFIGURADOS (painel de criar fila) ==========
async def obter_precos_configurados(guild_id):
    """Lê os preços que o admin configurou no painel de criar fila (via
    'Adicionar Preço' ou 'Vários de Uma Vez'), do maior pro menor -- é o que
    deve aparecer tanto ao reabrir o painel quanto no resumo do /configurar."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT valor FROM precos_configurados WHERE guild_id = ? ORDER BY valor DESC",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [row['valor'] for row in rows]


async def salvar_precos_configurados(guild_id, precos):
    """Substitui a lista inteira de preços configurados do servidor pelo
    conteúdo de `precos` -- chamada sempre que o admin adiciona (um ou
    vários) ou remove um preço no painel, pra manter o banco sempre
    espelhando exatamente o que está na tela."""
    db = await get_conn()
    await db.execute("DELETE FROM precos_configurados WHERE guild_id = ?", (guild_id,))
    valores_unicos = sorted(set(float(v) for v in precos), reverse=True)
    if valores_unicos:
        await db.executemany(
            "INSERT OR IGNORE INTO precos_configurados (guild_id, valor) VALUES (?, ?)",
            [(guild_id, v) for v in valores_unicos]
        )
    await db.commit()


# ========== TABELA DE APOSTAS ==========
async def obter_precos_filas_ativas(guild_id):
    """Lista os valores (distintos, do maior pro menor) de todas as filas
    atualmente postadas no servidor -- usada no resumo do /configurar pra
    mostrar de cara quais preços já estão configurados/publicados, sem
    precisar abrir o painel de criar fila só pra conferir."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT DISTINCT valor FROM filas WHERE guild_id = ? ORDER BY valor DESC",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [row['valor'] for row in rows]


async def adicionar_valor_tabela(guild_id, valor, paga, premio):
    db = await get_conn()
    await db.execute(
        "INSERT OR REPLACE INTO tabela_apostas (guild_id, valor, paga, premio) VALUES (?, ?, ?, ?)",
        (guild_id, valor, paga, premio)
    )
    await db.commit()


async def remover_valor_tabela(guild_id, valor):
    db = await get_conn()
    await db.execute("DELETE FROM tabela_apostas WHERE guild_id = ? AND valor = ?", (guild_id, valor))
    await db.commit()


async def obter_tabela_apostas(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT valor, paga, premio FROM tabela_apostas WHERE guild_id = ? ORDER BY valor",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return {row['valor']: {'paga': row['paga'], 'premio': row['premio']} for row in rows}


# ========== PIX MEDIADORES ==========
async def obter_pix_usuario(guild_id, user_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT nome, chave_pix, qr_code, banco, cidade FROM pix WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def salvar_pix_med(guild_id, user_id, nome, chave_pix, qr_code, banco=None, cidade=None):
    db = await get_conn()
    await db.execute(
        "INSERT OR REPLACE INTO pix (guild_id, user_id, nome, chave_pix, qr_code, banco, cidade) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, nome, chave_pix, qr_code, banco, cidade)
    )
    await db.commit()


async def apagar_pix_usuario(guild_id, user_id):
    """Remove o PIX de um mediador específico."""
    db = await get_conn()
    await db.execute("DELETE FROM pix WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    await db.commit()


async def obter_pix_cadastrados(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT user_id, nome, chave_pix, qr_code, banco, cidade FROM pix WHERE guild_id = ?",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def registrar_historico_pix(guild_id, user_id, acao, nome, chave_pix, banco, cidade, qr_code, executado_por):
    """Grava uma linha de auditoria (criado/atualizado/removido) pra um PIX.
    Chamar SEMPRE antes/depois de salvar_pix_med e apagar_pix_usuario."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO pix_historico (guild_id, user_id, acao, nome, chave_pix, banco, cidade, qr_code, executado_por) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, user_id, acao, nome, chave_pix, banco, cidade, qr_code, executado_por)
    )
    await db.commit()


async def obter_historico_pix(guild_id, user_id, limite=10):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM pix_historico WHERE guild_id = ? AND user_id = ? ORDER BY criado_em DESC LIMIT ?",
        (guild_id, user_id, limite)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ========== FILA DE MEDIADORES ==========
async def obter_mediadores_fila(guild_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM fila_mediadores WHERE guild_id = ? ORDER BY posicao ASC",
        (guild_id,)
    )
    return [row[0] for row in await cursor.fetchall()]


async def registrar_falha_evento(guild_id, tipo: str, erro: str):
    """Grava 1 linha em eventos_falhos quando um INSERT em apostas_eventos
    cai no except (ver cogs/fila.py e cogs/painelmediador.py). Chamada de
    dentro de um except, então é blindada ela mesma -- se até isso falhar
    (ex: banco travado no exato momento), só ignora, nunca propaga."""
    try:
        db = await get_conn()
        await db.execute(
            "INSERT INTO eventos_falhos (guild_id, tipo, erro) VALUES (?, ?, ?)",
            (guild_id, tipo, str(erro)[:300]),
        )
        await db.commit()
    except Exception:
        pass


async def proximo_contador_log(guild_id, tipo) -> int:
    """Contador sequencial por (guild, tipo de log) -- é o '#N' que aparece
    no título de cada log de fila (ex: 'Fila Lançada! #7'). Cada tipo
    ('lancada', 'aguardando', 'confirmada', 'cancelada', 'sala') conta
    separado, começando em 1 e nunca reseta sozinho."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO contadores_logs (guild_id, tipo, contador) VALUES (?, ?, 1) "
        "ON CONFLICT(guild_id, tipo) DO UPDATE SET contador = contador + 1",
        (guild_id, tipo)
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT contador FROM contadores_logs WHERE guild_id = ? AND tipo = ?",
        (guild_id, tipo)
    )
    row = await cursor.fetchone()
    return row[0] if row else 1


async def obter_proximo_med(guild_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM fila_mediadores WHERE guild_id = ? ORDER BY posicao ASC LIMIT 1",
        (guild_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def entrar_fila_med(guild_id, user_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COALESCE(MAX(posicao), 0) + 1 FROM fila_mediadores WHERE guild_id = ?",
        (guild_id,)
    )
    nova_posicao = (await cursor.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO fila_mediadores (guild_id, user_id, posicao) VALUES (?, ?, ?)",
        (guild_id, user_id, nova_posicao)
    )
    await db.commit()


async def sair_fila_med(guild_id, user_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT posicao FROM fila_mediadores WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    if not row:
        return
    posicao_saindo = row[0]
    await db.execute("DELETE FROM fila_mediadores WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    await db.execute(
        "UPDATE fila_mediadores SET posicao = posicao - 1 WHERE guild_id = ? AND posicao > ?",
        (guild_id, posicao_saindo)
    )
    await db.commit()


async def esta_na_fila_med(guild_id, user_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT 1 FROM fila_mediadores WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    return await cursor.fetchone() is not None


async def limpar_fila_med(guild_id):
    db = await get_conn()
    await db.execute("DELETE FROM fila_mediadores WHERE guild_id = ?", (guild_id,))
    await db.commit()


async def incrementar_contador_premium(guild_id, user_id):
    """Soma 1 no contador de filas pegas pelo mediador Premium (enquanto
    ele fica parado em 1º lugar) e retorna o valor já atualizado, pra
    quem chamou decidir se já bateu o limite (2) e deve rodar pro fim."""
    db = await get_conn()
    await db.execute(
        "UPDATE fila_mediadores SET contador_premium = contador_premium + 1 "
        "WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    await db.commit()
    cursor = await db.execute(
        "SELECT contador_premium FROM fila_mediadores WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def resetar_contador_premium(guild_id, user_id):
    db = await get_conn()
    await db.execute(
        "UPDATE fila_mediadores SET contador_premium = 0 WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    await db.commit()


# ========== COINS ==========
async def obter_saldo(guild_id, user_id):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT saldo FROM coins WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def adicionar_coins(guild_id, user_id, quantidade):
    db = await get_conn()
    await db.execute(
        "INSERT INTO coins (guild_id, user_id, saldo) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET saldo = saldo + ?",
        (guild_id, user_id, quantidade, quantidade)
    )
    await db.commit()


async def remover_coins(guild_id, user_id, quantidade):
    db = await get_conn()
    await db.execute(
        "UPDATE coins SET saldo = MAX(0, saldo - ?) WHERE guild_id = ? AND user_id = ?",
        (quantidade, guild_id, user_id)
    )
    await db.commit()


async def obter_ranking_coins(guild_id, limite=10):
    """Top de jogadores por saldo de coins nesse servidor (maior -> menor),
    usado no botão 'Rank Coins' da lojinha (/loja)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT user_id, saldo FROM coins WHERE guild_id = ? ORDER BY saldo DESC LIMIT ?",
        (guild_id, limite)
    )
    return await cursor.fetchall()


# ========== BUFFS ==========
async def setar_buff(guild_id, user_id, chave, quantidade):
    db = await get_conn()
    await db.execute(
        "INSERT INTO buffs (guild_id, user_id, chave, quantidade) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id, chave) DO UPDATE SET quantidade = quantidade + ?",
        (guild_id, user_id, chave, quantidade, quantidade)
    )
    await db.commit()

async def obter_buff(guild_id, user_id, chave):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT quantidade FROM buffs WHERE guild_id = ? AND user_id = ? AND chave = ?",
        (guild_id, user_id, chave)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0

async def consumir_buff(guild_id, user_id, chave, quantidade=1):
    atual = await obter_buff(guild_id, user_id, chave)
    if atual < quantidade:
        return False
    db = await get_conn()
    await db.execute(
        "UPDATE buffs SET quantidade = quantidade - ? WHERE guild_id = ? AND user_id = ? AND chave = ?",
        (quantidade, guild_id, user_id, chave)
    )
    await db.commit()
    return True


async def remover_buff(guild_id, user_id, chave, quantidade):
    """Remove até `quantidade` de um contador de buff, sem nunca deixar
    negativo (igual remover_coins) -- usado pelo /removergiro pra tirar
    giros grátis dados errado, sem falhar se o ADM pedir mais do que o
    jogador tem sobrando."""
    db = await get_conn()
    await db.execute(
        "UPDATE buffs SET quantidade = MAX(0, quantidade - ?) WHERE guild_id = ? AND user_id = ? AND chave = ?",
        (quantidade, guild_id, user_id, chave)
    )
    await db.commit()


# ========== LOJA (itens customizáveis pelo /configurar) ==========
async def obter_itens_loja(guild_id, somente_ativos=True):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM loja_itens WHERE guild_id = ?"
    if somente_ativos:
        query += " AND ativo = 1"
    query += " ORDER BY ordem ASC, id ASC"
    cursor = await db.execute(query, (guild_id,))
    return [dict(r) for r in await cursor.fetchall()]

async def obter_item_loja(item_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM loja_itens WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None

async def adicionar_item_loja(guild_id, nome, emoji, descricao, preco, tipo, cargo_nome=None, cargo_cor=None, cargo_id=None, buff_chave=None, valor_pix=None):
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO loja_itens (guild_id, nome, emoji, descricao, preco, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, valor_pix) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, nome, emoji, descricao, preco, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, valor_pix)
    )
    await db.commit()
    return cursor.lastrowid

async def remover_item_loja(item_id):
    db = await get_conn()
    await db.execute("DELETE FROM loja_itens WHERE id = ?", (item_id,))
    await db.commit()

async def alternar_item_loja(item_id):
    """Ativa/desativa um item sem precisar deletar (fica escondido da loja, mas guardado)."""
    db = await get_conn()
    await db.execute("UPDATE loja_itens SET ativo = 1 - ativo WHERE id = ?", (item_id,))
    await db.commit()


# Texto padrão da loja quando o ADM ainda não personalizou nada em
# /configurar — mantido simples e limpo (a lista de produtos vai num campo
# separado do embed, não emendada aqui, ver cogs/cassino.py).
LOJA_DESCRICAO_PADRAO = "🪙 Ganhe coins jogando\n🛍️ Gaste em cargos e buffs\n💸 Ou troque por PIX"

_LOJA_CONFIG_PADRAO = {
    "titulo": "🛒 Loja",
    "descricao": LOJA_DESCRICAO_PADRAO,
    "banner": "",
    "cor": "5865F2",
}


async def obter_loja_config(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM loja_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {"guild_id": guild_id, **_LOJA_CONFIG_PADRAO}


async def atualizar_loja_config(guild_id, campo, valor):
    db = await get_conn()
    await db.execute(
        f"INSERT INTO loja_config (guild_id, {campo}) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {campo} = ?",
        (guild_id, valor, valor)
    )
    await db.commit()


# ========== ROLETA (itens sorteáveis + config) ==========
async def obter_itens_roleta(guild_id, somente_ativos=True):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM roleta_itens WHERE guild_id = ?"
    if somente_ativos:
        # Mesma regra da caixa: item pausado OU com estoque zerado some
        # sozinho da roleta, sem precisar mexer em nada manualmente.
        query += " AND ativo = 1 AND (estoque IS NULL OR estoque > 0)"
    cursor = await db.execute(query, (guild_id,))
    return [dict(r) for r in await cursor.fetchall()]

async def obter_item_roleta(item_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM roleta_itens WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None

async def adicionar_item_roleta(guild_id, nome, emoji, tipo, cargo_nome=None, cargo_cor=None, cargo_id=None,
                                 buff_chave=None, coins_qtd=None, peso=10, estoque=None):
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO roleta_itens (guild_id, nome, emoji, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, coins_qtd, peso, estoque) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, nome, emoji, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, coins_qtd, peso, estoque)
    )
    await db.commit()
    return cursor.lastrowid

async def remover_item_roleta(item_id):
    db = await get_conn()
    await db.execute("DELETE FROM roleta_itens WHERE id = ?", (item_id,))
    await db.commit()

async def alternar_item_roleta(item_id):
    db = await get_conn()
    await db.execute("UPDATE roleta_itens SET ativo = 1 - ativo WHERE id = ?", (item_id,))
    await db.commit()

async def consumir_estoque_roleta(item_id) -> bool:
    """Desconta 1 do estoque do prêmio (se tiver estoque limitado). Ao zerar,
    some da roleta sozinho.
    Retorna True se conseguiu reservar (ainda tinha estoque no exato
    momento), False se alguém já ficou com a última unidade um instante
    antes — usado como trava contra 2 pessoas ganharem o mesmo prêmio
    limitado ao girar quase ao mesmo tempo."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE roleta_itens SET estoque = estoque - 1 WHERE id = ? AND estoque IS NOT NULL AND estoque > 0",
        (item_id,)
    )
    await db.commit()
    return cursor.rowcount > 0

async def obter_roleta_config(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM roleta_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {
        "guild_id": guild_id, "custo_giro": 10, "titulo": "🎰 Roleta", "emoji_girar": "🎰",
        "cor1": "B01E1E", "cor2": "781212",
        "descricao": "Gire a roleta e concorra a prêmios!", "banner": "",
    }

async def atualizar_roleta_config(guild_id, campo, valor):
    db = await get_conn()
    await db.execute(
        f"INSERT INTO roleta_config (guild_id, {campo}) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {campo} = ?",
        (guild_id, valor, valor)
    )
    await db.commit()


# ========== CAIXA PREMIADA (mystery box + estoque) ==========
async def obter_itens_caixa(guild_id, somente_ativos=True):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM caixa_itens WHERE guild_id = ?"
    if somente_ativos:
        query += " AND ativo = 1 AND (estoque IS NULL OR estoque > 0)"
    cursor = await db.execute(query, (guild_id,))
    return [dict(r) for r in await cursor.fetchall()]

async def obter_item_caixa(item_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM caixa_itens WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None

async def adicionar_item_caixa(guild_id, nome, emoji, tipo, cargo_nome=None, cargo_cor=None, cargo_id=None,
                                buff_chave=None, coins_qtd=None, valor_pix=None, peso=10, estoque=None):
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO caixa_itens (guild_id, nome, emoji, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, "
        "coins_qtd, valor_pix, peso, estoque) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, nome, emoji, tipo, cargo_nome, cargo_cor, cargo_id, buff_chave, coins_qtd, valor_pix, peso, estoque)
    )
    await db.commit()
    return cursor.lastrowid

async def remover_item_caixa(item_id):
    db = await get_conn()
    await db.execute("DELETE FROM caixa_itens WHERE id = ?", (item_id,))
    await db.commit()

async def alternar_item_caixa(item_id):
    db = await get_conn()
    await db.execute("UPDATE caixa_itens SET ativo = 1 - ativo WHERE id = ?", (item_id,))
    await db.commit()

async def consumir_estoque_caixa(item_id) -> bool:
    """Desconta 1 do estoque do prêmio (se ele tiver estoque limitado). Se
    chegar a 0, some da caixa sozinho.
    Retorna True se conseguiu reservar, False se alguém já ficou com a
    última unidade um instante antes (2 pessoas abrindo quase ao mesmo
    tempo)."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE caixa_itens SET estoque = estoque - 1 WHERE id = ? AND estoque IS NOT NULL AND estoque > 0",
        (item_id,)
    )
    await db.commit()
    return cursor.rowcount > 0

async def obter_caixa_config(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM caixa_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {
        "guild_id": guild_id, "custo_abrir": 10, "titulo": "🎁 Caixa Premiada", "emoji_abrir": "🎁",
        "descricao": "Tente sua sorte! Clique no botão abaixo pra abrir sua caixa e ver o que ganha!",
        "cor": "FFD700", "banner": "",
    }

async def atualizar_caixa_config(guild_id, campo, valor):
    db = await get_conn()
    await db.execute(
        f"INSERT INTO caixa_config (guild_id, {campo}) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {campo} = ?",
        (guild_id, valor, valor)
    )
    await db.commit()


# ========== RANKING ==========
async def add_vitoria(guild_id, vencedor_id, perdedor_id, valor, thread_id):
    db = await get_conn()
    premio_total = valor * 2
    # Lock global: ver comentário em _lock_vitoria. Garante que o INSERT em
    # partidas_finalizadas e os UPDATEs em rankings sempre commitam juntos,
    # mesmo com outras partidas/comandos rodando ao mesmo tempo.
    async with _lock_vitoria:
        try:
            await db.execute(
                "INSERT INTO partidas_finalizadas (thread_id, guild_id, vencedor_id, perdedor_id, valor) VALUES (?, ?, ?, ?, ?)",
                (thread_id, guild_id, vencedor_id, perdedor_id, valor)
            )
            await db.execute(
                "INSERT INTO rankings (user_id, guild_id, vitorias, sequencia, maior_sequencia, ultima_vitoria, maior_premio) "
                "VALUES (?, ?, 1, 1, 1, CURRENT_TIMESTAMP, ?) "
                "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
                "vitorias = vitorias + 1, "
                "sequencia = sequencia + 1, "
                "maior_sequencia = CASE WHEN sequencia + 1 > maior_sequencia THEN sequencia + 1 ELSE maior_sequencia END, "
                "ultima_vitoria = CURRENT_TIMESTAMP, "
                "maior_premio = CASE WHEN ? > maior_premio THEN ? ELSE maior_premio END",
                (vencedor_id, guild_id, premio_total, premio_total, premio_total)
            )
            await db.execute(
                "INSERT INTO rankings (user_id, guild_id, derrotas, sequencia, coins_gastas) VALUES (?, ?, 1, 0, ?) "
                "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
                "derrotas = derrotas + 1, sequencia = 0, coins_gastas = coins_gastas + ?",
                (perdedor_id, guild_id, valor, valor)
            )
            await db.execute(
                "UPDATE rankings SET coins_gastas = coins_gastas + ? WHERE guild_id = ? AND user_id = ?",
                (valor, guild_id, vencedor_id)
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def add_vitoria_wo(guild_id, vencedor_id, perdedor_id, valor, thread_id):
    db = await get_conn()
    premio_total = valor * 2
    # Lock global: ver comentário em _lock_vitoria.
    async with _lock_vitoria:
        try:
            await db.execute(
                "INSERT INTO partidas_finalizadas (thread_id, guild_id, vencedor_id, perdedor_id, valor) VALUES (?, ?, ?, ?, ?)",
                (thread_id, guild_id, vencedor_id, perdedor_id, valor)
            )
            await db.execute(
                "INSERT INTO rankings (user_id, guild_id, vitorias, wins_wo, sequencia, maior_sequencia, ultima_vitoria, maior_premio) "
                "VALUES (?, ?, 1, 1, 1, 1, CURRENT_TIMESTAMP, ?) "
                "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
                "vitorias = vitorias + 1, wins_wo = wins_wo + 1, "
                "sequencia = sequencia + 1, "
                "maior_sequencia = CASE WHEN sequencia + 1 > maior_sequencia THEN sequencia + 1 ELSE maior_sequencia END, "
                "ultima_vitoria = CURRENT_TIMESTAMP, "
                "maior_premio = CASE WHEN ? > maior_premio THEN ? ELSE maior_premio END",
                (vencedor_id, guild_id, premio_total, premio_total, premio_total)
            )
            await db.execute(
                "INSERT INTO rankings (user_id, guild_id, derrotas, losses_wo, sequencia, coins_gastas) VALUES (?, ?, 1, 1, 0, ?) "
                "ON CONFLICT(user_id, guild_id) DO UPDATE SET "
                "derrotas = derrotas + 1, losses_wo = losses_wo + 1, sequencia = 0, coins_gastas = coins_gastas + ?",
                (perdedor_id, guild_id, valor, valor)
            )
            await db.execute(
                "UPDATE rankings SET coins_gastas = coins_gastas + ? WHERE guild_id = ? AND user_id = ?",
                (valor, guild_id, vencedor_id)
            )
            # FIX (coin duplicado): removido o INSERT INTO coins fixo (+1) que
            # existia aqui em add_vitoria/add_vitoria_wo -- ele duplicava o
            # crédito junto com o db.adicionar_coins(coins_por_vitoria) chamado
            # logo depois por ViewDeclararVencedor/ViewDeclararVencedorWO
            # (cogs/painelmediador.py). Resultado: toda vitória (normal ou W.O)
            # creditava 2 coins em vez de 1 (ou o valor configurado em
            # coins_por_vitoria). Agora só existe 1 lugar creditando coin por
            # vitória: o adicionar_coins() de painelmediador.py.
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def recalcular_rankings(guild_id=None):
    """Reconstroi a tabela 'rankings' a partir de partidas_finalizadas (a
    fonte de verdade usada pelo /ranking). Usa isso pra corrigir perfis que
    já ficaram dessincronizados (0 vitórias no /perfil com o /ranking
    mostrando vitórias) por causa do bug corrigido em add_vitoria/add_vitoria_wo.
    Detalhe: partidas_finalizadas não distingue W.O de vitória normal, então
    wins_wo/losses_wo ficam zerados numa reconstrução (só vitorias/derrotas
    totais são recalculadas -- sequencia/maior_sequencia/maior_premio/
    coins_gastas também não dá pra reconstruir com precisão a partir só
    dessa tabela, então ficam zerados/preservados como estavam)."""
    db = await get_conn()
    filtro_guild = " AND guild_id = ?" if guild_id is not None else ""
    params = (guild_id,) if guild_id is not None else ()

    async with _lock_vitoria:
        try:
            cursor = await db.execute(
                "SELECT guild_id, vencedor_id AS uid, COUNT(*) AS n FROM partidas_finalizadas "
                f"WHERE vencedor_id IS NOT NULL{filtro_guild} GROUP BY guild_id, vencedor_id",
                params
            )
            vitorias_rows = await cursor.fetchall()

            cursor = await db.execute(
                "SELECT guild_id, perdedor_id AS uid, COUNT(*) AS n FROM partidas_finalizadas "
                f"WHERE perdedor_id IS NOT NULL{filtro_guild} GROUP BY guild_id, perdedor_id",
                params
            )
            derrotas_rows = await cursor.fetchall()

            for row in vitorias_rows:
                await db.execute(
                    "INSERT INTO rankings (user_id, guild_id, vitorias) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, guild_id) DO UPDATE SET vitorias = ?",
                    (row["uid"], row["guild_id"], row["n"], row["n"])
                )
            for row in derrotas_rows:
                await db.execute(
                    "INSERT INTO rankings (user_id, guild_id, derrotas) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, guild_id) DO UPDATE SET derrotas = ?",
                    (row["uid"], row["guild_id"], row["n"], row["n"])
                )
            await db.commit()
            return len(vitorias_rows) + len(derrotas_rows)
        except Exception:
            await db.rollback()
            raise


async def obter_partidas_abertas(guild_id, limite=50):
    """Lista as filas/partidas ainda em andamento (não canceladas, não
    finalizadas) do servidor -- usado pelo .filasativas em
    cogs/filas_ativas.py. A checagem de thread ainda existir (thread
    excluída no meio da partida sem o registro ser limpo) é feita no cog,
    na hora de montar o embed, não aqui."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM partidas WHERE guild_id = ? AND cancelado = 0 AND finalizado = 0 "
        "ORDER BY thread_id DESC LIMIT ?",
        (guild_id, limite)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_partida(thread_id):
    """Busca a partida (em andamento ou recém finalizada) pelo thread_id."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM partidas WHERE thread_id = ?", (thread_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def proximo_numero_partida(guild_id):
    """Sobe e devolve o próximo número de partida do servidor (1, 2, 3...),
    usado só pro nome do tópico enquanto ela ainda tá em andamento
    (aguardando{n} / fila{n}). Atômico: usa UPSERT, então não duplica número
    mesmo se duas partidas forem criadas ao mesmo tempo."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    await db.execute(
        """
        INSERT INTO numeracao_partidas (guild_id, ultimo) VALUES (?, 1)
        ON CONFLICT(guild_id) DO UPDATE SET ultimo = ultimo + 1
        """,
        (guild_id,)
    )
    await db.commit()
    cursor = await db.execute("SELECT ultimo FROM numeracao_partidas WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    return row["ultimo"] if row else 1


async def salvar_taxa_cobrada(thread_id, taxa):
    """Congela a taxa vigente no momento em que o pagamento foi liberado
    (chamado na confirmação da aposta, em fila.py) — pra o lucro do
    mediador não mudar depois se a taxa for alterada no /configurar."""
    db = await get_conn()
    await db.execute("UPDATE partidas SET taxa_cobrada = ? WHERE thread_id = ?", (taxa, thread_id))
    await db.commit()


async def registrar_partida_mediada(guild_id, thread_id, med_id, valor_aposta, taxa_unitaria, tipo, criado_em=None):
    """Grava 1 partida mediada no histórico, pro ranking de mediadores.
    Lucro = taxa cobrada de CADA jogador x2 (os dois pagam a taxa).
    Se 'criado_em' for passado (partida['criado_em']), calcula também
    quanto tempo essa mediação levou, do início ao fim."""
    db = await get_conn()
    taxa_unitaria = float(taxa_unitaria or 0)
    lucro = round(taxa_unitaria * 2, 2)

    duracao_segundos = None
    if criado_em:
        try:
            inicio = datetime.fromisoformat(criado_em)
            duracao_segundos = int((datetime.utcnow() - inicio).total_seconds())
        except (ValueError, TypeError):
            duracao_segundos = None

    await db.execute(
        "INSERT INTO mediador_partidas (guild_id, thread_id, med_id, valor_aposta, taxa_unitaria, lucro, tipo, duracao_segundos) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, thread_id, med_id, valor_aposta, taxa_unitaria, lucro, tipo, duracao_segundos)
    )
    await db.commit()


async def stats_mediador(guild_id, med_id, desde=None):
    """Totais de um mediador específico (pro /perfil_mediador)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT COUNT(*) AS partidas, "
        "COALESCE(SUM(CASE WHEN tipo = 'wo' THEN 1 ELSE 0 END), 0) AS wo, "
        "COALESCE(SUM(lucro), 0) AS lucro_total "
        "FROM mediador_partidas WHERE guild_id = ? AND med_id = ?"
    )
    params = [guild_id, med_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return dict(row) if row else {"partidas": 0, "wo": 0, "lucro_total": 0}


async def ranking_mediadores(guild_id, desde=None, limite=15):
    """Ranking geral de mediadores por lucro total, no período pedido."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT med_id, COUNT(*) AS partidas, COALESCE(SUM(lucro), 0) AS lucro_total "
        "FROM mediador_partidas WHERE guild_id = ?"
    )
    params = [guild_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    query += " GROUP BY med_id ORDER BY lucro_total DESC LIMIT ?"
    params.append(limite)
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def posicao_ranking_mediador(guild_id, med_id, desde=None):
    """Posição do mediador no ranking geral por lucro (sem limite de topo)."""
    completo = await ranking_mediadores(guild_id, desde=desde, limite=999999)
    for i, row in enumerate(completo, 1):
        if row["med_id"] == med_id:
            return i
    return None


async def obter_ou_criar_painel_fila_med(guild_id):
    """Painel de Ponto interno e automático, usado só pra contar as 'horas
    de fila' dos mediadores (tempo enquanto estão com status Entrar/Sair
    ativo). Aproveita o sistema de ponto já existente em vez de duplicar
    lógica de cronômetro. Identificado por título fixo — some servidores
    também podem ver isso listado no /ponto_painel, o que é esperado."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    titulo_fixo = "⏱️ Fila de Mediadores (auto)"
    cursor = await db.execute(
        "SELECT * FROM ponto_paineis WHERE guild_id = ? AND titulo = ?",
        (guild_id, titulo_fixo)
    )
    row = await cursor.fetchone()
    if row:
        return dict(row)

    config = await obter_config(guild_id)
    cargo_med = config.get("cargo_mediador")
    cursor = await db.execute(
        "INSERT INTO ponto_paineis (guild_id, titulo, cargo_ponto, status) VALUES (?, ?, ?, 'ativo')",
        (guild_id, titulo_fixo, cargo_med)
    )
    await db.commit()
    return {
        "painel_id": cursor.lastrowid, "guild_id": guild_id, "titulo": titulo_fixo,
        "cargo_ponto": cargo_med, "status": "ativo"
    }


# ---------- CANCELAMENTOS ----------
async def registrar_cancelamento_mediador(guild_id, thread_id, med_id, valor_aposta):
    """Grava 1 cancelamento ('Encerrar Aposta') ANTES da partida ser
    deletada — sem isso não sobra nenhum rastro pra calcular a taxa de
    cancelamento do mediador."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO mediador_cancelamentos (guild_id, thread_id, med_id, valor_aposta) VALUES (?, ?, ?, ?)",
        (guild_id, thread_id, med_id, valor_aposta)
    )
    await db.commit()


async def contar_cancelamentos(guild_id, med_id, desde=None):
    db = await get_conn()
    query = "SELECT COUNT(*) FROM mediador_cancelamentos WHERE guild_id = ? AND med_id = ?"
    params = [guild_id, med_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


# ---------- LOG UNIFICADO DE AÇÕES DO MEDIADOR (Win / W.O / Encerrar) ----------
async def registrar_log_acao_mediador(guild_id, thread_id, thread_nome, med_id, tipo,
                                       jogador1_id=None, jogador2_id=None, vencedor_id=None,
                                       valor=0, duracao_segundos=None, transcript_url=None):
    """Grava 1 linha no histórico unificado. 'tipo' = 'win' | 'wo' | 'encerrar'.
    Chamado em paralelo aos registros já existentes (registrar_partida_mediada /
    registrar_cancelamento_mediador), que continuam existindo pra não quebrar
    o lucro/dashboard já calculado em cima deles.
    `transcript_url` (opcional): link da mensagem com o transcript da
    partida, se já tiver sido gerado ANTES desse registro (ver ordem em
    cada chamada, cogs/painelmediador.py). Quando o transcript só é gerado
    DEPOIS, passa None aqui e completa depois com atualizar_transcript_log_acao.
    Retorna o id da linha inserida, pra permitir esse update posterior."""
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO log_acoes_mediador "
        "(guild_id, thread_id, thread_nome, med_id, tipo, jogador1_id, jogador2_id, vencedor_id, valor, duracao_segundos, transcript_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, thread_id, thread_nome, med_id, tipo, jogador1_id, jogador2_id, vencedor_id, valor, duracao_segundos, transcript_url)
    )
    await db.commit()
    return cursor.lastrowid


async def atualizar_transcript_log_acao(log_id: int, transcript_url: str):
    """Completa o transcript_url de uma linha de log_acoes_mediador já
    gravada -- usado quando o transcript só fica pronto DEPOIS do log
    (Win e Win W.O geram o transcript só depois de registrar o log, pra
    dar tempo do painel "Vencedor Definido" postar na thread antes)."""
    if not transcript_url:
        return
    db = await get_conn()
    await db.execute(
        "UPDATE log_acoes_mediador SET transcript_url = ? WHERE id = ?",
        (transcript_url, log_id)
    )
    await db.commit()


async def obter_logs_filas_jogador(guild_id, user_id, limite=15):
    """Histórico de filas (Win/W.O/Encerrar) em que o usuário participou
    como jogador1 ou jogador2 -- usado pelo .logs @user (cogs/logs_membro.py),
    diferente de obter_logs_mediador que filtra pelo mediador (med_id)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM log_acoes_mediador WHERE guild_id = ? AND (jogador1_id = ? OR jogador2_id = ?) "
        "ORDER BY data DESC LIMIT ?",
        (guild_id, user_id, user_id, limite)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_logs_mediador(guild_id, med_id=None, tipo=None, desde=None, limite=10, offset=0):
    """Lista paginada do histórico unificado, mais recente primeiro.
    med_id=None junta TODOS os mediadores (útil pro /exportar_logs geral)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM log_acoes_mediador WHERE guild_id = ?"
    params = [guild_id]
    if med_id is not None:
        query += " AND med_id = ?"
        params.append(med_id)
    if tipo:
        query += " AND tipo = ?"
        params.append(tipo)
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    query += " ORDER BY data DESC LIMIT ? OFFSET ?"
    params.extend([limite, offset])
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def contar_salas_criadas_mediador(guild_id, med_id, desde=None):
    """Quantas salas o mediador REALMENTE liberou (evento 'iniciada' em
    apostas_eventos, gravado quando o ID+senha é enviado -- ver comentário
    'FIX' em cogs/painelmediador.py:_enviar_sala_liberada). Usado pelo
    painel de Rendimento (cogs/pix.py).

    ANTES o Rendimento usava contar_logs_mediador aqui, que conta
    QUALQUER linha de log_acoes_mediador do mediador (win, wo E encerrar,
    sem filtro de tipo) -- ou seja, uma aposta CANCELADA (encerrar, sem
    vencedor, sem taxa cobrada) contava como "sala criada" do mesmo jeito
    que uma sala de verdade, inflando esse número e sem bater com
    Filas mediadas/Rendimento (que só contam win/wo). Trocado pra usar a
    mesma fonte que .apostas já usa pra 'Salas criadas' geral
    (apostas_eventos tipo='iniciada'), só que filtrada por med_id."""
    db = await get_conn()
    query = "SELECT COUNT(*) FROM apostas_eventos WHERE guild_id = ? AND tipo = 'iniciada' AND med_id = ?"
    params = [guild_id, med_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


async def contar_logs_mediador(guild_id, med_id=None, tipo=None, desde=None):
    """Total de linhas que batem com o mesmo filtro de obter_logs_mediador,
    pra calcular quantas páginas existem."""
    db = await get_conn()
    query = "SELECT COUNT(*) FROM log_acoes_mediador WHERE guild_id = ?"
    params = [guild_id]
    if med_id is not None:
        query += " AND med_id = ?"
        params.append(med_id)
    if tipo:
        query += " AND tipo = ?"
        params.append(tipo)
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


# ---------- MULTAS ----------
async def registrar_evento_multa(guild_id, user_id, evento):
    """evento = 'aplicada' ou 'removida'. Chamado pelo listener de
    on_member_update que detecta a troca do cargo_multa (aplicado/removido
    manualmente pelo ADM direto no Discord)."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO mediador_multas (guild_id, user_id, evento) VALUES (?, ?, ?)",
        (guild_id, user_id, evento)
    )
    await db.commit()


async def criar_multa(guild_id, user_id, admin_id, motivo, valor, chave_pagamento):
    """Cria o registro completo de uma multa aplicada pelo /painelmulta (ou
    pelo .bloquear). Fica com status='ativa' até o mediador confirmar
    pagamento e o ADM validar no canal de logs. Retorna o id da multa."""
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO mediador_multas "
        "(guild_id, user_id, evento, motivo, valor, admin_id, chave_pagamento, status) "
        "VALUES (?, ?, 'aplicada', ?, ?, ?, ?, 'ativa')",
        (guild_id, user_id, motivo, valor, admin_id, chave_pagamento)
    )
    await db.commit()
    return cursor.lastrowid


async def obter_multa_ativa(guild_id, user_id):
    """A multa em aberto mais recente desse usuário (status ativa OU
    aguardando_confirmacao). Como só existe 1 cargo_multa por servidor,
    tratamos como 1 multa em aberto por vez por usuário."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM mediador_multas WHERE guild_id = ? AND user_id = ? "
        "AND status IN ('ativa', 'aguardando_confirmacao') "
        "ORDER BY data DESC LIMIT 1",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def obter_multa_por_id(multa_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM mediador_multas WHERE id = ?", (multa_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def marcar_multa_aguardando_confirmacao(multa_id):
    """Mediador clicou 'Já Paguei' -- muda status pra que o ADM veja o
    botão de confirmação no canal de logs."""
    db = await get_conn()
    await db.execute(
        "UPDATE mediador_multas SET status = 'aguardando_confirmacao' WHERE id = ?",
        (multa_id,)
    )
    await db.commit()


async def confirmar_pagamento_multa(multa_id, confirmado_por):
    """ADM confirma no canal de logs -- fecha o ciclo: status='paga',
    grava quem confirmou e quando."""
    db = await get_conn()
    await db.execute(
        "UPDATE mediador_multas SET status = 'paga', pago_em = CURRENT_TIMESTAMP, "
        "confirmado_por = ?, evento = 'removida' WHERE id = ?",
        (confirmado_por, multa_id)
    )
    await db.commit()


async def remover_multa_manual(multa_id):
    """Botão 'Remover Multa' do painel -- ADM tira sem exigir confirmação
    de pagamento (perdão, engano, etc)."""
    db = await get_conn()
    await db.execute(
        "UPDATE mediador_multas SET status = 'removida_manual', evento = 'removida' WHERE id = ?",
        (multa_id,)
    )
    await db.commit()


async def contar_multas(guild_id, user_id, desde=None):
    """Quantas vezes esse usuário JÁ FOI multado (evento='aplicada')."""
    db = await get_conn()
    query = "SELECT COUNT(*) FROM mediador_multas WHERE guild_id = ? AND user_id = ? AND evento = 'aplicada'"
    params = [guild_id, user_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


async def historico_multas(guild_id, user_id, limite=10):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM mediador_multas WHERE guild_id = ? AND user_id = ? ORDER BY data DESC LIMIT ?",
        (guild_id, user_id, limite)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def listar_multas_pendentes(guild_id):
    """Todas as multas em aberto (status ativa OU aguardando_confirmacao) do
    servidor, de todos os mediadores -- usado pelo botão de listagem do
    painel de multa (painel_multa.py)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM mediador_multas WHERE guild_id = ? "
        "AND status IN ('ativa', 'aguardando_confirmacao') "
        "ORDER BY data ASC",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------- STATS COMPLETOS (pro dashboard) ----------
async def stats_completo_mediador(guild_id, med_id, desde=None):
    """Todos os números do dashboard pra 1 mediador: partidas, win/wo,
    lucro, tempo médio de mediação, maior partida já mediada e cancelamentos."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT COUNT(*) AS partidas, "
        "COALESCE(SUM(CASE WHEN tipo = 'wo' THEN 1 ELSE 0 END), 0) AS wo, "
        "COALESCE(SUM(CASE WHEN tipo = 'win' THEN 1 ELSE 0 END), 0) AS win, "
        "COALESCE(SUM(lucro), 0) AS lucro_total, "
        "COALESCE(AVG(duracao_segundos), 0) AS tempo_medio_segundos, "
        "COALESCE(MAX(valor_aposta), 0) AS maior_partida "
        "FROM mediador_partidas WHERE guild_id = ? AND med_id = ?"
    )
    params = [guild_id, med_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    stats = dict(row) if row else {
        "partidas": 0, "wo": 0, "win": 0, "lucro_total": 0,
        "tempo_medio_segundos": 0, "maior_partida": 0
    }
    stats["cancelamentos"] = await contar_cancelamentos(guild_id, med_id, desde)
    stats["multas"] = await contar_multas(guild_id, med_id, desde)
    total_finalizadas = stats["partidas"] + stats["cancelamentos"]
    stats["taxa_cancelamento"] = round((stats["cancelamentos"] / total_finalizadas) * 100, 1) if total_finalizadas else 0.0
    return stats


async def sequencia_dias_ativos(guild_id, med_id):
    """Quantos dias SEGUIDOS (até hoje ou ontem) o mediador mediou pelo
    menos 1 partida. Se o último dia ativo foi anteontem ou antes, a
    sequência já quebrou e volta pra 0."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT DISTINCT date(data) AS dia FROM mediador_partidas "
        "WHERE guild_id = ? AND med_id = ? ORDER BY dia DESC",
        (guild_id, med_id)
    )
    dias = [row[0] for row in await cursor.fetchall()]
    if not dias:
        return 0

    hoje = datetime.utcnow().date()
    primeiro_dia = datetime.fromisoformat(dias[0]).date()
    if (hoje - primeiro_dia).days > 1:
        return 0

    sequencia = 1
    esperado = primeiro_dia
    for dia_str in dias[1:]:
        dia = datetime.fromisoformat(dia_str).date()
        esperado = esperado - timedelta(days=1)
        if dia == esperado:
            sequencia += 1
        else:
            break
    return sequencia


async def atividade_por_dia_hora(guild_id, med_id=None, desde=None):
    """Matriz de atividade: quantas partidas mediadas em cada dia da
    semana (0=domingo) e em cada hora (0-23). Se med_id for None, é a
    atividade do SERVIDOR inteiro (todos os mediadores juntos)."""
    db = await get_conn()
    query = (
        "SELECT CAST(strftime('%w', data) AS INTEGER) AS dia_semana, "
        "CAST(strftime('%H', data) AS INTEGER) AS hora, COUNT(*) AS total "
        "FROM mediador_partidas WHERE guild_id = ?"
    )
    params = [guild_id]
    if med_id is not None:
        query += " AND med_id = ?"
        params.append(med_id)
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    query += " GROUP BY dia_semana, hora"
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [{"dia_semana": r[0], "hora": r[1], "total": r[2]} for r in rows]


async def comparativo_mediadores(guild_id, desde=None, limite=10):
    """Ranking estendido com várias métricas lado a lado, pra comparar
    mediadores (não só por lucro como o /ranking_mediadores original)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT med_id, COUNT(*) AS partidas, "
        "COALESCE(SUM(CASE WHEN tipo = 'wo' THEN 1 ELSE 0 END), 0) AS wo, "
        "COALESCE(SUM(lucro), 0) AS lucro_total, "
        "COALESCE(AVG(duracao_segundos), 0) AS tempo_medio_segundos, "
        "COALESCE(MAX(valor_aposta), 0) AS maior_partida "
        "FROM mediador_partidas WHERE guild_id = ?"
    )
    params = [guild_id]
    if desde:
        query += " AND datetime(data) >= datetime(?)"
        params.append(desde)
    query += " GROUP BY med_id ORDER BY lucro_total DESC LIMIT ?"
    params.append(limite)
    cursor = await db.execute(query, params)
    rows = [dict(r) for r in await cursor.fetchall()]
    for row in rows:
        row["cancelamentos"] = await contar_cancelamentos(guild_id, row["med_id"], desde)
    return rows


# ---------- CONFIG DO PAINEL /dashboard_mediador ----------
_DASHBOARD_MED_CONFIG_PADRAO = {
    "titulo": "📊 Dashboard de Mediadores",
    "descricao": "Acompanhe as estatísticas completas dos mediadores.",
    "cor": "0099FF",
    "rodape": "",
    "label_perfil": "Meu Perfil", "emoji_perfil": "👤",
    "label_atividade": "Atividade", "emoji_atividade": "📈",
    "label_emblemas": "Emblemas", "emoji_emblemas": "🏅",
    "label_comparativo": "Comparativo", "emoji_comparativo": "⚖️",
}


async def obter_dashboard_med_config(guild_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM dashboard_med_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return {"guild_id": guild_id, **_DASHBOARD_MED_CONFIG_PADRAO}


async def salvar_dashboard_med_config(guild_id, **campos):
    atual = await obter_dashboard_med_config(guild_id)
    atual.update(campos)
    db = await get_conn()
    await db.execute(
        "INSERT INTO dashboard_med_config (guild_id, titulo, descricao, cor, rodape, "
        "label_perfil, emoji_perfil, label_atividade, emoji_atividade, "
        "label_emblemas, emoji_emblemas, label_comparativo, emoji_comparativo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "titulo=excluded.titulo, descricao=excluded.descricao, cor=excluded.cor, rodape=excluded.rodape, "
        "label_perfil=excluded.label_perfil, emoji_perfil=excluded.emoji_perfil, "
        "label_atividade=excluded.label_atividade, emoji_atividade=excluded.emoji_atividade, "
        "label_emblemas=excluded.label_emblemas, emoji_emblemas=excluded.emoji_emblemas, "
        "label_comparativo=excluded.label_comparativo, emoji_comparativo=excluded.emoji_comparativo",
        (guild_id, atual["titulo"], atual["descricao"], atual["cor"], atual["rodape"],
         atual["label_perfil"], atual["emoji_perfil"], atual["label_atividade"], atual["emoji_atividade"],
         atual["label_emblemas"], atual["emoji_emblemas"], atual["label_comparativo"], atual["emoji_comparativo"])
    )
    await db.commit()


async def obter_ranking_geral(guild_id, limite=10):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT user_id, (vitorias + wins_wo) as total_wins, (derrotas + losses_wo) as total_losses, "
        "wins_wo, losses_wo FROM rankings WHERE guild_id = ? ORDER BY total_wins DESC LIMIT ?",
        (guild_id, limite)
    )
    return await cursor.fetchall()


async def obter_ranking_diario(guild_id):
    """Retorna os vencedores de HOJE (antes do reset), baseado em partidas_finalizadas."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("""
        SELECT vencedor_id, COUNT(*) as vitorias_hoje
        FROM partidas_finalizadas
        WHERE guild_id = ? AND date(data) = date('now')
        GROUP BY vencedor_id
        ORDER BY vitorias_hoje DESC
    """, (guild_id,))
    return await cursor.fetchall()


async def obter_perfil(guild_id, user_id):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM rankings WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    coins = await obter_saldo(guild_id, user_id)
    if row:
        row = dict(row)
        row['coins'] = coins
        row['total_wins'] = row['vitorias'] + row['wins_wo']
        row['total_losses'] = row['derrotas'] + row['losses_wo']
        return row
    return {
        'vitorias': 0, 'derrotas': 0, 'wins_wo': 0, 'losses_wo': 0,
        'total_wins': 0, 'total_losses': 0,
        'sequencia': 0, 'maior_sequencia': 0, 'maior_premio': 0, 'coins': coins
    }


async def resetar_ranking(guild_id):
    db = await get_conn()
    await db.execute("DELETE FROM rankings WHERE guild_id = ?", (guild_id,))
    await db.commit()


# ========== PAINEL MED ==========
async def obter_config_painel(guild_id, tipo):
    config = await obter_config(guild_id)
    if tipo == "painel_med":
        return config.get('canal_painel_med'), config.get('msg_painel_med')
    elif tipo == "painel_pix":
        return config.get('canal_painel_pix'), config.get('msg_painel_pix')
    return None, None


async def setar_canal_painel(guild_id, tipo, canal_id, msg_id):
    if tipo == "painel_med":
        await atualizar_config(guild_id, 'canal_painel_med', canal_id)
        await atualizar_config(guild_id, 'msg_painel_med', msg_id)
    elif tipo == "painel_pix":
        await atualizar_config(guild_id, 'canal_painel_pix', canal_id)
        await atualizar_config(guild_id, 'msg_painel_pix', msg_id)


# ========== FUNÇÕES AUXILIARES RANKING ==========
async def verificar_mvp_semana(guild_id, user_id):
    """Retorna True se o user foi MVP (mais vitórias) na semana atual."""
    db = await get_conn()
    cursor = await db.execute("""
        SELECT vencedor_id, COUNT(*) as total
        FROM partidas_finalizadas
        WHERE guild_id = ? AND data >= date('now', '-7 days')
        GROUP BY vencedor_id
        ORDER BY total DESC
        LIMIT 1
    """, (guild_id,))
    row = await cursor.fetchone()
    return row is not None and row[0] == user_id


async def obter_ranking_periodo(guild_id: int, periodo: str, tipo: str, limit: int = 50):
    """Ranking com filtro de período (diario/semanal/mensal/total) e tipo
    (vitorias/derrotas), usando o histórico real de partidas_finalizadas
    (não mexe na tabela 'rankings' usada pelo /perfil, que continua igual)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row

    coluna = "vencedor_id" if tipo == "vitorias" else "perdedor_id"

    where_data = ""
    params = [guild_id]
    if periodo == "diario":
        where_data = " AND date(data) = date('now', 'localtime')"
    elif periodo == "semanal":
        where_data = " AND data >= datetime('now', '-7 days', 'localtime')"
    elif periodo == "mensal":
        where_data = " AND strftime('%Y-%m', data) = strftime('%Y-%m', 'now', 'localtime')"
    # "total" não filtra nada

    query = (
        f"SELECT {coluna} AS user_id, COUNT(*) AS total FROM partidas_finalizadas "
        f"WHERE guild_id = ?{where_data} AND {coluna} IS NOT NULL "
        f"GROUP BY {coluna} ORDER BY total DESC LIMIT ?"
    )
    params.append(limit)

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_posicao_periodo(guild_id: int, user_id: int, periodo: str, tipo: str):
    """Posição do usuário dentro do ranking filtrado (sem limite de top 50).

    Antes chamava obter_ranking_periodo() de novo com limit=999999 e
    procurava o usuário em Python -- só que montar_embed() já tinha
    acabado de rodar essa MESMA query (com limit=50) pra montar a página.
    Ou seja, todo /ranking (e cada clique de página) fazia o scan pesado
    de partidas_finalizadas duas vezes. Agora calcula a posição direto no
    SQLite com RANK(), numa query só, sem trazer o ranking inteiro pro
    Python."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row

    coluna = "vencedor_id" if tipo == "vitorias" else "perdedor_id"

    where_data = ""
    if periodo == "diario":
        where_data = " AND date(data) = date('now', 'localtime')"
    elif periodo == "semanal":
        where_data = " AND data >= datetime('now', '-7 days', 'localtime')"
    elif periodo == "mensal":
        where_data = " AND strftime('%Y-%m', data) = strftime('%Y-%m', 'now', 'localtime')"
    # "total" não filtra nada

    query = f"""
        SELECT posicao FROM (
            SELECT {coluna} AS uid, RANK() OVER (ORDER BY COUNT(*) DESC) AS posicao
            FROM partidas_finalizadas
            WHERE guild_id = ?{where_data} AND {coluna} IS NOT NULL
            GROUP BY {coluna}
        ) WHERE uid = ?
    """
    cursor = await db.execute(query, (guild_id, user_id))
    row = await cursor.fetchone()
    return row[0] if row else None


RANKING_CONFIG_PADRAO = {
    "titulo": "🏆 Ranking de Jogadores",
    "descricao": "",
    "rodape": "FFZ E-SPORTS SYSTEM",
    "cor": 0xF1C40F,
    "emoji_ouro": "🥇",
    "emoji_prata": "🥈",
    "emoji_bronze": "🥉",
    "nome_marca": "E-SPORTS",
    "destaque_ativo": 1,
    "destaque_horario": "00:00",
    "ultimo_destaque": None,
    "emoji_perfil": "👤",
    "emoji_ranking": "🏆",
    # 0 (não None!) representa "sem cargo configurado" -- salvar_ranking_config
    # ignora valores None pra não sobrescrever campo que não foi passado, então
    # se usasse None aqui não teria como o usuário DESMARCAR o cargo depois de
    # já ter escolhido um (o campo simplesmente nunca mudaria de volta).
    "cargo_premio_top1": 0,
}


async def obter_ranking_config(guild_id: int) -> dict:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ranking_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    config = dict(RANKING_CONFIG_PADRAO)
    if row:
        for chave in config:
            if row[chave] is not None:
                config[chave] = row[chave]
    return config


async def salvar_ranking_config(guild_id: int, **campos):
    """Atualiza só os campos passados, mantendo o resto como já estava
    (ou o padrão, se ainda não existir linha)."""
    atual = await obter_ranking_config(guild_id)
    atual.update({k: v for k, v in campos.items() if v is not None})

    db = await get_conn()
    await db.execute(
        "INSERT INTO ranking_config (guild_id, titulo, descricao, rodape, cor, emoji_ouro, emoji_prata, emoji_bronze, "
        "nome_marca, destaque_ativo, destaque_horario, ultimo_destaque, emoji_perfil, emoji_ranking, cargo_premio_top1) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "titulo=excluded.titulo, descricao=excluded.descricao, rodape=excluded.rodape, cor=excluded.cor, "
        "emoji_ouro=excluded.emoji_ouro, emoji_prata=excluded.emoji_prata, emoji_bronze=excluded.emoji_bronze, "
        "nome_marca=excluded.nome_marca, destaque_ativo=excluded.destaque_ativo, "
        "destaque_horario=excluded.destaque_horario, ultimo_destaque=excluded.ultimo_destaque, "
        "emoji_perfil=excluded.emoji_perfil, emoji_ranking=excluded.emoji_ranking, "
        "cargo_premio_top1=excluded.cargo_premio_top1",
        (guild_id, atual["titulo"], atual["descricao"], atual["rodape"], atual["cor"],
         atual["emoji_ouro"], atual["emoji_prata"], atual["emoji_bronze"],
         atual["nome_marca"], atual["destaque_ativo"], atual["destaque_horario"], atual["ultimo_destaque"],
         atual["emoji_perfil"], atual["emoji_ranking"], atual["cargo_premio_top1"])
    )
    await db.commit()
    return atual


async def obter_posicao_geral(guild_id: int, user_id: int):
    """Retorna a posição do user no ranking geral por vitórias totais."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM rankings WHERE guild_id = ? ORDER BY (vitorias + wins_wo) DESC",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    for i, row in enumerate(rows, 1):
        if row[0] == user_id:
            return i
    return "-"


async def obter_posicao_mensal(guild_id: int, user_id: int, tipo: str = "vitorias"):
    """Posição do usuário no ranking do mês atual (usa a mesma lógica de
    obter_posicao_periodo, já existente, filtrando periodo='mensal')."""
    posicao = await obter_posicao_periodo(guild_id, user_id, "mensal", tipo)
    return posicao if posicao is not None else "-"


# ========== SISTEMA DE LICENÇA (KEYS) — REESTRUTURADO ==========
# Keys nunca mais ficam salvas em texto puro no banco. Motivo real: o
# ffz_esports.db inteiro sai como anexo cru pra DM do dono todo backup
# horário (painel_owner._enviar_backup) — se esse arquivo vazar por
# qualquer motivo (DM comprometida, anexo compartilhado sem querer,
# device perdido), toda key já gerada (usada ou ainda em estoque) ficava
# exposta em texto puro pra quem pegasse o arquivo, mesmo sem acesso ao
# bot. Agora só se guarda um HASH (HMAC-SHA256 com pepper próprio do bot)
# -- MESMO com o arquivo do banco em mãos, não dá pra recuperar a key
# original nem gerar uma key válida sem o pepper (que não sai no backup,
# fica só na env var ou, na falta dela, gerado uma única vez e persistido
# separado — ver _obter_pepper_licenca). A key completa em texto só existe
# no exato momento em que é gerada (retorno de criar_chave) e na tela do
# dono quando ele recebe o /gerarkey -- depois disso, nunca mais.
import secrets
import string
import hmac
import hashlib

_pepper_licenca_cache: str | None = None


async def _obter_pepper_licenca() -> str:
    """Segredo usado pra 'temperar' o hash das keys (HMAC), pra que nem
    quem tiver o banco consiga forçar/gerar hashes válidos sem esse valor.
    Prioridade: env var LICENCA_PEPPER (mais seguro, não viaja com o
    banco/backup de jeito nenhum) -- se não tiver configurada, gera um
    valor aleatório na primeira vez e persiste em bot_meta (ainda MUITO
    melhor que texto puro sem pepper nenhum, mas se puder, configure a
    env var no Discloud pra esse segredo nunca estar no mesmo arquivo que
    os hashes que ele protege)."""
    global _pepper_licenca_cache
    if _pepper_licenca_cache:
        return _pepper_licenca_cache

    pepper = os.environ.get("LICENCA_PEPPER")
    if not pepper:
        pepper = await get_meta("licenca_pepper")
        if not pepper:
            pepper = secrets.token_hex(32)
            await set_meta("licenca_pepper", pepper)
            print(
                "🔐 LICENCA_PEPPER não configurado na env var -- gerei um "
                "novo automaticamente e salvei no banco (bot_meta). "
                "Recomendado: mover esse valor pra uma env var no Discloud "
                "pra ele nunca ficar no mesmo arquivo que os backups do banco."
            )
    _pepper_licenca_cache = pepper
    return pepper


async def _hash_chave(chave: str) -> str:
    pepper = await _obter_pepper_licenca()
    return hmac.new(pepper.encode("utf-8"), chave.strip().upper().encode("utf-8"), hashlib.sha256).hexdigest()


def _mascarar_chave(chave: str) -> str:
    """FFZ-HACKER-X7K2-A9M4-QW3R -> FFZ-HACKER-X7K2-****-**** (mostra só o
    prefixo/plano + o primeiro bloco, o resto vira asterisco). Suficiente
    pra reconhecer/achar a key numa lista, insuficiente pra alguém usar."""
    partes = chave.strip().upper().split("-")
    if len(partes) <= 3:
        return chave  # formato inesperado, devolve como veio (defensivo)
    visivel = partes[:3]
    mascarado = ["*" * len(p) for p in partes[3:]]
    return "-".join(visivel + mascarado)


def _gerar_codigo_chave() -> str:
    """Gera o código aleatório da key usando secrets (criptograficamente
    seguro — NÃO usar random/random.choices aqui, é previsível)."""
    alfabeto = string.ascii_uppercase + string.digits
    blocos = ["".join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(3)]
    return "-".join(blocos)


async def _migrar_chaves_para_hash(db):
    """Migração ÚNICA (roda uma vez só, detecta pela presença da coluna
    chave_hash): reconstrói a tabela 'chaves' pra guardar hash em vez de
    texto puro. A tabela antiga é RENOMEADA (não apagada) pra
    'chaves_texto_puro_pre_migracao', como rede de segurança -- dá pra
    conferir que a migração saiu certa e só então apagar manualmente essa
    tabela antiga quando quiser (ela é a ÚNICA coisa no banco daqui pra
    frente que ainda vai ter as keys antigas em texto puro, então vale
    apagar depois de confirmar que tá tudo certo)."""
    cursor = await db.execute("PRAGMA table_info(chaves)")
    colunas = [linha[1] for linha in await cursor.fetchall()]
    if "chave_hash" in colunas:
        return  # já migrado

    cursor = await db.execute("SELECT COUNT(*) FROM chaves")
    total = (await cursor.fetchone())[0]
    print(f"🔐 Migrando {total} key(s) da tabela 'chaves' pra formato hasheado (nunca mais texto puro)...")

    await db.execute("""
        CREATE TABLE chaves_novo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave_hash TEXT NOT NULL UNIQUE,
            chave_mascarada TEXT NOT NULL,
            plano TEXT NOT NULL,
            dias INTEGER NOT NULL,
            criada_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            criada_por INTEGER,
            usado_por INTEGER,
            usado_em TIMESTAMP,
            vence TIMESTAMP,
            cancelada INTEGER DEFAULT 0
        )
    """)

    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM chaves")
    linhas = await cursor.fetchall()
    for linha in linhas:
        linha = dict(linha)
        chave_plana = linha["chave"]
        await db.execute(
            "INSERT INTO chaves_novo "
            "(chave_hash, chave_mascarada, plano, dias, criada_em, criada_por, usado_por, usado_em, vence, cancelada) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                await _hash_chave(chave_plana), _mascarar_chave(chave_plana),
                linha["plano"], linha["dias"], linha["criada_em"], linha["criada_por"],
                linha["usado_por"], linha["usado_em"], linha["vence"], linha["cancelada"],
            )
        )

    await db.execute("ALTER TABLE chaves RENAME TO chaves_texto_puro_pre_migracao")
    await db.execute("ALTER TABLE chaves_novo RENAME TO chaves")

    # Backfill em 'assinaturas': liga cada servidor já ativo à nova linha
    # (por id) e preenche a versão mascarada, sem depender mais da coluna
    # 'chave' antiga (que continua existindo, só não é mais usada).
    cursor = await db.execute("SELECT guild_id, chave FROM assinaturas WHERE chave IS NOT NULL")
    for guild_id, chave_antiga in await cursor.fetchall():
        cursor2 = await db.execute("SELECT id, chave_mascarada FROM chaves WHERE chave_hash = ?", (await _hash_chave(chave_antiga),))
        row = await cursor2.fetchone()
        if row:
            await db.execute(
                "UPDATE assinaturas SET chave_id = ?, chave_mascarada = ? WHERE guild_id = ?",
                (row["id"], row["chave_mascarada"], guild_id)
            )

    await db.commit()
    print(
        f"✅ Migração de keys concluída ({total} registro(s)). A tabela antiga foi preservada "
        f"como 'chaves_texto_puro_pre_migracao' -- confira se tá tudo certo e pode apagar ela manualmente depois."
    )


async def criar_chave(plano: str, dias: int, criada_por: int) -> str:
    """Cria uma key nova e retorna ela em TEXTO PURO -- essa é a ÚNICA vez
    que isso acontece; a partir daqui só o hash fica salvo no banco. Quem
    chama essa função (ex: /gerarkey) precisa mostrar esse retorno pro
    dono NA HORA, porque não tem como recuperar depois."""
    db = await get_conn()
    while True:
        chave = f"FFZ-{plano.upper()}-{_gerar_codigo_chave()}"
        hash_ = await _hash_chave(chave)
        cursor = await db.execute("SELECT 1 FROM chaves WHERE chave_hash = ?", (hash_,))
        if not await cursor.fetchone():
            break
    await db.execute(
        "INSERT INTO chaves (chave_hash, chave_mascarada, plano, dias, criada_por) VALUES (?, ?, ?, ?, ?)",
        (hash_, _mascarar_chave(chave), plano.upper(), dias, criada_por)
    )
    await db.commit()
    return chave


async def obter_chave(chave: str):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (await _hash_chave(chave),))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_chaves(limit: int = 50):
    """Retorna as keys mais recentes -- SEM a key em texto puro (não existe
    mais no banco pra devolver). Cada item vem com 'id' (pra referenciar
    em /cancelarkey) e 'chave_mascarada' (pra exibir com segurança)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM chaves ORDER BY criada_em DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def ativar_chave(chave: str, guild_id: int, ativado_por: int):
    """Ativação ATÔMICA: o UPDATE só afeta a linha se 'usado_por' ainda
    estiver NULL. Se rowcount vier 0, outra pessoa já pegou essa key no
    mesmo instante (ou ela já foi usada/cancelada) — evita duas guilds
    reivindicando a mesma key numa corrida.

    Retorna (sucesso: bool, motivo: str, dados: dict|None)
    """
    chave = chave.strip().upper()
    hash_ = await _hash_chave(chave)
    db = await get_conn()
    db.row_factory = aiosqlite.Row

    cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (hash_,))
    row = await cursor.fetchone()
    if not row:
        return False, "nao_encontrada", None
    if row["cancelada"]:
        return False, "cancelada", None
    if row["usado_por"] is not None:
        return False, "ja_usada", None

    dias = row["dias"]
    plano = row["plano"]
    vence = datetime.now() + timedelta(days=dias)
    vence_str = vence.strftime("%Y-%m-%d %H:%M:%S")

    cursor = await db.execute(
        "UPDATE chaves SET usado_por = ?, usado_em = CURRENT_TIMESTAMP, vence = ? "
        "WHERE chave_hash = ? AND usado_por IS NULL",
        (guild_id, vence_str, hash_)
    )
    if cursor.rowcount == 0:
        # Perdeu a corrida pra outra ativação simultânea
        await db.commit()
        return False, "ja_usada", None

    await db.execute(
        "INSERT INTO assinaturas (guild_id, plano, ativo, vence, chave_id, chave_mascarada, ativado_por, ativado_em, avisado_vencimento) "
        "VALUES (?, ?, 1, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "plano = excluded.plano, ativo = 1, vence = excluded.vence, "
        "chave_id = excluded.chave_id, chave_mascarada = excluded.chave_mascarada, "
        "ativado_por = excluded.ativado_por, ativado_em = CURRENT_TIMESTAMP, "
        "avisado_vencimento = 0",
        (guild_id, plano, vence_str, row["id"], row["chave_mascarada"], ativado_por)
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return True, "ok", {"plano": plano, "dias": dias, "vence": vence_str}


async def cancelar_chave(identificador: str):
    """Cancela uma key. Aceita tanto o ID numérico (mostrado em
    /listarkeys, sempre funciona) quanto a key completa em texto puro (só
    funciona se quem digitou ainda tiver ela anotada -- o bot não consegue
    mais mostrar a key completa de novo depois de gerada). Se ela já
    tiver sido usada por um servidor, revoga o acesso desse servidor
    também."""
    identificador = identificador.strip()
    db = await get_conn()
    db.row_factory = aiosqlite.Row

    if identificador.isdigit():
        cursor = await db.execute("SELECT * FROM chaves WHERE id = ?", (int(identificador),))
    else:
        cursor = await db.execute("SELECT * FROM chaves WHERE chave_hash = ?", (await _hash_chave(identificador),))
    dados = await cursor.fetchone()
    if not dados:
        return False

    if dados["usado_por"]:
        await db.execute("UPDATE assinaturas SET ativo = 0 WHERE guild_id = ?", (dados["usado_por"],))
        _cache_assinaturas.pop(dados["usado_por"], None)
    await db.execute("UPDATE chaves SET cancelada = 1 WHERE id = ?", (dados["id"],))
    await db.commit()
    return True


async def obter_assinatura(guild_id: int):
    if guild_id in _cache_assinaturas:
        return _cache_assinaturas[guild_id]

    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM assinaturas WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    resultado = dict(row) if row else None
    _cache_assinaturas[guild_id] = resultado
    return resultado


async def obter_assinatura_fresca(guild_id: int):
    """Igual a obter_assinatura(), mas IGNORA o cache e sempre lê o banco
    na hora.

    BLINDAGEM (licença "vazando" pra servidor sem key): usada só pelo
    gate de licença (licenca.verificar_licenca), que roda em TODOS os 64
    comandos protegidos por @requer_licenca() nos 21 cogs do bot. Licença
    é checada raras vezes comparado a outras leituras (fila, config etc.),
    então cachear aqui não traz ganho de performance que justifique o
    risco — e é justamente o tipo de leitura onde "confiar num cache que
    pode estar desatualizado" é mais perigoso: se dois processos do bot
    chegarem a ficar de pé ao mesmo tempo com o mesmo token (deploy
    sobreposto, restart que não matou o processo antigo etc.), cada um
    tem seu PRÓPRIO cache em memória, sem comunicação entre si — só o
    banco (compartilhado pelos dois) é fonte de verdade confiável. Ler
    sempre fresco aqui garante que a checagem de licença nunca libera
    passagem baseada em estado velho, não importa quantos processos do
    bot estejam de pé.

    Roda numa conexão SEPARADA da principal (ver _conn_licenca lá em cima),
    então mesmo que a conexão principal esteja ocupada com um lote de
    escritas (automod em pico de mensagens, por exemplo), essa leitura
    não fica enfileirada atrás delas — WAL permite leitor e escritor
    andando em paralelo de verdade quando são conexões diferentes.
    """
    db = await _get_conn_licenca()
    cursor = await db.execute("SELECT * FROM assinaturas WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    resultado = dict(row) if row else None
    _cache_assinaturas[guild_id] = resultado  # mantém o cache coerente pra quem MAIS ler (ex: painel de status)
    return resultado


async def desativar_assinatura(guild_id: int):
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET ativo = 0 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def revogar_licenca(guild_id: int):
    """Bloqueia manualmente um servidor (ex: chargeback, calote)."""
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET ativo = 0 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def reativar_licenca(guild_id: int):
    """Reativa um servidor sem mexer na data de validade já salva."""
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET ativo = 1 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def liberar_licenca_manual(guild_id: int, plano: str, dias: int, liberado_por: int):
    """Libera acesso manualmente SEM consumir key do estoque
    (ex: teste, parceria, cortesia)."""
    db = await get_conn()
    vence = (datetime.now() + timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "INSERT INTO assinaturas (guild_id, plano, ativo, vence, chave, ativado_por, ativado_em, avisado_vencimento) "
        "VALUES (?, ?, 1, ?, NULL, ?, CURRENT_TIMESTAMP, 0) "
        "ON CONFLICT(guild_id) DO UPDATE SET "
        "plano = excluded.plano, ativo = 1, vence = excluded.vence, ativado_por = excluded.ativado_por, "
        "ativado_em = CURRENT_TIMESTAMP, avisado_vencimento = 0",
        (guild_id, plano.upper(), vence, liberado_por)
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return vence


async def estender_licenca(guild_id: int, dias: int):
    """Estende a validade de um servidor somando dias a partir do MAIOR
    valor entre 'agora' e a validade atual já salva. Isso evita o bug de
    'estender' resetar a data pro zero e o cliente perder os dias que
    ainda tinha (ex: faltavam 20 dias, dono clica 'estender 30' achando
    que vai somar, e o cliente acaba só com 30 em vez de 50).
    Também reativa o servidor e zera o aviso de vencimento, pra ele
    poder ser avisado de novo no novo prazo."""
    db = await get_conn()
    assinatura = await obter_assinatura(guild_id)
    base = datetime.now()
    if assinatura and assinatura["vence"]:
        try:
            vence_atual = datetime.strptime(assinatura["vence"], "%Y-%m-%d %H:%M:%S")
            if vence_atual > base:
                base = vence_atual
        except ValueError:
            pass
    nova_vence = (base + timedelta(days=dias)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE assinaturas SET vence = ?, ativo = 1, avisado_vencimento = 0 WHERE guild_id = ?",
        (nova_vence, guild_id)
    )
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)
    return nova_vence


async def vincular_cliente(user_id: int, guild_id: int, guild_nome: str, plano: str, cadastrado_por: int):
    """Cria ou atualiza o vínculo comprador -> servidor. Suporta MÚLTIPLOS
    servidores por user_id ao mesmo tempo -- cadastrar de novo pro MESMO
    par (user_id, guild_id) atualiza esse vínculo específico (ex: corrigir
    plano/nome); cadastrar esse mesmo user_id pra um guild_id DIFERENTE
    adiciona um vínculo novo, sem apagar os anteriores."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO clientes_registrados (user_id, guild_id, guild_nome, plano, cadastrado_por, cadastrado_em) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(user_id, guild_id) DO UPDATE SET guild_nome = excluded.guild_nome, "
        "plano = excluded.plano, cadastrado_por = excluded.cadastrado_por, cadastrado_em = CURRENT_TIMESTAMP",
        (user_id, guild_id, guild_nome, plano, cadastrado_por)
    )
    await db.commit()


async def obter_vinculos_cliente(user_id: int) -> list[dict]:
    """TODOS os servidores vinculados a esse comprador (pode ser mais de
    um), do cadastro mais recente pro mais antigo. Lista vazia se ele
    nunca foi cadastrado via .shop. Use isso (e não obter_vinculo_cliente)
    em qualquer lugar novo que precise lidar com múltiplos vínculos."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM clientes_registrados WHERE user_id = ? ORDER BY cadastrado_em DESC",
        (user_id,)
    )
    linhas = await cursor.fetchall()
    return [dict(r) for r in linhas]


async def obter_vinculo_cliente(user_id: int, guild_id: int | None = None):
    """Devolve UM vínculo só. Com guild_id, esse par específico; sem
    guild_id, o cadastro mais recente desse usuário (mantido pra código
    antigo que ainda assume 1 servidor por cliente). Prefira
    obter_vinculos_cliente() em código novo, que já lida com o caso de
    múltiplos servidores por cliente."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    if guild_id is not None:
        cursor = await db.execute(
            "SELECT * FROM clientes_registrados WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM clientes_registrados WHERE user_id = ? ORDER BY cadastrado_em DESC LIMIT 1",
            (user_id,)
        )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def remover_vinculo_cliente(user_id: int, guild_id: int | None = None) -> int:
    """Remove vínculo(s) desse comprador. Com guild_id, remove só aquele
    vínculo específico (os outros servidores dele, se tiver, continuam
    vinculados). Sem guild_id, remove TODOS os vínculos desse user_id --
    usado quando ele não quer mais ser cliente de nenhum servidor.
    Devolve quantas linhas foram apagadas."""
    db = await get_conn()
    if guild_id is not None:
        cursor = await db.execute(
            "DELETE FROM clientes_registrados WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id)
        )
    else:
        cursor = await db.execute(
            "DELETE FROM clientes_registrados WHERE user_id = ?",
            (user_id,)
        )
    await db.commit()
    return cursor.rowcount


async def listar_clientes_guild(guild_id: int) -> list[dict]:
    """Todos os clientes (user_ids) vinculados a esse servidor específico,
    do cadastro mais recente pro mais antigo."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM clientes_registrados WHERE guild_id = ? ORDER BY cadastrado_em DESC",
        (guild_id,)
    )
    linhas = await cursor.fetchall()
    return [dict(r) for r in linhas]


async def obter_precos_plano(plano: str) -> dict:
    """Retorna {dias: preco} pra um plano (ex: 'BASIC'), já ordenado.
    Usado pra montar o menu de renovação com os preços atuais."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT dias, preco FROM planos_precos WHERE plano = ? ORDER BY dias", (plano.upper(),)
    )
    linhas = await cursor.fetchall()
    return {row["dias"]: row["preco"] for row in linhas}


async def definir_preco_plano(plano: str, dias: int, preco: float):
    """Cria ou atualiza o preço de uma combinação (plano, dias). Usado
    pelo /configurarprecos -- o dono ajusta valor sem precisar redeploy."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO planos_precos (plano, dias, preco) VALUES (?, ?, ?) "
        "ON CONFLICT(plano, dias) DO UPDATE SET preco = excluded.preco",
        (plano.upper(), dias, preco)
    )
    await db.commit()


async def criar_pagamento_pix(payment_id: int, guild_id: int, user_id: int, plano: str, dias: int, valor: float, canal_id: int = None):
    """Registra uma cobrança Pix recém-criada no Mercado Pago. `payment_id`
    é o ID que a API do MP devolveu -- é a chave que o webhook usa depois
    pra saber pra qual guild/quantos dias creditar quando o pagamento for
    aprovado."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO pagamentos_pix (id, guild_id, user_id, plano, dias, valor, status, canal_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (payment_id, guild_id, user_id, plano.upper(), dias, valor, canal_id)
    )
    await db.commit()


async def obter_pagamento_pix(payment_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM pagamentos_pix WHERE id = ?", (payment_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def marcar_pagamento_processado(payment_id: int, status: str):
    """Marca o pagamento como 'approved'/'rejected'/etc, com timestamp de
    quando foi processado. O webhook SEMPRE checa esse status antes de
    creditar dias de novo -- evita creditar duas vezes se o Mercado Pago
    reenviar a mesma notificação (ele reenvia em caso de timeout)."""
    db = await get_conn()
    await db.execute(
        "UPDATE pagamentos_pix SET status = ?, processado_em = CURRENT_TIMESTAMP WHERE id = ?",
        (status, payment_id)
    )
    await db.commit()


async def criar_pagamento_manual(guild_id: int, user_id: int, plano: str, dias: int, valor: float, canal_id: int = None) -> int:
    """Registra uma solicitação de renovação via Pix MANUAL (chave Pix
    do dono). Não existe payment_id de nenhuma API aqui -- o id é gerado
    pelo próprio SQLite (AUTOINCREMENT) e a linha nasce 'pendente',
    esperando o dono aprovar ou recusar pela DM. Retorna o id criado."""
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO pagamentos_manuais (guild_id, user_id, plano, dias, valor, status, canal_id) "
        "VALUES (?, ?, ?, ?, ?, 'pendente', ?)",
        (guild_id, user_id, plano.upper(), dias, valor, canal_id)
    )
    await db.commit()
    return cursor.lastrowid


async def obter_pagamento_manual(pagamento_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM pagamentos_manuais WHERE id = ?", (pagamento_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def marcar_pagamento_manual(pagamento_id: int, status: str, processado_por: int):
    """status: 'aprovado' ou 'recusado'. Idempotência é responsabilidade
    de quem chama (ver AprovacaoManualView.on_click em renovacao.py) --
    aqui só grava."""
    db = await get_conn()
    await db.execute(
        "UPDATE pagamentos_manuais SET status = ?, processado_em = CURRENT_TIMESTAMP, processado_por = ? WHERE id = ?",
        (status, processado_por, pagamento_id)
    )
    await db.commit()


async def remover_preco_plano(plano: str, dias: int):
    """Apaga uma combinação (plano, dias) da tabela de preços -- usado
    pelo painel de configuração pra tirar uma opção de renovação do ar."""
    db = await get_conn()
    await db.execute("DELETE FROM planos_precos WHERE plano = ? AND dias = ?", (plano.upper(), dias))
    await db.commit()


async def obter_todos_precos() -> list[dict]:
    """Todos os preços cadastrados, de todos os planos -- usado no preview
    do painel de configuração da renovação."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT plano, dias, preco FROM planos_precos ORDER BY plano, dias")
    linhas = await cursor.fetchall()
    return [dict(row) for row in linhas]


async def obter_licencas_vencendo(dias: int = 3):
    """Licenças ativas que vencem dentro de X dias e ainda não foram avisadas."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM assinaturas WHERE ativo = 1 AND avisado_vencimento = 0 "
        "AND vence IS NOT NULL AND vence <= datetime('now', 'localtime', ?)",        (f"+{dias} days",)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_avisado_vencimento(guild_id: int):
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET avisado_vencimento = 1 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def resetar_aviso_vencimento(guild_id: int):
    """Chamado sempre que a licença é renovada, pra poder avisar de novo
    no próximo vencimento."""
    db = await get_conn()
    await db.execute("UPDATE assinaturas SET avisado_vencimento = 0 WHERE guild_id = ?", (guild_id,))
    await db.commit()
    _cache_assinaturas.pop(guild_id, None)


async def listar_servidores_licenciados(limit: int = 50):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM assinaturas ORDER BY ativado_em DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


    


# ========================================================================
# SS (SOLICITAR ANÁLISE) — funções integradas do Bot de solicitar SS
# ========================================================================
async def evento_ja_processado(evento_id: int) -> bool:
    """Versão PERSISTENTE de dedupe.ja_processado() -- ver comentário da
    tabela eventos_processados acima. Insere o id como PRIMARY KEY: se der
    certo, é a primeira vez que esse evento passa por aqui (retorna False,
    quem chamou pode seguir normalmente); se estourar erro de chave
    duplicada, outro processo (ou essa mesma chamada, reentregue) já
    processou esse id (retorna True, quem chamou deve abortar sem repetir
    a ação)."""
    db = await get_conn()
    try:
        await db.execute("INSERT INTO eventos_processados (evento_id) VALUES (?)", (evento_id,))
        await db.commit()
    except Exception:
        return True

    # Limpeza oportunista (mesmo espírito do dedupe.py em memória): de vez
    # em quando apaga o que já passou da janela de retenção, sem precisar
    # de uma tasks.loop dedicada só pra isso.
    import random
    if random.random() < 0.02:
        try:
            await db.execute("DELETE FROM eventos_processados WHERE criado_em < datetime('now', '-1 day')")
            await db.commit()
        except Exception:
            pass
    return False


async def obter_ss_config(guild_id: int) -> dict:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ss_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO ss_config (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM ss_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    return dict(row)


async def atualizar_ss_config(guild_id: int, **kwargs):
    if not kwargs:
        return await obter_ss_config(guild_id)
    await obter_ss_config(guild_id)  # garante que a linha existe
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(f"UPDATE ss_config SET {campos} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    await db.commit()
    return await obter_ss_config(guild_id)


# ========== FILA DE ANALISTAS SS (mesmo modelo da fila_mediadores) ==========
async def obter_fila_ss(guild_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM fila_ss WHERE guild_id = ? AND modalidade = ? ORDER BY posicao ASC",
        (guild_id, modalidade)
    )
    return [row[0] for row in await cursor.fetchall()]


async def entrar_fila_ss(guild_id: int, user_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COALESCE(MAX(posicao), 0) + 1 FROM fila_ss WHERE guild_id = ? AND modalidade = ?",
        (guild_id, modalidade)
    )
    nova_posicao = (await cursor.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO fila_ss (guild_id, user_id, modalidade, posicao) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, modalidade, nova_posicao)
    )
    await db.commit()


async def sair_fila_ss(guild_id: int, user_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT posicao FROM fila_ss WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    row = await cursor.fetchone()
    if not row:
        return
    posicao_saindo = row[0]
    await db.execute(
        "DELETE FROM fila_ss WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    await db.execute(
        "UPDATE fila_ss SET posicao = posicao - 1 WHERE guild_id = ? AND modalidade = ? AND posicao > ?",
        (guild_id, modalidade, posicao_saindo)
    )
    await db.commit()


async def esta_na_fila_ss(guild_id: int, user_id: int, modalidade: str) -> bool:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT 1 FROM fila_ss WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    return await cursor.fetchone() is not None


async def limpar_fila_ss(guild_id: int, modalidade: str = None):
    """Sem modalidade, limpa as duas filas (mobile e emulador) do servidor."""
    db = await get_conn()
    if modalidade:
        await db.execute("DELETE FROM fila_ss WHERE guild_id = ? AND modalidade = ?", (guild_id, modalidade))
    else:
        await db.execute("DELETE FROM fila_ss WHERE guild_id = ?", (guild_id,))
    await db.commit()


async def puxar_proximo_ss(guild_id: int, modalidade: str):
    """Puxa o analista da frente da fila da modalidade pedida e o reenvia
    direto pro FINAL da mesma fila -- mesmo comportamento round-robin do
    pegar_proximo_mediador (cogs/fila.py) pra fila de mediadores, aplicado
    aqui pra fila de SS: quem é puxado não desaparece da fila, só perde a
    vez pros próximos até rodar de novo.

    O DELETE...RETURNING resolve a escolha e a remoção da posição antiga
    num único statement (pra duas solicitações simultâneas nunca puxarem
    o mesmo analista); o re-INSERT no final é o que dá a vez aos outros.
    Retorna o user_id puxado, ou None se a fila estiver vazia."""
    db = await get_conn()
    cursor = await db.execute(
        """DELETE FROM fila_ss
           WHERE guild_id = ? AND modalidade = ? AND posicao = (
               SELECT MIN(posicao) FROM fila_ss WHERE guild_id = ? AND modalidade = ?
           )
           RETURNING user_id, posicao""",
        (guild_id, modalidade, guild_id, modalidade)
    )
    row = await cursor.fetchone()
    if not row:
        await db.commit()
        return None
    user_id, posicao_removida = row
    await db.execute(
        "UPDATE fila_ss SET posicao = posicao - 1 WHERE guild_id = ? AND modalidade = ? AND posicao > ?",
        (guild_id, modalidade, posicao_removida)
    )
    # Reenvia pro final da fila (mesma modalidade) -- calcula a próxima
    # posição livre já sem o analista puxado (que acabou de ser removido
    # acima), então ele sempre vai pro fim de verdade, atrás de quem já
    # estava esperando.
    cursor2 = await db.execute(
        "SELECT COALESCE(MAX(posicao), 0) + 1 FROM fila_ss WHERE guild_id = ? AND modalidade = ?",
        (guild_id, modalidade)
    )
    nova_posicao = (await cursor2.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO fila_ss (guild_id, user_id, modalidade, posicao) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, modalidade, nova_posicao)
    )
    await db.commit()
    return user_id


async def registrar_ss_solicitacao(guild_id: int, solicitante_id: int, jogador_id: int, modalidade: str, origem_id: int) -> int:
    """Cria o log no momento em que a solicitação é criada (/solicitar),
    ainda SEM analista definido (isso é marcado depois por ss_assumir_log,
    quando alguém clica em "Assumir análise").

    Guardar a linha desde a criação — mesmo antes de alguém assumir — é o
    que permite reconstruir os botões depois de um restart do bot (ver
    obter_ss_pendentes e obter_ss_em_analise, usadas em bot.py). Antes essa
    função só era chamada na hora de assumir, então uma solicitação parada
    (sem ninguém assumir) não deixava rastro nenhum no banco.

    Retorna o id do log, pra depois: (1) marcar quem assumiu com
    ss_assumir_log, (2) guardar o id da mensagem de resultado com
    definir_ss_message_id_resultado, e (3) fechar com registrar_ss_resultado."""
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO ss_logs (guild_id, solicitante_id, jogador_id, modalidade, origem_id) VALUES (?, ?, ?, ?, ?)",
        (guild_id, solicitante_id, jogador_id, modalidade, origem_id)
    )
    await db.commit()
    return cursor.lastrowid


async def definir_ss_message_id_solicitacao(log_id: int, message_id: int):
    """Guarda o id da mensagem do painel 'Assumir análise', depois que ela
    já foi enviada (precisa do log_id pra existir a view, e da view pra
    existir a mensagem — por isso isso é um passo separado do INSERT)."""
    db = await get_conn()
    await db.execute("UPDATE ss_logs SET message_id_solicitacao = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def ss_assumir_log(log_id: int, analista_id: int):
    """Marca quem assumiu a análise (chamado pelo botão 'Assumir análise' e
    também quando o /solicitar puxa alguém direto da fila) e carimba
    assumido_em, pra dar pra montar a linha do tempo completa no log."""
    db = await get_conn()
    await db.execute(
        "UPDATE ss_logs SET analista_id = ?, assumido_em = CURRENT_TIMESTAMP WHERE id = ?",
        (analista_id, log_id)
    )
    await db.commit()


async def definir_ss_message_id_resultado(log_id: int, message_id: int):
    """Guarda o id da mensagem de resultado (botões Limpo/W.O), pelo mesmo
    motivo do message_id_solicitacao acima."""
    db = await get_conn()
    await db.execute("UPDATE ss_logs SET message_id_resultado = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def obter_ss_pendentes():
    """Solicitações que ainda não foram assumidas por ninguém — usado no
    startup do bot pra re-registrar os botões 'Assumir análise' que
    sobreviveram a um restart."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ss_logs WHERE analista_id IS NULL AND resultado IS NULL AND message_id_solicitacao IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_ss_em_analise():
    """Análises já assumidas mas ainda sem resultado — usado no startup do
    bot pra re-registrar os botões 'Limpo' / 'W.O' que sobreviveram a um
    restart."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ss_logs WHERE analista_id IS NOT NULL AND resultado IS NULL AND message_id_resultado IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_ss_log(log_id: int):
    """Busca uma linha de ss_logs pelo id (usado pra ler o message_id_log
    atual antes de decidir se edita ou cria uma mensagem nova no canal de logs)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ss_logs WHERE id = ?", (log_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_ss_message_id_log(log_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE ss_logs SET message_id_log = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def registrar_ss_resultado(log_id: int, resultado: str):
    """resultado: 'limpo' ou 'wo'"""
    db = await get_conn()
    await db.execute(
        "UPDATE ss_logs SET resultado = ?, decidido_em = CURRENT_TIMESTAMP WHERE id = ?",
        (resultado, log_id)
    )
    await db.commit()


async def registrar_ss_wo(
    guild_id: int, alvo_id: int, registrado_por_id: int, origem: str,
    motivo: str = None, provas: list = None, log_id: int = None,
) -> int:
    """Registra um W.O com provas. origem: 'comando' (.wo do mediador/analista)
    ou 'painel' (botão W.O do painel de análise). Retorna o id do registro."""
    db = await get_conn()
    cursor = await db.execute(
        """INSERT INTO ss_wo_registros
           (guild_id, alvo_id, registrado_por_id, origem, log_id, motivo, provas)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, alvo_id, registrado_por_id, origem, log_id, motivo, json.dumps(provas or [])),
    )
    await db.commit()
    return cursor.lastrowid


async def resetar_ss_wo(guild_id: int, alvo_id: int = None) -> int:
    """Remove registros de W.O. Se alvo_id vier None, apaga TODOS os W.O do
    servidor (uso do .resetwo geral); caso contrário, apaga só os do alvo
    (.resetwo @usuário). Retorna quantos registros foram removidos."""
    db = await get_conn()
    if alvo_id is None:
        cursor = await db.execute(
            "DELETE FROM ss_wo_registros WHERE guild_id = ?", (guild_id,)
        )
    else:
        cursor = await db.execute(
            "DELETE FROM ss_wo_registros WHERE guild_id = ? AND alvo_id = ?",
            (guild_id, alvo_id),
        )
    await db.commit()
    return cursor.rowcount


async def remover_ss_wo_por_id(guild_id: int, registro_id: int) -> bool:
    """Remove um único registro de W.O pelo ID (pra corrigir um lançamento
    errado sem precisar zerar o histórico inteiro do usuário)."""
    db = await get_conn()
    cursor = await db.execute(
        "DELETE FROM ss_wo_registros WHERE guild_id = ? AND id = ?",
        (guild_id, registro_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def obter_ss_wo_historico(guild_id: int, alvo_id: int, limite: int = 25) -> list:
    """Todos os W.O registrados pra esse membro nesse servidor, mais recente primeiro."""
    db = await get_conn()
    cursor = await db.execute(
        """SELECT * FROM ss_wo_registros
           WHERE guild_id = ? AND alvo_id = ?
           ORDER BY criado_em DESC LIMIT ?""",
        (guild_id, alvo_id, limite),
    )
    rows = await cursor.fetchall()
    resultado = []
    for row in rows:
        item = dict(row)
        try:
            item["provas"] = json.loads(item.get("provas") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["provas"] = []
        resultado.append(item)
    return resultado


async def obter_ss_ranking(guild_id: int, limit: int = 10, offset: int = 0):
    """Ranking de analistas: total de análises, quantos limpos, quantos W.O."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT analista_id,
               COUNT(*) AS total,
               SUM(CASE WHEN resultado = 'limpo' THEN 1 ELSE 0 END) AS limpos,
               SUM(CASE WHEN resultado = 'wo' THEN 1 ELSE 0 END) AS wos
        FROM ss_logs
        WHERE guild_id = ? AND analista_id IS NOT NULL
        GROUP BY analista_id
        ORDER BY total DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def contar_ss_analistas(guild_id: int) -> int:
    """Quantos analistas distintos já constam no ranking (pra calcular paginação)."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT analista_id) FROM ss_logs WHERE guild_id = ? AND analista_id IS NOT NULL",
        (guild_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def obter_ss_stats_analista(guild_id: int, analista_id: int) -> dict:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN resultado = 'limpo' THEN 1 ELSE 0 END) AS limpos,
               SUM(CASE WHEN resultado = 'wo' THEN 1 ELSE 0 END) AS wos
        FROM ss_logs WHERE guild_id = ? AND analista_id = ?
        """,
        (guild_id, analista_id)
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"total": 0, "limpos": 0, "wos": 0}


# ========================================================================
# TELADOR — funções (espelho de SS, sistema de análise de tela dos Teladores)
# ========================================================================
async def obter_tel_config(guild_id: int) -> dict:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM tel_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO tel_config (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM tel_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    return dict(row)


async def atualizar_tel_config(guild_id: int, **kwargs):
    if not kwargs:
        return await obter_tel_config(guild_id)
    await obter_tel_config(guild_id)  # garante que a linha existe
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(f"UPDATE tel_config SET {campos} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    await db.commit()
    return await obter_tel_config(guild_id)


# ========== FILA DE ANALISTAS Telador (mesmo modelo da fila_mediadores) ==========
async def obter_fila_tel(guild_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM fila_tel WHERE guild_id = ? AND modalidade = ? ORDER BY posicao ASC",
        (guild_id, modalidade)
    )
    return [row[0] for row in await cursor.fetchall()]


async def entrar_fila_tel(guild_id: int, user_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COALESCE(MAX(posicao), 0) + 1 FROM fila_tel WHERE guild_id = ? AND modalidade = ?",
        (guild_id, modalidade)
    )
    nova_posicao = (await cursor.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO fila_tel (guild_id, user_id, modalidade, posicao) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, modalidade, nova_posicao)
    )
    await db.commit()


async def sair_fila_tel(guild_id: int, user_id: int, modalidade: str):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT posicao FROM fila_tel WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    row = await cursor.fetchone()
    if not row:
        return
    posicao_saindo = row[0]
    await db.execute(
        "DELETE FROM fila_tel WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    await db.execute(
        "UPDATE fila_tel SET posicao = posicao - 1 WHERE guild_id = ? AND modalidade = ? AND posicao > ?",
        (guild_id, modalidade, posicao_saindo)
    )
    await db.commit()


async def esta_na_fila_tel(guild_id: int, user_id: int, modalidade: str) -> bool:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT 1 FROM fila_tel WHERE guild_id = ? AND user_id = ? AND modalidade = ?",
        (guild_id, user_id, modalidade)
    )
    return await cursor.fetchone() is not None


async def limpar_fila_tel(guild_id: int, modalidade: str = None):
    """Sem modalidade, limpa as duas filas (mobile e emulador) do servidor."""
    db = await get_conn()
    if modalidade:
        await db.execute("DELETE FROM fila_tel WHERE guild_id = ? AND modalidade = ?", (guild_id, modalidade))
    else:
        await db.execute("DELETE FROM fila_tel WHERE guild_id = ?", (guild_id,))
    await db.commit()


async def puxar_proximo_tel(guild_id: int, modalidade: str):
    """Puxa o analista da frente da fila da modalidade pedida e o reenvia
    direto pro FINAL da mesma fila -- mesmo comportamento round-robin do
    pegar_proximo_mediador (cogs/fila.py) pra fila de mediadores, aplicado
    aqui pra fila de Telador: quem é puxado não desaparece da fila, só perde a
    vez pros próximos até rodar de novo.

    O DELETE...RETURNING resolve a escolha e a remoção da posição antiga
    num único statement (pra duas solicitações simultâneas nunca puxarem
    o mesmo analista); o re-INSERT no final é o que dá a vez aos outros.
    Retorna o user_id puxado, ou None se a fila estiver vazia."""
    db = await get_conn()
    cursor = await db.execute(
        """DELETE FROM fila_tel
           WHERE guild_id = ? AND modalidade = ? AND posicao = (
               SELECT MIN(posicao) FROM fila_tel WHERE guild_id = ? AND modalidade = ?
           )
           RETURNING user_id, posicao""",
        (guild_id, modalidade, guild_id, modalidade)
    )
    row = await cursor.fetchone()
    if not row:
        await db.commit()
        return None
    user_id, posicao_removida = row
    await db.execute(
        "UPDATE fila_tel SET posicao = posicao - 1 WHERE guild_id = ? AND modalidade = ? AND posicao > ?",
        (guild_id, modalidade, posicao_removida)
    )
    # Reenvia pro final da fila (mesma modalidade) -- calcula a próxima
    # posição livre já sem o analista puxado (que acabou de ser removido
    # acima), então ele sempre vai pro fim de verdade, atrás de quem já
    # estava esperando.
    cursor2 = await db.execute(
        "SELECT COALESCE(MAX(posicao), 0) + 1 FROM fila_tel WHERE guild_id = ? AND modalidade = ?",
        (guild_id, modalidade)
    )
    nova_posicao = (await cursor2.fetchone())[0]
    await db.execute(
        "INSERT OR IGNORE INTO fila_tel (guild_id, user_id, modalidade, posicao) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, modalidade, nova_posicao)
    )
    await db.commit()
    return user_id


async def registrar_tel_solicitacao(guild_id: int, solicitante_id: int, jogador_id: int, modalidade: str, origem_id: int) -> int:
    """Cria o log no momento em que a solicitação é criada (/solicitar),
    ainda SEM analista definido (isso é marcado depois por tel_assumir_log,
    quando alguém clica em "Assumir análise").

    Guardar a linha desde a criação — mesmo antes de alguém assumir — é o
    que permite reconstruir os botões depois de um restart do bot (ver
    obter_tel_pendentes e obter_tel_em_analise, usadas em bot.py). Antes essa
    função só era chamada na hora de assumir, então uma solicitação parada
    (sem ninguém assumir) não deixava rastro nenhum no banco.

    Retorna o id do log, pra depois: (1) marcar quem assumiu com
    tel_assumir_log, (2) guardar o id da mensagem de resultado com
    definir_tel_message_id_resultado, e (3) fechar com registrar_tel_resultado."""
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO tel_logs (guild_id, solicitante_id, jogador_id, modalidade, origem_id) VALUES (?, ?, ?, ?, ?)",
        (guild_id, solicitante_id, jogador_id, modalidade, origem_id)
    )
    await db.commit()
    return cursor.lastrowid


async def definir_tel_message_id_solicitacao(log_id: int, message_id: int):
    """Guarda o id da mensagem do painel 'Assumir análise', depois que ela
    já foi enviada (precisa do log_id pra existir a view, e da view pra
    existir a mensagem — por isso isso é um passo separado do INSERT)."""
    db = await get_conn()
    await db.execute("UPDATE tel_logs SET message_id_solicitacao = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def tel_assumir_log(log_id: int, analista_id: int):
    """Marca quem assumiu a análise (chamado pelo botão 'Assumir análise' e
    também quando o /solicitar puxa alguém direto da fila) e carimba
    assumido_em, pra dar pra montar a linha do tempo completa no log."""
    db = await get_conn()
    await db.execute(
        "UPDATE tel_logs SET analista_id = ?, assumido_em = CURRENT_TIMESTAMP WHERE id = ?",
        (analista_id, log_id)
    )
    await db.commit()


async def definir_tel_message_id_resultado(log_id: int, message_id: int):
    """Guarda o id da mensagem de resultado (botões Limpo/W.O), pelo mesmo
    motivo do message_id_solicitacao acima."""
    db = await get_conn()
    await db.execute("UPDATE tel_logs SET message_id_resultado = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def obter_tel_pendentes():
    """Solicitações que ainda não foram assumidas por ninguém — usado no
    startup do bot pra re-registrar os botões 'Assumir análise' que
    sobreviveram a um restart."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tel_logs WHERE analista_id IS NULL AND resultado IS NULL AND message_id_solicitacao IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_tel_em_analise():
    """Análises já assumidas mas ainda sem resultado — usado no startup do
    bot pra re-registrar os botões 'Limpo' / 'W.O' que sobreviveram a um
    restart."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tel_logs WHERE analista_id IS NOT NULL AND resultado IS NULL AND message_id_resultado IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_tel_log(log_id: int):
    """Busca uma linha de tel_logs pelo id (usado pra ler o message_id_log
    atual antes de decidir se edita ou cria uma mensagem nova no canal de logs)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM tel_logs WHERE id = ?", (log_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_tel_message_id_log(log_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE tel_logs SET message_id_log = ? WHERE id = ?", (message_id, log_id))
    await db.commit()


async def registrar_tel_resultado(log_id: int, resultado: str):
    """resultado: 'limpo' ou 'wo'"""
    db = await get_conn()
    await db.execute(
        "UPDATE tel_logs SET resultado = ?, decidido_em = CURRENT_TIMESTAMP WHERE id = ?",
        (resultado, log_id)
    )
    await db.commit()


async def registrar_tel_wo(
    guild_id: int, alvo_id: int, registrado_por_id: int, origem: str,
    motivo: str = None, provas: list = None, log_id: int = None,
) -> int:
    """Registra um W.O com provas. origem: 'comando' (.wo do mediador/analista)
    ou 'painel' (botão W.O do painel de análise). Retorna o id do registro."""
    db = await get_conn()
    cursor = await db.execute(
        """INSERT INTO tel_wo_registros
           (guild_id, alvo_id, registrado_por_id, origem, log_id, motivo, provas)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, alvo_id, registrado_por_id, origem, log_id, motivo, json.dumps(provas or [])),
    )
    await db.commit()
    return cursor.lastrowid


async def resetar_tel_wo(guild_id: int, alvo_id: int = None) -> int:
    """Remove registros de W.O. Se alvo_id vier None, apaga TODOS os W.O do
    servidor (uso do .resetwo geral); caso contrário, apaga só os do alvo
    (.resetwo @usuário). Retorna quantos registros foram removidos."""
    db = await get_conn()
    if alvo_id is None:
        cursor = await db.execute(
            "DELETE FROM tel_wo_registros WHERE guild_id = ?", (guild_id,)
        )
    else:
        cursor = await db.execute(
            "DELETE FROM tel_wo_registros WHERE guild_id = ? AND alvo_id = ?",
            (guild_id, alvo_id),
        )
    await db.commit()
    return cursor.rowcount


async def remover_tel_wo_por_id(guild_id: int, registro_id: int) -> bool:
    """Remove um único registro de W.O pelo ID (pra corrigir um lançamento
    errado sem precisar zerar o histórico inteiro do usuário)."""
    db = await get_conn()
    cursor = await db.execute(
        "DELETE FROM tel_wo_registros WHERE guild_id = ? AND id = ?",
        (guild_id, registro_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def obter_tel_wo_historico(guild_id: int, alvo_id: int, limite: int = 25) -> list:
    """Todos os W.O registrados pra esse membro nesse servidor, mais recente primeiro."""
    db = await get_conn()
    cursor = await db.execute(
        """SELECT * FROM tel_wo_registros
           WHERE guild_id = ? AND alvo_id = ?
           ORDER BY criado_em DESC LIMIT ?""",
        (guild_id, alvo_id, limite),
    )
    rows = await cursor.fetchall()
    resultado = []
    for row in rows:
        item = dict(row)
        try:
            item["provas"] = json.loads(item.get("provas") or "[]")
        except (json.JSONDecodeError, TypeError):
            item["provas"] = []
        resultado.append(item)
    return resultado


async def obter_tel_ranking(guild_id: int, limit: int = 10, offset: int = 0):
    """Ranking de analistas: total de análises, quantos limpos, quantos W.O."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT analista_id,
               COUNT(*) AS total,
               SUM(CASE WHEN resultado = 'limpo' THEN 1 ELSE 0 END) AS limpos,
               SUM(CASE WHEN resultado = 'wo' THEN 1 ELSE 0 END) AS wos
        FROM tel_logs
        WHERE guild_id = ? AND analista_id IS NOT NULL
        GROUP BY analista_id
        ORDER BY total DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, limit, offset)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def contar_tel_teladores(guild_id: int) -> int:
    """Quantos analistas distintos já constam no ranking (pra calcular paginação)."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(DISTINCT analista_id) FROM tel_logs WHERE guild_id = ? AND analista_id IS NOT NULL",
        (guild_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def obter_tel_stats_telador(guild_id: int, analista_id: int) -> dict:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN resultado = 'limpo' THEN 1 ELSE 0 END) AS limpos,
               SUM(CASE WHEN resultado = 'wo' THEN 1 ELSE 0 END) AS wos
        FROM tel_logs WHERE guild_id = ? AND analista_id = ?
        """,
        (guild_id, analista_id)
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"total": 0, "limpos": 0, "wos": 0}



# ========================================================================
# ANÚNCIOS — funções integradas do Bot de anúncios
# ========================================================================
async def criar_anuncio(guild_id: int, nome: str, canal_id: int | None = None):
    """canal_id aqui é só legado (mantém a coluna antiga preenchida por
    compatibilidade) — os canais de verdade de um anúncio (podem ser vários)
    ficam em anuncios_canais, definidos separadamente via definir_canais_anuncio."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    # novo anúncio sempre entra no FIM da lista (não bagunça a reorganização
    # manual que já existia antes dele)
    cursor = await db.execute("SELECT COUNT(*) AS c FROM anuncios WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    ordem = row["c"] if row else 0
    await db.execute(
        "INSERT INTO anuncios (guild_id, nome, canal_id, ordem) VALUES (?, ?, ?, ?)",
        (guild_id, nome, canal_id, ordem)
    )
    await db.commit()


async def obter_canais_anuncio(guild_id: int, nome: str) -> list[dict]:
    """Lista os canais de UM anúncio (multi-canal) com o msg_id de cada um."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT canal_id, msg_id, msg_id_mencao FROM anuncios_canais WHERE guild_id = ? AND nome = ?",
        (guild_id, nome)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def definir_canais_anuncio(guild_id: int, nome: str, canal_ids: list[int]) -> list[dict]:
    """Sincroniza a lista de canais de um anúncio com `canal_ids`: adiciona
    os novos (com msg_id vazio, pra postar do zero neles) e remove os que
    saíram da seleção. Retorna os canais REMOVIDOS (com o msg_id que tinham)
    pra quem chamou poder apagar a mensagem antiga deles."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    atuais = await obter_canais_anuncio(guild_id, nome)
    atuais_ids = {c["canal_id"] for c in atuais}
    novos_ids = set(canal_ids)

    removidos = [c for c in atuais if c["canal_id"] not in novos_ids]
    for c in removidos:
        await db.execute(
            "DELETE FROM anuncios_canais WHERE guild_id = ? AND nome = ? AND canal_id = ?",
            (guild_id, nome, c["canal_id"])
        )
    for cid in novos_ids - atuais_ids:
        await db.execute(
            "INSERT OR IGNORE INTO anuncios_canais (guild_id, nome, canal_id, msg_id, msg_id_mencao) "
            "VALUES (?, ?, ?, NULL, NULL)",
            (guild_id, nome, cid)
        )
    await db.commit()
    return removidos


async def salvar_msg_canal(guild_id: int, nome: str, canal_id: int, msg_id: int | None, msg_id_mencao: int | None = None):
    """Grava o id da mensagem postada/editada nesse canal específico do anúncio."""
    db = await get_conn()
    await db.execute(
        "UPDATE anuncios_canais SET msg_id = ?, msg_id_mencao = ? "
        "WHERE guild_id = ? AND nome = ? AND canal_id = ?",
        (msg_id, msg_id_mencao, guild_id, nome, canal_id)
    )
    await db.commit()


async def apagar_canais_anuncio(guild_id: int, nome: str) -> list[dict]:
    """Remove TODOS os canais de um anúncio (usado quando o anúncio inteiro é
    apagado). Retorna a lista removida pra quem chamou apagar as mensagens."""
    canais = await obter_canais_anuncio(guild_id, nome)
    db = await get_conn()
    await db.execute("DELETE FROM anuncios_canais WHERE guild_id = ? AND nome = ?", (guild_id, nome))
    await db.commit()
    return canais


async def obter_anuncio(guild_id: int, nome: str):
    """Retorna o anúncio já com 'canais_ids' (lista de int) preenchida a
    partir de anuncios_canais — quem consome não precisa fazer outra query."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM anuncios WHERE guild_id = ? AND nome = ?", (guild_id, nome))
    row = await cursor.fetchone()
    if not row:
        return None
    anuncio = dict(row)
    anuncio["canais_ids"] = [c["canal_id"] for c in await obter_canais_anuncio(guild_id, nome)]
    return anuncio


async def listar_anuncios(guild_id: int):
    """Lista os anúncios do servidor, cada um já com 'canais_ids' (lista de
    int) preenchida — 1 query só (GROUP_CONCAT), sem N+1 pra montar o painel."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("""
        SELECT a.*, GROUP_CONCAT(ac.canal_id) AS canais_ids_raw
        FROM anuncios a
        LEFT JOIN anuncios_canais ac ON ac.guild_id = a.guild_id AND ac.nome = a.nome
        WHERE a.guild_id = ?
        GROUP BY a.guild_id, a.nome
        ORDER BY a.ordem ASC, a.nome ASC
    """, (guild_id,))
    rows = await cursor.fetchall()
    resultado = []
    for r in rows:
        d = dict(r)
        bruto = d.pop("canais_ids_raw", None)
        d["canais_ids"] = [int(x) for x in bruto.split(",")] if bruto else []
        resultado.append(d)
    return resultado


async def mover_anuncio(guild_id: int, nome: str, direcao: str) -> bool:
    """Troca a posição do anúncio com o vizinho anterior (direcao='cima') ou
    seguinte (direcao='baixo') na lista ordenada, pra reorganizar o painel
    manualmente. Retorna False se ele já estiver na ponta (sem vizinho pra
    trocar) ou se o nome não existir mais.

    Antes de trocar, renumera o campo 'ordem' de TODOS os anúncios do guild
    pra bater exatamente com a posição atual deles na lista — isso corrige
    de graça qualquer anúncio antigo com 'ordem' zerada/duplicada (ex: os
    que existiam antes dessa coluna existir), sem precisar de uma migração
    separada só pra normalizar dados velhos."""
    anuncios = await listar_anuncios(guild_id)
    posicoes = {a["nome"]: i for i, a in enumerate(anuncios)}
    if nome not in posicoes:
        return False
    i = posicoes[nome]
    j = i - 1 if direcao == "cima" else i + 1
    if j < 0 or j >= len(anuncios):
        return False

    db = await get_conn()
    for k, a in enumerate(anuncios):
        await db.execute(
            "UPDATE anuncios SET ordem = ? WHERE guild_id = ? AND nome = ?",
            (k, guild_id, a["nome"])
        )
    await db.execute(
        "UPDATE anuncios SET ordem = ? WHERE guild_id = ? AND nome = ?",
        (j, guild_id, anuncios[i]["nome"])
    )
    await db.execute(
        "UPDATE anuncios SET ordem = ? WHERE guild_id = ? AND nome = ?",
        (i, guild_id, anuncios[j]["nome"])
    )
    await db.commit()
    return True


async def atualizar_anuncio(guild_id: int, nome: str, **kwargs):
    if not kwargs:
        return
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(
        f"UPDATE anuncios SET {campos} WHERE guild_id = ? AND nome = ?",
        (*kwargs.values(), guild_id, nome)
    )
    await db.commit()


async def apagar_anuncio(guild_id: int, nome: str):
    db = await get_conn()
    await db.execute("DELETE FROM anuncios WHERE guild_id = ? AND nome = ?", (guild_id, nome))
    await db.execute("DELETE FROM anuncios_canais WHERE guild_id = ? AND nome = ?", (guild_id, nome))
    await db.commit()


async def obter_anuncios_para_renovar():
    """Usado só no on_ready (uma vez, no startup do bot): retorna todos os
    anúncios ativos com renovação automática configurada, pra recriar a task
    individual de cada um (o sistema não usa mais uma task única comparando
    datas — cada anúncio tem sua própria task, que só conta os segundos e
    dispara, igual o sistema antigo que funcionava)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM anuncios WHERE ativo = 1 AND tempo_renovacao IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ========================================================================
# CONVITES (INVITES) — funções integradas do Bot de invites
# ========================================================================
async def obter_convites_config(guild_id: int) -> dict:
    # FIX conexão: essa é a função mais quente do bot inteiro — o automod do
    # convites.py (anti-invite, anti-menções, anti-caps, anti-spam, anti-link,
    # anti-palavrão) chama ela em TODA mensagem de TODO servidor, e o
    # anti-raid chama em toda entrada de membro. Sem cache, cada mensagem
    # enviada em qualquer servidor virava um SELECT na conexão única do
    # SQLite -- em vários servidores ativos ao mesmo tempo isso enfileira o
    # processamento de mensagens de todo mundo atrás desses SELECTs. Com
    # cache em memória, só bate no banco quando a config muda de verdade.
    if guild_id in _cache_convites_config:
        return _cache_convites_config[guild_id]
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM convites_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO convites_config (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM convites_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    resultado = dict(row)
    _cache_convites_config[guild_id] = resultado
    return resultado


async def atualizar_convites_config(guild_id: int, **kwargs):
    if not kwargs:
        return await obter_convites_config(guild_id)
    await obter_convites_config(guild_id)
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(f"UPDATE convites_config SET {campos} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    await db.commit()
    _cache_convites_config.pop(guild_id, None)
    return await obter_convites_config(guild_id)


async def registrar_convite_usado(guild_id, user_id, inviter_id, invite_code="", fake=0):
    db = await get_conn()
    # INSERT OR REPLACE recria a linha do zero — se a pessoa tinha saído antes e voltou,
    # "saiu"/"saiu_em" voltam pro default (0/NULL) automaticamente, sem precisar de UPDATE extra.
    await db.execute(
        "INSERT OR REPLACE INTO convites_data (guild_id, user_id, inviter_id, invite_code, joined_at, fake) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)",
        (guild_id, user_id, inviter_id, invite_code, fake)
    )
    await db.commit()


async def marcar_convite_saiu(guild_id: int, user_id: int):
    """Chamado quando alguém sai do servidor: o convite de quem trouxe essa pessoa
    deixa de contar como 'válido' em tempo real, sem apagar o histórico."""
    db = await get_conn()
    await db.execute(
        "UPDATE convites_data SET saiu = 1, saiu_em = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    await db.commit()


async def contar_convites(guild_id: int, inviter_id: int) -> dict:
    db = await get_conn()
    cursor = await db.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN fake = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN saiu = 1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN fake = 0 AND saiu = 0 THEN 1 ELSE 0 END)
        FROM convites_data WHERE guild_id = ? AND inviter_id = ?
        """,
        (guild_id, inviter_id)
    )
    total, fakes, saiu, validos = await cursor.fetchone()
    return {"total": total or 0, "fakes": fakes or 0, "saiu": saiu or 0, "validos": validos or 0}


async def obter_ranking_convites(guild_id: int, limit: int = 100):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT inviter_id,
               COUNT(*) AS total,
               SUM(CASE WHEN fake = 1 THEN 1 ELSE 0 END) AS fakes,
               SUM(CASE WHEN saiu = 1 THEN 1 ELSE 0 END) AS saiu,
               SUM(CASE WHEN fake = 0 AND saiu = 0 THEN 1 ELSE 0 END) AS validos
        FROM convites_data
        WHERE guild_id = ? AND inviter_id IS NOT NULL
        GROUP BY inviter_id
        ORDER BY validos DESC
        LIMIT ?
        """,
        (guild_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_ranking_convites_periodo(guild_id: int, desde: str, limit: int = 100):
    """Mesma ideia do obter_ranking_convites, mas só considera quem entrou a partir de
    `desde` (joined_at) -- usado pela competição periódica pra não misturar convites de
    ciclos anteriores. Não apaga nem ignora os dados vitalícios do resto do sistema."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT inviter_id,
               COUNT(*) AS total,
               SUM(CASE WHEN fake = 1 THEN 1 ELSE 0 END) AS fakes,
               SUM(CASE WHEN saiu = 1 THEN 1 ELSE 0 END) AS saiu,
               SUM(CASE WHEN fake = 0 AND saiu = 0 THEN 1 ELSE 0 END) AS validos
        FROM convites_data
        WHERE guild_id = ? AND inviter_id IS NOT NULL AND joined_at >= ?
        GROUP BY inviter_id
        ORDER BY validos DESC
        LIMIT ?
        """,
        (guild_id, desde, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_guilds_com_competicao_ativa() -> list[dict]:
    """Todo servidor com a competição periódica de convites ligada -- usado pela task
    em loop que confere, a cada hora, se algum já bateu a data de virar de ciclo."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT guild_id, competicao_frequencia, competicao_canal_id, competicao_periodo_inicio "
        "FROM convites_config WHERE competicao_enabled = 1"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def iniciar_periodo_competicao(guild_id: int, inicio_iso: str):
    """Marca o começo de um novo ciclo (chamado ao ligar a competição pela primeira vez
    e de novo toda vez que um ciclo vira, depois de anunciar o vencedor)."""
    db = await get_conn()
    await db.execute(
        "UPDATE convites_config SET competicao_periodo_inicio = ? WHERE guild_id = ?",
        (inicio_iso, guild_id)
    )
    await db.commit()


async def obter_convites_auto_roles(guild_id: int) -> list[int]:
    db = await get_conn()
    cursor = await db.execute("SELECT role_id FROM convites_auto_roles WHERE guild_id = ?", (guild_id,))
    return [row[0] for row in await cursor.fetchall()]


async def add_convites_auto_role(guild_id: int, role_id: int):
    db = await get_conn()
    await db.execute("INSERT OR IGNORE INTO convites_auto_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))
    await db.commit()


async def remove_convites_auto_role(guild_id: int, role_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM convites_auto_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
    await db.commit()


async def definir_convites_auto_roles(guild_id: int, role_ids: list[int]):
    """Substitui a lista inteira de autoroles de uma vez (usado pelo seletor de cargos do painel)."""
    db = await get_conn()
    await db.execute("DELETE FROM convites_auto_roles WHERE guild_id = ?", (guild_id,))
    for rid in role_ids:
        await db.execute("INSERT OR IGNORE INTO convites_auto_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, rid))
    await db.commit()


async def obter_ranking_live_convites(guild_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_ranking_live WHERE guild_id = ?", (guild_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_ranking_live_convites(guild_id: int, channel_id: int, message_id: int):
    db = await get_conn()
    await db.execute(
        "INSERT INTO convites_ranking_live (guild_id, channel_id, message_id) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, message_id = excluded.message_id",
        (guild_id, channel_id, message_id)
    )
    await db.commit()


# ========================================================================
# TICKETS — funções integradas do Bot de ticket (sem loja)
# ========================================================================
async def criar_painel_ticket(guild_id: int, nome: str) -> int:
    db = await get_conn()
    # Grava mensagem_abertura explicitamente (em vez de deixar cair no
    # DEFAULT da coluna) -- ver comentário em MENSAGEM_ABERTURA_PADRAO logo
    # no topo do arquivo. Garante que painel novo nasce sempre com o texto
    # atual, mesmo que o .db em uso seja antigo e tenha esse DEFAULT
    # desatualizado gravado na própria tabela.
    cursor = await db.execute(
        "INSERT INTO ticket_paineis (guild_id, nome, mensagem_abertura) VALUES (?, ?, ?)",
        (guild_id, nome, MENSAGEM_ABERTURA_PADRAO)
    )
    await db.commit()
    return cursor.lastrowid


# === CARGOS DE SUPORTE (sem limite de 2 — até o máximo que o Discord deixar
# selecionar de uma vez, 25) ===
async def _cargos_suporte_por_painel(painel_ids: list) -> dict:
    """Retorna {painel_id: [cargo_id, ...]} pra uma lista de painel_ids, numa
    query só (evita N+1 quando listando vários painéis de uma vez)."""
    painel_ids = [pid for pid in painel_ids if pid is not None]
    if not painel_ids:
        return {}
    db = await get_conn()
    placeholders = ",".join("?" * len(painel_ids))
    cursor = await db.execute(
        f"SELECT painel_id, cargo_id FROM ticket_painel_cargos_suporte WHERE painel_id IN ({placeholders})",
        tuple(painel_ids)
    )
    rows = await cursor.fetchall()
    resultado = {}
    for painel_id, cargo_id in rows:
        resultado.setdefault(painel_id, []).append(cargo_id)
    return resultado


async def obter_cargos_suporte(painel_id: int) -> list:
    mapa = await _cargos_suporte_por_painel([painel_id])
    return mapa.get(painel_id, [])


async def definir_cargos_suporte(painel_id: int, cargo_ids: list):
    """Substitui a lista inteira de cargos de suporte do painel pelos IDs
    passados (sem limite de 2)."""
    db = await get_conn()
    await db.execute("DELETE FROM ticket_painel_cargos_suporte WHERE painel_id = ?", (painel_id,))
    cargo_ids = list(dict.fromkeys(cid for cid in cargo_ids if cid))
    if cargo_ids:
        await db.executemany(
            "INSERT OR IGNORE INTO ticket_painel_cargos_suporte (painel_id, cargo_id) VALUES (?, ?)",
            [(painel_id, cid) for cid in cargo_ids]
        )
    await db.commit()


async def obter_painel(painel_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ticket_paineis WHERE id = ?", (painel_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    painel = dict(row)
    painel['cargos_suporte'] = await obter_cargos_suporte(painel_id)
    return painel


async def listar_paineis(guild_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ticket_paineis WHERE guild_id = ? ORDER BY id", (guild_id,))
    rows = await cursor.fetchall()
    paineis = [dict(r) for r in rows]
    mapa_cargos = await _cargos_suporte_por_painel([p['id'] for p in paineis])
    for p in paineis:
        p['cargos_suporte'] = mapa_cargos.get(p['id'], [])
    return paineis


async def obter_todos_paineis():
    """Usado no startup pra registrar as views persistentes de todos os servidores."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ticket_paineis")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def atualizar_painel(painel_id: int, **kwargs):
    if not kwargs:
        return
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(f"UPDATE ticket_paineis SET {campos} WHERE id = ?", (*kwargs.values(), painel_id))
    await db.commit()


async def deletar_painel(painel_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM ticket_categorias WHERE painel_id = ?", (painel_id,))
    await db.execute("DELETE FROM ticket_paineis WHERE id = ?", (painel_id,))
    await db.commit()


async def adicionar_categoria(painel_id: int, nome: str, descricao: str, emoji: str, slug: str = None) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO ticket_categorias (painel_id, nome, descricao, emoji, slug) VALUES (?, ?, ?, ?, ?)",
        (painel_id, nome, descricao, emoji, slug)
    )
    await db.commit()
    return cursor.lastrowid


async def obter_slugs_usados_painel(painel_id: int) -> list:
    """Slugs de preset já usados nesse painel — pra tirar da lista do
    dropdown '📦 Usar Padrão' o que já foi adicionado (evita duplicar)."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT slug FROM ticket_categorias WHERE painel_id = ? AND slug IS NOT NULL", (painel_id,)
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def obter_categoria(categoria_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ticket_categorias WHERE id = ?", (categoria_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def obter_categorias_painel(painel_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ticket_categorias WHERE painel_id = ? ORDER BY id", (painel_id,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def remover_categoria(categoria_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM ticket_categorias WHERE id = ?", (categoria_id,))
    await db.commit()


async def atualizar_cargos_categoria(categoria_id: int, cargo_id_1, cargo_id_2=None):
    """Define (ou remove, se vier None) até 2 cargos específicos dessa categoria.
    Quando os dois vierem None, a abertura de ticket volta a pingar (e dar
    visibilidade via canal) só nos cargos gerais de suporte do painel."""
    db = await get_conn()
    await db.execute(
        "UPDATE ticket_categorias SET cargo_ping_id = ?, cargo_ping_id_2 = ? WHERE id = ?",
        (cargo_id_1, cargo_id_2, categoria_id)
    )
    await db.commit()


async def obter_cargos_absolutos(guild_id: int) -> list:
    """Retorna a lista de IDs de cargo 'absoluto' do servidor (mexem em
    QUALQUER ticket, mesmo já assumido por outro staff). Sem limite de
    quantidade.

    MIGRAÇÃO: se a tabela nova ainda tá vazia pra esse guild mas existe um
    valor antigo em `configuracoes.cargo_absoluto_ticket` (de antes, quando
    só dava pra ter 1 cargo), importa ele automaticamente pra tabela nova
    na primeira leitura — ninguém perde o cargo absoluto já configurado.
    """
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT cargo_id FROM cargos_absolutos WHERE guild_id = ?", (guild_id,))
    rows = await cursor.fetchall()
    if rows:
        return [r["cargo_id"] for r in rows]

    cursor = await db.execute("SELECT cargo_absoluto_ticket FROM configuracoes WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    cargo_antigo = row["cargo_absoluto_ticket"] if row else None
    if cargo_antigo:
        await db.execute(
            "INSERT OR IGNORE INTO cargos_absolutos (guild_id, cargo_id) VALUES (?, ?)",
            (guild_id, cargo_antigo)
        )
        await db.commit()
        return [cargo_antigo]
    return []


async def adicionar_cargo_absoluto(guild_id: int, cargo_id: int):
    db = await get_conn()
    await db.execute(
        "INSERT OR IGNORE INTO cargos_absolutos (guild_id, cargo_id) VALUES (?, ?)",
        (guild_id, cargo_id)
    )
    await db.commit()


async def remover_cargo_absoluto(guild_id: int, cargo_id: int):
    db = await get_conn()
    await db.execute(
        "DELETE FROM cargos_absolutos WHERE guild_id = ? AND cargo_id = ?",
        (guild_id, cargo_id)
    )
    await db.commit()


async def atualizar_canal_categoria(categoria_id: int, canal_id):
    """Define (ou remove, se vier None) o canal onde o tópico dessa categoria
    é criado. Quando vier None, a abertura de ticket volta a usar o canal
    onde o painel foi enviado (comportamento padrão)."""
    db = await get_conn()
    await db.execute(
        "UPDATE ticket_categorias SET canal_destino_id = ? WHERE id = ?",
        (canal_id, categoria_id)
    )
    await db.commit()


async def gerar_numero_ticket(guild_id: int) -> int:
    db = await get_conn()
    await db.execute("INSERT OR IGNORE INTO ticket_contador (guild_id, ultimo_numero) VALUES (?, 0)", (guild_id,))
    await db.execute("UPDATE ticket_contador SET ultimo_numero = ultimo_numero + 1 WHERE guild_id = ?", (guild_id,))
    cursor = await db.execute("SELECT ultimo_numero FROM ticket_contador WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    await db.commit()
    return row[0]


async def criar_ticket_registro(numero, guild_id, thread_id, usuario_id, painel_id, categoria_id):
    db = await get_conn()
    await db.execute(
        "INSERT INTO tickets (thread_id, guild_id, numero, usuario_id, painel_id, categoria) VALUES (?, ?, ?, ?, ?, ?)",
        (thread_id, guild_id, numero, usuario_id, painel_id, str(categoria_id))
    )
    await db.commit()


async def obter_ticket(thread_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM tickets WHERE thread_id = ?", (thread_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def contar_tickets_usuario_painel(usuario_id: int, painel_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM tickets WHERE usuario_id = ? AND painel_id = ? AND fechado_em IS NULL",
        (usuario_id, painel_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def obter_tickets_abertos_usuario_painel(usuario_id: int, painel_id: int):
    """Lista os tickets ABERTOS (fechado_em IS NULL) desse usuário nesse painel,
    mais recentes primeiro -- usado pra montar o botão 'Ir ao Ticket' quando ele
    tenta abrir um novo e já bateu no limite (max_tickets_usuario)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tickets WHERE usuario_id = ? AND painel_id = ? AND fechado_em IS NULL "
        "ORDER BY numero DESC",
        (usuario_id, painel_id)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def assumir_ticket(thread_id: int, staff_id: int) -> bool:
    """UPDATE condicional (WHERE assumido_por IS NULL): evita que dois staff
    cliquem em 'Assumir' quase ao mesmo tempo e os dois "ganhem" o ticket —
    só o primeiro UPDATE que realmente bater na condição tem efeito, os
    outros retornam False (rowcount 0) e o caller sabe que perdeu a corrida."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE tickets SET assumido_por = ? WHERE thread_id = ? AND assumido_por IS NULL", (staff_id, thread_id)
    )
    await db.commit()
    return cursor.rowcount > 0


async def transferir_ticket(thread_id: int, novo_staff_id: int):
    """Diferente de assumir_ticket: aqui o ticket JÁ está assumido (é uma
    reatribuição de propósito, não uma corrida por quem assume primeiro),
    então o UPDATE não tem a condição WHERE assumido_por IS NULL — senão
    a transferência nunca aconteceria de verdade."""
    db = await get_conn()
    await db.execute("UPDATE tickets SET assumido_por = ? WHERE thread_id = ?", (novo_staff_id, thread_id))
    await db.commit()


async def fechar_ticket(thread_id: int, staff_id: int, motivo: str = None):
    db = await get_conn()
    await db.execute(
        "UPDATE tickets SET fechado_em = CURRENT_TIMESTAMP, fechado_por = ?, fechado_motivo = ? WHERE thread_id = ?",
        (staff_id, motivo, thread_id)
    )
    await db.commit()


async def registrar_stat_staff(guild_id: int, user_id: int, tipo: str, assumiu_antes: bool = True):
    """tipo = 'assumido' ou 'fechado'. Usado pelo /ranking_suporte."""
    db = await get_conn()
    await db.execute(
        "INSERT INTO ticket_staff_stats (guild_id, user_id, assumidos, fechados, fechados_sem_assumir) "
        "VALUES (?, ?, 0, 0, 0) ON CONFLICT(guild_id, user_id) DO NOTHING",
        (guild_id, user_id)
    )
    if tipo == "assumido":
        await db.execute(
            "UPDATE ticket_staff_stats SET assumidos = assumidos + 1 WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
    elif tipo == "fechado":
        if assumiu_antes:
            await db.execute(
                "UPDATE ticket_staff_stats SET fechados = fechados + 1 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
        else:
            await db.execute(
                "UPDATE ticket_staff_stats SET fechados = fechados + 1, "
                "fechados_sem_assumir = fechados_sem_assumir + 1 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
    await db.commit()


async def resetar_ranking_staff(guild_id: int, user_id: int | None = None) -> int:
    """Zera as estatísticas do /ranking_suporte. Sem user_id, apaga a
    equipe inteira do servidor (usado pelo .resetsup); com user_id, só a
    linha desse staff (.resetsup @user). Retorna quantas linhas foram
    apagadas, pra quem chamou saber se tinha algo pra zerar ou não."""
    db = await get_conn()
    if user_id is None:
        cursor = await db.execute(
            "DELETE FROM ticket_staff_stats WHERE guild_id = ?", (guild_id,)
        )
    else:
        cursor = await db.execute(
            "DELETE FROM ticket_staff_stats WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id)
        )
    await db.commit()
    return cursor.rowcount


async def obter_ranking_staff(guild_id: int, limit: int = 25):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ticket_staff_stats WHERE guild_id = ? "
        "ORDER BY assumidos DESC, fechados DESC LIMIT ?",
        (guild_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# NOVO: histórico de tickets de um usuário específico num servidor — usado
# pelo /tickets-usuario, pra staff conseguir ver rapidinho todo o
# atendimento anterior de alguém (inclusive fechados) sem precisar catar
# mensagem por mensagem no canal de logs.
async def obter_tickets_por_usuario(guild_id: int, usuario_id: int, limit: int = 25):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tickets WHERE guild_id = ? AND usuario_id = ? ORDER BY criado_em DESC LIMIT ?",
        (guild_id, usuario_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_tickets_abertos_guild(guild_id: int):
    """Todos os tickets ainda abertos do servidor (fechado_em IS NULL), com o
    nome da categoria já junto -- usado pelo .ticketativos."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """
        SELECT t.*, c.nome AS categoria_nome
        FROM tickets t
        LEFT JOIN ticket_categorias c ON c.id = t.categoria
        WHERE t.guild_id = ? AND t.fechado_em IS NULL
        ORDER BY t.criado_em ASC
        """,
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_tickets_fechados_recentes_guild(guild_id: int, limit: int = 15):
    """Últimos tickets fechados do servidor -- usado pelo .ticketativos."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tickets WHERE guild_id = ? AND fechado_em IS NOT NULL ORDER BY fechado_em DESC LIMIT ?",
        (guild_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def registrar_log_acao(guild_id, painel_id, thread_id, numero, acao, autor_id, alvo_id=None, detalhes=None):
    db = await get_conn()
    await db.execute(
        "INSERT INTO ticket_logs_acoes (guild_id, painel_id, thread_id, numero, acao, autor_id, alvo_id, detalhes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, painel_id, thread_id, numero, acao, autor_id, alvo_id, detalhes)
    )
    await db.commit()


async def obter_logs_ticket(thread_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ticket_logs_acoes WHERE thread_id = ? ORDER BY criado_em ASC", (thread_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# NOVO: histórico pelo (guild_id, numero) em vez do thread_id. Como o
# thread_id muda quando um ticket é reaberto (novo tópico criado), buscar
# pelo número garante que o histórico completo sobrevive à reabertura —
# usado tanto no log vivo quanto na transcrição HTML.
async def obter_logs_por_numero(guild_id: int, numero: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ticket_logs_acoes WHERE guild_id = ? AND numero = ? ORDER BY criado_em ASC",
        (guild_id, numero)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# NOVO: tickets ainda não assumidos, já passando do SLA configurado no
# painel, que ainda não receberam o aviso — usado pelo loop de cobrança de
# 1ª resposta.
async def obter_tickets_nao_assumidos_para_sla():
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("""
        SELECT t.*, p.sla_assumir_minutos, p.cargo_suporte_id, p.cargo_suporte_id_2
        FROM tickets t
        JOIN ticket_paineis p ON t.painel_id = p.id
        WHERE t.fechado_em IS NULL
          AND t.assumido_por IS NULL
          AND t.sla_aviso_enviado = 0
          AND p.sla_assumir_minutos > 0
    """)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_sla_aviso_enviado(thread_id: int):
    db = await get_conn()
    await db.execute("UPDATE tickets SET sla_aviso_enviado = 1 WHERE thread_id = ?", (thread_id,))
    await db.commit()


# NOVO: reabre um ticket fechado ligando ele a um tópico novo (o antigo já
# foi deletado). Muda o thread_id (chave primária) pro do tópico novo e
# limpa os campos de fechamento/atribuição — o histórico de ações não se
# perde porque é buscado por (guild_id, numero), não por thread_id.
async def reatribuir_thread_ticket(numero: int, guild_id: int, novo_thread_id: int):
    db = await get_conn()
    await db.execute(
        "UPDATE tickets SET thread_id = ?, fechado_em = NULL, fechado_por = NULL, "
        "assumido_por = NULL, sla_aviso_enviado = 0, aviso_inatividade_enviado = 0, "
        "ultima_atividade = CURRENT_TIMESTAMP, painel_message_id = NULL "
        "WHERE numero = ? AND guild_id = ?",
        (novo_thread_id, numero, guild_id)
    )
    await db.commit()


# ---------- prioridade ----------
async def definir_prioridade_ticket(thread_id: int, prioridade: str):
    db = await get_conn()
    await db.execute("UPDATE tickets SET prioridade = ? WHERE thread_id = ?", (prioridade, thread_id))
    await db.commit()


async def definir_painel_message_id(thread_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE tickets SET painel_message_id = ? WHERE thread_id = ?", (message_id, thread_id))
    await db.commit()


async def definir_log_message_id(thread_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE tickets SET log_message_id = ? WHERE thread_id = ?", (message_id, thread_id))
    await db.commit()


# ---------- avaliação (pós-fechamento, via DM) ----------
async def salvar_avaliacao_ticket(thread_id: int, nota: int, comentario: str | None = None):
    db = await get_conn()
    await db.execute(
        "UPDATE tickets SET avaliacao = ?, avaliacao_comentario = ? WHERE thread_id = ?",
        (nota, comentario, thread_id)
    )
    await db.commit()


async def obter_ticket_por_numero(guild_id: int, numero: int):
    """Usado pra achar o ticket na hora de gravar a avaliação vinda por DM
    (a thread já pode estar arquivada/deletada, então busca por número+guild)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM tickets WHERE guild_id = ? AND numero = ? ORDER BY thread_id DESC LIMIT 1",
        (guild_id, numero)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def media_avaliacoes_painel(painel_id: int) -> tuple[float, int]:
    """Média de estrelas e total de avaliações desse painel."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT AVG(avaliacao), COUNT(avaliacao) FROM tickets WHERE painel_id = ? AND avaliacao IS NOT NULL",
        (painel_id,)
    )
    row = await cursor.fetchone()
    media = round(row[0], 1) if row and row[0] is not None else 0.0
    total = row[1] if row else 0
    return media, total


# ---------- atividade / auto-close por inatividade ----------
async def marcar_atividade_ticket(thread_id: int):
    """Chamado a cada mensagem na thread. Reseta o contador de inatividade
    (e limpa o aviso já enviado, já que alguém voltou a responder)."""
    db = await get_conn()
    await db.execute(
        "UPDATE tickets SET ultima_atividade = CURRENT_TIMESTAMP, aviso_inatividade_enviado = 0 WHERE thread_id = ?",
        (thread_id,)
    )
    await db.commit()


async def marcar_aviso_inatividade_enviado(thread_id: int):
    db = await get_conn()
    await db.execute("UPDATE tickets SET aviso_inatividade_enviado = 1 WHERE thread_id = ?", (thread_id,))
    await db.commit()


async def obter_tickets_abertos_para_autoclose():
    """Todos os tickets abertos cujo painel tem auto-close ligado (auto_close_horas > 0
    OU aviso_inatividade_horas > 0), já com os dados do painel juntos — usado pela
    task em loop que roda periodicamente."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("""
        SELECT t.*, p.aviso_inatividade_horas, p.auto_close_horas, p.canal_logs_id, p.notificar_dm, p.cor
        FROM tickets t
        JOIN ticket_paineis p ON p.id = t.painel_id
        WHERE t.fechado_em IS NULL
          AND (p.aviso_inatividade_horas > 0 OR p.auto_close_horas > 0)
    """)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ---------- emoji de categoria (usado pelo seletor paginado) ----------
async def atualizar_emoji_categoria(categoria_id: int, emoji: str):
    db = await get_conn()
    await db.execute("UPDATE ticket_categorias SET emoji = ? WHERE id = ?", (emoji, categoria_id))
    await db.commit()


# ========================================================================
# SORTEIOS E EVENTOS — parte do sistema de convites
# ========================================================================
async def criar_sorteio(guild_id, channel_id, titulo, descricao, premio, vencedores, termina_em, criado_por,
                         emoji="🎉", cor="5865F2", banner="", cargo_anuncio=None, cargo_necessario=None,
                         conta_min_dias=None):
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO convites_sorteios (guild_id, channel_id, titulo, descricao, premio, emoji, cor, banner, "
        "vencedores, termina_em, criado_por, cargo_anuncio, cargo_necessario, conta_min_dias) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, titulo, descricao, premio, emoji, cor, banner, vencedores, termina_em, criado_por,
         cargo_anuncio, cargo_necessario, conta_min_dias)
    )
    await db.commit()
    return cursor.lastrowid


async def definir_msg_sorteio(sorteio_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE convites_sorteios SET message_id = ? WHERE id = ?", (message_id, sorteio_id))
    await db.commit()


async def obter_sorteio(sorteio_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM convites_sorteios WHERE id = ?", (sorteio_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_todos_sorteios_ativos():
    """Todos os sorteios não encerrados, de qualquer servidor — usado no (re)agendamento
    direto: cada sorteio ganha sua própria tarefa que dorme exatamente até `termina_em` e
    sorteia sozinha, em vez de depender de um polling tentando acertar a janela certa."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_sorteios WHERE encerrado = 0 AND termina_em IS NOT NULL"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_sorteios_aviso_pendente(campo: str, minutos: int):
    """Sorteios ativos que entraram na janela de X minutos antes do fim e ainda não
    receberam esse aviso específico (campo = 'aviso_10min' ou 'aviso_1min')."""
    if campo not in ("aviso_10min", "aviso_1min"):
        raise ValueError("campo inválido")
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        f"SELECT * FROM convites_sorteios WHERE encerrado = 0 AND termina_em IS NOT NULL AND {campo} = 0 "
        f"AND datetime(termina_em) <= datetime('now', ?) AND datetime(termina_em) > datetime('now')",
        (f"+{minutos} minutes",)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_aviso_sorteio(sorteio_id: int, campo: str):
    if campo not in ("aviso_10min", "aviso_1min"):
        raise ValueError("campo inválido")
    db = await get_conn()
    await db.execute(f"UPDATE convites_sorteios SET {campo} = 1 WHERE id = ?", (sorteio_id,))
    await db.commit()


async def obter_sorteios_ativos_cronometro():
    """Sorteios em andamento (ainda não venceram) com mensagem já publicada — usados pra
    reeditar o embed periodicamente e mostrar o cronômetro HH:MM:SS contando de verdade."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_sorteios WHERE encerrado = 0 AND termina_em IS NOT NULL "
        "AND message_id IS NOT NULL AND datetime(termina_em) > datetime('now')"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def definir_vencedores_sorteio(sorteio_id: int, ids: list[int]):
    """Grava quem foram os últimos vencedores sorteados (usado pelo reroll pra saber
    quem já ganhou e não sortear a mesma pessoa de novo)."""
    db = await get_conn()
    await db.execute(
        "UPDATE convites_sorteios SET vencedores_atuais = ? WHERE id = ?",
        (json.dumps(ids), sorteio_id)
    )
    await db.commit()


async def encerrar_sorteio(sorteio_id: int):
    db = await get_conn()
    await db.execute("UPDATE convites_sorteios SET encerrado = 1 WHERE id = ?", (sorteio_id,))
    await db.commit()


async def cancelar_sorteio(sorteio_id: int):
    """Cancela o sorteio sem sortear ninguém (encerrado = 2, distingue de 'terminou normal')."""
    db = await get_conn()
    await db.execute("UPDATE convites_sorteios SET encerrado = 2 WHERE id = ?", (sorteio_id,))
    await db.commit()


async def listar_sorteios_ativos(guild_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_sorteios WHERE guild_id = ? AND encerrado = 0 ORDER BY id DESC", (guild_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def listar_sorteios_recentes(guild_id: int, limit: int = 15):
    """Sorteios recentes de qualquer status (ativo, encerrado ou cancelado) — usado no
    painel de gerenciamento, pra permitir reroll em sorteios já encerrados."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_sorteios WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (guild_id, limit)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_sorteio_por_mensagem(message_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM convites_sorteios WHERE message_id = ?", (message_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def alternar_participante_sorteio(sorteio_id: int, user_id: int) -> bool:
    """Entra ou sai do sorteio (toggle). Retorna True se entrou agora, False se saiu."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT 1 FROM convites_sorteio_participantes WHERE sorteio_id = ? AND user_id = ?",
        (sorteio_id, user_id)
    )
    ja_participa = await cursor.fetchone()
    if ja_participa:
        await db.execute(
            "DELETE FROM convites_sorteio_participantes WHERE sorteio_id = ? AND user_id = ?",
            (sorteio_id, user_id)
        )
        await db.commit()
        return False
    await db.execute(
        "INSERT INTO convites_sorteio_participantes (sorteio_id, user_id, entrou_em) VALUES (?, ?, ?)",
        (sorteio_id, user_id, datetime.utcnow().isoformat())
    )
    await db.commit()
    return True


async def contar_participantes_sorteio(sorteio_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM convites_sorteio_participantes WHERE sorteio_id = ?", (sorteio_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def listar_participantes_sorteio(sorteio_id: int) -> list[int]:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM convites_sorteio_participantes WHERE sorteio_id = ?", (sorteio_id,)
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def criar_evento(guild_id, channel_id, titulo, descricao, data_evento, local, criado_por, emoji="📅",
                        data_hora=None, lembrete_minutos=60):
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO convites_eventos (guild_id, channel_id, titulo, descricao, data_evento, local, criado_por, "
        "emoji, data_hora, lembrete_minutos) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, titulo, descricao, data_evento, local, criado_por, emoji, data_hora, lembrete_minutos)
    )
    await db.commit()
    return cursor.lastrowid


async def obter_eventos_para_lembrete():
    """Eventos ativos, com data/hora definida, dentro da janela de lembrete e que ainda não avisaram."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_eventos WHERE encerrado = 0 AND data_hora IS NOT NULL AND lembrete_enviado = 0 "
        "AND datetime(data_hora) <= datetime('now', '+' || lembrete_minutos || ' minutes')"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def obter_eventos_para_encerrar():
    """Eventos ativos cuja data/hora já passou — encerra automaticamente, igual ao sorteio."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM convites_eventos WHERE encerrado = 0 AND data_hora IS NOT NULL "
        "AND datetime(data_hora) <= datetime('now')"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_lembrete_enviado(evento_id: int):
    db = await get_conn()
    await db.execute("UPDATE convites_eventos SET lembrete_enviado = 1 WHERE id = ?", (evento_id,))
    await db.commit()


async def listar_rsvp_evento(evento_id: int, status: str) -> list[int]:
    """IDs de quem confirmou um status específico ('sim', 'talvez' ou 'nao')."""
    db = await get_conn()
    cursor = await db.execute(
        "SELECT user_id FROM convites_evento_rsvp WHERE evento_id = ? AND status = ?", (evento_id, status)
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


async def definir_msg_evento(evento_id: int, message_id: int):
    db = await get_conn()
    await db.execute("UPDATE convites_eventos SET message_id = ? WHERE id = ?", (message_id, evento_id))
    await db.commit()


async def listar_eventos(guild_id: int, apenas_ativos: bool = True):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM convites_eventos WHERE guild_id = ?"
    if apenas_ativos:
        query += " AND encerrado = 0"
    query += " ORDER BY id DESC"
    cursor = await db.execute(query, (guild_id,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def encerrar_evento(evento_id: int):
    db = await get_conn()
    await db.execute("UPDATE convites_eventos SET encerrado = 1 WHERE id = ?", (evento_id,))
    await db.commit()


async def cancelar_evento(evento_id: int):
    """Cancela o evento (encerrado = 2, distingue de 'aconteceu / foi encerrado normal')."""
    db = await get_conn()
    await db.execute("UPDATE convites_eventos SET encerrado = 2 WHERE id = ?", (evento_id,))
    await db.commit()


async def obter_evento(evento_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM convites_eventos WHERE id = ?", (evento_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def obter_evento_por_mensagem(message_id: int):
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM convites_eventos WHERE message_id = ?", (message_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_rsvp_evento(evento_id: int, user_id: int, status: str):
    db = await get_conn()
    await db.execute(
        "INSERT INTO convites_evento_rsvp (evento_id, user_id, status) VALUES (?, ?, ?) "
        "ON CONFLICT(evento_id, user_id) DO UPDATE SET status = excluded.status",
        (evento_id, user_id, status)
    )
    await db.commit()


async def obter_rsvp_usuario(evento_id: int, user_id: int):
    db = await get_conn()
    cursor = await db.execute(
        "SELECT status FROM convites_evento_rsvp WHERE evento_id = ? AND user_id = ?", (evento_id, user_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def contar_rsvp_evento(evento_id: int) -> dict:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM convites_evento_rsvp WHERE evento_id = ? GROUP BY status", (evento_id,)
    )
    rows = await cursor.fetchall()
    contagem = {"sim": 0, "talvez": 0, "nao": 0}
    for status, qtd in rows:
        contagem[status] = qtd
    return contagem


# ========================================================================
# ALIASES — nomes usados pelo código original do bot de tickets (tickets.py
# e painel_config.py), mantidos pra não precisar reescrever esses arquivos
# inteiros na integração.
# ========================================================================
async def obter_paineis_guild(guild_id: int):
    return await listar_paineis(guild_id)


async def criar_painel(guild_id: int, nome: str) -> int:
    return await criar_painel_ticket(guild_id, nome)


async def add_categoria_painel(painel_id: int, nome: str, emoji: str, descricao: str, slug: str = None) -> int:
    return await adicionar_categoria(painel_id, nome, descricao, emoji, slug)


async def remover_categoria_painel(categoria_id: int):
    return await remover_categoria(categoria_id)


async def atualizar_transcript_url(thread_id: int, url: str):
    db = await get_conn()
    await db.execute("UPDATE tickets SET transcript_url = ? WHERE thread_id = ?", (url, thread_id))
    await db.commit()


async def listar_todos_paineis_ativos():
    return await obter_todos_paineis()


# ========================================================================
# BATER PONTO — múltiplos painéis independentes por servidor
# ========================================================================

# ---------- CRUD dos painéis ----------
async def criar_painel_ponto(guild_id: int, titulo: str, criado_por: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO ponto_paineis (guild_id, titulo, criado_por) VALUES (?, ?, ?)",
        (guild_id, titulo, criado_por)
    )
    await db.commit()
    return cursor.lastrowid


async def obter_painel_ponto(painel_id: int) -> dict | None:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ponto_paineis WHERE painel_id = ?", (painel_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_paineis_ponto(guild_id: int) -> list[dict]:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ponto_paineis WHERE guild_id = ? ORDER BY painel_id ASC", (guild_id,))
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def listar_todos_paineis_ponto() -> list[dict]:
    """Todos os painéis de ponto de todos os servidores — usado só pra
    reanexar as views persistentes quando o bot reinicia."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM ponto_paineis")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def atualizar_painel_ponto(painel_id: int, campo: str, valor):
    db = await get_conn()
    await db.execute(f"UPDATE ponto_paineis SET {campo} = ? WHERE painel_id = ?", (valor, painel_id))
    await db.commit()


async def apagar_painel_ponto(painel_id: int):
    """Apaga só a definição do painel (config + vínculo com a mensagem
    publicada). Os registros de ponto já batidos continuam guardados pra
    histórico/export, só deixam de ter um painel ativo por trás."""
    db = await get_conn()
    await db.execute("DELETE FROM ponto_paineis WHERE painel_id = ?", (painel_id,))
    await db.commit()


# ---------- sessões de ponto, por painel ----------
async def ponto_aberto(painel_id: int, user_id: int):
    """Retorna o registro ABERTO do usuário nesse painel, ou None."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ponto_registros WHERE painel_id = ? AND user_id = ? AND status = 'aberto'",
        (painel_id, user_id)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def iniciar_ponto(painel_id: int, guild_id: int, user_id: int):
    """Abre um ponto novo nesse painel. Retorna o id do registro, ou None
    se já tinha um aberto NESSE painel (pode ter aberto em outro)."""
    if await ponto_aberto(painel_id, user_id):
        return None
    db = await get_conn()
    cursor = await db.execute(
        "INSERT INTO ponto_registros (painel_id, guild_id, user_id, inicio, status) VALUES (?, ?, ?, ?, 'aberto')",
        (painel_id, guild_id, user_id, datetime.utcnow().isoformat())
    )
    await db.commit()
    return cursor.lastrowid


async def fechar_ponto(painel_id: int, user_id: int, fechado_por: int | None = None):
    """Fecha o ponto aberto do usuário nesse painel. Retorna o registro
    fechado (com duração em segundos), ou None se não tinha ponto aberto
    nesse painel."""
    aberto = await ponto_aberto(painel_id, user_id)
    if not aberto:
        return None

    db = await get_conn()
    agora = datetime.utcnow()
    inicio = datetime.fromisoformat(aberto["inicio"])
    duracao = int((agora - inicio).total_seconds())
    status = "fechado_admin" if (fechado_por and fechado_por != user_id) else "fechado"

    await db.execute(
        "UPDATE ponto_registros SET fim = ?, duracao_segundos = ?, status = ?, fechado_por = ? WHERE id = ?",
        (agora.isoformat(), duracao, status, fechado_por, aberto["id"])
    )
    await db.commit()

    aberto["fim"] = agora.isoformat()
    aberto["duracao_segundos"] = duracao
    aberto["status"] = status
    return aberto


async def listar_pontos_abertos(painel_id: int):
    """Todos os pontos abertos agora nesse painel (pro painel e pro admin fechar)."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ponto_registros WHERE painel_id = ? AND status = 'aberto' ORDER BY inicio ASC",
        (painel_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def cancelar_ponto(painel_id: int, user_id: int, cancelado_por: int, motivo: str | None = None):
    """Descarta a sessão ABERTA do usuário nesse painel sem contar tempo."""
    aberto = await ponto_aberto(painel_id, user_id)
    if not aberto:
        return None
    db = await get_conn()
    agora = datetime.utcnow().isoformat()
    await db.execute(
        "UPDATE ponto_registros SET fim = ?, duracao_segundos = 0, status = 'cancelado', fechado_por = ?, motivo = ? WHERE id = ?",
        (agora, cancelado_por, motivo, aberto["id"])
    )
    await db.commit()
    aberto["fim"] = agora
    aberto["duracao_segundos"] = 0
    aberto["status"] = "cancelado"
    return aberto


async def ajustar_horas(painel_id: int, guild_id: int, user_id: int, delta_segundos: int, ajustado_por: int, motivo: str | None = None):
    """Lança uma correção manual de horas nesse painel específico."""
    db = await get_conn()
    agora = datetime.utcnow().isoformat()
    cursor = await db.execute(
        "INSERT INTO ponto_registros (painel_id, guild_id, user_id, inicio, fim, duracao_segundos, status, fechado_por, motivo) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ajuste', ?, ?)",
        (painel_id, guild_id, user_id, agora, agora, delta_segundos, ajustado_por, motivo)
    )
    await db.commit()
    return cursor.lastrowid


async def historico_ponto(painel_id: int, user_id: int, limite: int = 10):
    """Últimos registros FECHADOS/AJUSTADOS do usuário nesse painel."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM ponto_registros WHERE painel_id = ? AND user_id = ? AND status NOT IN ('aberto', 'cancelado') "
        "ORDER BY fim DESC LIMIT ?",
        (painel_id, user_id, limite)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def tempo_total_ponto(painel_id: int, user_id: int, desde: str | None = None) -> int:
    """Soma em segundos do tempo já fechado/ajustado do usuário nesse painel."""
    db = await get_conn()
    query = (
        "SELECT COALESCE(SUM(duracao_segundos), 0) FROM ponto_registros "
        "WHERE painel_id = ? AND user_id = ? AND status NOT IN ('aberto', 'cancelado')"
    )
    params = [painel_id, user_id]
    if desde:
        query += " AND fim >= ?"
        params.append(desde)
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return row[0] if row else 0


async def ranking_ponto(painel_id: int, desde: str | None = None, limite: int = 15):
    """Ranking por tempo total nesse painel no período."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = (
        "SELECT user_id, SUM(duracao_segundos) AS total_segundos, COUNT(*) AS sessoes "
        "FROM ponto_registros WHERE painel_id = ? AND status NOT IN ('aberto', 'cancelado')"
    )
    params = [painel_id]
    if desde:
        query += " AND fim >= ?"
        params.append(desde)
    query += " GROUP BY user_id ORDER BY total_segundos DESC LIMIT ?"
    params.append(limite)
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def registros_periodo(painel_id: int, desde: str | None = None):
    """Todos os registros fechados/ajustados desse painel num período — CSV."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM ponto_registros WHERE painel_id = ? AND status NOT IN ('aberto', 'cancelado')"
    params = [painel_id]
    if desde:
        query += " AND fim >= ?"
        params.append(desde)
    query += " ORDER BY fim DESC"
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ========================================================================
# BLACKLIST — usado por cogs/blacklist.py
# ========================================================================
async def adicionar_blacklist(
    guild_id: int, id_alvo: str, adicionado_por: int,
    motivo: str | None = None, provas: list[str] | None = None,
) -> bool:
    """Adiciona um ID na blacklist do servidor. Retorna False se já estava
    cadastrado (não sobrescreve data/quem adicionou original). `provas` é
    uma lista de URLs de anexo (imagens/prints) enviadas no chat, guardada
    como JSON."""
    db = await get_conn()
    provas_json = json.dumps(provas) if provas else None
    cursor = await db.execute(
        "INSERT OR IGNORE INTO blacklist (guild_id, id_alvo, motivo, adicionado_por, provas) VALUES (?, ?, ?, ?, ?)",
        (guild_id, str(id_alvo), motivo, adicionado_por, provas_json)
    )
    await db.commit()
    return cursor.rowcount > 0


async def remover_blacklist(guild_id: int, id_alvo: str) -> bool:
    """Remove um ID da blacklist. Retorna False se ele não estava cadastrado."""
    db = await get_conn()
    cursor = await db.execute(
        "DELETE FROM blacklist WHERE guild_id = ? AND id_alvo = ?",
        (guild_id, str(id_alvo))
    )
    await db.commit()
    return cursor.rowcount > 0


async def verificar_blacklist(guild_id: int, id_alvo: str) -> dict | None:
    """Retorna os dados do registro (motivo, quem/quando adicionou) se o ID
    estiver na blacklist do servidor, ou None se não estiver."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM blacklist WHERE guild_id = ? AND id_alvo = ?",
        (guild_id, str(id_alvo))
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def listar_blacklist(guild_id: int) -> list[dict]:
    """Todos os IDs banidos desse servidor, do mais recente pro mais antigo."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM blacklist WHERE guild_id = ? ORDER BY adicionado_em DESC",
        (guild_id,)
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def contar_blacklist(guild_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute("SELECT COUNT(*) FROM blacklist WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    return row[0] if row else 0


def parse_provas_blacklist(valor: str | None) -> list[str]:
    """Converte o JSON guardado em `blacklist.provas` numa lista de URLs.
    Nunca quebra em valor vazio/corrompido — cai em lista vazia."""
    if not valor:
        return []
    try:
        lista = json.loads(valor)
        return lista if isinstance(lista, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


async def obter_cargos_blacklist(guild_id: int) -> list[int]:
    """IDs dos cargos (até 3) com permissão de add/remover na blacklist,
    configurados em /painel_blacklist."""
    config = await obter_config(guild_id)
    valor = config.get("cargos_blacklist")
    if not valor:
        return []
    return [int(x) for x in str(valor).split(",") if x.strip().isdigit()]


async def definir_cargos_blacklist(guild_id: int, ids: list[int]) -> None:
    """Salva os cargos (até 3) com permissão de add/remover na blacklist."""
    texto = ",".join(str(i) for i in ids) if ids else None
    await atualizar_config(guild_id, "cargos_blacklist", texto)


# ========================================================================
# MODERAÇÃO — usado por cogs/moderacao.py
# ========================================================================
async def obter_moderacao_config(guild_id: int) -> dict:
    # FIX conexão: essa função era chamada em TODO /warn, /mute, /kick, /ban
    # etc, e também em todo caso de automod do convites.py — cada chamada
    # era um SELECT novo na conexão única do SQLite. Com cache em memória,
    # só bate no banco na 1ª vez (ou depois de uma config nova). Invalidado
    # automaticamente em atualizar_moderacao_config.
    if guild_id in _cache_moderacao_config:
        return _cache_moderacao_config[guild_id]
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM moderacao_config WHERE guild_id = ?", (guild_id,))
    row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO moderacao_config (guild_id) VALUES (?)", (guild_id,))
        await db.commit()
        cursor = await db.execute("SELECT * FROM moderacao_config WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
    resultado = dict(row) if row else {}
    _cache_moderacao_config[guild_id] = resultado
    return resultado


_COLUNAS_MODERACAO_CONFIG = {
    "canal_log", "warns_limite", "warns_acao", "warns_acao_duracao",
}


async def atualizar_moderacao_config(guild_id: int, **kwargs) -> dict:
    if not kwargs:
        return await obter_moderacao_config(guild_id)
    colunas_invalidas = set(kwargs) - _COLUNAS_MODERACAO_CONFIG
    if colunas_invalidas:
        raise ValueError(f"Coluna(s) inválida(s) em moderacao_config: {colunas_invalidas}")
    await obter_moderacao_config(guild_id)  # garante que a linha existe
    db = await get_conn()
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    await db.execute(f"UPDATE moderacao_config SET {campos} WHERE guild_id = ?", (*kwargs.values(), guild_id))
    await db.commit()
    _cache_moderacao_config.pop(guild_id, None)
    return await obter_moderacao_config(guild_id)


async def criar_caso_moderacao(
    guild_id: int, tipo: str, user_id: int, moderador_id: int,
    motivo: str | None = None, duracao_segundos: int | None = None, expira_em: str | None = None
) -> dict:
    """Cria um novo caso de moderação (warn/mute/unmute/kick/ban/unban) e
    devolve o registro criado já com o número sequencial do servidor
    (Caso #N).

    FIX: "ler o contador -> incrementar -> gravar o caso" é feito em 3
    instruções SQL separadas. O aiosqlite serializa cada instrução
    individual na conexão, mas isso NÃO torna a sequência inteira atômica
    -- duas coroutines criando casos quase ao mesmo tempo (ex: anti-raid
    banindo vários membros do mesmo pico de entrada de uma vez) podiam
    intercalar entre essas instruções e as duas lerem o mesmo "próximo
    número" antes de qualquer uma terminar de gravar. Como não existe
    UNIQUE constraint em (guild_id, numero), isso não dava erro — só
    criava dois casos com o mesmo número, silenciosamente. O lock por
    guild garante que a sequência roda inteira antes da próxima começar."""
    async with _lock_guild(guild_id):
        db = await get_conn()
        await db.execute(
            "INSERT INTO moderacao_contador (guild_id, proximo) VALUES (?, 2) "
            "ON CONFLICT(guild_id) DO UPDATE SET proximo = proximo + 1",
            (guild_id,)
        )
        cursor = await db.execute("SELECT proximo FROM moderacao_contador WHERE guild_id = ?", (guild_id,))
        row = await cursor.fetchone()
        numero = (row[0] - 1) if row else 1

        await db.execute(
            "INSERT INTO moderacao_casos "
            "(guild_id, numero, tipo, user_id, moderador_id, motivo, duracao_segundos, expira_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, numero, tipo, user_id, moderador_id, motivo, duracao_segundos, expira_em)
        )
        await db.commit()
        return await obter_caso(guild_id, numero)


async def obter_caso(guild_id: int, numero: int) -> dict | None:
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM moderacao_casos WHERE guild_id = ? AND numero = ?", (guild_id, numero)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def obter_casos_usuario(guild_id: int, user_id: int, tipo: str | None = None, limite: int = 25) -> list[dict]:
    """Histórico de casos de um usuário, do mais recente pro mais antigo.
    Inclui casos revogados (ativo=0) — pra revogado usar contar_warns_ativos."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    query = "SELECT * FROM moderacao_casos WHERE guild_id = ? AND user_id = ?"
    params = [guild_id, user_id]
    if tipo:
        query += " AND tipo = ?"
        params.append(tipo)
    query += " ORDER BY numero DESC LIMIT ?"
    params.append(limite)
    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def contar_warns_ativos(guild_id: int, user_id: int) -> int:
    db = await get_conn()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM moderacao_casos WHERE guild_id = ? AND user_id = ? AND tipo = 'warn' AND ativo = 1",
        (guild_id, user_id)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def revogar_caso(guild_id: int, numero: int) -> bool:
    """Marca um caso (normalmente um warn) como inativo — some da contagem
    de warns ativos mas continua no histórico. Retorna False se o caso não existir."""
    db = await get_conn()
    cursor = await db.execute(
        "UPDATE moderacao_casos SET ativo = 0 WHERE guild_id = ? AND numero = ?", (guild_id, numero)
    )
    await db.commit()
    return cursor.rowcount > 0


async def obter_bans_temporarios_para_expirar() -> list[dict]:
    """Bans com prazo (expira_em) já vencido e ainda ativos, de todos os
    servidores — usado pela task em loop que desbane automaticamente."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM moderacao_casos WHERE tipo = 'ban' AND ativo = 1 "
        "AND expira_em IS NOT NULL AND datetime(expira_em) <= datetime('now')"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def marcar_caso_expirado(guild_id: int, numero: int):
    """Fecha o ciclo de um ban temporário depois que o unban automático rodou."""
    db = await get_conn()
    await db.execute(
        "UPDATE moderacao_casos SET ativo = 0 WHERE guild_id = ? AND numero = ?", (guild_id, numero)
    )
    await db.commit()


# ========================================================================
# DONOS DO BOT + SENHA DO PAINEL — usado por cogs/owner.py e cogs/painel_owner.py
# ========================================================================
async def eh_owner(user_id: int) -> bool:
    """True se esse usuário está cadastrado como dono do bot."""
    db = await get_conn()
    cursor = await db.execute("SELECT 1 FROM bot_owners WHERE user_id = ?", (user_id,))
    return await cursor.fetchone() is not None


async def listar_owners() -> list[int]:
    """IDs de todos os donos cadastrados, do mais antigo pro mais novo."""
    db = await get_conn()
    cursor = await db.execute("SELECT user_id FROM bot_owners ORDER BY adicionado_em ASC")
    rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def contar_owners() -> int:
    db = await get_conn()
    cursor = await db.execute("SELECT COUNT(*) FROM bot_owners")
    row = await cursor.fetchone()
    return row[0] if row else 0


async def adicionar_owner(user_id: int, adicionado_por: int):
    db = await get_conn()
    await db.execute(
        "INSERT OR IGNORE INTO bot_owners (user_id, adicionado_por) VALUES (?, ?)",
        (user_id, adicionado_por)
    )
    await db.commit()


async def remover_owner(user_id: int):
    db = await get_conn()
    await db.execute("DELETE FROM bot_owners WHERE user_id = ?", (user_id,))
    await db.commit()


# ---------- Senha persistente do painel ----------
async def obter_senha_owner() -> dict | None:
    """Retorna {'senha_hash':..., 'senha_salt':...} ou None se ainda não foi configurada."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT senha_hash, senha_salt FROM owner_senha WHERE id = 1")
    row = await cursor.fetchone()
    return dict(row) if row else None


async def definir_senha_owner(senha_hash: str, senha_salt: str, definida_por: int):
    db = await get_conn()
    await db.execute(
        "INSERT INTO owner_senha (id, senha_hash, senha_salt, definida_por, definida_em) "
        "VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(id) DO UPDATE SET "
        "senha_hash = excluded.senha_hash, senha_salt = excluded.senha_salt, "
        "definida_por = excluded.definida_por, definida_em = CURRENT_TIMESTAMP",
        (senha_hash, senha_salt, definida_por)
    )
    await db.commit()


# ---------- Código de recuperação de senha (/esqueciasenha) ----------
async def criar_codigo_reset(codigo: str, expira_em: str, criado_por: int):
    db = await get_conn()
    await db.execute(
        "INSERT INTO owner_codigos_reset (codigo, expira_em, criado_por) VALUES (?, ?, ?)",
        (codigo, expira_em, criado_por)
    )
    await db.commit()


async def consumir_codigo_reset(codigo: str) -> bool:
    """Valida o código (existe, não usado, não expirado) e já marca como usado.
    Retorna True se era válido, False caso contrário."""
    db = await get_conn()
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM owner_codigos_reset WHERE codigo = ? AND usado = 0 "
        "AND datetime(expira_em) >= datetime('now')",
        (codigo,)
    )
    row = await cursor.fetchone()
    if not row:
        return False
    await db.execute("UPDATE owner_codigos_reset SET usado = 1 WHERE codigo = ?", (codigo,))
    await db.commit()
    return True


# ============================================================================
# LOJA DIGITAL (produto + estoque + entrega automática) — ver
# cogs/loja_produtos.py. Sistema novo, independente da "Loja" de coins do
# cassino.py (aquela vende cargo/cosmético com moeda interna; essa aqui
# entrega produto de verdade — conta, key, código — pro cliente).
# ============================================================================

async def criar_produto(guild_id: int, nome: str, descricao: str, preco: float, emoji: str, criado_por: int) -> int:
    conn = await get_conn()
    cursor = await conn.execute(
        "INSERT INTO produtos_digitais (guild_id, nome, descricao, preco, emoji, criado_por) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, nome, descricao, preco, emoji, criado_por)
    )
    await conn.commit()
    return cursor.lastrowid


async def listar_produtos(guild_id: int, somente_ativos: bool = True) -> list:
    conn = await get_conn()
    conn.row_factory = aiosqlite.Row
    if somente_ativos:
        cursor = await conn.execute(
            "SELECT * FROM produtos_digitais WHERE guild_id = ? AND ativo = 1 ORDER BY id", (guild_id,)
        )
    else:
        cursor = await conn.execute(
            "SELECT * FROM produtos_digitais WHERE guild_id = ? ORDER BY id", (guild_id,)
        )
    return await cursor.fetchall()


async def obter_produto(produto_id: int):
    conn = await get_conn()
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute("SELECT * FROM produtos_digitais WHERE id = ?", (produto_id,))
    return await cursor.fetchone()


async def editar_produto(produto_id: int, **campos) -> None:
    """Atualiza só os campos passados (ex: editar_produto(5, preco=9.90,
    ativo=0)). Sem SQL solto no cog -- monta o SET dinamicamente aqui."""
    if not campos:
        return
    conn = await get_conn()
    sets = ", ".join(f"{k} = ?" for k in campos)
    valores = list(campos.values()) + [produto_id]
    await conn.execute(f"UPDATE produtos_digitais SET {sets} WHERE id = ?", valores)
    await conn.commit()


async def excluir_produto(produto_id: int) -> None:
    """Apaga o produto e qualquer estoque AINDA NÃO entregue dele. O
    histórico de entregas (produtos_entregas_log) nunca é apagado junto --
    fica como registro permanente de vendas passadas."""
    conn = await get_conn()
    await conn.execute("DELETE FROM produtos_estoque WHERE produto_id = ? AND entregue = 0", (produto_id,))
    await conn.execute("DELETE FROM produtos_digitais WHERE id = ?", (produto_id,))
    await conn.commit()


async def adicionar_estoque(produto_id: int, linhas: list, adicionado_por: int) -> int:
    """Adiciona várias unidades de estoque de uma vez (1 linha = 1 unidade
    entregável, ex: 1 conta ou 1 key por linha). Devolve quantas linhas
    foram realmente adicionadas (ignora linhas em branco)."""
    linhas_limpas = [l.strip() for l in linhas if l.strip()]
    if not linhas_limpas:
        return 0
    conn = await get_conn()
    await conn.executemany(
        "INSERT INTO produtos_estoque (produto_id, conteudo, adicionado_por) VALUES (?, ?, ?)",
        [(produto_id, linha, adicionado_por) for linha in linhas_limpas]
    )
    await conn.commit()
    return len(linhas_limpas)


async def contar_estoque_disponivel(produto_id: int) -> int:
    conn = await get_conn()
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM produtos_estoque WHERE produto_id = ? AND entregue = 0", (produto_id,)
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def entregar_um_item(produto_id: int, usuario_id: int, tentativas: int = 3):
    """Pega 1 unidade disponível do estoque e marca como entregue pro
    usuário, devolvendo {"id": ..., "conteudo": ...} — ou None se o
    estoque estiver vazio.

    Protegido contra 2 pessoas clicando "Comprar" ao mesmo tempo levarem a
    MESMA unidade: o UPDATE tem `WHERE ... AND entregue = 0` embutido, e
    depois confere `rowcount` -- se vier 0, é sinal de que outra entrega
    ganhou a corrida entre o SELECT e o UPDATE, então tenta de novo com a
    próxima unidade disponível (até `tentativas` vezes)."""
    conn = await get_conn()
    conn.row_factory = aiosqlite.Row
    for _ in range(tentativas):
        cursor = await conn.execute(
            "SELECT id, conteudo FROM produtos_estoque WHERE produto_id = ? AND entregue = 0 ORDER BY id LIMIT 1",
            (produto_id,)
        )
        item = await cursor.fetchone()
        if not item:
            return None
        update_cursor = await conn.execute(
            "UPDATE produtos_estoque SET entregue = 1, entregue_para = ?, entregue_em = CURRENT_TIMESTAMP "
            "WHERE id = ? AND entregue = 0",
            (usuario_id, item["id"])
        )
        await conn.commit()
        if update_cursor.rowcount > 0:
            return {"id": item["id"], "conteudo": item["conteudo"]}
    return None


async def registrar_entrega_log(guild_id: int, produto_id: int, estoque_id, usuario_id: int, nome_produto: str) -> None:
    conn = await get_conn()
    await conn.execute(
        "INSERT INTO produtos_entregas_log (guild_id, produto_id, estoque_id, usuario_id, nome_produto) VALUES (?, ?, ?, ?, ?)",
        (guild_id, produto_id, estoque_id, usuario_id, nome_produto)
    )
    await conn.commit()


async def historico_entregas(guild_id: int, limite: int = 20) -> list:
    conn = await get_conn()
    conn.row_factory = aiosqlite.Row
    cursor = await conn.execute(
        "SELECT * FROM produtos_entregas_log WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
        (guild_id, limite)
    )
    return await cursor.fetchall()
