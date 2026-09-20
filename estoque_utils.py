"""
estoque_utils.py — FFZ Vendas V2

Funções PURAS (sem Discord, sem banco) pra transformar o conteúdo de um
arquivo/texto colado em uma lista limpa de itens de estoque. Ficam separadas
do database.py pra dar pra testar sem subir o bot.
"""

from constants import MAX_LINHAS_ESTOQUE, MAX_TAMANHO_ITEM_ESTOQUE


class ArquivoEstoqueInvalido(ValueError):
    """Arquivo grande demais, vazio ou com conteúdo que não dá pra ler."""


def decodificar_arquivo(dados: bytes) -> str:
    """Decodifica os bytes de um .txt. Aceita UTF-8 (com ou sem BOM, que é
    o que o Bloco de Notas do Windows costuma gravar) e cai pra Latin-1
    como último recurso pra não travar com arquivo de encoding antigo."""
    if b"\x00" in dados[:2048]:
        raise ArquivoEstoqueInvalido("Esse arquivo parece binário, não texto. Envie um .txt.")
    try:
        return dados.decode("utf-8-sig")
    except UnicodeDecodeError:
        return dados.decode("latin-1")


def extrair_itens(texto: str) -> list[str]:
    """Um item por linha. Tira espaços nas pontas e ignora linhas vazias.
    Não remove repetidos aqui — quem decide isso é o banco (ver
    database.adicionar_estoque_itens), porque depende do que já existe."""
    return [linha.strip() for linha in texto.splitlines() if linha.strip()]


def itens_do_arquivo(dados: bytes) -> list[str]:
    """Bytes do arquivo -> lista de itens, já validando o limite de linhas."""
    itens = extrair_itens(decodificar_arquivo(dados))
    if not itens:
        raise ArquivoEstoqueInvalido("O arquivo está vazio (nenhuma linha com conteúdo).")
    if len(itens) > MAX_LINHAS_ESTOQUE:
        raise ArquivoEstoqueInvalido(
            f"O arquivo tem {len(itens)} linhas — o máximo por envio é {MAX_LINHAS_ESTOQUE}. Divida em partes."
        )
    return itens


def separar_validos(itens: list[str]) -> tuple[list[str], int]:
    """Separa os itens que cabem no limite de tamanho. Retorna (validos,
    qtd_invalidos). Item gigante quase sempre é arquivo errado (ex: um
    JSON inteiro numa linha só) e não caberia na DM/mensagem de entrega."""
    validos = [i for i in itens if len(i) <= MAX_TAMANHO_ITEM_ESTOQUE]
    return validos, len(itens) - len(validos)
