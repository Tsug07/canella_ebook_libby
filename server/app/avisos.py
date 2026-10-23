"""avisos.py — Discord (lead novo para a equipe) e e-mail (boas-vindas ao lead).

Mesmo esquema da Libby: webhook do Discord e Gmail por SMTP com senha de app
(GMAIL_USER / GMAIL_APP_PASSWORD). Tudo é best-effort: sem configuração, ou
com o serviço fora do ar, o aviso é pulado e o visitante nunca percebe —
o cadastro já foi gravado antes. As funções rodam em BackgroundTasks, depois
da resposta, para não atrasar a liberação do acesso.

Variáveis (.env.production):
  DISCORD_WEBHOOK_LEADS   URL do webhook do canal que recebe os leads
  DISCORD_MENCOES         opcional, ex.: "<@&1299044385899548752>"
  AVISAR_EVENTO=0         desliga o aviso de quem entrou só pelo QR
  AVISAR_ESTUDANTE=0      desliga o aviso de estudantes (útil em evento grande de faculdade)
  GMAIL_USER / GMAIL_APP_PASSWORD   remetente do e-mail de boas-vindas
  EMAIL_AVISO_LEADS       opcional: também manda o aviso de lead por e-mail
"""
from __future__ import annotations
import html
import json
import os
import smtplib
import ssl
import urllib.request
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

VINHO = 0x6C1F2E
DOURADO = 0xC8A369


def _log(msg: str) -> None:
    print(f"[avisos] {msg}", flush=True)


def discord(titulo: str, linhas: list[str], cor: int = VINHO, url: str | None = None) -> bool:
    webhook = os.environ.get("DISCORD_WEBHOOK_LEADS", "").strip()
    if not webhook:
        return False
    mencoes = os.environ.get("DISCORD_MENCOES", "").strip()
    embed = {"title": titulo[:250], "description": "\n".join(linhas)[:4000], "color": cor,
             "timestamp": datetime.now(timezone.utc).isoformat(),
             "footer": {"text": "Biblioteca Canella & Santos"}}
    if url:
        embed["url"] = url
    payload = {"content": mencoes or None, "allowed_mentions": {"parse": ["users", "roles"]},
               "embeds": [embed]}
    req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "BibliotecaCanella/1.0"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
        return True
    except Exception as e:
        _log(f"Discord falhou: {str(e)[:150]}")
        return False


def email_configurado() -> bool:
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


def email(para: str, assunto: str, html: str, texto: str) -> bool:
    usuario = os.environ.get("GMAIL_USER", "")
    senha = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not usuario or not senha or not para:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = assunto
    msg["From"] = formataddr(("Canella & Santos", usuario))
    msg["To"] = para
    msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=20) as s:
            s.login(usuario, senha)
            s.sendmail(usuario, [para], msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        _log("E-mail: falha de autenticação (confira GMAIL_USER/GMAIL_APP_PASSWORD).")
    except Exception as e:
        _log(f"E-mail falhou para {para}: {str(e)[:150]}")
    return False


def lead_novo(p: dict, tipo: str, ebook_titulo: str | None, url_painel: str) -> None:
    """Aviso à equipe. tipo: 'cadastro' (biblioteca completa) ou 'evento: <nome>'."""
    if tipo != "cadastro" and os.environ.get("AVISAR_EVENTO", "1") == "0":
        return
    estudante = p.get("perfil") == "estudante"
    if estudante and os.environ.get("AVISAR_ESTUDANTE", "1") == "0":
        return
    linhas = [f"**{p['nome']}** · {p['email']}"]
    if p.get("telefone"):
        dig = "".join(c for c in p["telefone"] if c.isdigit())
        linhas.append(f"📱 [{p['telefone']}](https://wa.me/55{dig})")
    if estudante:
        linhas.append("🎓 Estudante" + (f": {p['curso']}" if p.get("curso") else "")
                      + (f" · {p['instituicao']}" if p.get("instituicao") else "")
                      + (f" ({p['periodo']})" if p.get("periodo") else ""))
    elif p.get("empresa"):
        linhas.append(f"🏢 {p['empresa']}" + (f" — {p['cargo']}" if p.get("cargo") else ""))
    if ebook_titulo:
        linhas.append(f"📖 Pediu: *{ebook_titulo}*")
    origem = p.get("campanha_origem") or p.get("como_conheceu")
    if origem:
        linhas.append(f"📣 Origem: {origem}" + (f" · campanha {p['campanha_nome']}" if p.get("campanha_nome") else ""))
    if tipo == "cadastro":
        titulo, cor = ("🎓 Novo lead estudante: biblioteca completa" if estudante
                       else "📚 Novo lead: biblioteca completa"), VINHO
    else:
        titulo, cor = f"🎟️ Participante de evento ({tipo.removeprefix('evento: ')})", DOURADO
    discord(titulo, linhas, cor, url_painel)
    para = os.environ.get("EMAIL_AVISO_LEADS", "").strip()
    if para:
        texto = titulo + "\n\n" + "\n".join(l.replace("**", "").replace("*", "") for l in linhas) + f"\n\n{url_painel}"
        email(para, f"{titulo} — {p['nome']}", "<pre style='font:14px sans-serif'>" + html.escape(texto) + "</pre>", texto)
