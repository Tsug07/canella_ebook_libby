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

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import arquivos as arq
from . import database as db
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
    return f"{v / 1024 / 1024:.1f} MB".replace(".", ",")


tpl.env.filters["dtbr"] = _dt_br
tpl.env.filters["tamanho"] = _tamanho
tpl.env.filters["cnpj"] = lambda v: f"{v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]}" if v and len(v) == 14 else (v or "")


def _render(request: Request, nome: str, **ctx):
    status = ctx.pop("status_code", 200)
    ctx.setdefault("leitor", leitor_atual(request))
    return tpl.TemplateResponse(request, nome, ctx, status_code=status)


def _url_base(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def _proximo(valor: str | None, padrao: str = "/") -> str:
    """Só aceita caminho relativo do próprio site (evita redirecionar para fora)."""
    if valor and valor.startswith("/") and not valor.startswith("//") and "\\" not in valor:
        return valor
    return padrao


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


def pode_ler(leitor: dict | None, ebook: dict) -> bool:
    if not leitor or ebook["visibilidade"] == "oculto":
        return False
    if ebook["visibilidade"] == "publicado" and leitor["acesso_completo"]:
        return True
    return ebook["id"] in ids_de_evento(leitor)


def _ebook_visivel(slug: str) -> dict | None:
    return db.one("SELECT * FROM ebook WHERE slug = %s AND visibilidade <> 'oculto'", (slug,))


# ---------------------------------------------------------------------------
# Estante
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def estante(request: Request):
    leitor = leitor_atual(request)
    completo = bool(leitor and leitor["acesso_completo"])
    ganhos = ids_de_evento(leitor)
    campos = "id, slug, titulo, subtitulo, descricao, categoria, capa, paginas"
    ebooks = db.q(f"SELECT {campos} FROM ebook WHERE visibilidade = 'publicado' "
                  "ORDER BY ordem, criado_em DESC")
    prateleiras: dict[str, list] = {}
    # Os exclusivos de evento não estão na estante pública: quem os ganhou
    # precisa reencontrá-los, então eles abrem uma prateleira própria no topo.
    if ganhos:
        meus = db.q(f"SELECT {campos} FROM ebook WHERE visibilidade = 'evento' AND id = ANY(%s) "
                    "ORDER BY ordem, criado_em DESC", (list(ganhos),))
        for e in meus:
            e["liberado"] = True
        if meus:
            prateleiras["Seus presentes de eventos"] = meus
    for e in ebooks:
        e["liberado"] = completo or e["id"] in ganhos
        prateleiras.setdefault(e["categoria"] or "Ebooks", []).append(e)
    return _render(request, "estante.html", prateleiras=prateleiras, total=len(ebooks),
                   acesso_parcial=leitor is not None and not completo)


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
COMO_CONHECEU = ["Instagram", "LinkedIn", "Google", "Indicação", "Evento / palestra",
                 "Já sou cliente", "Outro"]


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
def acesso_form(request: Request, next: str = "/", aba: str = "cadastro"):
    leitor = leitor_atual(request)
    if leitor and leitor["acesso_completo"]:
        return RedirectResponse(_proximo(next), 303)
    # Quem veio de um evento já tem nome/e-mail: o formulário abre preenchido.
    f = {k: (leitor or {}).get(k) or "" for k in ("nome", "email", "telefone", "empresa", "cargo", "cnpj")}
    return _render(request, "acesso.html", f=f, erros={}, next=_proximo(next),
                   opcoes_conheceu=COMO_CONHECEU, aba="entrar" if aba == "entrar" and not leitor else "cadastro")


@app.post("/acesso", response_class=HTMLResponse)
def acesso_enviar(request: Request,
                  nome: str = Form(""), email: str = Form(""), telefone: str = Form(""),
                  empresa: str = Form(""), cargo: str = Form(""), cnpj: str = Form(""),
                  como_conheceu: str = Form(""), consentimento: str = Form(""),
                  novidades: str = Form(""), site: str = Form(""), next: str = Form("/")):
    proximo = _proximo(next)
    if site:  # honeypot: campo invisível que só robô preenche
        return RedirectResponse(proximo, 303)
    f = {"nome": _limpo(nome, 120), "email": _limpo(email, 160).lower(),
         "telefone": _limpo(telefone, 30), "empresa": _limpo(empresa, 160),
         "cargo": _limpo(cargo, 120), "cnpj": _limpo(cnpj, 20),
         "como_conheceu": _limpo(como_conheceu, 60), "consentimento": consentimento,
         "novidades": novidades}
    erros = _validar_basico(f)
    if len(re.sub(r"\D", "", f["telefone"])) < 10:
        erros["telefone"] = "Informe o telefone com DDD."
    if len(f["empresa"]) < 2:
        erros["empresa"] = "Informe a empresa."
    if len(f["cargo"]) < 2:
        erros["cargo"] = "Informe seu cargo."
    cnpj_dig = re.sub(r"\D", "", f["cnpj"])
    if cnpj_dig and len(cnpj_dig) != 14:
        erros["cnpj"] = "O CNPJ tem 14 dígitos (ou deixe em branco)."
    if erros:
        return _render(request, "acesso.html", f=f, erros=erros, next=proximo,
                       opcoes_conheceu=COMO_CONHECEU, aba="cadastro")

    linha = db.ex(
        """INSERT INTO pessoa(nome, email, telefone, empresa, cargo, cnpj, como_conheceu,
                              aceita_novidades, consentimento_em, acesso_completo, origem, liberado_em)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now(), TRUE, 'formulario', now())
           ON CONFLICT ((LOWER(email))) DO UPDATE SET
             nome = EXCLUDED.nome, telefone = EXCLUDED.telefone, empresa = EXCLUDED.empresa,
             cargo = EXCLUDED.cargo, cnpj = EXCLUDED.cnpj,
             como_conheceu = COALESCE(NULLIF(EXCLUDED.como_conheceu, ''), pessoa.como_conheceu),
             aceita_novidades = EXCLUDED.aceita_novidades, consentimento_em = now(),
             acesso_completo = TRUE, liberado_em = COALESCE(pessoa.liberado_em, now())
           RETURNING id""",
        (f["nome"], f["email"], f["telefone"], f["empresa"], f["cargo"], cnpj_dig or None,
         f["como_conheceu"] or None, bool(novidades)))
    return _entrar(RedirectResponse(proximo + ("&" if "?" in proximo else "?") + "bemvindo=1", 303),
                   linha["id"])


@app.post("/entrar", response_class=HTMLResponse)
def entrar(request: Request, email: str = Form(""), next: str = Form("/")):
    """'Já tenho acesso': reentrada pelo e-mail (outro aparelho, cookie apagado)."""
    proximo = _proximo(next)
    pessoa = db.one("SELECT id FROM pessoa WHERE LOWER(email) = LOWER(%s)", (_limpo(email, 160),))
    if not pessoa:
        return _render(request, "acesso.html", f={"email": _limpo(email, 160)}, next=proximo,
                       erros={"entrar": "Não encontramos esse e-mail. Preencha o cadastro para liberar o acesso."},
                       opcoes_conheceu=COMO_CONHECEU, aba="entrar")
    return _entrar(RedirectResponse(proximo, 303), pessoa["id"])


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
    if ebook["visibilidade"] == "evento":
        # O cadastro não libera exclusivo de evento: mandar ao formulário seria
        # prometer algo que ele não entrega.
        return _render(request, "aviso.html", titulo="Exclusivo para participantes",
                       texto="Este ebook é um presente para quem participou de um evento da Canella & Santos "
                             "e é liberado pelo QR code entregue no evento. Já participou? Escaneie o QR de novo "
                             "ou use \"Já tenho acesso\" com o e-mail que você informou.",
                       link_texto="Conhecer a biblioteca", link="/", status_code=403)
    return RedirectResponse(f"/acesso?next={quote('/ler/' + slug)}", 303)


@app.get("/ler/{slug}", response_class=HTMLResponse)
def ler(request: Request, slug: str):
    ebook = _ebook_visivel(slug)
    leitor = leitor_atual(request)
    if not ebook or not pode_ler(leitor, ebook):
        return _sem_acesso(request, ebook, slug)
    db.ex("INSERT INTO acesso_log(pessoa_id, ebook_id, acao) VALUES (%s,%s,'leitura')",
          (leitor["id"], ebook["id"]))
    return _render(request, "ler.html", ebook=ebook, acesso_parcial=not leitor["acesso_completo"])


@app.get("/ler/{slug}/arquivo")
def arquivo(request: Request, slug: str):
    ebook = _ebook_visivel(slug)
    if not ebook or not pode_ler(leitor_atual(request), ebook):
        return Response(status_code=403)
    return FileResponse(arq.PDFS / ebook["arquivo"], media_type="application/pdf",
                        headers={"Cache-Control": "private, no-store",
                                 "Content-Disposition": "inline"})


@app.get("/ler/{slug}/download")
def download(request: Request, slug: str):
    ebook = _ebook_visivel(slug)
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
    if not ev["ativo"] or ev["visibilidade"] == "oculto":
        return None, "O acesso por este QR code foi encerrado. Mas a biblioteca continua aberta: conheça os outros materiais."
    if ev["valido_ate"] and ev["valido_ate"] < date.today():
        return None, (f"O acesso por este QR code valia até {ev['valido_ate']:%d/%m/%Y}. "
                      "Mas a biblioteca continua aberta: conheça os outros materiais.")
    return ev, None


def _liberar_do_evento(pessoa_id: int, ev: dict) -> None:
    db.ex("""INSERT INTO pessoa_ebook(pessoa_id, ebook_id, evento_id) VALUES (%s,%s,%s)
             ON CONFLICT DO NOTHING""", (pessoa_id, ev["ebook_id"], ev["id"]))


@app.get("/e/{token}", response_class=HTMLResponse)
def evento(request: Request, token: str):
    ev, erro = _evento_valido(token)
    if erro:
        return _render(request, "aviso.html", titulo="Acesso indisponível", texto=erro,
                       link_texto="Conhecer a biblioteca", link="/")
    leitor = leitor_atual(request)
    if leitor:
        _liberar_do_evento(leitor["id"], ev)
        return RedirectResponse(f"/ler/{ev['slug']}?presente=1", 303)
    return _render(request, "evento.html", ev=ev, f={}, erros={})


@app.post("/e/{token}", response_class=HTMLResponse)
def evento_enviar(request: Request, token: str, nome: str = Form(""), email: str = Form(""),
                  consentimento: str = Form(""), site: str = Form("")):
    ev, erro = _evento_valido(token)
    if erro:
        return _render(request, "aviso.html", titulo="Acesso indisponível", texto=erro,
                       link_texto="Conhecer a biblioteca", link="/")
    if site:
        return RedirectResponse("/", 303)
    f = {"nome": _limpo(nome, 120), "email": _limpo(email, 160).lower(), "consentimento": consentimento}
    erros = _validar_basico(f)
    if erros:
        return _render(request, "evento.html", ev=ev, f=f, erros=erros)
    linha = db.ex(
        """INSERT INTO pessoa(nome, email, consentimento_em, origem)
           VALUES (%s, %s, now(), %s)
           ON CONFLICT ((LOWER(email))) DO UPDATE SET consentimento_em = now()
           RETURNING id""",
        (f["nome"], f["email"], f"evento: {ev['nome']}"))
    _liberar_do_evento(linha["id"], ev)
    return _entrar(RedirectResponse(f"/ler/{ev['slug']}?presente=1", 303), linha["id"])


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def _admin_ok(request: Request) -> bool:
    return bool(sessao.ler(request.cookies.get(sessao.COOKIE_ADMIN)))


def _admin_render(request: Request, nome: str, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("msg_erro", request.query_params.get("err"))
    return tpl.TemplateResponse(request, f"admin/{nome}", ctx)


def _para_login(request: Request):
    return RedirectResponse(f"/admin/login?next={quote(request.url.path)}", 303)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request, next: str = "/admin"):
    return _admin_render(request, "login.html", erro=None, next=_proximo(next, "/admin"),
                         sem_senha=not os.environ.get("ADMIN_SENHA"))


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, senha: str = Form(""), next: str = Form("/admin")):
    if not sessao.senha_admin_ok(senha):
        return _admin_render(request, "login.html", erro="Senha incorreta.", next=_proximo(next, "/admin"),
                             sem_senha=not os.environ.get("ADMIN_SENHA"))
    resp = RedirectResponse(_proximo(next, "/admin"), 303)
    resp.set_cookie(sessao.COOKIE_ADMIN, sessao.assinar({"adm": 1}, sessao.DURACAO_ADMIN),
                    max_age=sessao.DURACAO_ADMIN, httponly=True, samesite="strict",
                    secure=COOKIE_SEGURO, path="/admin")
    return resp


@app.get("/admin/sair")
def admin_sair():
    resp = RedirectResponse("/admin/login", 303)
    resp.delete_cookie(sessao.COOKIE_ADMIN, path="/admin")
    return resp


@app.get("/admin")
def admin_inicio(request: Request):
    if not _admin_ok(request):
        return _para_login(request)
    return RedirectResponse("/admin/ebooks", 303)


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
               (SELECT COUNT(*) FROM pessoa_ebook pe WHERE pe.evento_id = ev.id) AS participantes
        FROM evento ev JOIN ebook e ON e.id = ev.ebook_id {where}
        ORDER BY COALESCE(ev.data_evento, ev.criado_em::date) DESC, ev.id DESC""", params)


def _ctx_eventos(request: Request) -> dict:
    return {"url_base": _url_base(request), "hoje": date.today(), "visibilidades": VISIBILIDADES}


@app.get("/admin/ebooks", response_class=HTMLResponse)
def admin_ebooks(request: Request):
    if not _admin_ok(request):
        return _para_login(request)
    return _admin_render(request, "ebooks.html", ebooks=_lista_ebooks(), categorias=_categorias(),
                         erro=None, f={}, aba="ebooks", visibilidades=VISIBILIDADES)


def _lista_ebooks() -> list[dict]:
    return db.q("""
        SELECT e.*,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.ebook_id = e.id AND a.acao = 'leitura')  AS leituras,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.ebook_id = e.id AND a.acao = 'download') AS downloads,
          (SELECT COUNT(*) FROM evento ev WHERE ev.ebook_id = e.id) AS eventos
        FROM ebook e ORDER BY e.ordem, e.criado_em DESC""")


@app.post("/admin/ebooks", response_class=HTMLResponse)
async def admin_ebook_criar(request: Request, titulo: str = Form(""), subtitulo: str = Form(""),
                            descricao: str = Form(""), categoria: str = Form(""),
                            ordem: int = Form(0), visibilidade: str = Form("publicado"),
                            pdf: UploadFile = File(None), capa: UploadFile = File(None)):
    if not _admin_ok(request):
        return _para_login(request)
    f = {"titulo": _limpo(titulo, 200), "subtitulo": _limpo(subtitulo, 250),
         "descricao": _limpo(descricao, 3000), "categoria": _limpo(categoria, 80),
         "ordem": ordem, "visibilidade": _visibilidade(visibilidade)}
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
        return _admin_render(request, "ebooks.html", ebooks=_lista_ebooks(), categorias=_categorias(),
                             erro=erro, f=f, aba="ebooks", visibilidades=VISIBILIDADES)
    linha = db.ex("""INSERT INTO ebook(slug, titulo, subtitulo, descricao, categoria, arquivo, capa,
                                       paginas, tamanho, visibilidade, ordem)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                  (slug, f["titulo"], f["subtitulo"] or None, f["descricao"] or None, f["categoria"] or None,
                   nome_pdf, nome_capa, paginas, len(conteudo_pdf), f["visibilidade"], ordem))
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
    return _admin_render(request, "ebook_editar.html", ebook=ebook, categorias=_categorias(), erro=None,
                         aba="ebooks", eventos=_eventos("WHERE ev.ebook_id = %s", (eid,)),
                         **_ctx_eventos(request))


@app.post("/admin/ebooks/{eid}", response_class=HTMLResponse)
async def admin_ebook_salvar(request: Request, eid: int, titulo: str = Form(""), subtitulo: str = Form(""),
                             descricao: str = Form(""), categoria: str = Form(""),
                             ordem: int = Form(0), visibilidade: str = Form("publicado"),
                             regerar_capa: str = Form(""),
                             pdf: UploadFile = File(None), capa: UploadFile = File(None)):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("SELECT * FROM ebook WHERE id = %s", (eid,))
    if not ebook:
        return _com_msg("/admin/ebooks", "Ebook não encontrado.", erro=True)

    def _erro(texto: str):
        return _admin_render(request, "ebook_editar.html", ebook=ebook, categorias=_categorias(), erro=texto,
                             aba="ebooks", eventos=_eventos("WHERE ev.ebook_id = %s", (eid,)),
                             **_ctx_eventos(request))

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
    db.ex("""UPDATE ebook SET titulo=%s, subtitulo=%s, descricao=%s, categoria=%s, ordem=%s,
                              visibilidade=%s, arquivo=%s, capa=%s, paginas=%s, tamanho=%s,
                              atualizado_em=now()
             WHERE id=%s""",
          (titulo, _limpo(subtitulo, 250) or None, _limpo(descricao, 3000) or None,
           _limpo(categoria, 80) or None, ordem, _visibilidade(visibilidade), novo["arquivo"], novo["capa"],
           novo["paginas"], novo["tamanho"], eid))
    if novo["arquivo"] != ebook["arquivo"]:
        arq.apagar(arq.PDFS, ebook["arquivo"])
    if novo["capa"] != ebook["capa"]:
        arq.apagar(arq.CAPAS, ebook["capa"])
    return _com_msg(f"/admin/ebooks/{eid}", "Alterações salvas.")


@app.post("/admin/ebooks/{eid}/excluir")
def admin_ebook_excluir(request: Request, eid: int):
    if not _admin_ok(request):
        return _para_login(request)
    ebook = db.one("DELETE FROM ebook WHERE id = %s RETURNING arquivo, capa, titulo", (eid,))
    if ebook:
        arq.apagar(arq.PDFS, ebook["arquivo"])
        arq.apagar(arq.CAPAS, ebook["capa"])
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
    ebooks = db.q("SELECT id, titulo, visibilidade FROM ebook ORDER BY titulo")
    return _admin_render(request, "eventos.html", eventos=_eventos(), ebooks=ebooks, ebook_sel=ebook,
                         aba="eventos", **_ctx_eventos(request))


def _data(v: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


@app.post("/admin/eventos")
def admin_evento_criar(request: Request, nome: str = Form(""), ebook_id: int = Form(0),
                       data_evento: str = Form(""), valido_ate: str = Form(""), voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    volta = _voltar(voltar, "/admin/eventos")
    nome = _limpo(nome, 160)
    if not nome:
        return _com_msg(volta, "Informe o nome do evento.", erro=True)
    ebook = db.one("SELECT visibilidade FROM ebook WHERE id = %s", (ebook_id,))
    if not ebook:
        return _com_msg(volta, "Escolha o ebook que o evento vai liberar.", erro=True)
    dt_evento = _data(data_evento)
    # Sem validade informada: 30 dias depois do evento (ou de hoje).
    dt_validade = _data(valido_ate) or (dt_evento or date.today()) + timedelta(days=30)
    if dt_validade < date.today():
        return _com_msg(volta, "A validade do QR já passou. Escolha uma data a partir de hoje.", erro=True)
    linha = db.ex("""INSERT INTO evento(nome, token, ebook_id, data_evento, valido_ate)
                     VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                  (nome, secrets.token_urlsafe(8), ebook_id, dt_evento, dt_validade))
    msg = f"Evento criado. O QR code está pronto e vale até {dt_validade:%d/%m/%Y}."
    if ebook["visibilidade"] == "oculto":
        return _com_msg(volta + f"#ev{linha['id']}", msg + " Atenção: o ebook está OCULTO e o QR não vai abrir "
                        "até você mudar a visibilidade dele.", erro=True)
    return _com_msg(volta + f"#ev{linha['id']}", msg)


@app.post("/admin/eventos/{evid}/alternar")
def admin_evento_alternar(request: Request, evid: int, voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    ev = db.one("UPDATE evento SET ativo = NOT ativo WHERE id = %s RETURNING ativo", (evid,))
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
    return _com_msg(volta, f"Validade alterada para {nova:%d/%m/%Y}." if nova else "O QR agora não expira.")


@app.post("/admin/eventos/{evid}/excluir")
def admin_evento_excluir(request: Request, evid: int, voltar: str = Form("")):
    if not _admin_ok(request):
        return _para_login(request)
    # Quem já entrou pelo evento mantém o ebook (pessoa_ebook.evento_id vira NULL).
    db.ex("DELETE FROM evento WHERE id = %s", (evid,))
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

def _leads(busca: str, tipo: str) -> list[dict]:
    where, params = [], []
    if busca:
        where.append("(p.nome ILIKE %s OR p.email ILIKE %s OR p.empresa ILIKE %s)")
        params += [f"%{busca}%"] * 3
    if tipo == "completo":
        where.append("p.acesso_completo")
    elif tipo == "evento":
        where.append("NOT p.acesso_completo")
    sql = f"""
        SELECT p.*,
          (SELECT string_agg(DISTINCT ev.nome, ', ') FROM pessoa_ebook pe
             JOIN evento ev ON ev.id = pe.evento_id WHERE pe.pessoa_id = p.id) AS eventos,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.pessoa_id = p.id AND a.acao = 'leitura')  AS leituras,
          (SELECT COUNT(*) FROM acesso_log a WHERE a.pessoa_id = p.id AND a.acao = 'download') AS downloads
        FROM pessoa p {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY p.criado_em DESC"""
    return db.q(sql, params)


@app.get("/admin/leads", response_class=HTMLResponse)
def admin_leads(request: Request, busca: str = "", tipo: str = ""):
    if not _admin_ok(request):
        return _para_login(request)
    resumo = db.one("""SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE acesso_completo) AS completos,
                              COUNT(*) FILTER (WHERE NOT acesso_completo) AS so_evento,
                              COUNT(*) FILTER (WHERE criado_em > now() - interval '7 days') AS semana
                       FROM pessoa""")
    return _admin_render(request, "leads.html", leads=_leads(busca.strip(), tipo), resumo=resumo,
                         busca=busca, tipo=tipo, aba="leads")


@app.get("/admin/leads.csv")
def admin_leads_csv(request: Request, busca: str = "", tipo: str = ""):
    if not _admin_ok(request):
        return _para_login(request)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Nome", "E-mail", "Telefone", "Empresa", "Cargo", "CNPJ", "Como conheceu",
                "Aceita novidades", "Acesso", "Origem", "Eventos", "Leituras", "Downloads",
                "Cadastro", "Último acesso"])
    for p in _leads(busca.strip(), tipo):
        w.writerow([p["nome"], p["email"], p["telefone"] or "", p["empresa"] or "", p["cargo"] or "",
                    p["cnpj"] or "", p["como_conheceu"] or "", "Sim" if p["aceita_novidades"] else "Não",
                    "Biblioteca completa" if p["acesso_completo"] else "Só evento", p["origem"] or "",
                    p["eventos"] or "", p["leituras"], p["downloads"], _dt_br(p["criado_em"]),
                    _dt_br(p["ultimo_acesso"])])
    # utf-8-sig: o Excel em PT-BR só reconhece o acento com o BOM.
    nome = f"leads-biblioteca-{date.today():%Y-%m-%d}.csv"
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=\"{nome}\""})


@app.post("/admin/leads/{pid}/excluir")
def admin_lead_excluir(request: Request, pid: int):
    """Atende pedido de exclusão de dados (LGPD)."""
    if not _admin_ok(request):
        return _para_login(request)
    db.ex("DELETE FROM pessoa WHERE id = %s", (pid,))
    return RedirectResponse(f"/admin/leads?msg={quote('Contato excluído.')}", 303)


@app.get("/saude")
def saude():
    db.one("SELECT 1")
    return {"ok": True}
