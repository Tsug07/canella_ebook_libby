# canella_ebook_libby
Biblioteca de Ebooks Canella e Santos: estante pública de ebooks com funil de leads e acesso por QR code em eventos.

## Como funciona
| Quem | Como chega | O que vê |
|---|---|---|
| Visitante | abre o site | a estante com as capas (cadeado) e a ficha de cada livro |
| Participante de evento | escaneia o QR (`/e/<token>`) e informa nome + e-mail | **só** o ebook do evento (ler + baixar) e um convite para a biblioteca completa |
| Lead | preenche o cadastro (`/acesso`) | a biblioteca inteira, com liberação automática |

- **Visibilidade de cada ebook:** *Na estante* (aparece na prateleira), *Só eventos* (fora da estante; só quem escaneia o QR recebe — nem o lead completo vê) ou *Oculto* (ninguém acessa, nem pelo QR).
- **Painel `/admin`** (senha `ADMIN_SENHA`): subir PDFs (a capa sai da 1ª página), criar o evento/QR direto na tela do ebook, imprimir o cartaz A4 do QR, ver/exportar leads (CSV).
- **Leitura** no navegador com PDF.js. O **download** recebe uma marca d'água com o nome e o e-mail de quem baixou.
- **Reentrada:** "Já tenho acesso" + e-mail (outro aparelho, cookie apagado).

## Stack
FastAPI + Jinja2 + JS/CSS puros · PostgreSQL (psycopg) · PyMuPDF · Docker. Mesmo padrão da Libby.

```
server/app/main.py        rotas (estante, funil, leitor, eventos, admin)
server/app/database.py    schema + helpers q/one/ex
server/app/sessao.py      cookies assinados (leitor e admin)
server/app/arquivos.py    PDFs, capas, marca d'água, QR
server/app/templates/     telas (admin/ = painel)
server/app/static/        style.css + logo/favicons
```

## Rodar local
```bat
docker compose -f docker-compose.dev.yml up -d
copy .env.example server\.env
cd server
pip install -r requirements.txt
run_local.bat
```
Abra http://localhost:8020 (painel: http://localhost:8020/admin, senha `admin`).

## Deploy
Ver [DEPLOY.md](DEPLOY.md).
