"""Validação de chave PIX e de link de QR Code.

Compartilhado entre cogs/pix.py (cadastro dos mediadores) e
cogs/apagarpixadmin.py (geração pública de QR Code), pra não duplicar
a mesma regra de validação em dois lugares diferentes.
"""
import re
from urllib.parse import urlparse

DOMINIOS_IMAGEM_CONFIAVEIS = (
    "imgur.com", "i.imgur.com",
    "cdn.discordapp.com", "media.discordapp.net",
    "ibb.co", "i.ibb.co",
    "postimg.cc", "i.postimg.cc",
)
EXTENSOES_IMAGEM = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# Mesmos caracteres invisíveis tratados em database.py (limpar_url_imagem) —
# comuns quando o link é colado pelo celular. Sem essa limpeza, um link
# válido podia ser rejeitado como "inválido" só por causa de um caractere
# que nem aparece na tela.
_CARACTERES_INVISIVEIS = re.compile(r'[\u200b\u200c\u200d\u2060\ufeff\u00a0]')

EMAIL_REGEX = re.compile(r"^[\w.\+\-]+@[\w\-]+\.[a-zA-Z]{2,}$")
UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

TIPO_CHAVE_LABEL = {
    "cpf": "CPF",
    "telefone": "Telefone",
    "email": "E-mail",
    "aleatoria": "Chave Aleatória",
}


def _somente_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto)


def chave_normalizada(chave: str) -> str:
    """Normaliza pra comparar duas chaves ignorando formatação (pontos,
    traços, parênteses, espaços, maiúsc/minúsc)."""
    return re.sub(r"[.\-()\s]", "", chave).strip().lower()


def validar_cpf(cpf: str) -> bool:
    """Validação real de CPF, com os dois dígitos verificadores (mod 11).
    Rejeita sequências óbvias tipo 111.111.111-11."""
    cpf = _somente_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False

    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    resto = (soma * 10) % 11
    dv1 = 0 if resto == 10 else resto
    if dv1 != int(cpf[9]):
        return False

    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    resto = (soma * 10) % 11
    dv2 = 0 if resto == 10 else resto
    return dv2 == int(cpf[10])


def validar_telefone(tel: str) -> bool:
    tel = _somente_digitos(tel)
    if tel.startswith("55") and len(tel) in (12, 13):
        tel = tel[2:]
    return len(tel) in (10, 11)


def detectar_tipo_chave(chave: str) -> str | None:
    """Identifica o tipo da chave PIX e valida seu formato.
    Retorna None se não bater com nenhum formato reconhecido."""
    chave = chave.strip()
    if UUID_REGEX.match(chave):
        return "aleatoria"
    if EMAIL_REGEX.match(chave):
        return "email"
    if validar_cpf(chave):
        return "cpf"
    if validar_telefone(chave):
        return "telefone"
    return None


def link_qr_valido(link: str) -> bool:
    """QR Code é opcional. Se preenchido, exige HTTPS e ou uma extensão de
    imagem válida, ou um domínio de host de imagem confiável — evita
    embutir links de rastreamento/phishing no painel."""
    link = _CARACTERES_INVISIVEIS.sub('', link).strip()
    if not link:
        return True
    if not link.startswith("https://"):
        return False
    try:
        host = urlparse(link).netloc.lower()
    except ValueError:
        return False
    if not host:
        return False

    dominio_ok = any(host == d or host.endswith("." + d) for d in DOMINIOS_IMAGEM_CONFIAVEIS)
    extensao_ok = link.lower().split("?")[0].endswith(EXTENSOES_IMAGEM)
    return dominio_ok or extensao_ok
