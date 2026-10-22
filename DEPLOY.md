# Deploy — Biblioteca de Ebooks (VPS)

App Python **FastAPI** atrás do **NGINX reverso da VPS** (SSL), no mesmo esquema da Libby.

- **Subdomínio:** `biblioteca.canellaesantos.com.br` (troque no `nginx-ebooks.conf` e no `BASE_URL` se for outro)
- **Container:** `canella_ebooks` (network `contask_network`), porta interna `8020`
- **Banco:** PostgreSQL `ebooks` no Postgres **compartilhado** da VPS
- **Arquivos (PDFs e capas):** volume `ebooks_dados` → `/var/lib/ebooks`

## Pré-requisitos
1. **DNS:** registro `A` de `biblioteca.canellaesantos.com.br` → IP da VPS.
2. **Banco próprio** no Postgres compartilhado (com o MESMO usuário dos outros apps):
   ```bash
   docker exec -it task_manager-postgres-1 psql -U <usuario_dos_apps> -d gestao_vendas -c "CREATE DATABASE ebooks;"
   ```
   As tabelas são criadas sozinhas no 1º start.

## Passos
1. **Clonar** na VPS:
   ```bash
   git clone https://github.com/<usuario>/canella_ebook_libby.git
   cd canella_ebook_libby
   ```
2. **.env de produção:**
   ```bash
   cp .env.production.example .env.production
   python3 -c "import secrets; print(secrets.token_hex(32))"   # cole em SECRET_KEY
   nano .env.production
   ```
   - `DATABASE_URL=postgresql://<usuario>:<senha>@postgres:5432/ebooks` (senha com `@` → `%40`)
   - `BASE_URL=https://biblioteca.canellaesantos.com.br` (é o link que vai dentro do QR code)
   - `SECRET_KEY=` (o valor gerado acima — **não troque depois**, isso desloga todo mundo)
   - `ADMIN_SENHA=` (senha do painel da equipe)
3. **Subir:**
   ```bash
   docker-compose -f docker-compose.prod.yml up -d --build
   docker logs canella_ebooks --tail 40
   ```
4. **Emitir o SSL** (antes de habilitar o conf):
   ```bash
   docker exec task_manager-certbot-1 certbot certonly \
     --webroot --webroot-path=/var/www/certbot -d biblioteca.canellaesantos.com.br
   ```
5. **NGINX do reverso:**
   ```bash
   cp nginx-ebooks.conf /root/task_manager/docker/nginx/conf.d/ebooks.conf
   docker exec task_manager_nginx nginx -t && docker exec task_manager_nginx nginx -s reload
   ```
6. **1º acesso:** entre em `https://biblioteca.canellaesantos.com.br/admin` com a `ADMIN_SENHA` e suba os ebooks.

## Atualizar
```bash
git pull && docker-compose -f docker-compose.prod.yml up -d --build
```
Os ebooks ficam no volume e os leads no Postgres: nada se perde no rebuild.

## Backup
- **Leads/eventos:** junto do `pg_dump` dos demais bancos (banco `ebooks`).
- **PDFs e capas:**
  ```bash
  docker run --rm -v canella_ebook_libby_ebooks_dados:/d -v $(pwd):/b alpine tar czf /b/ebooks_dados.tgz -C /d .
  ```
  (confira o nome exato do volume com `docker volume ls | grep ebooks`).

## Operação
- **Trocar a senha do painel:** edite `ADMIN_SENHA` no `.env.production` e recrie o container:
  `docker-compose -f docker-compose.prod.yml up -d --force-recreate ebooks`
- **Pedido de exclusão de dados (LGPD):** painel → Leads → Excluir.
