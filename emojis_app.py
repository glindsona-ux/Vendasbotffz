"""
emojis_app.py — emojis CUSTOM do app (Discord Developer Portal → Emojis).

O bot não usa mais nenhum emoji "normal" de teclado: tudo que aparece pro
usuário (mensagens, cards, títulos, botões, menus) vem daqui. Os emojis
foram enviados no portal do desenvolvedor (Application Emojis), então
funcionam em qualquer servidor onde o bot estiver, sem precisar subir
emoji em cada um.

COMO USAR NO CÓDIGO
    from emojis_app import E

    f"{E.OK} Salvo!"                        # dentro de texto
    discord.ui.Button(label="X", emoji=E.RAIO)   # em botão / select

COMO TROCAR UM EMOJI
    Mude só o nome do portal na linha do apelido em `class E` lá embaixo
    (ex.: CHAVE = _e("king") → CHAVE = _e("nome_do_emoji_novo")) e, se o
    emoji for novo, adicione o nome + ID na tabela `_IDS`.
    O ID está no portal: Developer Portal → seu app → Emojis → coluna
    "ID do emoji".

ONDE CUSTOM EMOJI NÃO APARECE (limitação do Discord, não do bot):
título de modal, rodapé/autor de embed, nome/descrição de slash command,
nome de tópico/canal e texto do label de botão. Nesses lugares o bot usa
só texto (no botão o emoji vai no campo `emoji=`, separado do label).
"""

# Nome EXATO no portal → ID do emoji.
_IDS: dict[str, int] = {
    # ── página 1 ─────────────────────────────────────────────────────
    "Cart": 1551167908191674429,
    "Raio": 1551167875652259951,
    "Eclma": 1551167811534192710,
    "Seta": 1551167766306754610,
    "Estoque": 1551167717552300032,
    "Lapis": 1551167681871220797,
    "wrong": 1541250723864510675,
    "truck": 1541250721209655338,
    "time": 1541250718755979304,
    "red": 1541250715639357608,
    "receipt": 1541250713391341618,
    "reaction": 1541250710509846710,
    "pin": 1541250707473170432,
    "picpay": 1541250704759455927,
    "php": 1541250701139906641,
    "pause": 1541250698971320422,
    "mobile": 1541250695959941241,
    "minus": 1541250693309136896,
    "member": 1541250690217680896,
    "mail2": 1541250687768334366,
    "link": 1541250685230907412,
    "light_on": 1541250682852605952,
    "king": 1541250680319250505,
    "information": 1541250677836218458,
    "hrench2": 1541250675533553715,
    # ── página 2 ─────────────────────────────────────────────────────
    "heart2": 1541250673268625488,
    "heart": 1541250670500253718,
    "hammer": 1541250667434213477,
    "gift2": 1541250665433669733,
    "ghost": 1541250663026135180,
    "fire": 1541250660765274203,
    "donation": 1541250656008937523,
    "controller": 1541250653387628546,
    "colors": 1541250651017977926,
    "cloud": 1541250648077508730,
    "blue": 1541250642839076907,
    "basket": 1541250640620032092,
    "arrow3": 1541250638095056967,
    "announcement2": 1541250635620683837,
    "lixo": 1540298989054861372,
    "_ban_emoji": 1540298987683188789,
    "REEMBOLSO": 1540298984239792180,
    "emoji_38": 1540298983845531720,
    "001analytics": 1540298980993273857,
    "refresh": 1540298979605225523,
    "001megafone": 1540298978317434911,
    "Icon_Negative": 1540298977474252913,
    "Icon_Confirm": 1540298976031416331,
    "faturamento": 1540298974802739210,
    "lapis": 1540298973762555984,
    # ── página 3 ─────────────────────────────────────────────────────
    "mistosstk": 1540298972109865033,
    "celular": 1540298971321466920,
    "faturamento_emu": 1540298969693950025,
    "emoji_180": 1540298966871314524,
    "emoji_77": 1540298962601513060,
    "emoji_30": 1540298961674444836,
    "emoji_212": 1540298960332267583,
    "emoji_36": 1540298959371903059,
    "lupa": 1540298952732057630,
    "001pix": 1540298951738003516,
    "emoji_249": 1540298950765060096,
    "emoji_24": 1540298949913747486,
}


def _e(nome: str) -> str:
    """Nome do portal → markup `<:nome:id>` que o Discord renderiza."""
    return f"<:{nome}:{_IDS[nome]}>"


def obter(nome: str, padrao: str = "") -> str:
    """Compat com a interface antiga. Devolve o emoji custom pelo nome do
    portal; se o nome não existir devolve `padrao` (vazio por padrão —
    nunca cai em emoji de teclado)."""
    return _e(nome) if nome in _IDS else padrao


def total() -> int:
    return len(_IDS)


class E:
    """Apelidos usados no código → emoji custom do portal."""

    # ── feedback / status ────────────────────────────────────────────
    OK = _e("emoji_249")            # check verde
    ERRO = _e("wrong")              # X vermelho em círculo
    AVISO = _e("Eclma")             # exclamação
    CANCELADO = _e("emoji_24")      # X vermelho (cancelado / inativo / desativado)
    ATIVO = _e("Icon_Confirm")      # toggle ligado (configurado)
    INATIVO = _e("Icon_Negative")   # toggle desligado (não configurado)
    DISPONIVEL = _e("blue")         # bolinha azul (key disponível)
    TEMPO = _e("time")              # relógio (aguardando / cooldown)
    EXPIRADO = _e("pause")          # expirado

    # ── loja ─────────────────────────────────────────────────────────
    CARRINHO = _e("Cart")
    SACOLA = _e("basket")           # vitrines / "seu pedido"
    ESTOQUE = _e("Estoque")         # produtos / pedidos
    CUPOM = _e("gift2")
    TICKET = _e("receipt")          # entrega manual
    ARQUIVO = _e("receipt")         # leitura de arquivo .txt
    COPIAR = _e("receipt")          # botão "Copiar Key"
    PIX = _e("001pix")              # pagamento / Pix
    RECEBIDO = _e("faturamento")    # pagamento recebido a confirmar
    RAIO = _e("Raio")               # entrega / pagamento automático

    # ── ações ────────────────────────────────────────────────────────
    LIXO = _e("lixo")               # apagar / esvaziar / limpar
    VOLTAR = _e("emoji_38")         # restaurar / voltar
    ATUALIZAR = _e("refresh")       # sincronizar
    LINK = _e("link")
    FERRAMENTA = _e("hrench2")      # configurar

    # ── visual ───────────────────────────────────────────────────────
    IMAGEM = _e("colors")           # foto de perfil
    PALETA = _e("emoji_36")         # banner

    # ── licença ──────────────────────────────────────────────────────
    CHAVE = _e("king")              # key de licença (não tem emoji de chave no portal)
    BLOQUEIO = _e("_ban_emoji")     # sem acesso (não tem emoji de cadeado no portal)
    PLANO_BASICO = _e("blue")
    PLANO_PREMIUM = _e("fire")
    PLANO_VITALICIO = _e("king")
    BARRA_CHEIA = _e("blue")        # barra de tempo restante
    BARRA_VAZIA = _e("red")

    # ── gateways de pagamento ────────────────────────────────────────
    GATEWAY_MERCADOPAGO = _e("blue")
    GATEWAY_LIVEPIX = _e("001pix")
    GATEWAY_PAGBANK = _e("faturamento")
