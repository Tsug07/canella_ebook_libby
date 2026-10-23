# canella_ebook_libby
Biblioteca de Ebooks Canella e Santos: estante pública de ebooks com funil de leads e acesso por QR code em eventos.

## Como funciona
| Quem | Como chega | O que vê |
|---|---|---|
| Visitante | abre o site | a estante com as capas (cadeado) e a ficha de cada livro |
| Participante de evento | escaneia o QR (`/e/<token>`) e informa nome + e-mail | **só** o ebook do evento (ler + baixar) e um convite para a biblioteca completa |
| Lead | preenche o cadastro (`/acesso`) | a biblioteca inteira, com liberação automática |

- **Visibilidade de cada ebook:** *Na estante* (aparece na prateleira), *Só eventos* (fora da estante; só quem escaneia o QR recebe — nem o lead completo vê) ou *Oculto* (ninguém acessa, nem pelo QR).
- **Equipe no painel:** login por pessoa (Administrador / Equipe), verificação em duas etapas, limite de tentativas,
  registro de atividade e backup diário automático (banco + PDFs) com download pelo painel. `ADMIN_SENHA` só cria o 1º admin.
- **Painel `/admin`**: subir PDFs (a capa sai da 1ª página), criar o evento/QR direto na tela do ebook, imprimir o cartaz A4 do QR, ver/exportar leads (CSV).
- **Arquivar** material desatualizado: sai da estante e dos QR sem perder leads/histórico; links antigos avisam "material atualizado" e levam à edição nova (se indicada). Restaurável.
- **Página própria por ebook** (`/ebook/<slug>`) com Open Graph: no WhatsApp/LinkedIn a prévia mostra capa e título.
- **Links de campanha** (`?origem=instagram&campanha=x`, ou `utm_source`/`utm_campaign`): o canal do 1º contato vai para o lead; a tela do ebook gera um link por canal e Leads mostra "De onde vêm os leads".
- **Destaque e "Novo"**: até 3 ebooks em "Em destaque" no topo; fita "Novo" nos ebooks dos últimos `NOVO_DIAS` dias.
- **Avisos**: lead novo no Discord (`DISCORD_WEBHOOK_LEADS`) e e-mail de boas-vindas pelo Gmail (`GMAIL_USER`/`GMAIL_APP_PASSWORD`), com link que entra direto e confirma o e-mail.
- **Funil por evento**: abriram o QR → pegaram o ebook → viraram lead completo.
- **Leitura** no navegador com PDF.js. O **download** recebe uma marca d'água com o nome e o e-mail de quem baixou.
- **Reentrada:** "Já tenho acesso" + e-mail (outro aparelho, cookie apagado).

## Stack
FastAPI + Jinja2 + JS/CSS puros · PostgreSQL (psycopg) · PyMuPDF · Docker. Mesmo padrão da Libby.

```
server/app/main.py        rotas (estante, funil, leitor, eventos, admin)
server/app/database.py    schema + helpers q/one/ex
server/app/sessao.py      cookies assinados (leitor e admin)
server/app/arquivos.py    PDFs, capas, marca d'água, QR
server/app/avisos.py      Discord (lead novo) e e-mail (boas-vindas)
server/app/equipe.py      usuários do painel, senha, 2 etapas, atividade, limite de tentativas
server/resetar_admin.py   emergência: redefinir acesso pelo servidor
backup/backup.sh          backup diário (container ebooks_backup)
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
Abra http://localhost:8020. No painel (http://localhost:8020/admin), o 1º acesso pede a chave
`admin` (a `ADMIN_SENHA` do `.env`) e cria o seu usuário.

## Deploy
Ver [DEPLOY.md](DEPLOY.md).
