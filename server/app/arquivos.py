"""arquivos.py — PDFs, capas, marca d'água e QR code.

Os arquivos ficam FORA da imagem, em DATA_DIR (volume do Docker): a imagem é
recriada a cada deploy e levaria os ebooks junto.

  DATA_DIR/pdfs/   PDFs originais (nunca servidos por rota estática: só pela
                   rota que confere o acesso)
  DATA_DIR/capas/  capas (públicas — são a vitrine da estante)
"""
from __future__ import annotations
import io
import os
import re
import secrets
import unicodedata
from pathlib import Path

import pymupdf
import qrcode
from PIL import Image

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parents[1] / "dados"))
PDFS = DATA_DIR / "pdfs"
CAPAS = DATA_DIR / "capas"
for _p in (PDFS, CAPAS):
    _p.mkdir(parents=True, exist_ok=True)

LARGURA_CAPA = 600
MAX_PDF = 100 * 1024 * 1024
MAX_CAPA = 10 * 1024 * 1024


class ArquivoInvalido(ValueError):
    pass


def slugify(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:80] or "ebook"


def salvar_pdf(conteudo: bytes, slug: str) -> tuple[str, int]:
    """Grava o PDF e devolve (nome_do_arquivo, nº de páginas)."""
    if len(conteudo) > MAX_PDF:
        raise ArquivoInvalido("O PDF passa de 100 MB.")
    if not conteudo.startswith(b"%PDF"):
        raise ArquivoInvalido("O arquivo enviado não é um PDF.")
    try:
        with pymupdf.open(stream=conteudo, filetype="pdf") as doc:
            paginas = doc.page_count
    except Exception:
        raise ArquivoInvalido("Não foi possível abrir o PDF (arquivo corrompido?).")
    if paginas == 0:
        raise ArquivoInvalido("O PDF não tem páginas.")
    nome = f"{slug}-{secrets.token_hex(4)}.pdf"
    (PDFS / nome).write_bytes(conteudo)
    return nome, paginas


def capa_do_pdf(nome_pdf: str, slug: str) -> str:
    """Renderiza a 1ª página do PDF como capa (JPEG)."""
    with pymupdf.open(PDFS / nome_pdf) as doc:
        pag = doc[0]
        zoom = LARGURA_CAPA / pag.rect.width
        pix = pag.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return _gravar_capa(img, slug)


def salvar_capa(conteudo: bytes, slug: str) -> str:
    """Capa enviada à mão (PNG/JPG/WebP), normalizada para JPEG."""
    if len(conteudo) > MAX_CAPA:
        raise ArquivoInvalido("A imagem da capa passa de 10 MB.")
    try:
        img = Image.open(io.BytesIO(conteudo))
        img.load()
    except Exception:
        raise ArquivoInvalido("A capa precisa ser uma imagem (PNG, JPG ou WebP).")
    if img.mode in ("RGBA", "LA", "P"):
        fundo = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        fundo.paste(img, mask=img.split()[-1])
        img = fundo
    return _gravar_capa(img.convert("RGB"), slug)


def _gravar_capa(img: Image.Image, slug: str) -> str:
    if img.width > LARGURA_CAPA:
        img = img.resize((LARGURA_CAPA, round(img.height * LARGURA_CAPA / img.width)), Image.LANCZOS)
    # Nome novo a cada troca: a capa é servida com cache longo.
    nome = f"{slug}-{secrets.token_hex(4)}.jpg"
    img.save(CAPAS / nome, "JPEG", quality=86, optimize=True, progressive=True)
    return nome


def apagar(pasta: Path, nome: str | None) -> None:
    if nome:
        try:
            (pasta / Path(nome).name).unlink()
        except FileNotFoundError:
            pass


def pdf_com_marca(nome_pdf: str, nome: str, email: str) -> bytes:
    """Cópia do PDF com 'Exclusivo para <nome> · <email>' no rodapé de cada página.

    Não impede repassar o arquivo, mas identifica de quem ele saiu — o que
    desestimula bastante. O original em disco não é alterado.
    """
    texto = f"Exclusivo para {nome} · {email} · Canella & Santos Contabilidade"
    with pymupdf.open(PDFS / nome_pdf) as doc:
        for pag in doc:
            r = pag.rect
            caixa = pymupdf.Rect(r.x0 + 20, r.y1 - 18, r.x1 - 20, r.y1 - 6)
            # insert_textbox trabalha em coordenadas da página SEM rotação;
            # derotation_matrix leva a caixa visível para esse espaço.
            caixa = caixa * pag.derotation_matrix
            pag.insert_textbox(caixa, texto, fontsize=6.5, fontname="helv",
                               color=(0.45, 0.45, 0.45), align=pymupdf.TEXT_ALIGN_CENTER,
                               rotate=pag.rotation)
        return doc.tobytes(garbage=3, deflate=True)


def qr_png(url: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=16, border=3)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#6C1F2E", back_color="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
