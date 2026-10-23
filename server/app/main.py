"""Biblioteca de Ebooks — Canella & Santos.

Três níveis de acesso:
  - visitante:   vê a estante (capas + descrição) e o formulário de acesso
  - evento:      escaneou o QR de um evento -> lê/baixa SÓ o ebook do evento
  - completo:    passou pelo formulário -> biblioteca inteira (liberação automática)

O painel /admin (senha única da equipe, ADMIN_SENHA) sobe ebooks, cria os
eventos com QR e lista os leads.
"""
from __future__ import annotations
import csv
import io
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import arquivos as arq
from . import avisos
from . import database as db
from . import equipe
from . import sessao

BASE = Path(__file__).resolve().parent
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
# Em produção (HTTPS) o cookie só trafega cifrado. Em dev (http://localhost) não.
COOKIE_SEGURO = os.environ.get("COOKIE_SEGURO", "1" if BASE_URL.startswith("https") else "0") == "1"


@asynccontextmanager
async def ciclo_de_vida(_app):
    db.init_db()
    yield
    db.close_db()


app = FastAPI(title="Biblioteca Canella & Santos", lifespan=ciclo_de_vida,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
tpl = Jinja2Templates(directory=str(BASE / "templates"))


def _dt_br(v):
    if not v:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d/%m/%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d/%m/%Y")
    return str(v)


def _tamanho(v):
    if not v:
        return "—"
    if v < 1024 * 1024:
        return f"{max(1, round(v / 1024))} KB"
    return f"{v / 1024 / 1024:.1f} MB".replace(".", ",")


tpl.env.filters["dtbr"] = _dt_br

_MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def _mes_ano(v) -> str:
    """'out/2026' — para o leitor saber de quando é o conteúdo."""
    return f"{_MESES[v.month - 1]}/{v.year}" if v else ""


def _idade(v) -> str:
    """'hoje', 'há 5 dias', 'há 3 meses', 'há 1 ano e 2 meses'."""
    if not v:
        return ""
    d = v.date() if isinstance(v, datetime) else v
    dias = (date.today() - d).days
    if dias <= 0:
        return "hoje"
    if dias == 1:
        return "ontem"
    if dias < 30:
        return f"há {dias} dias"
    meses = (date.today().year - d.year) * 12 + date.today().month - d.month - (date.today().day < d.day)
    meses = max(meses, 1)
    if meses < 12:
        return f"há {meses} {'mês' if meses == 1 else 'meses'}"
    anos, resto = divmod(meses, 12)
    txt = f"há {anos} {'ano' if anos == 1 else 'anos'}"
    return txt + (f" e {resto} {'mês' if resto == 1 else 'meses'}" if resto else "")


tpl.env.filters["mes_ano"] = _mes_ano
tpl.env.filters["idade"] = _idade
# Ebook na estante há mais tempo que isso ganha a etiqueta "Revisar?" no painel.
REVISAR_MESES = int(os.environ.get("REVISAR_MESES", "12"))
tpl.env.globals["revisar_meses"] = REVISAR_MESES
# Botão "Fale conosco" (flutuante nas páginas públicas). Por padrão, o número da
# Canella com mensagem pronta e utm próprio — o link do RD Station ("Link da
# Bio") misturaria as conversas da biblioteca com as da bio do Instagram.
WHATSAPP_URL = os.environ.get("WHATSAPP_URL") or (
    "https://api.whatsapp.com/send/?phone=5524999314445&type=phone_number&app_absent=0"
    "&text=" + quote("Olá! Vim pela Biblioteca de Ebooks da Canella & Santos e gostaria de falar com vocês.")
    + "&utm_source=biblioteca-ebooks&utm_campaign=botao-whatsapp")
tpl.env.globals["whatsapp_url"] = WHATSAPP_URL
tpl.env.filters["tamanho"] = _tamanho
tpl.env.filters["cnpj"] = lambda v: f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}" if v and len(v) == 14 else (v or "")


def equipe_na_estante(request: Request) -> dict | None:
    """Usuário do painel navegando pela estante (vê e lê tudo, sem cadastro).

    Mesmas regras do painel: só vale se a pessoa continua ativa e a sessão não
    foi derrubada (troca de senha, "desconectar outros aparelhos").
    """
    if hasattr(request.state, "equipe_estante"):
        return request.state.equipe_estante
    dados = sessao.ler(request.cookies.get(sessao.COOKIE_EQUIPE))
    u = equipe.por_id(dados["uid"]) if dados and dados.get("uid") else None
    if u and (not u["ativo"] or u["sessao_versao"] != dados.get("v")):
        u = None
    request.state.equipe_estante = u
    return u


def _render(request: Request, template: str, **ctx):
    status = ctx.pop("status_code", 200)
    ctx.setdefault("leitor", leitor_atual(request))
    ctx.setdefault("equipe_logada", equipe_na_estante(request))
    ctx.setdefault("url_base", _url_base(request))  # links absolutos (Open Graph, e-mail)
    return tpl.TemplateResponse(request, template, ctx, status_code=status)


def _url_base(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def _proximo(valor: str | None, padrao: str = "/") -> str:
    """Só aceita caminho relativo do próprio site (evita redirecionar para fora)."""
    if valor and valor.startswith("/") and not valor.startswith("//") and "\\" not in valor:
        return valor
    return padrao


# ---------------------------------------------------------------------------
# Campanhas: ?origem=instagram&campanha=reforma-2026 (ou utm_source/utm_campaign)
# ---------------------------------------------------------------------------
# Vale o PRIMEIRO contato: quem chegou pelo Instagram e voltou dias depois pelo
# Google continua contando para o Instagram (cookie de 90 dias, não sobrescrito).
COOKIE_CAMPANHA = "biblioteca_campanha"
DURACAO_CAMPANHA = 60 * 60 * 24 * 90
# origem do link -> opção do "Como conheceu", para o formulário já abrir marcado.
ORIGEM_PARA_CONHECEU = {"instagram": "Instagram", "linkedin": "LinkedIn", "google": "Google",
                        "whatsapp": "WhatsApp", "email": "E-mail / newsletter",
                        "evento": "Evento / palestra", "indicacao": "Indicação"}
# Canais oferecidos no gerador de links do painel.
CANAIS = [("instagram", "Instagram"), ("linkedin", "LinkedIn"), ("whatsapp", "WhatsApp"),
          ("email", "E-mail")]


def _campanha_da_url(request: Request) -> dict | None:
    qp = request.query_params
    origem = (qp.get("origem") or qp.get("utm_source") or "").strip().lower()
    if not origem:
        return None
    nome = (qp.get("campanha") or qp.get("utm_campaign") or "").strip()
    return {"o": re.sub(r"[^a-z0-9_-]", "", origem)[:40], "c": nome[:80]}


def campanha_atual(request: Request) -> dict | None:
    return sessao.ler(request.cookies.get(COOKIE_CAMPANHA)) or _campanha_da_url(request)


@app.middleware("http")
async def _guardar_campanha(request: Request, call_next):
    resp = await call_next(request)
    if request.method == "GET" and not request.cookies.get(COOKIE_CAMPANHA):
        camp = _campanha_da_url(request)
        if camp and camp["o"]:
            resp.set_cookie(COOKIE_CAMPANHA, sessao.assinar(camp, DURACAO_CAMPANHA), max_age=DURACAO_CAMPANHA,
                            httponly=True, samesite="lax", secure=COOKIE_SEGURO)
    return resp


# ---------------------------------------------------------------------------
# Leitor (visitante identificado)
# ---------------------------------------------------------------------------

def leitor_atual(request: Request) -> dict | None:
    if hasattr(request.state, "leitor"):  # já resolvido nesta requisição
        return request.state.leitor
    dados = sessao.ler(request.cookies.get(sessao.COOKIE_LEITOR))
    leitor = None
    if dados and dados.get("pid"):
        leitor = db.one("SELECT * FROM pessoa WHERE id = %s", (dados["pid"],))
    request.state.leitor = leitor
    return leitor


def _entrar(resp: Response, pessoa_id: int) -> Response:
    db.ex("UPDATE pessoa SET ultimo_acesso = now() WHERE id = %s", (pessoa_id,))
    resp.set_cookie(sessao.COOKIE_LEITOR, sessao.assinar({"pid": pessoa_id}, sessao.DURACAO_LEITOR),
                    max_age=sessao.DURACAO_LEITOR, httponly=True, samesite="lax", secure=COOKIE_SEGURO)
    return resp


# Visibilidade de cada ebook:
#   publicado  na estante; abre para quem tem a biblioteca completa (ou ganhou no evento)
#   evento     fora da estante; SÓ quem entrou pelo QR de um evento dele lê —
#              é o material exclusivo de participante, nem o lead completo vê
#   oculto     ninguém lê, nem pelo QR (rascunho / retirado do ar)
VISIBILIDADES = {
    "publicado": ("Na estante", "Aparece na prateleira. Abre para quem fez o cadastro e para quem ganhou no evento."),
    "evento": ("Só eventos", "Não aparece na estante. Só quem escaneia o QR de um evento recebe."),
    "oculto": ("Oculto", "Ninguém acessa, nem pelo QR. Use para rascunhos ou para tirar do ar."),
}


def ids_de_evento(leitor: dict | None) -> set[int]:
    """Ebooks que o leitor ganhou individualmente (QR de evento)."""
    if not leitor:
        return set()
    return {r["ebook_id"] for r in db.q("SELECT ebook_id FROM pessoa_ebook WHERE pessoa_id = %s",
                                         (leitor["id"],))}


# 'arquivado' NÃO entra no seletor acima: não é uma escolha de quem vê, é o fim
# de vida do material (ver admin_ebook_arquivar). Ninguém lê, como o oculto,
# mas fica separado no painel e os links antigos explicam que foi atualizado.
FORA_DO_AR = ("oculto", "arquivado")
NOVO_DIAS = int(os.environ.get("NOVO_DIAS", "30"))


def pode_ler(leitor: dict | None, ebook: dict) -> bool:
    if not leitor or ebook["visibilidade"] in FORA_DO_AR:
        return False
    if ebook["visibilidade"] == "publicado" and leitor["acesso_completo"]:
        return True
    return ebook["id"] in ids_de_evento(leitor)


def _ebook_por_slug(slug: str) -> dict | None:
    return db.one("SELECT * FROM ebook WHERE slug = %s AND visibilidade <> 'oculto'", (slug,))


def _substituto(ebook: dict) -> dict | None:
    """A edição nova de um arquivado — se ela mesma estiver na estante."""
    if not ebook.get("substituto_id"):
        return None
    return db.one("SELECT slug, titulo FROM ebook WHERE id = %s AND visibilidade = 'publicado'",
                  (ebook["substituto_id"],))


# ---------------------------------------------------------------------------
# Estante
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def estante(request: Request):
    leitor = leitor_atual(request)
    staff = equipe_na_estante(request)
    completo = bool(staff or (leitor and leitor["acesso_completo"]))
    ganhos = ids_de_evento(leitor)
    campos = (f"id, slug, titulo, subtitulo, descricao, categoria, capa, paginas, destaque, criado_em, "
              f"pdf_atualizado_em, "
              f"criado_em > now() - interval '{NOVO_DIAS} days' AS novo")
    ebooks = db.q(f"SELECT {campos} FROM ebook WHERE visibilidade = 'publicado' "
                  "ORDER BY ordem, criado_em DESC")
    prateleiras: dict[str, list] = {}
    # A equipe vê também os exclusivos de evento, numa prateleira própria.
    if staff:
        exclusivos = db.q(f"SELECT {campos} FROM ebook WHERE visibilidade = 'evento' ORDER BY ordem, criado_em DESC")
        for e in exclusivos:
            e["liberado"], e["publico"] = True, False
        if exclusivos:
            prateleiras["Exclusivos de eventos (só a equipe vê)"] = exclusivos
    # Os exclusivos de evento não estão na estante pública: quem os ganhou
    # precisa reencontrá-los, então eles abrem uma prateleira própria no topo.
    elif ganhos:
        meus = db.q(f"SELECT {campos} FROM ebook WHERE visibilidade = 'evento' AND id = ANY(%s) "
                    "ORDER BY ordem, criado_em DESC", (list(ganhos),))
        for e in meus:
            e["liberado"], e["publico"] = True, False  # exclusivo: sem página pública/compartilhar
        if meus:
            prateleiras["Seus presentes de eventos"] = meus
    for e in ebooks:
        e["liberado"], e["publico"] = completo or e["id"] in ganhos, True
        prateleiras.setdefault(e["categoria"] or "Ebooks", []).append(e)
    destaques = [e for e in ebooks if e["destaque"]][:3]
    return _render(request, "estante.html", prateleiras=prateleiras, total=len(ebooks), destaques=destaques,
                   acesso_parcial=leitor is not None and not completo)


def _equipe_le(request: Request, slug: str) -> dict | None:
    """Ebook que a equipe pode abrir: tudo, menos o arquivado (oculto também,
    para conferir antes de publicar)."""
    if not equipe_na_estante(request):
        return None
    return db.one("SELECT * FROM ebook WHERE slug = %s AND visibilidade <> 'arquivado'", (slug,))


@app.get("/ebook/{slug}", response_class=HTMLResponse)
def ebook_pagina(request: Request, slug: str):
    """Página própria de cada ebook — é o link para compartilhar nas redes.

    Tem as meta tags Open Graph: no WhatsApp/LinkedIn/Instagram a prévia mostra
    a capa, o título e a descrição, em vez do link cru.
    """
    ebook = _ebook_por_slug(slug)
    if not ebook or ebook["visibilidade"] != "publicado":
        return _sem_acesso(request, ebook, slug)
    leitor = leitor_atual(request)
    liberado = bool(equipe_na_estante(request)) or pode_ler(leitor, ebook)
    outros = db.q("""SELECT slug, titulo, capa FROM ebook WHERE visibilidade = 'publicado' AND id <> %s
                     ORDER BY (categoria IS NOT DISTINCT FROM %s) DESC, destaque DESC, criado_em DESC LIMIT 4""",
                  (ebook["id"], ebook["categoria"]))
    return _render(request, "ebook.html", ebook=ebook, liberado=liberado, outros=outros,
                   url_base=_url_base(request))


@app.get("/capas/{nome}")
def capa(nome: str):
    caminho = arq.CAPAS / Path(nome).name
    if not caminho.is_file():
        return Response(status_code=404)
    return FileResponse(caminho, headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------------------
# Funil: formulário completo -> biblioteca inteira
# ---------------------------------------------------------------------------

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
PERFIS = {"profissional": "Profissional / empresa", "estudante": "Estudante"}
PERIODOS = ["1º ou 2º ano", "3º ou 4º ano", "5º ano ou mais", "Curso técnico", "Pós-graduação", "Recém-formado(a)"]
COMO_CONHECEU = ["Instagram", "LinkedIn", "Google", "WhatsApp", "E-mail / newsletter", "Indicação",
                 "Evento / palestra", "Já sou cliente", "Outro"]


def _ebook_do_next(proximo: str) -> dict | None:
    """O livro que a pessoa clicou antes de cair no cadastro (/ler/x ou /ebook/x)."""
    m = re.match(r"^/(?:ler|ebook)/([a-z0-9-]+)", proximo)
    if not m:
        return None
    return db.one("SELECT id, slug, titulo, subtitulo, capa FROM ebook WHERE slug = %s AND visibilidade = 'publicado'",
                  (m.group(1),))


def _link_magico(request: Request, pessoa_id: int, proximo: str) -> str:
    """Link do e-mail: entra sem digitar nada (outro aparelho) e confirma o e-mail."""
    t = sessao.assinar({"pid": pessoa_id, "nx": proximo, "lnk": 1}, 60 * 60 * 24 * 30)
    return f"{_url_base(request)}/entrar/link?t={quote(t)}"


def _boas_vindas(request: Request, pessoa: dict, ebook: dict | None, proximo: str, evento: str | None = None):
    """Monta (assunto, html, texto) do e-mail de boas-vindas."""
    link = _link_magico(request, pessoa["id"], proximo)
    biblioteca = _link_magico(request, pessoa["id"], "/")
    html = tpl.get_template("emails/boas_vindas.html").render(
        p=pessoa, ebook=ebook, link=link, evento=evento, biblioteca=biblioteca, url_base=_url_base(request))
    primeiro = pessoa["nome"].split(" ")[0]
    if evento and ebook:
        assunto = f"{primeiro}, seu ebook do evento chegou: {ebook['titulo']}"
    else:
        assunto = f"{primeiro}, sua biblioteca Canella & Santos está liberada"
    texto = (f"Olá, {primeiro}!\n\n"
             + (f"Aqui está o ebook \"{ebook['titulo']}\": {link}\n\n" if ebook else "")
             + ("" if evento else f"Acesse a biblioteca quando quiser, de qualquer aparelho: {biblioteca}\n\n")
             + "Equipe Canella & Santos")
    return assunto, html, texto


def _avisar(tarefas: BackgroundTasks, request: Request, pessoa_id: int, tipo: str,
            ebook: dict | None, proximo: str, evento: str | None = None) -> None:
    """Agenda aviso à equipe + boas-vindas ao lead (rodam depois da resposta)."""
    p = db.one("SELECT * FROM pessoa WHERE id = %s", (pessoa_id,))
    url_painel = f"{_url_base(request)}/admin/leads?busca={quote(p['email'])}"
    tarefas.add_task(avisos.lead_novo, p, tipo, ebook["titulo"] if ebook else None, url_painel)
    if avisos.email_configurado():
        assunto, html, texto = _boas_vindas(request, p, ebook, proximo, evento)
        tarefas.add_task(avisos.email, p["email"], assunto, html, texto)


def _excesso(request: Request, nome: str, maximo: int, minutos: int) -> bool:
    """Conta o envio e diz se este IP passou do limite (robô, ou alguém insistindo)."""
    chave = f"{nome}:{request.client.host if request.client else ''}"
    if equipe.estourou(chave, maximo, minutos):
        return True
    equipe.anotar(chave)
    return False


def _devagar(request: Request):
    return _render(request, "aviso.html", titulo="Muitas tentativas seguidas",
                   texto="Recebemos muitos envios deste aparelho/rede em pouco tempo. Aguarde alguns minutos e tente de novo.",
                   status_code=429)


def _limpo(v: str | None, maximo: int = 200) -> str:
    return (v or "").strip()[:maximo]


def _validar_basico(f: dict) -> dict:
    erros = {}
    if len(f["nome"]) < 3:
        erros["nome"] = "Informe seu nome."
    if not _RE_EMAIL.match(f["email"]):
        erros["email"] = "Informe um e-mail válido."
    if not f.get("consentimento"):
        erros["consentimento"] = "É preciso concordar para liberar o acesso."
    return erros


@app.get("/acesso", response_class=HTMLResponse)
def acesso_form(request: Request, next: str = "/", aba: str = "cadastro", perfil: str = ""):
    leitor = leitor_atual(request)
    if leitor and leitor["acesso_completo"]:
        return RedirectResponse(_proximo(next), 303)
    # Quem veio de um evento já tem nome/e-mail: o formulário abre preenchido
    # (e no perfil certo, se o evento era para estudantes).
    f = {k: (leitor or {}).get(k) or "" for k in ("nome", "email", "telefone", "empresa", "cargo", "cnpj",
                                                   "instituicao", "curso", "periodo")}
    f["perfil"] = perfil if perfil in PERFIS else ((leitor or {}).get("perfil") or "profissional")
    camp = campanha_atual(request)
    if camp:
        f["como_conheceu"] = ORIGEM_PARA_CONHECEU.get(camp.get("o"), "")
    proximo = _proximo(next)
    return _render(request, "acesso.html", f=f, erros={}, next=proximo, pedido=_ebook_do_next(proximo),
                   opcoes_conheceu=COMO_CONHECEU, perfis=PERFIS, periodos=PERIODOS,
                   aba="entrar" if aba == "entrar" and not leitor else "cadastro")


@app.post("/acesso", response_class=HTMLResponse)
def acesso_enviar(request: Request, tarefas: BackgroundTasks,
                  nome: str = Form(""), email: str = Form(""), telefone: str = Form(""),
                  empresa: str = Form(""), cargo: str = Form(""), cnpj: str = Form(""),
                  perfil: str = Form("profissional"), instituicao: str = Form(""), curso: str = Form(""),
                  periodo: str = Form(""),
                  como_conheceu: str = Form(""), consentimento: str = Form(""),
                  site: str = Form(""), next: str = Form("/")):
    proximo = _proximo(next)
    if site:  # honeypot: campo invisível que só robô preenche
        return RedirectResponse(proximo, 303)
    # Folgado: numa palestra em faculdade, a turma toda sai pelo mesmo Wi-Fi (mesmo IP).
    if _excesso(request, "acesso", 150, 60):
        return _devagar(request)
    estudante = perfil == "estudante"
    f = {"nome": _limpo(nome, 120), "email": _limpo(email, 160).lower(),
         "telefone": _limpo(telefone, 30), "perfil": "estudante" if estudante else "profissional",
         # Estudante não tem empresa/cargo/CNPJ; profissional não tem instituição/curso.
         "empresa": "" if estudante else _limpo(empresa, 160),
         "cargo": "" if estudante else _limpo(cargo, 120),
         "cnpj": "" if estudante else _limpo(cnpj, 20),
         "instituicao": _limpo(instituicao, 160) if estudante else "",
         "curso": _limpo(curso, 120) if estudante else "",
         "periodo": _limpo(periodo, 40) if estudante else "",
         "como_conheceu": _limpo(como_conheceu, 60), "consentimento": consentimento}
    erros = _validar_basico(f)
    tel = re.sub(r"\D", "", f["telefone"])
    if tel.startswith("55") and len(tel) in (12, 13):  # colaram com +55
        tel = tel[2:]
    if len(tel) < 10:
        erros["telefone"] = "Informe o telefone com DDD."
    elif len(tel) <= 11:  # grava sempre no mesmo formato: (11) 98888-7777
        f["telefone"] = f"({tel[:2]}) {tel[2:-4]}-{tel[-4:]}"
    if estudante:
        if len(f["instituicao"]) < 2:
            erros["instituicao"] = "Informe a faculdade, universidade ou escola."
        if len(f["curso"]) < 2:
            erros["curso"] = "Informe o seu curso."
    else:
        if len(f["empresa"]) < 2:
            erros["empresa"] = "Informe a empresa."
        if len(f["cargo"]) < 2:
            erros["cargo"] = "Informe seu cargo."
    cnpj_dig = re.sub(r"\D", "", f["cnpj"])
    if cnpj_dig and len(cnpj_dig) != 14:
        erros["cnpj"] = "O CNPJ tem 14 dígitos (ou deixe em branco)."
    if erros:
        return _render(request, "acesso.html", f=f, erros=erros, next=proximo, pedido=_ebook_do_next(proximo),
                       opcoes_conheceu=COMO_CONHECEU, perfis=PERFIS, periodos=PERIODOS, aba="cadastro")

    pedido = _ebook_do_next(proximo)
    camp = campanha_atual(request) or {}
    ja_era = db.one("SELECT acesso_completo FROM pessoa WHERE LOWER(email) = %s", (f["email"],))
    linha = db.ex(
        """INSERT INTO pessoa(nome, email, telefone, empresa, cargo, cnpj, como_conheceu,
                              consentimento_em, acesso_completo, origem, liberado_em,
                              campanha_origem, campanha_nome, ebook_pedido_id,
                              perfil, instituicao, curso, periodo)
           VALUES (%s,%s,%s,%s,%s,%s,%s, now(), TRUE, 'formulario', now(), %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT ((LOWER(email))) DO UPDATE SET
             nome = EXCLUDED.nome, telefone = EXCLUDED.telefone, empresa = EXCLUDED.empresa,
             cargo = EXCLUDED.cargo, cnpj = EXCLUDED.cnpj, perfil = EXCLUDED.perfil,
             instituicao = EXCLUDED.instituicao, curso = EXCLUDED.curso, periodo = EXCLUDED.periodo,
             como_conheceu = COALESCE(NULLIF(EXCLUDED.como_conheceu, ''), pessoa.como_conheceu),
             consentimento_em = now(),
             acesso_completo = TRUE, liberado_em = COALESCE(pessoa.liberado_em, now()),
             campanha_origem = COALESCE(pessoa.campanha_origem, EXCLUDED.campanha_origem),
             campanha_nome = COALESCE(pessoa.campanha_nome, EXCLUDED.campanha_nome),
             ebook_pedido_id = COALESCE(pessoa.ebook_pedido_id, EXCLUDED.ebook_pedido_id)
           RETURNING id""",
        (f["nome"], f["email"], f["telefone"], f["empresa"] or None, f["cargo"] or None, cnpj_dig or None,
         f["como_conheceu"] or None, camp.get("o") or None, camp.get("c") or None,
         pedido["id"] if pedido else None, f["perfil"], f["instituicao"] or None, f["curso"] or None,
         f["periodo"] or None))
    # Só avisa quando a biblioteca abre pela 1ª vez (reenvio do formulário não repete).
    if not (ja_era and ja_era["acesso_completo"]):
        _avisar(tarefas, request, linha["id"], "cadastro", pedido, proximo)
    return _entrar(RedirectResponse(proximo + ("&" if "?" in proximo else "?") + "bemvindo=1", 303),
                   linha["id"])


@app.post("/entrar", response_class=HTMLResponse)
def entrar(request: Request, email: str = Form(""), next: str = Form("/")):
    """'Já tenho acesso': reentrada pelo e-mail (outro aparelho, cookie apagado)."""
    proximo = _proximo(next)
    # Mais apertado: sem limite, daria para testar uma lista de e-mails e descobrir quem é lead.
    if _excesso(request, "entrar", 30, 15):
        return _devagar(request)
    pessoa = db.one("SELECT id FROM pessoa WHERE LOWER(email) = LOWER(%s)", (_limpo(email, 160),))
    if not pessoa:
        return _render(request, "acesso.html", f={"email": _limpo(email, 160), "perfil": "profissional"}, next=proximo,
                       erros={"entrar": "Não encontramos esse e-mail. Preencha o cadastro para liberar o acesso."},
                       opcoes_conheceu=COMO_CONHECEU, perfis=PERFIS, periodos=PERIODOS, aba="entrar")
    return _entrar(RedirectResponse(proximo, 303), pessoa["id"])


@app.get("/entrar/link")
def entrar_link(t: str = ""):
    """Link do e-mail de boas-vindas: entra direto e marca o e-mail como confirmado."""
    dados = sessao.ler(t)
    if not dados or not dados.get("lnk") or not db.one("SELECT 1 FROM pessoa WHERE id = %s", (dados["pid"],)):
        return RedirectResponse("/acesso?aba=entrar", 303)
    db.ex("UPDATE pessoa SET email_confirmado_em = COALESCE(email_confirmado_em, now()) WHERE id = %s",
          (dados["pid"],))
    return _entrar(RedirectResponse(_proximo(dados.get("nx")), 303), dados["pid"])


@app.get("/sair")
def sair():
    resp = RedirectResponse("/", 303)
    resp.delete_cookie(sessao.COOKIE_LEITOR)
    return resp


# ---------------------------------------------------------------------------
# Leitura e download
# ---------------------------------------------------------------------------

def _sem_acesso(request: Request, ebook: dict | None, slug: str):
    """Para onde mandar quem tentou abrir um ebook que não pode ler."""
    if not ebook:
        return _render(request, "aviso.html", titulo="Ebook não encontrado",
                       texto="Este ebook não está mais disponível. Veja os outros materiais da biblioteca.",
                       status_code=404)
    if ebook["visibilidade"] == "arquivado":
        return _aviso_arquivado(request, ebook)
    if ebook["visibilidade"] == "evento":
        # O cadastro não libera exclusivo de evento: mandar ao formulário seria
        # prometer algo que ele não entrega.
        return _render(request, "aviso.html", titulo="Exclusivo para participantes",
                       texto="Este ebook é um presente para quem participou de um evento da Canella & Santos "
                             "e é liberado pelo QR code entregue no evento. Já participou? Escaneie o QR de novo "
                             "ou use \"Já tenho acesso\" com o e-mail que você informou.",
                       link_texto="Conhecer a biblioteca", link="/", status_code=403)
    return RedirectResponse(f"/acesso?next={quote('/ler/' + slug)}", 303)


def _aviso_arquivado(request: Request, ebook: dict):
    """Link/QR antigo de material desatualizado: explica e aponta a edição nova."""
    novo = _substituto(ebook)
    if novo:
        return _render(request, "aviso.html", titulo="Este material foi atualizado",
                       texto=f"“{ebook['titulo']}” saiu de circulação porque o conteúdo mudou. "
                             f"A versão atualizada é “{novo['titulo']}”.",
                       link_texto="Ver a versão atualizada", link=f"/ebook/{novo['slug']}", status_code=410)
    return _render(request, "aviso.html", titulo="Material fora de circulação",
                   texto=f"“{ebook['titulo']}” foi retirado da biblioteca porque o conteúdo ficou desatualizado. "
                         "Veja os materiais atuais na estante.",
                   link_texto="Ver a biblioteca", link="/", status_code=410)


@app.get("/ler/{slug}", response_class=HTMLResponse)
def ler(request: Request, slug: str):
    interno = _equipe_le(request, slug)
    if interno:  # equipe: não conta como leitura de lead
        return _render(request, "ler.html", ebook=interno, acesso_parcial=False)
    ebook = _ebook_por_slug(slug)
    leitor = leitor_atual(request)
    if not ebook or not pode_ler(leitor, ebook):
        return _sem_acesso(request, ebook, slug)
    db.ex("INSERT INTO acesso_log(pessoa_id, ebook_id, acao) VALUES (%s,%s,'leitura')",
          (leitor["id"], ebook["id"]))
    return _render(request, "ler.html", ebook=ebook, acesso_parcial=not leitor["acesso_completo"])


@app.get("/ler/{slug}/arquivo")
def arquivo(request: Request, slug: str):
    ebook = _equipe_le(request, slug) or _ebook_por_slug(slug)
    if not ebook or not (equipe_na_estante(request) or pode_ler(leitor_atual(request), ebook)):
        return Response(status_code=403)
    return FileResponse(arq.PDFS / ebook["arquivo"], media_type="application/pdf",
                        headers={"Cache-Control": "private, no-store",
                                 "Content-Disposition": "inline"})


@app.get("/ler/{slug}/download")
def download(request: Request, slug: str):
    interno = _equipe_le(request, slug)
    if interno:
        staff = equipe_na_estante(request)
        conteudo = arq.pdf_com_marca(interno["arquivo"], f"{staff['nome']} (equipe)", staff["email"] or f"@{staff['usuario']}")
        return Response(conteudo, media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename=\"{interno['slug']}.pdf\"",
            "Cache-Control": "private, no-store"})
    ebook = _ebook_por_slug(slug)
    leitor = leitor_atual(request)
    if not ebook or not pode_ler(leitor, ebook):
        return _sem_acesso(request, ebook, slug)
    conteudo = arq.pdf_com_marca(ebook["arquivo"], leitor["nome"], leitor["email"])
    db.ex("INSERT INTO acesso_log(pessoa_id, ebook_id, acao) VALUES (%s,%s,'download')",
          (leitor["id"], ebook["id"]))
    return Response(conteudo, media_type="application/pdf", headers={
        "Content-Disposition": f"attachment; filename=\"{ebook['slug']}.pdf\"",
        "Cache-Control": "private, no-store"})


# ---------------------------------------------------------------------------
# Eventos (QR code) -> só o ebook do evento
# ---------------------------------------------------------------------------

def _evento_valido(token: str) -> tuple[dict | None, str | None]:
    ev = db.one("""SELECT ev.*, e.slug, e.titulo, e.subtitulo, e.capa, e.descricao, e.visibilidade
                   FROM evento ev JOIN ebook e ON e.id = ev.ebook_id WHERE ev.token = %s""", (token,))
    if not ev:
        return None, "Este QR code não é válido. Confira se escaneou o código certo."
    if ev["visibilidade"] == "arquivado":
        return ev, "arquivado"
    if not ev["ativo"] or ev["visibilidade"] == "oculto":
        return None, "O acesso por este QR code foi encerrado. Mas a biblioteca continua aberta: conheça os outros materiais."
    if ev["valido_ate"] and ev["valido_ate"] < date.today():
        return None, (f"O acesso por este QR code valia até {ev['valido_ate']:%d/%m/%Y}. "
                      "Mas a biblioteca continua aberta: conheça os outros materiais.")
    return ev, None


def _liberar_do_evento(pessoa_id: int, ev: dict) -> bool:
    """True se o ebook foi liberado agora (False: a pessoa já tinha)."""
    return db.ex("""INSERT INTO pessoa_ebook(pessoa_id, ebook_id, evento_id) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING RETURNING pessoa_id""", (pessoa_id, ev["ebook_id"], ev["id"])) is not None


def _evento_indisponivel(request: Request, ev: dict | None, erro: str):
    if erro == "arquivado":
        return _aviso_arquivado(request, db.one("SELECT * FROM ebook WHERE id = %s", (ev["ebook_id"],)))
    return _render(request, "aviso.html", titulo="Acesso indisponível", texto=erro,
                   link_texto="Conhecer a biblioteca", link="/")


@app.get("/e/{token}", response_class=HTMLResponse)
def evento(request: Request, token: str, tarefas: BackgroundTasks):
    ev, erro = _evento_valido(token)
    if erro:
        return _evento_indisponivel(request, ev, erro)
    # Topo do funil: quantas vezes o QR foi aberto. Um cookie por evento evita
    # contar de novo quem só recarregou a página.
    marca = f"ev{ev['id']}"
    contar = not request.cookies.get(marca)
    if contar:
        db.ex("UPDATE evento SET visitas = visitas + 1 WHERE id = %s", (ev["id"],))
    leitor = leitor_atual(request)
    if leitor:
        if _liberar_do_evento(leitor["id"], ev):
            _avisar(tarefas, request, leitor["id"], f"evento: {ev['nome']}", ev, f"/ler/{ev['slug']}", ev["nome"])
        resp = RedirectResponse(f"/ler/{ev['slug']}?presente=1", 303)
    else:
        resp = _render(request, "evento.html", ev=ev, f={}, erros={})
    if contar:
        resp.set_cookie(marca, "1", max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax", secure=COOKIE_SEGURO)
    return resp


@app.post("/e/{token}", response_class=HTMLResponse)
def evento_enviar(request: Request, token: str, tarefas: BackgroundTasks, nome: str = Form(""),
                  email: str = Form(""), consentimento: str = Form(""), site: str = Form("")):
    ev, erro = _evento_valido(token)
    if erro:
        return _evento_indisponivel(request, ev, erro)
    if site:
        return RedirectResponse("/", 303)
    # Folgado: no evento, dezenas de pessoas saem pelo mesmo Wi-Fi (mesmo IP).
    if _excesso(request, "evento", 200, 60):
        return _devagar(request)
    f = {"nome": _limpo(nome, 120), "email": _limpo(email, 160).lower(), "consentimento": consentimento}
    erros = _validar_basico(f)
    if erros:
        return _render(request, "evento.html", ev=ev, f=f, erros=erros)
    linha = db.ex(
        """INSERT INTO pessoa(nome, email, consentimento_em, origem, perfil)
           VALUES (%s, %s, now(), %s, %s)
           ON CONFLICT ((LOWER(email))) DO UPDATE SET consentimento_em = now(),
             -- participante de evento para estudantes que ainda não fez o cadastro completo
             perfil = CASE WHEN pessoa.acesso_completo THEN pessoa.perfil ELSE EXCLUDED.perfil END
           RETURNING id""",
        (f["nome"], f["email"], f"evento: {ev['nome']}", "estudante" if ev["para_estudantes"] else "profissional"))
    if _liberar_do_evento(linha["id"], ev):
        _avisar(tarefas, request, linha["id"], f"evento: {ev['nome']}", ev, f"/ler/{ev['slug']}", ev["nome"])
    return _entrar(RedirectResponse(f"/ler/{ev['slug']}?presente=1", 303), linha["id"])


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

COOKIE_2FA = "biblioteca_admin_2fa"        # login no meio do caminho: senha ok, falta o código
COOKIE_2FA_NOVO = "biblioteca_admin_2fa_novo"  # segredo sendo configurado em Minha conta
ROTAS_LIVRES = ("/admin/login", "/admin/primeiro-acesso")
SO_ADMIN = ("/admin/usuarios", "/admin/atividade", "/admin/backups")
# (máximo, minutos). Por IP é mais folgado: num escritório todos saem pelo mesmo IP.
LIMITE_LOGIN_IP = (10, 15)
LIMITE_LOGIN_CONTA = (5, 15)   # por conta (usuário digitado)


def _quem(u: dict) -> str:
    return f"{u['nome']} (@{u['usuario']})"


def _ip(request: Request) -> str:
    # Atrás do nginx, o uvicorn (--proxy-headers) já põe aqui o IP real do visitante.
    return request.client.host if request.client else ""


def _admin_atual(request: Request) -> dict | None:
    """Usuário do painel desta requisição, ou None. O cookie só vale se a pessoa
    continua ativa e a versão da sessão bate (trocar senha derruba as outras)."""
    if hasattr(request.state, "admin"):
        return request.state.admin
    dados = sessao.ler(request.cookies.get(sessao.COOKIE_ADMIN))
    u = equipe.por_id(dados["uid"]) if dados and dados.get("uid") else None
    if u and (not u["ativo"] or u["sessao_versao"] != dados.get("v")):
        u = None
    request.state.admin = u
    return u


# As rotas antigas testam `if not _admin_ok(request)`: devolver o usuário mantém isso.
_admin_ok = _admin_atual


def _registrar(request: Request, acao: str, detalhe: str = "") -> None:
    equipe.registrar(_admin_atual(request), acao, detalhe, _ip(request))


@app.middleware("http")
async def _porteiro_admin(request: Request, call_next):
    """Porta única do /admin: login, troca de senha obrigatória e telas só de administrador."""
    caminho = request.url.path
    if (caminho == "/admin" or caminho.startswith("/admin/")) and not caminho.startswith(ROTAS_LIVRES):
        u = await run_in_threadpool(_admin_atual, request)
        if not u:
            destino = f"/admin/login?next={quote(caminho)}" if request.method == "GET" else "/admin/login"
            return RedirectResponse(destino, 303)
        if u["trocar_senha"] and not caminho.startswith(("/admin/conta", "/admin/sair")):
            return RedirectResponse("/admin/conta", 303)
        if caminho.startswith(SO_ADMIN) and u["papel"] != "admin":
            return RedirectResponse("/admin/ebooks?err=" + quote("Essa tela é só para administradores."), 303)
    return await call_next(request)


def _admin_render(request: Request, template: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("msg_erro", request.query_params.get("err"))
    ctx.setdefault("eu", _admin_atual(request))
    ctx.setdefault("papeis", equipe.PAPEIS)
    return tpl.TemplateResponse(request, f"admin/{template}", ctx, status_code=ctx.pop("status_code", 200))


def _para_login(request: Request):
    return RedirectResponse(f"/admin/login?next={quote(request.url.path)}", 303)


def _cookie_admin(resp: Response, u: dict) -> Response:
    valor = sessao.assinar({"uid": u["id"], "v": u["sessao_versao"]}, sessao.DURACAO_ADMIN)
    resp.set_cookie(sessao.COOKIE_ADMIN, valor, max_age=sessao.DURACAO_ADMIN, httponly=True, samesite="strict",
                    secure=COOKIE_SEGURO, path="/admin")
    # Mesma sessão, visível na estante (para a equipe ver tudo liberado).
    resp.set_cookie(sessao.COOKIE_EQUIPE, valor, max_age=sessao.DURACAO_ADMIN, httponly=True, samesite="lax",
                    secure=COOKIE_SEGURO, path="/")
    return resp


def _concluir_login(request: Request, u: dict, proximo: str) -> Response:
    equipe.zerar(f"login-conta:{u['usuario'].lower()}")
    if u.get("email"):
        equipe.zerar(f"login-conta:{u['email'].lower()}")
    db.ex("UPDATE admin_usuario SET ultimo_login = now() WHERE id = %s", (u["id"],))
    equipe.registrar(u, "entrou no painel", "", _ip(request))
    resp = _cookie_admin(RedirectResponse(_proximo(proximo, "/admin"), 303), u)
    resp.delete_cookie(COOKIE_2FA, path="/admin")
    return resp


def _bloqueado(request: Request, login: str) -> bool:
    return (equipe.estourou(f"login-ip:{_ip(request)}", *LIMITE_LOGIN_IP)
            or equipe.estourou(f"login-conta:{login.lower()}", *LIMITE_LOGIN_CONTA))


def _falhou(request: Request, tarefas: BackgroundTasks, login: str) -> None:
    ip = _ip(request)
    n_ip = equipe.anotar(f"login-ip:{ip}")
    n_conta = equipe.anotar(f"login-conta:{login.lower()}") if login else 0
    # Avisa a equipe uma vez, quando a chave acaba de estourar.
    if n_ip == LIMITE_LOGIN_IP[0] or n_conta == LIMITE_LOGIN_CONTA[0]:
        equipe.registrar(None, "login bloqueado", f"{login or '(em branco)'} · muitas senhas erradas", ip)
        tarefas.add_task(avisos.discord, "🔒 Painel: login bloqueado por excesso de tentativas",
                         [f"Usuário tentado: **{login or '—'}**", f"IP: `{ip}`",
                          "O acesso fica travado por 15 minutos. Se não foi alguém da equipe, ninguém precisa fazer nada; "
                          "se for recorrente, avise o responsável pela VPS."], 0xB3261E)


_MSG_BLOQUEIO = "Muitas tentativas erradas. Por segurança, o acesso foi pausado por 15 minutos."


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request, next: str = "/admin"):
    if not equipe.existe_algum():
        return _admin_render(request, "primeiro_acesso.html", erro=None, f={},
                             sem_chave=not os.environ.get("ADMIN_SENHA"), eu=None)
    return _admin_render(request, "login.html", erro=None, next=_proximo(next, "/admin"), usuario="", eu=None)


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, tarefas: BackgroundTasks, usuario: str = Form(""), senha: str = Form(""),
                next: str = Form("/admin")):
    login = _limpo(usuario, 160).lower()
    proximo = _proximo(next, "/admin")

    def _erro(texto: str, status: int = 200):
        return _admin_render(request, "login.html", erro=texto, next=proximo, usuario=login, eu=None, status_code=status)

    if _bloqueado(request, login):
        return _erro(_MSG_BLOQUEIO, 429)
    u = equipe.por_login(login)
    if not u or not u["ativo"] or not equipe.confere_senha(senha, u["senha_hash"]):
        _falhou(request, tarefas, login)
        return _erro("Usuário ou senha incorretos.")
    if u["totp_segredo"]:
        resp = RedirectResponse("/admin/login/codigo", 303)
        resp.set_cookie(COOKIE_2FA, sessao.assinar({"uid": u["id"], "v": u["sessao_versao"], "nx": proximo}, 300),
                        max_age=300, httponly=True, samesite="strict", secure=COOKIE_SEGURO, path="/admin")
        return resp
    return _concluir_login(request, u, proximo)


def _pendente_2fa(request: Request) -> tuple[dict | None, dict | None]:
    dados = sessao.ler(request.cookies.get(COOKIE_2FA))
    u = equipe.por_id(dados["uid"]) if dados else None
    if not u or not u["ativo"] or u["sessao_versao"] != dados.get("v") or not u["totp_segredo"]:
        return None, None
    return u, dados


@app.get("/admin/login/codigo", response_class=HTMLResponse)
def admin_login_codigo_form(request: Request):
    u, _ = _pendente_2fa(request)
    if not u:
        return RedirectResponse("/admin/login", 303)
    return _admin_render(request, "login_codigo.html", erro=None, nome=u["nome"].split(" ")[0], eu=None)


@app.post("/admin/login/codigo", response_class=HTMLResponse)
def admin_login_codigo(request: Request, tarefas: BackgroundTasks, codigo: str = Form("")):
    u, dados = _pendente_2fa(request)
    if not u:
        return RedirectResponse("/admin/login?err=" + quote("O tempo para digitar o código acabou. Entre de novo."), 303)
    if _bloqueado(request, u["usuario"]):
        return _admin_render(request, "login_codigo.html", erro=_MSG_BLOQUEIO, nome=u["nome"].split(" ")[0],
                             eu=None, status_code=429)
    if not equipe.codigo_ok(u["totp_segredo"], codigo):
        _falhou(request, tarefas, u["usuario"])
        return _admin_render(request, "login_codigo.html", erro="Código incorreto. Confira o app e tente de novo.",
                             nome=u["nome"].split(" ")[0], eu=None)
    return _concluir_login(request, u, dados.get("nx") or "/admin")


@app.post("/admin/primeiro-acesso", response_class=HTMLResponse)
def admin_primeiro_acesso(request: Request, tarefas: BackgroundTasks, chave: str = Form(""), nome: str = Form(""),
                          usuario: str = Form(""), email: str = Form(""), senha: str = Form(""),
                          senha2: str = Form("")):
    """Cria o 1º administrador. Só funciona enquanto não existe nenhum usuário,
    e pede a ADMIN_SENHA do .env (quem não tem acesso à VPS não consegue)."""
    if equipe.existe_algum():
        return RedirectResponse("/admin/login", 303)
    f = {"nome": _limpo(nome, 120), "usuario": _limpo(usuario, 30).lower(), "email": _limpo(email, 160).lower()}

    def _erro(texto: str, status: int = 200):
        return _admin_render(request, "primeiro_acesso.html", erro=texto, f=f, eu=None,
                             sem_chave=not os.environ.get("ADMIN_SENHA"), status_code=status)

    if _bloqueado(request, "primeiro-acesso"):
        return _erro(_MSG_BLOQUEIO, 429)
    if not sessao.senha_admin_ok(chave):
        _falhou(request, tarefas, "primeiro-acesso")
        return _erro("A chave de instalação não confere com a ADMIN_SENHA do servidor.")
    if len(f["nome"]) < 3:
        return _erro("Informe seu nome.")
    problema = (equipe.problema_no_usuario(f["usuario"])
                or ("E-mail inválido (ou deixe em branco)." if f["email"] and not _RE_EMAIL.match(f["email"]) else None)
                or equipe.problema_na_senha(senha, f["usuario"])
                or ("As duas senhas não são iguais." if senha != senha2 else None))
    if problema:
        return _erro(problema)
    linha = db.ex("""INSERT INTO admin_usuario(nome, usuario, email, senha_hash, papel, trocar_senha)
                     VALUES (%s,%s,%s,%s,'admin',FALSE) RETURNING id""",
                  (f["nome"], f["usuario"], f["email"] or None, equipe.hash_senha(senha)))
    u = equipe.por_id(linha["id"])
    equipe.registrar(u, "criou o primeiro administrador", _quem(u), _ip(request))
    return _concluir_login(request, u, "/admin/conta?msg=" + quote(
        "Conta criada! Recomendado: ative a verificação em duas etapas abaixo e cadastre o resto da equipe em Usuários."))


@app.get("/admin/sair")
def admin_sair(request: Request):
    u = _admin_atual(request)
    if u:
        equipe.registrar(u, "saiu do painel", "", _ip(request))
    resp = RedirectResponse("/admin/login", 303)
    resp.delete_cookie(sessao.COOKIE_ADMIN, path="/admin")
    resp.delete_cookie(sessao.COOKIE_EQUIPE, path="/")
    return resp


@app.get("/admin")
def admin_inicio(request: Request):
    return RedirectResponse("/admin/ebooks", 303)


# --- minha conta ------------------------------------------------------------

@app.get("/admin/conta", response_class=HTMLResponse)
def admin_conta(request: Request):
    u = _admin_atual(request)
    novo = sessao.ler(request.cookies.get(COOKIE_2FA_NOVO))
    qr = equipe.qr_2fa_svg(novo["s"], u["usuario"]) if novo and not u["totp_segredo"] else None
    return _admin_render(request, "conta.html", aba="conta", qr_2fa=qr, segredo_2fa=novo["s"] if qr else None,
                         erro_senha=None, erro_2fa=None)


@app.post("/admin/conta/senha", response_class=HTMLResponse)
def admin_conta_senha(request: Request, atual: str = Form(""), nova: str = Form(""), nova2: str = Form("")):
    u = _admin_atual(request)

    def _erro(texto: str):
        return _admin_render(request, "conta.html", aba="conta", qr_2fa=None, segredo_2fa=None,
                             erro_senha=texto, erro_2fa=None)

    if not equipe.confere_senha(atual, u["senha_hash"]):
        return _erro("A senha atual não confere.")
    problema = equipe.problema_na_senha(nova, u["usuario"])
    if problema:
        return _erro(problema)
    if nova != nova2:
        return _erro("As duas senhas novas não são iguais.")
    if nova == atual:
        return _erro("A senha nova precisa ser diferente da atual.")
    # Sobe a versão: sessões abertas em outros aparelhos caem; esta recebe cookie novo.
    db.ex("""UPDATE admin_usuario SET senha_hash = %s, trocar_senha = FALSE, sessao_versao = sessao_versao + 1
             WHERE id = %s""", (equipe.hash_senha(nova), u["id"]))
    equipe.registrar(u, "trocou a própria senha", "", _ip(request))
    return _cookie_admin(_com_msg("/admin/ebooks" if u["trocar_senha"] else "/admin/conta",
                                  "Senha alterada. Outros aparelhos conectados foram desconectados."),
                         equipe.por_id(u["id"]))


@app.post("/admin/conta/sair-todos")
def admin_conta_sair_todos(request: Request):
    u = _admin_atual(request)
    db.ex("UPDATE admin_usuario SET sessao_versao = sessao_versao + 1 WHERE id = %s", (u["id"],))
    equipe.registrar(u, "desconectou os outros aparelhos", "", _ip(request))
    return _cookie_admin(_com_msg("/admin/conta", "Pronto: só este aparelho continua conectado."), equipe.por_id(u["id"]))


@app.post("/admin/conta/2fa/iniciar")
def admin_2fa_iniciar(request: Request):
    resp = RedirectResponse("/admin/conta#duas-etapas", 303)
    resp.set_cookie(COOKIE_2FA_NOVO, sessao.assinar({"s": equipe.novo_segredo_2fa()}, 900), max_age=900,
                    httponly=True, samesite="strict", secure=COOKIE_SEGURO, path="/admin")
    return resp


@app.post("/admin/conta/2fa/ativar", response_class=HTMLResponse)
def admin_2fa_ativar(request: Request, codigo: str = Form("")):
    u = _admin_atual(request)
    novo = sessao.ler(request.cookies.get(COOKIE_2FA_NOVO))
    if not novo:
        return _com_msg("/admin/conta#duas-etapas", "O tempo para configurar acabou. Comece de novo.", erro=True)
    if not equipe.codigo_ok(novo["s"], codigo):
        return _admin_render(request, "conta.html", aba="conta", qr_2fa=equipe.qr_2fa_svg(novo["s"], u["usuario"]),
                             segredo_2fa=novo["s"], erro_senha=None,
                             erro_2fa="Código incorreto. Confira se o relógio do celular está certo e tente de novo.")
    db.ex("UPDATE admin_usuario SET totp_segredo = %s, sessao_versao = sessao_versao + 1 WHERE id = %s",
          (novo["s"], u["id"]))
    equipe.registrar(u, "ativou a verificação em duas etapas", "", _ip(request))
    resp = _cookie_admin(_com_msg("/admin/conta", "Verificação em duas etapas ativada. A partir de agora o login pede o código do app."),
                         equipe.por_id(u["id"]))
    resp.delete_cookie(COOKIE_2FA_NOVO, path="/admin")
    return resp


@app.post("/admin/conta/2fa/desativar", response_class=HTMLResponse)
def admin_2fa_desativar(request: Request, senha: str = Form("")):
    u = _admin_atual(request)
    if not equipe.confere_senha(senha, u["senha_hash"]):
        return _admin_render(request, "conta.html", aba="conta", qr_2fa=None, segredo_2fa=None, erro_senha=None,
                             erro_2fa="Senha incorreta: a verificação continua ativa.")
    db.ex("UPDATE admin_usuario SET totp_segredo = NULL WHERE id = %s", (u["id"],))
    equipe.registrar(u, "desativou a verificação em duas etapas", "", _ip(request))
    return _com_msg("/admin/conta", "Verificação em duas etapas desativada.")


# --- usuários (só administrador) ---------------------------------------------

def _usuarios_render(request: Request, **ctx):
    f = ctx.get("f") or {}
    if f.get("nome") and not f.get("usuario"):
        f["usuario"] = equipe.sugerir_usuario(f["nome"])
    usuarios = db.q("SELECT * FROM admin_usuario ORDER BY ativo DESC, papel, LOWER(nome)")
    ctx.setdefault("erro", None)
    ctx.setdefault("f", {})
    return _admin_render(request, "usuarios.html", aba="usuarios", usuarios=usuarios, url_base=_url_base(request), **ctx)


@app.get("/admin/usuarios", response_class=HTMLResponse)
def admin_usuarios(request: Request):
    return _usuarios_render(request)


@app.post("/admin/usuarios", response_class=HTMLResponse)
def admin_usuario_criar(request: Request, nome: str = Form(""), usuario: str = Form(""), email: str = Form(""),
                        papel: str = Form("equipe")):
    f = {"nome": _limpo(nome, 120), "usuario": _limpo(usuario, 30).lower(), "email": _limpo(email, 160).lower(),
         "papel": papel if papel in equipe.PAPEIS else "equipe"}
    if len(f["nome"]) < 3:
        return _usuarios_render(request, erro="Informe o nome da pessoa.", f=f)
    problema = (equipe.problema_no_usuario(f["usuario"])
                or ("Já existe alguém com esse usuário. Escolha outro (ex.: com o sobrenome)." if equipe.por_usuario(f["usuario"]) else None)
                or ("E-mail inválido (ou deixe em branco)." if f["email"] and not _RE_EMAIL.match(f["email"]) else None)
                or ("Já existe alguém com esse e-mail." if f["email"] and equipe.por_email(f["email"]) else None))
    if problema:
        return _usuarios_render(request, erro=problema, f=f)
    temp = equipe.senha_temporaria()
    db.ex("""INSERT INTO admin_usuario(nome, usuario, email, senha_hash, papel, trocar_senha)
             VALUES (%s,%s,%s,%s,%s,TRUE)""",
          (f["nome"], f["usuario"], f["email"] or None, equipe.hash_senha(temp), f["papel"]))
    _registrar(request, "criou usuário", f"{f['nome']} (@{f['usuario']}) · {equipe.PAPEIS[f['papel']][0]}")
    # A senha temporária aparece UMA vez, nesta resposta (nunca vai para a URL nem para o log).
    return _usuarios_render(request, entregar={"nome": f["nome"], "usuario": f["usuario"], "senha": temp, "novo": True})


def _usuario_alvo(request: Request, uid: int) -> tuple[dict | None, Response | None]:
    alvo = equipe.por_id(uid)
    if not alvo:
        return None, _com_msg("/admin/usuarios", "Usuário não encontrado.", erro=True)
    return alvo, None


@app.post("/admin/usuarios/{uid}/senha", response_class=HTMLResponse)
def admin_usuario_senha(request: Request, uid: int):
    alvo, erro = _usuario_alvo(request, uid)
    if erro:
        return erro
    temp = equipe.senha_temporaria()
    db.ex("""UPDATE admin_usuario SET senha_hash = %s, trocar_senha = TRUE, sessao_versao = sessao_versao + 1
             WHERE id = %s""", (equipe.hash_senha(temp), uid))
    equipe.zerar(f"login-conta:{alvo['usuario'].lower()}")
    if alvo.get("email"):
        equipe.zerar(f"login-conta:{alvo['email'].lower()}")
    _registrar(request, "redefiniu a senha de", _quem(alvo))
    return _usuarios_render(request, entregar={"nome": alvo["nome"], "usuario": alvo["usuario"], "senha": temp, "novo": False})


@app.post("/admin/usuarios/{uid}/ativo")
def admin_usuario_ativo(request: Request, uid: int):
    alvo, erro = _usuario_alvo(request, uid)
    if erro:
        return erro
    eu = _admin_atual(request)
    if alvo["id"] == eu["id"]:
        return _com_msg("/admin/usuarios", "Você não pode desativar a sua própria conta.", erro=True)
    if alvo["ativo"] and alvo["papel"] == "admin" and equipe.admins_ativos(exceto=alvo["id"]) == 0:
        return _com_msg("/admin/usuarios", "É preciso manter pelo menos um administrador ativo.", erro=True)
    db.ex("UPDATE admin_usuario SET ativo = NOT ativo, sessao_versao = sessao_versao + 1 WHERE id = %s", (uid,))
    _registrar(request, "desativou" if alvo["ativo"] else "reativou", _quem(alvo))
    return _com_msg("/admin/usuarios", f"{alvo['nome']} foi {'desativado(a): o acesso caiu na hora' if alvo['ativo'] else 'reativado(a)'}.")


@app.post("/admin/usuarios/{uid}/papel")
def admin_usuario_papel(request: Request, uid: int, papel: str = Form("")):
    alvo, erro = _usuario_alvo(request, uid)
    if erro:
        return erro
    if papel not in equipe.PAPEIS or papel == alvo["papel"]:
        return RedirectResponse("/admin/usuarios", 303)
    if alvo["papel"] == "admin" and equipe.admins_ativos(exceto=alvo["id"]) == 0:
        return _com_msg("/admin/usuarios", "É preciso manter pelo menos um administrador ativo.", erro=True)
    db.ex("UPDATE admin_usuario SET papel = %s WHERE id = %s", (papel, uid))
    _registrar(request, "mudou o papel de", f"{alvo['nome']}: {equipe.PAPEIS[alvo['papel']][0]} → {equipe.PAPEIS[papel][0]}")
    return _com_msg("/admin/usuarios", f"{alvo['nome']} agora é {equipe.PAPEIS[papel][0]}.")


@app.post("/admin/usuarios/{uid}/2fa")
def admin_usuario_2fa(request: Request, uid: int):
    """Para quem perdeu/trocou o celular: tira a verificação, a pessoa configura de novo."""
    alvo, erro = _usuario_alvo(request, uid)
    if erro:
        return erro
    db.ex("UPDATE admin_usuario SET totp_segredo = NULL, sessao_versao = sessao_versao + 1 WHERE id = %s", (uid,))
    _registrar(request, "removeu a verificação em duas etapas de", _quem(alvo))
    return _com_msg("/admin/usuarios", f"Verificação em duas etapas de {alvo['nome']} removida. Peça para configurar de novo.")


# --- atividade e backups (só administrador) ----------------------------------

@app.get("/admin/atividade", response_class=HTMLResponse)
def admin_atividade(request: Request, usuario: int = 0):
    where, params = ("WHERE usuario_id = %s", (usuario,)) if usuario else ("", ())
    registros = db.q(f"SELECT * FROM admin_log {where} ORDER BY ts DESC LIMIT 400", params)
    usuarios = db.q("SELECT id, nome FROM admin_usuario ORDER BY LOWER(nome)")
    return _admin_render(request, "atividade.html", aba="atividade", registros=registros, usuarios=usuarios,
                         usuario_sel=usuario)


BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/lib/ebooks-backups"))


def _backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    itens = []
    for p in BACKUP_DIR.iterdir():
        if p.is_file() and p.suffix in (".dump", ".tgz"):
            st = p.stat()
            itens.append({"nome": p.name, "tamanho": st.st_size, "quando": datetime.fromtimestamp(st.st_mtime),
                          "tipo": "Banco (leads, eventos, usuários)" if p.suffix == ".dump" else "PDFs e capas"})
    return sorted(itens, key=lambda i: i["quando"], reverse=True)


@app.get("/admin/backups", response_class=HTMLResponse)
def admin_backups(request: Request):
    itens = _backups()
    ultimo = itens[0]["quando"] if itens else None
    atrasado = ultimo is None or datetime.now() - ultimo > timedelta(hours=36)
    return _admin_render(request, "backups.html", aba="backups", itens=itens, ultimo=ultimo, atrasado=atrasado,
                         pasta_existe=BACKUP_DIR.is_dir())


@app.get("/admin/backups/{nome}")
def admin_backup_baixar(request: Request, nome: str):
    caminho = BACKUP_DIR / Path(nome).name
    if caminho.suffix not in (".dump", ".tgz") or not caminho.is_file():
        return _com_msg("/admin/backups", "Arquivo de backup não encontrado.", erro=True)
    _registrar(request, "baixou backup", caminho.name)
    return FileResponse(caminho, filename=caminho.name, media_type="application/octet-stream")


# --- ebooks -----------------------------------------------------------------

def _categorias() -> list[str]:
    return [r["categoria"] for r in db.q(
        "SELECT DISTINCT categoria FROM ebook WHERE categoria IS NOT NULL AND categoria <> '' ORDER BY 1")]


def _slug_livre(base: str, ignorar_id: int | None = None) -> str:
    slug, n = base, 2
    while db.one("SELECT 1 FROM ebook WHERE slug = %s AND id IS DISTINCT FROM %s", (slug, ignorar_id)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def _visibilidade(v: str) -> str:
    return v if v in VISIBILIDADES else "publicado"


def _voltar(v: str, padrao: str) -> str:
    """Retorno após uma ação do painel (lista de eventos ou tela do ebook)."""
    v = _proximo(v, padrao)
    return v if v.startswith("/admin/") else padrao


def _com_msg(url: str, msg: str, erro: bool = False) -> RedirectResponse:
    caminho, _, ancora = url.partition("#")
    sep = "&" if "?" in caminho else "?"
    return RedirectResponse(f"{caminho}{sep}{'err' if erro else 'msg'}={quote(msg)}"
                            + (f"#{ancora}" if ancora else ""), 303)


def _eventos(where: str = "", params: tuple = ()) -> list[dict]:
    return db.q(f"""
        SELECT ev.*, e.titulo AS ebook_titulo, e.capa, e.visibilidade,
               (SELECT COUNT(*) FROM pessoa_ebook pe WHERE pe.evento_id = ev.id) AS participantes,
               -- fundo do funil: participantes que depois abriram a biblioteca completa
               (SELECT COUNT(*) FROM pessoa_ebook pe JOIN pessoa p ON p.id = pe.pessoa_id
                 WHERE pe.evento_id = ev.id AND p.acesso_completo) AS completaram
        FROM evento ev JOIN ebook e ON e.id = ev.ebook_id {where}
        ORDER BY COALESCE(ev.data_evento, ev.criado_em::date) DESC, ev.id DESC""", params)


def _ctx_eventos(request: Request) -> dict:
    return {"url_base": _url_base(request), "hoje": date.today(), "visibilidades": VISIBILIDADES_PAINEL}


# Rótulos do painel (o seletor só oferece os 3 primeiros; 'arquivado' tem ação própria).
VISIBILIDADES_PAINEL = {**VISIBILIDADES, "arquivado": ("Arquivado", "Fora de circulação.")}


@app.get("/admin/ebooks", response_class=HTMLResponse)
def admin_ebooks(request: Request, ver: str = "", ordem: str = ""):
    if not _admin_ok(request):
        return _para_login(request)
    return _ebooks_render(request, ver, erro=None, f={}, ordem=ordem)


ORDENS_EBOOKS = {"": ("Posição na estante", "e.ordem, e.criado_em DESC"),
                 "recentes": ("Mais recentes primeiro", "e.criado_em DESC"),
                 "antigos": ("Mais antigos primeiro", "COALESCE(e.pdf_atualizado_em, e.criado_em) ASC")}


def _ebooks_render(request: Request, ver: str, erro: str | None, f: dict, ordem: str = ""):
    arquivados = ver == "arquivados"
    ordem = ordem if ordem in ORDENS_EBOOKS else ""
    n_arq = db.one("SELECT COUNT(*) AS n FROM ebook WHERE visibilidade = 'arquivado'")["n"]
    return _admin_render(request, "ebooks.html", ebooks=_lista_ebooks(arquivados, ordem), categorias=_categorias(),
                         erro=erro, f=f, aba="ebooks", visibilidades=VISIBILIDADES,
                         rotulos=VISIBILIDADES_PAINEL, ver_arquivados=arquivados, n_arquivados=n_arq,
                         ordem=ordem, ordens=ORDENS_EBOOKS)


def _lista_ebooks(arquivados: bool = False, ordem: str = "") -> list[dict]:
    ordenar = "e.arquivado_em DESC" if arquivados else ORDENS_EBOOKS.get(ordem, ORDENS_EBOOKS[""])[1]
    return db.q(f"""
        SELECT e.*, s.titulo AS substituto_titulo,
          -- "Revisar?": na estante há mais de REVISAR_MESES sem trocar o PDF
          (e.visibilidade = 'publicado' AND COALESCE(e.pdf_atualizado_em, e.criado_em)
             < now() - make_interval(months => {REVISAR_MESES})) AS revisar,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.ebook_id = e.id AND a.acao = 'leitura')  AS leituras,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.ebook_id = e.id AND a.acao = 'download') AS downloads,
          (SELECT COUNT(*) FROM evento ev WHERE ev.ebook_id = e.id) AS eventos,
          (SELECT COUNT(*) FROM pessoa p WHERE p.ebook_pedido_id = e.id) AS leads
        FROM ebook e LEFT JOIN ebook s ON s.id = e.substituto_id
        WHERE e.visibilidade {'=' if arquivados else '<>'} 'arquivado'
        ORDER BY {ordenar}""")


@app.post("/admin/ebooks", response_class=HTMLResponse)
async def admin_ebook_criar(request: Request, titulo: str = Form(""), subtitulo: str = Form(""),
                            descricao: str = Form(""), categoria: str = Form(""),
                            ordem: int = Form(0), visibilidade: str = Form("publicado"),
                            destaque: str = Form(""),
                            pdf: UploadFile = File(None), capa: UploadFile = File(None)):
    if not _admin_ok(request):
        return _para_login(request)
    f = {"titulo": _limpo(titulo, 200), "subtitulo": _limpo(subtitulo, 250),
         "descricao": _limpo(descricao, 3000), "categoria": _limpo(categoria, 80),
         "ordem": ordem, "visibilidade": _visibilidade(visibilidade), "destaque": bool(destaque)}
    erro = None
    conteudo_pdf = await pdf.read() if pdf and pdf.filename else b""
    if not f["titulo"]:
        erro = "Informe o título."
    elif not conteudo_pdf:
        erro = "Selecione o arquivo PDF."
    nome_pdf = nome_capa = None
    if not erro:
        slug = _slug_livre(arq.slugify(f["titulo"]))
        try:
            nome_pdf, paginas = arq.salvar_pdf(conteudo_pdf, slug)
            conteudo_capa = await capa.read() if capa and capa.filename else b""
            nome_capa = arq.salvar_capa(conteudo_capa, slug) if conteudo_capa else arq.capa_do_pdf(nome_pdf, slug)
        except arq.ArquivoInvalido as e:
            arq.apagar(arq.PDFS, nome_pdf)
            erro = str(e)
    if erro:
        return _ebooks_render(request, "", erro=erro, f=f)
    linha = db.ex("""INSERT INTO ebook(slug, titulo, subtitulo, descricao, categoria, arquivo, capa,
                                       paginas, tamanho, visibilidade, ordem, destaque)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (slug, f["titulo"], f["subtitulo"] or None, f["descricao"] or None, f["categoria"] or None,
                   nome_pdf, nome_capa, paginas, len(conteudo_pdf), f["visibilidade"], ordem, f["destaque"]))
    _registrar(request, "adicionou ebook", f"{f['titulo']} · {VISIBILIDADES[f['visibilidade']][0]}")
    # Vai direto para a tela do ebook: é lá que se cria o evento/QR dele.
    msg = {"publicado": "Ebook adicionado à estante. Vai usar num evento? Crie o QR code logo abaixo.",
           "evento": "Ebook salvo como exclusivo de eventos. Agora crie o evento abaixo para gerar o QR code.",
           "oculto": "Ebook salvo como oculto. Quando estiver pronto, mude a visibilidade e salve."}[f["visibilidade"]]
    return _com_msg(f"/admin/ebooks/{linha['id']}#eventos", msg)


@app.get("/admin/ebooks/{eid}", response_class=HTMLResponse)
def admin_ebook_editar(request: Request, eid: int):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("SELECT * FROM ebook WHERE id = %s", (eid,))
    if not ebook:
        return _com_msg("/admin/ebooks", "Ebook não encontrado.", erro=True)
    return _admin_render(request, "ebook_editar.html", **_ctx_editar(request, ebook), erro=None)


def _ctx_editar(request: Request, ebook: dict) -> dict:
    base = _url_base(request)
    return {"ebook": ebook, "categorias": _categorias(), "aba": "ebooks",
            "eventos": _eventos("WHERE ev.ebook_id = %s", (ebook["id"],)),
            "substitutos": db.q("SELECT id, titulo FROM ebook WHERE id <> %s AND visibilidade = 'publicado' "
                                "ORDER BY criado_em DESC", (ebook["id"],)),
            "substituto": db.one("SELECT id, titulo, slug FROM ebook WHERE id = %s", (ebook["substituto_id"],))
                          if ebook.get("substituto_id") else None,
            "link_publico": f"{base}/ebook/{ebook['slug']}", "canais": CANAIS,
            "opcoes_vis": VISIBILIDADES, **_ctx_eventos(request)}


@app.post("/admin/ebooks/{eid}", response_class=HTMLResponse)
async def admin_ebook_salvar(request: Request, eid: int, titulo: str = Form(""), subtitulo: str = Form(""),
                             descricao: str = Form(""), categoria: str = Form(""),
                             ordem: int = Form(0), visibilidade: str = Form("publicado"),
                             destaque: str = Form(""), regerar_capa: str = Form(""),
                             pdf: UploadFile = File(None), capa: UploadFile = File(None)):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("SELECT * FROM ebook WHERE id = %s", (eid,))
    if not ebook:
        return _com_msg("/admin/ebooks", "Ebook não encontrado.", erro=True)

    def _erro(texto: str):
        return _admin_render(request, "ebook_editar.html", **_ctx_editar(request, ebook), erro=texto)

    titulo = _limpo(titulo, 200)
    if not titulo:
        return _erro("Informe o título.")
    novo = {"arquivo": ebook["arquivo"], "capa": ebook["capa"], "paginas": ebook["paginas"],
            "tamanho": ebook["tamanho"]}
    try:
        conteudo_pdf = await pdf.read() if pdf and pdf.filename else b""
        if conteudo_pdf:
            novo["arquivo"], novo["paginas"] = arq.salvar_pdf(conteudo_pdf, ebook["slug"])
            novo["tamanho"] = len(conteudo_pdf)
        conteudo_capa = await capa.read() if capa and capa.filename else b""
        if conteudo_capa:
            novo["capa"] = arq.salvar_capa(conteudo_capa, ebook["slug"])
        elif regerar_capa or (conteudo_pdf and not ebook["capa"]):
            novo["capa"] = arq.capa_do_pdf(novo["arquivo"], ebook["slug"])
    except arq.ArquivoInvalido as e:
        if novo["arquivo"] != ebook["arquivo"]:
            arq.apagar(arq.PDFS, novo["arquivo"])
        return _erro(str(e))
    # Arquivado só sai do arquivo pelo botão Restaurar: o formulário não mexe nisso.
    vis = ebook["visibilidade"] if ebook["visibilidade"] == "arquivado" else _visibilidade(visibilidade)
    db.ex("""UPDATE ebook SET titulo=%s, subtitulo=%s, descricao=%s, categoria=%s, ordem=%s,
                              visibilidade=%s, destaque=%s, arquivo=%s, capa=%s, paginas=%s, tamanho=%s,
                              atualizado_em=now(),
                              pdf_atualizado_em = CASE WHEN %s THEN now() ELSE pdf_atualizado_em END
             WHERE id=%s""",
          (titulo, _limpo(subtitulo, 250) or None, _limpo(descricao, 3000) or None,
           _limpo(categoria, 80) or None, ordem, vis, bool(destaque), novo["arquivo"], novo["capa"],
           novo["paginas"], novo["tamanho"], novo["arquivo"] != ebook["arquivo"], eid))
    if novo["arquivo"] != ebook["arquivo"]:
        arq.apagar(arq.PDFS, ebook["arquivo"])
    if novo["capa"] != ebook["capa"]:
        arq.apagar(arq.CAPAS, ebook["capa"])
    mudou = [m for m, c in (("PDF trocado", novo["arquivo"] != ebook["arquivo"]),
                             ("capa trocada", novo["capa"] != ebook["capa"]),
                             (f"visibilidade → {VISIBILIDADES_PAINEL[vis][0]}", vis != ebook["visibilidade"])) if c]
    _registrar(request, "editou ebook", titulo + (f" ({', '.join(mudou)})" if mudou else ""))
    return _com_msg(f"/admin/ebooks/{eid}", "Alterações salvas.")


@app.post("/admin/ebooks/{eid}/arquivar")
def admin_ebook_arquivar(request: Request, eid: int, motivo: str = Form(""), substituto_id: int = Form(0)):
    """Tira de circulação sem perder nada: leads, leituras e eventos ficam.

    Links e QR antigos passam a dizer "material atualizado" (e levam à edição
    nova, se houver) em vez de dar erro.
    """
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("SELECT * FROM ebook WHERE id = %s", (eid,))
    if not ebook or ebook["visibilidade"] == "arquivado":
        return _com_msg("/admin/ebooks", "Este ebook já está arquivado.", erro=True)
    subst = substituto_id if substituto_id and substituto_id != eid and db.one(
        "SELECT 1 FROM ebook WHERE id = %s AND visibilidade <> 'arquivado'", (substituto_id,)) else None
    db.ex("""UPDATE ebook SET vis_antes_arquivo = visibilidade, visibilidade = 'arquivado', destaque = FALSE,
                              arquivado_em = now(), motivo_arquivo = %s, substituto_id = %s
             WHERE id = %s""", (_limpo(motivo, 300) or None, subst, eid))
    _registrar(request, "arquivou ebook", ebook["titulo"] + (f" · motivo: {_limpo(motivo, 300)}" if motivo.strip() else ""))
    return _com_msg(f"/admin/ebooks/{eid}", "Ebook arquivado. Saiu da estante; os leads e o histórico continuam guardados.")


@app.post("/admin/ebooks/{eid}/restaurar")
def admin_ebook_restaurar(request: Request, eid: int):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("""UPDATE ebook SET visibilidade = COALESCE(vis_antes_arquivo, 'oculto'), arquivado_em = NULL,
                                       motivo_arquivo = NULL, substituto_id = NULL, vis_antes_arquivo = NULL
                      WHERE id = %s AND visibilidade = 'arquivado' RETURNING visibilidade""", (eid,))
    if not ebook:
        return _com_msg(f"/admin/ebooks/{eid}", "Este ebook não está arquivado.", erro=True)
    _registrar(request, "restaurou ebook", f"#{eid} → {VISIBILIDADES[ebook['visibilidade']][0]}")
    return _com_msg(f"/admin/ebooks/{eid}", f"Ebook restaurado como “{VISIBILIDADES[ebook['visibilidade']][0]}”.")


@app.post("/admin/ebooks/{eid}/excluir")
def admin_ebook_excluir(request: Request, eid: int):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("DELETE FROM ebook WHERE id = %s RETURNING arquivo, capa, titulo", (eid,))
    if ebook:
        arq.apagar(arq.PDFS, ebook["arquivo"])
        arq.apagar(arq.CAPAS, ebook["capa"])
        _registrar(request, "EXCLUIU ebook", ebook["titulo"])
    return _com_msg("/admin/ebooks", f"“{ebook['titulo']}” foi excluído." if ebook else "Ebook excluído.")


@app.get("/admin/ebooks/{eid}/pdf")
def admin_ebook_pdf(request: Request, eid: int):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("SELECT arquivo FROM ebook WHERE id = %s", (eid,))
    if not ebook:
        return Response(status_code=404)
    return FileResponse(arq.PDFS / ebook["arquivo"], media_type="application/pdf",
                        headers={"Content-Disposition": "inline"})


# --- eventos ----------------------------------------------------------------

@app.get("/admin/eventos", response_class=HTMLResponse)
def admin_eventos(request: Request, ebook: int = 0):
    if not _admin_ok(request):
        return _para_login(request)
    ebooks = db.q("SELECT id, titulo, visibilidade FROM ebook WHERE visibilidade <> 'arquivado' ORDER BY titulo")
    return _admin_render(request, "eventos.html", eventos=_eventos(), ebooks=ebooks, ebook_sel=ebook,
                         aba="eventos", **_ctx_eventos(request))


def _data(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@app.post("/admin/eventos")
def admin_evento_criar(request: Request, nome: str = Form(""), ebook_id: int = Form(0),
                       data_evento: str = Form(""), valido_ate: str = Form(""), voltar: str = Form(""),
                       para_estudantes: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    volta = _voltar(voltar, "/admin/eventos")
    nome = _limpo(nome, 160)
    if not nome:
        return _com_msg(volta, "Informe o nome do evento.", erro=True)
    ebook = db.one("SELECT visibilidade FROM ebook WHERE id = %s", (ebook_id,))
    if not ebook:
        return _com_msg(volta, "Escolha o ebook que o evento vai liberar.", erro=True)
    if ebook["visibilidade"] == "arquivado":
        return _com_msg(volta, "Este ebook está arquivado. Restaure-o ou escolha a edição atual para o evento.", erro=True)
    dt_evento = _data(data_evento)
    # Sem validade informada: 30 dias depois do evento (ou de hoje).
    dt_validade = _data(valido_ate) or (dt_evento or date.today()) + timedelta(days=30)
    if dt_validade < date.today():
        return _com_msg(volta, "A validade do QR já passou. Escolha uma data a partir de hoje.", erro=True)
    linha = db.ex("""INSERT INTO evento(nome, token, ebook_id, data_evento, valido_ate, para_estudantes)
                     VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (nome, secrets.token_urlsafe(8), ebook_id, dt_evento, dt_validade, bool(para_estudantes)))
    _registrar(request, "criou evento", f"{nome} · vale até {dt_validade:%d/%m/%Y}")
    msg = f"Evento criado. O QR code está pronto e vale até {dt_validade:%d/%m/%Y}."
    if ebook["visibilidade"] == "oculto":
        return _com_msg(volta + f"#ev{linha['id']}", msg + " Atenção: o ebook está OCULTO e o QR não vai abrir "
                        "até você mudar a visibilidade dele.", erro=True)
    return _com_msg(volta + f"#ev{linha['id']}", msg)


@app.post("/admin/eventos/{evid}/alternar")
def admin_evento_alternar(request: Request, evid: int, voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    ev = db.one("UPDATE evento SET ativo = NOT ativo WHERE id = %s RETURNING ativo, nome", (evid,))
    if ev:
        _registrar(request, "reativou QR do evento" if ev["ativo"] else "desativou QR do evento", ev["nome"])
    msg = "QR reativado." if ev and ev["ativo"] else "QR desativado: quem escanear verá que o acesso foi encerrado."
    return _com_msg(_voltar(voltar, "/admin/eventos"), msg)


@app.post("/admin/eventos/{evid}/validade")
def admin_evento_validade(request: Request, evid: int, valido_ate: str = Form(""), voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    volta = _voltar(voltar, "/admin/eventos")
    nova = _data(valido_ate)
    if valido_ate and not nova:
        return _com_msg(volta, "Data inválida.", erro=True)
    db.ex("UPDATE evento SET valido_ate = %s WHERE id = %s", (nova, evid))
    _registrar(request, "mudou validade do QR", f"evento #{evid} → {nova:%d/%m/%Y}" if nova else f"evento #{evid} → sem validade")
    return _com_msg(volta, f"Validade alterada para {nova:%d/%m/%Y}." if nova else "O QR agora não expira.")


@app.post("/admin/eventos/{evid}/excluir")
def admin_evento_excluir(request: Request, evid: int, voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    # Quem já entrou pelo evento mantém o ebook (pessoa_ebook.evento_id vira NULL).
    ev = db.one("DELETE FROM evento WHERE id = %s RETURNING nome", (evid,))
    if ev:
        _registrar(request, "excluiu evento", ev["nome"])
    return _com_msg(_voltar(voltar, "/admin/eventos"), "Evento excluído. Quem já tinha entrado continua com o ebook.")


@app.get("/admin/eventos/{evid}/qr.png")
def admin_evento_qr(request: Request, evid: int, baixar: int = 0):
    if not _admin_ok(request):
        return _para_login(request)
    ev = db.one("SELECT token, nome FROM evento WHERE id = %s", (evid,))
    if not ev:
        return Response(status_code=404)
    png = arq.qr_png(f"{_url_base(request)}/e/{ev['token']}")
    headers = {"Cache-Control": "private, max-age=3600"}
    if baixar:
        headers["Content-Disposition"] = f"attachment; filename=\"qr-{arq.slugify(ev['nome'])}.png\""
    return Response(png, media_type="image/png", headers=headers)


@app.get("/admin/eventos/{evid}/imprimir", response_class=HTMLResponse)
def admin_evento_imprimir(request: Request, evid: int):
    """Cartaz A4 pronto para imprimir/projetar no evento."""
    if not _admin_ok(request):
        return _para_login(request)
    ev = _eventos("WHERE ev.id = %s", (evid,))
    if not ev:
        return _com_msg("/admin/eventos", "Evento não encontrado.", erro=True)
    return tpl.TemplateResponse(request, "admin/imprimir.html", {"ev": ev[0], "url_base": _url_base(request)})


# --- leads ------------------------------------------------------------------

# Canal de chegada de cada lead, numa coluna só: o link de campanha vale mais
# (é dado medido); sem ele, o que a pessoa marcou; sem nada, se veio de evento.
# A origem do link é traduzida para o mesmo rótulo do "Como conheceu", para que
# "Instagram" do link e "Instagram" marcado no formulário somem juntos.
_ORIGEM_ROTULO = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in ORIGEM_PARA_CONHECEU.items())
_CANAL_SQL = f"""COALESCE(CASE p.campanha_origem {_ORIGEM_ROTULO} ELSE INITCAP(p.campanha_origem) END,
                          NULLIF(p.como_conheceu, ''),
                          CASE WHEN p.origem LIKE 'evento:%%' THEN 'Evento / palestra' END, 'Não informado')"""


def _filtro_leads(busca: str, tipo: str, canal: str = "") -> tuple[str, list]:
    where, params = [], []
    if busca:
        where.append("(p.nome ILIKE %s OR p.email ILIKE %s OR p.empresa ILIKE %s OR p.instituicao ILIKE %s)")
        params += [f"%{busca}%"] * 4
    if tipo == "completo":
        where.append("p.acesso_completo")
    elif tipo == "evento":
        where.append("NOT p.acesso_completo")
    elif tipo == "estudante":
        where.append("p.perfil = 'estudante'")
    elif tipo == "profissional":
        where.append("COALESCE(p.perfil, 'profissional') = 'profissional'")
    if canal:
        where.append(f"{_CANAL_SQL} = %s")
        params.append(canal)
    return ("WHERE " + " AND ".join(where) if where else ""), params


def _leads(busca: str, tipo: str, canal: str = "", limite: int | None = None, offset: int = 0) -> list[dict]:
    where, params = _filtro_leads(busca, tipo, canal)
    sql = f"""
        SELECT p.*, {_CANAL_SQL} AS canal,
          (SELECT titulo FROM ebook e WHERE e.id = p.ebook_pedido_id) AS ebook_pedido,
          (SELECT string_agg(DISTINCT ev.nome, ', ') FROM pessoa_ebook pe
             JOIN evento ev ON ev.id = pe.evento_id WHERE pe.pessoa_id = p.id) AS eventos,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.pessoa_id = p.id AND a.acao = 'leitura')  AS leituras,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.pessoa_id = p.id AND a.acao = 'download') AS downloads
        FROM pessoa p {where}
        ORDER BY p.criado_em DESC"""
    if limite:
        sql += f" LIMIT {int(limite)} OFFSET {int(offset)}"
    return db.q(sql, params)


def _conta_leads(busca: str, tipo: str, canal: str) -> int:
    """Total do filtro, para a paginação (mesmo WHERE de _leads)."""
    where, params = _filtro_leads(busca, tipo, canal)
    return db.one(f"SELECT COUNT(*) AS n FROM pessoa p {where}", params)["n"]


POR_PAGINA = 100


@app.get("/admin/leads", response_class=HTMLResponse)
def admin_leads(request: Request, busca: str = "", tipo: str = "", canal: str = "", pagina: int = 1):
    if not _admin_ok(request):
        return _para_login(request)
    resumo = db.one("""SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE acesso_completo) AS completos,
                              COUNT(*) FILTER (WHERE NOT acesso_completo) AS so_evento,
                              COUNT(*) FILTER (WHERE criado_em > now() - interval '7 days') AS semana,
                              COUNT(*) FILTER (WHERE perfil = 'estudante') AS estudantes
                       FROM pessoa""")
    canais = db.q(f"""SELECT {_CANAL_SQL} AS canal, COUNT(*) AS n,
                             COUNT(*) FILTER (WHERE p.acesso_completo) AS completos
                      FROM pessoa p GROUP BY 1 ORDER BY 2 DESC""")
    total = _conta_leads(busca.strip(), tipo, canal)
    paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(1, pagina), paginas)
    leads = _leads(busca.strip(), tipo, canal, POR_PAGINA, (pagina - 1) * POR_PAGINA)
    return _admin_render(request, "leads.html", leads=leads, resumo=resumo, canais=canais, busca=busca, tipo=tipo,
                         canal=canal, aba="leads", total=total, pagina=pagina, paginas=paginas, por_pagina=POR_PAGINA)


@app.get("/admin/leads.csv")
def admin_leads_csv(request: Request, busca: str = "", tipo: str = "", canal: str = ""):
    if not _admin_ok(request):
        return _para_login(request)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nome", "E-mail", "Telefone", "Perfil", "Empresa", "Cargo", "CNPJ", "Instituição", "Curso", "Período",
                "Canal", "Campanha",
                "Como conheceu", "Ebook pedido", "E-mail confirmado",
                "Aceita novidades", "Acesso", "Origem", "Eventos", "Leituras", "Downloads",
                "Cadastro", "Último acesso"])
    for p in _leads(busca.strip(), tipo, canal):
        w.writerow([p["nome"], p["email"], p["telefone"] or "", "Estudante" if p["perfil"] == "estudante" else "Profissional",
                    p["empresa"] or "", p["cargo"] or "", p["cnpj"] or "", p["instituicao"] or "", p["curso"] or "",
                    p["periodo"] or "", p["canal"], p["campanha_nome"] or "", p["como_conheceu"] or "",
                    p["ebook_pedido"] or "", "Sim" if p["email_confirmado_em"] else "Não",
                    "Sim" if p["aceita_novidades"] else "Não",
                    "Biblioteca completa" if p["acesso_completo"] else "Só evento", p["origem"] or "",
                    p["eventos"] or "", p["leituras"], p["downloads"], _dt_br(p["criado_em"]),
                    _dt_br(p["ultimo_acesso"])])
    _registrar(request, "exportou leads (CSV)", ", ".join(f"{k}={v}" for k, v in (("busca", busca), ("tipo", tipo), ("canal", canal)) if v))
    # utf-8-sig: o Excel em PT-BR só reconhece o acento com o BOM.
    nome = f"leads-biblioteca-{date.today():%Y-%m-%d}.csv"
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=\"{nome}\""})


@app.post("/admin/leads/{pid}/excluir")
def admin_lead_excluir(request: Request, pid: int):
    """Atende pedido de exclusão de dados (LGPD)."""
    if not _admin_ok(request):
        return _para_login(request)
    p = db.one("DELETE FROM pessoa WHERE id = %s RETURNING nome, email", (pid,))
    if p:
        _registrar(request, "excluiu dados de lead (LGPD)", f"{p['nome']} <{p['email']}>")
    return RedirectResponse(f"/admin/leads?msg={quote('Contato excluído.')}", 303)


@app.get("/saude")
def saude():
    db.one("SELECT 1")
    return {"ok": True}
