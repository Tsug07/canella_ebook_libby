"""sessao.py — cookies assinados (HMAC) do visitante e do admin.

Mesmo modelo da Libby: o conteúdo vai em claro (só ids, nunca senha) e a
assinatura impede adulterar. Dois cookies separados porque são públicos
diferentes — o leitor que pediu acesso e a equipe que administra.

Visitante: login sem senha. A barreira é o formulário (funil de leads), não
segurança de conta — quem informa o e-mail de um lead já liberado entra como
ele. Para ebooks de marketing isso é aceitável; a marca d'água no download
identifica quem baixou.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

COOKIE_LEITOR = "biblioteca_leitor"
COOKIE_ADMIN = "biblioteca_admin"
# O cookie do painel só trafega em /admin. Este, com o mesmo conteúdo, vale no
# site todo e só serve para a estante reconhecer a equipe (tudo liberado).
# Não abre o painel: quem decide isso continua sendo o COOKIE_ADMIN.
COOKIE_EQUIPE = "biblioteca_equipe"
DURACAO_LEITOR = 60 * 60 * 24 * 365      # 1 ano: o lead não deve refazer o funil
DURACAO_ADMIN = 60 * 60 * 12

_SEGREDO = os.environ.get("SECRET_KEY", "")
if not _SEGREDO:
    # Sem SECRET_KEY o app sobe (útil em dev), mas cada restart derruba as sessões.
    _SEGREDO = secrets.token_hex(32)
    print("[sessao] SECRET_KEY nao definido: usando chave temporaria")
_CHAVE = _SEGREDO.encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def assinar(dados: dict, duracao: int) -> str:
    corpo = _b64(json.dumps({**dados, "exp": int(time.time()) + duracao}).encode())
    sig = _b64(hmac.new(_CHAVE, corpo.encode(), hashlib.sha256).digest())
    return f"{corpo}.{sig}"


def ler(valor: str | None) -> dict | None:
    if not valor or "." not in valor:
        return None
    corpo, sig = valor.rsplit(".", 1)
    esperado = _b64(hmac.new(_CHAVE, corpo.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, esperado):
        return None
    try:
        dados = json.loads(_unb64(corpo))
    except (ValueError, json.JSONDecodeError):
        return None
    if dados.get("exp", 0) < time.time():
        return None
    return dados


def senha_admin_ok(senha: str) -> bool:
    """Senha única da equipe, definida em ADMIN_SENHA no .env.production."""
    certa = os.environ.get("ADMIN_SENHA", "")
    return bool(certa) and hmac.compare_digest(senha.encode(), certa.encode())
