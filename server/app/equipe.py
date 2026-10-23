"""equipe.py — usuários do painel, senhas, 2 etapas, atividade e limite de tentativas.

Senha: PBKDF2-SHA256 com salt (mesma escolha da Libby: hash de mão única,
nunca reversível). Verificação em duas etapas: TOTP (Google Authenticator,
Microsoft Authenticator, Authy...), opcional por pessoa.

Limite de tentativas: cada tentativa vira uma linha em `tentativa` com uma
chave ("login-ip:1.2.3.4", "acesso:1.2.3.4"...). Contar as linhas da janela
diz se a chave estourou. Fica no banco para valer nos 2 workers.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import io
import random
import re
import secrets
import unicodedata

import pyotp
import qrcode
import qrcode.image.svg

from . import database as db

PAPEIS = {
    "admin": ("Administrador", "Tudo, inclusive gerenciar usuários, ver a atividade e baixar backups."),
    "equipe": ("Equipe", "Ebooks, eventos e leads. Não gerencia usuários nem backups."),
}
EMISSOR_2FA = "Biblioteca Canella & Santos"
_ITERACOES = 310_000

# Senhas que robôs testam primeiro. Não é a lista inteira do mundo, mas barra
# o óbvio ("canella123", "12345678"...).
_COMUNS = {"12345678", "123456789", "1234567890", "senha123", "senha1234", "password", "qwerty123",
           "canella", "canella123", "canellaesantos", "santos123", "admin123", "biblioteca", "contabilidade"}


# --- senha ------------------------------------------------------------------

def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, _ITERACOES)
    return f"pbkdf2_sha256${_ITERACOES}${base64.b64encode(sal).decode()}${base64.b64encode(h).decode()}"


def confere_senha(senha: str, guardado: str | None) -> bool:
    try:
        _, it, sal, h = (guardado or "").split("$")
        calc = hashlib.pbkdf2_hmac("sha256", senha.encode(), base64.b64decode(sal), int(it))
        return hmac.compare_digest(calc, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def problema_na_senha(senha: str, usuario: str = "") -> str | None:
    """Mensagem do que falta na senha, ou None se está boa."""
    if len(senha) < 10:
        return "A senha precisa de pelo menos 10 caracteres."
    if senha.isdigit():
        return "Não use só números: misture letras."
    base = senha.lower()
    if base in _COMUNS or any(base.startswith(c) and len(base) - len(c) <= 4 for c in ("canella", "santos", "senha", "admin")):
        return "Essa senha é fácil de adivinhar. Use uma frase ou uma senha gerada pelo gerenciador de senhas."
    if usuario and usuario.split("@")[0].lower() in base:
        return "Não use o seu nome de usuário como parte da senha."
    return None


def senha_temporaria() -> str:
    """Senha de entrega (a pessoa troca no 1º acesso). Sem caracteres ambíguos."""
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alfabeto) for _ in range(4)) for _ in range(3))


# --- 2 etapas ---------------------------------------------------------------

def novo_segredo_2fa() -> str:
    return pyotp.random_base32()


def codigo_ok(segredo: str | None, codigo: str) -> bool:
    codigo = "".join(c for c in (codigo or "") if c.isdigit())
    if not segredo or len(codigo) != 6:
        return False
    # valid_window=1 aceita o código anterior/seguinte (relógio do celular adiantado/atrasado).
    return pyotp.TOTP(segredo).verify(codigo, valid_window=1)


def qr_2fa_svg(segredo: str, usuario: str) -> str:
    uri = pyotp.TOTP(segredo).provisioning_uri(name=usuario, issuer_name=EMISSOR_2FA)
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


# --- usuários ---------------------------------------------------------------

def por_id(uid: int) -> dict | None:
    return db.one("SELECT * FROM admin_usuario WHERE id = %s", (uid,))


def por_email(email: str) -> dict | None:
    return db.one("SELECT * FROM admin_usuario WHERE LOWER(email) = LOWER(%s)", ((email or "").strip(),))


def por_usuario(usuario: str) -> dict | None:
    return db.one("SELECT * FROM admin_usuario WHERE LOWER(usuario) = LOWER(%s)", ((usuario or "").strip(),))


def por_login(texto: str) -> dict | None:
    """O campo do login aceita o usuário (o normal) ou o e-mail (quem tiver cadastrado)."""
    texto = (texto or "").strip()
    return por_usuario(texto) or (por_email(texto) if "@" in texto else None)


_RE_USUARIO = re.compile(r"^[a-z0-9][a-z0-9._-]{2,29}$")


def problema_no_usuario(usuario: str) -> str | None:
    if not _RE_USUARIO.match(usuario or ""):
        return "O usuário precisa ter de 3 a 30 caracteres: letras minúsculas sem acento, números, ponto, hífen ou _ (ex.: hugo, maria.silva)."
    return None


def sugerir_usuario(nome: str) -> str:
    """'Mariana Lima Souza' -> 'mariana.souza' (primeiro + último nome)."""
    partes = re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()).split()
    if not partes:
        return ""
    base = partes[0] if len(partes) == 1 else f"{partes[0]}.{partes[-1]}"
    return base[:30]


def existe_algum() -> bool:
    return db.one("SELECT 1 FROM admin_usuario LIMIT 1") is not None


def admins_ativos(exceto: int | None = None) -> int:
    return db.one("SELECT COUNT(*) AS n FROM admin_usuario WHERE papel = 'admin' AND ativo AND id IS DISTINCT FROM %s",
                  (exceto,))["n"]


# --- atividade --------------------------------------------------------------

def registrar(usuario: dict | None, acao: str, detalhe: str = "", ip: str = "") -> None:
    db.ex("INSERT INTO admin_log(usuario_id, usuario_nome, acao, detalhe, ip) VALUES (%s,%s,%s,%s,%s)",
          (usuario["id"] if usuario else None, usuario["nome"] if usuario else "—", acao, detalhe[:500], ip[:60]))


# --- limite de tentativas ---------------------------------------------------

def estourou(chave: str, maximo: int, minutos: int) -> bool:
    n = db.one("SELECT COUNT(*) AS n FROM tentativa WHERE chave = %s AND ts > now() - make_interval(mins => %s)",
               (chave, minutos))["n"]
    return n >= maximo


def anotar(chave: str) -> int:
    """Grava a tentativa e devolve quantas a chave tem na última hora."""
    db.ex("INSERT INTO tentativa(chave) VALUES (%s)", (chave,))
    if random.random() < 0.02:  # faxina de vez em quando: nada aqui precisa durar mais de 1 dia
        db.ex("DELETE FROM tentativa WHERE ts < now() - interval '1 day'")
    return db.one("SELECT COUNT(*) AS n FROM tentativa WHERE chave = %s AND ts > now() - interval '1 hour'",
                  (chave,))["n"]


def zerar(chave: str) -> None:
    db.ex("DELETE FROM tentativa WHERE chave = %s", (chave,))
