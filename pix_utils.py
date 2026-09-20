import qrcode
from qrcode.image.pure import PyPNGImage
import io
import unicodedata


def _remover_acentos(texto: str) -> str:
    """'José' -> 'JOSE', não 'JOS'. encode('ascii', 'ignore') sozinho
    APAGA letras acentuadas ao invés de convertê-las; NFKD primeiro separa
    a letra do acento, aí sim o ignore descarta só o acento."""
    return unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode()

def formatar_campo(id, valor):
    return f"{id}{len(valor):02d}{valor}"

def crc16(data):
    poly = 0x1021
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ poly
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc

def gerar_payload_pix(chave_pix, nome_recebedor, cidade="Sao Paulo", valor=0.0):
    """Monta o BR Code (Pix Copia e Cola) no padrão EMV do Banco Central.
    chave_pix vai sem alteração (a chave PRECISA bater exatamente com o
    que está cadastrado no banco). nome/cidade são só exibição — o app do
    pagador limita a 25 e 15 caracteres, então cortamos e removemos
    acentos aqui pra não gerar um payload fora do padrão."""
    chave_pix = (chave_pix or "").strip()[:77]  # limite do campo 01, campo 26
    nome = _remover_acentos(nome_recebedor or "PAGAMENTO PIX")[:25].strip().upper() or "PAGAMENTO PIX"
    cidade = _remover_acentos(cidade or "BRASIL")[:15].strip().upper() or "BRASIL"

    payload = ""
    payload += formatar_campo("00", "01")
    payload += formatar_campo("01", "11")  # Point of Initiation Method: 11 = QR estático (sem endpoint dinâmico)
    payload += formatar_campo("26", formatar_campo("00", "BR.GOV.BCB.PIX") + formatar_campo("01", chave_pix))
    payload += formatar_campo("52", "0000")
    payload += formatar_campo("53", "986")
    if valor and valor > 0:
        payload += formatar_campo("54", f"{valor:.2f}")
    payload += formatar_campo("58", "BR")
    payload += formatar_campo("59", nome)
    payload += formatar_campo("60", cidade)
    payload += formatar_campo("62", formatar_campo("05", "***"))
    payload += "6304"
    payload += f"{crc16(payload.encode('utf-8')):04X}"
    return payload

TAMANHO_ALVO_PX = 600
# Largura final alvo do QR (em pixels), fixa não importa o tamanho do
# payload. Motivo: com box_size FIXO, um payload mais longo (ex: quando
# tem valor incluso, campo "54", ou nome/cidade grandes) precisa de mais
# módulos -- e mesmo com o mesmo box_size, a imagem final sai bem mais
# larga. Passado um certo tamanho, o Discord passa a tratar o anexo como
# imagem "grande" (full-bleed) e para de desenhar a barrinha colorida da
# embed do lado dela -- por isso o "!pix" sozinho (payload curto, sem
# valor) sempre ficou com a barra do lado, e o "!pix <valor>" (payload
# mais longo) não. Calculando o box_size na hora, pro resultado final
# bater sempre no mesmo tamanho físico, isso não acontece mais.
#
# AJUSTE (pedido: QR saindo em modo "thumbnail" com a barra do lado, em
# TODO card -- Pix detectado E Pagamento Liberado): 330px acabou ficando
# ABAIXO do limiar que o Discord usa pra trocar pro modo "grande" -- por
# isso os dois saíam sempre no modo compacto, nunca no modo full-bleed.
# 600px fica folgado acima desse limiar, então passa a sair sempre no
# modo grande, com o QR (e a logo do servidor no centro) bem legível,
# em qualquer card e em qualquer servidor.


def _colar_logo_no_centro(img_qr, logo_bytes):
    """Cola a logo (ex: ícone do servidor) no centro do QR, com um fundo
    branco redondo por baixo pra manter contraste e não deixar módulos
    pretos vazando por trás de cantos transparentes do ícone.

    Só é seguro por causa do ERROR_CORRECT_H usado em gerar_qrcode_bytes
    quando tem logo (~30% de tolerância a dano) -- cobrir uma fatia
    pequena do centro não impede o scanner de ler; cobrir demais, ou usar
    correção de erro mais baixa, quebraria o QR de verdade."""
    from PIL import Image, ImageDraw

    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")

    largura_qr, altura_qr = img_qr.size
    # Logo ocupa no máximo ~22% da largura do QR -- acima disso o
    # ERROR_CORRECT_H (30%) não segura mais o dano.
    tamanho_logo = int(min(largura_qr, altura_qr) * 0.22)
    logo = logo.resize((tamanho_logo, tamanho_logo), Image.LANCZOS)

    # Máscara circular -- fica com cara de "selo" e não briga com o
    # formato quadrado dos módulos do QR ao redor.
    mascara = Image.new("L", (tamanho_logo, tamanho_logo), 0)
    ImageDraw.Draw(mascara).ellipse((0, 0, tamanho_logo, tamanho_logo), fill=255)

    fundo_tam = int(tamanho_logo * 1.25)
    fundo = Image.new("RGBA", (fundo_tam, fundo_tam), (255, 255, 255, 0))
    ImageDraw.Draw(fundo).ellipse((0, 0, fundo_tam, fundo_tam), fill=(255, 255, 255, 255))
    pos_logo = ((fundo_tam - tamanho_logo) // 2, (fundo_tam - tamanho_logo) // 2)
    fundo.paste(logo, pos_logo, mascara)

    img_qr = img_qr.convert("RGBA")
    pos_final = ((largura_qr - fundo_tam) // 2, (altura_qr - fundo_tam) // 2)
    img_qr.paste(fundo, pos_final, fundo)
    return img_qr.convert("RGB")


def gerar_qrcode_bytes(chave, nome, cidade="Sao Paulo", valor=0.0, logo_bytes=None):
    """Gera a imagem PNG (em bytes) do QR Code real, a partir do payload
    Pix. Retorna (img_bytes, payload) — payload também serve como o
    texto "Pix Copia e Cola" pra quem não conseguir escanear.

    logo_bytes (opcional): bytes de uma imagem (ex: ícone do servidor,
    via obter_logo_guild) pra colar no centro do QR, deixando o QR "com
    a cara" de cada servidor (branding, multi-tenant por guild_id)."""
    payload = gerar_payload_pix(chave, nome, cidade=cidade, valor=valor)
    com_logo = logo_bytes is not None

    # Com logo cobrindo o centro precisa da correção de erro mais alta
    # (H, ~30%) pra continuar escaneável -- sem logo, M (~15%) já basta
    # e deixa o QR com menos módulos/mais legível de longe.
    nivel_correcao = qrcode.constants.ERROR_CORRECT_H if com_logo else qrcode.constants.ERROR_CORRECT_M

    # 1ª passada só pra descobrir quantos módulos ESSE payload específico
    # vai precisar (varia com o tamanho do payload) -- box_size=1 aqui é só
    # pra sondar, a imagem dessa passada é descartada.
    qr_sonda = qrcode.QRCode(error_correction=nivel_correcao, box_size=1, border=2)
    qr_sonda.add_data(payload)
    qr_sonda.make(fit=True)
    total_modulos = qr_sonda.modules_count + 2 * qr_sonda.border

    # box_size calculado pra bater sempre perto de TAMANHO_ALVO_PX de
    # largura final, não importa quantos módulos o payload precisou.
    # Nunca deixa passar de 3px por módulo (senão fica ilegível pra
    # câmera de celular em payload muito longo).
    box_size = max(3, round(TAMANHO_ALVO_PX / total_modulos))

    qr = qrcode.QRCode(
        error_correction=nivel_correcao,
        box_size=box_size,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img_bytes = io.BytesIO()
    if com_logo:
        try:
            # Precisa do factory padrão (PIL), não do PyPNGImage, pra dar
            # pra colar a logo em cima depois -- PyPNGImage não suporta
            # paste(). BytesIO não tem nome de arquivo, então o format
            # precisa ser explícito no save.
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            img = _colar_logo_no_centro(img, logo_bytes)
            img.save(img_bytes, format="PNG")
        except Exception:
            # Best-effort: se algo no PIL/logo falhar, não perde o QR
            # inteiro por causa do branding -- cai pro QR normal sem logo.
            img = qr.make_image(image_factory=PyPNGImage)
            img.save(img_bytes)
    else:
        img = qr.make_image(image_factory=PyPNGImage)
        img.save(img_bytes)

    img_bytes.seek(0)
    return img_bytes, payload


async def obter_logo_guild(guild):
    """Baixa o ícone do servidor pra usar como logo central do QR --
    cada servidor (multi-tenant/SaaS, um por guild_id) manda o QR com a
    própria marca. Retorna None se o servidor não tiver ícone (best
    -effort: quem chamar cai pro QR sem logo, igual antes)."""
    if guild is None or guild.icon is None:
        return None
    try:
        return await guild.icon.read()
    except Exception:
        return None


async def baixar_e_redimensionar_qr(url: str, tamanho_max: int = TAMANHO_ALVO_PX):
    """Baixa uma imagem de QR Code que o MEDIADOR colou manualmente (link
    externo, tipo print do banco) e redimensiona pro mesmo tamanho enxuto
    dos QR Codes que o bot gera sozinho -- pedido depois que reparamos que
    o ajuste de box_size em gerar_qrcode_bytes só afeta QR gerado pelo bot,
    não esses links externos (ver cogs/fila.py -> montar_pagamento_liberado).
    Mantém a proporção original (maior lado = tamanho_max). Retorna
    (img_bytes, None) em caso de sucesso, ou (None, None) se falhar --
    best-effort, quem chamar deve cair pro link original nesse caso."""
    import aiohttp
    from PIL import Image

    try:
        async with aiohttp.ClientSession() as sessao:
            async with sessao.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                dados = await resp.read()

        img = Image.open(io.BytesIO(dados))
        img = img.convert("RGBA") if img.mode not in ("RGB", "RGBA") else img
        largura, altura = img.size
        maior_lado = max(largura, altura)
        if maior_lado > tamanho_max:
            escala = tamanho_max / maior_lado
            img = img.resize((max(1, int(largura * escala)), max(1, int(altura * escala))), Image.LANCZOS)

        saida = io.BytesIO()
        img.save(saida, format="PNG")
        saida.seek(0)
        return saida
    except Exception:
        return None
