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
6. **Avisos (opcional)** — no `.env.production`:
   - `DISCORD_WEBHOOK_LEADS`: Discord → canal → Editar canal → Integrações → Webhooks → Novo webhook → Copiar URL.
   - `GMAIL_USER` + `GMAIL_APP_PASSWORD`: a mesma conta/senha de app que a Libby usa serve. Sem elas, o e-mail de boas-vindas não sai (o resto funciona).
   - Depois de mudar: `docker-compose -f docker-compose.prod.yml up -d --force-recreate ebooks`.
7. **Primeiro acesso ao painel:** abra `https://biblioteca.canellaesantos.com.br/admin`. Como ainda não há
   usuários, aparece a tela **Primeiro acesso**: informe a `ADMIN_SENHA` (chave de instalação), seu nome,
   e-mail e uma senha. Depois disso a `ADMIN_SENHA` não abre mais o painel: cada pessoa entra com o
   próprio e-mail e senha.
8. **Equipe:** em **Usuários**, cadastre cada pessoa (Administrador ou Equipe). O painel mostra uma senha
   temporária **uma única vez**: envie por mensagem privada; no 1º acesso a pessoa cria a dela.
   Recomendado: cada um ativar a **verificação em duas etapas** em *Minha conta*.

## Segurança do painel
- Login por pessoa (e-mail + senha com hash PBKDF2) e verificação em duas etapas opcional (app autenticador).
- **5 senhas erradas** para o mesmo e-mail (ou 10 do mesmo IP) em 15 min → bloqueio de 15 min e aviso no Discord.
- Formulários públicos também têm limite por IP (cadastro, "Já tenho acesso", QR de evento).
- Tudo o que a equipe faz fica em **Atividade** (só administradores veem).
- **Alguém ficou trancado para fora** (esqueceu a senha, perdeu o celular, o único admin saiu):
  ```bash
  docker exec canella_ebooks python resetar_admin.py --listar
  docker exec canella_ebooks python resetar_admin.py fulano@canellaesantos.com.br            # nova senha temporária
  docker exec canella_ebooks python resetar_admin.py fulano@canellaesantos.com.br --sem-2fa  # + tira as 2 etapas
  docker exec canella_ebooks python resetar_admin.py --criar "Nome" novo@canellaesantos.com.br  # admin novo
  ```

## Atualizar
```bash
git pull && docker-compose -f docker-compose.prod.yml up -d --build
```
Os ebooks ficam no volume e os leads no Postgres: nada se perde no rebuild.

## Backup (automático)
O serviço `ebooks_backup` (container `canella_ebooks_backup`) faz, todo dia às `BACKUP_HORA` (padrão 3h):
- `db-AAAAMMDD-HHMM.dump`: banco inteiro (leads, eventos, usuários, atividade);
- `arquivos-AAAAMMDD-HHMM.tgz`: PDFs e capas.

Guarda `BACKUP_DIAS` dias (padrão 14) no volume `ebooks_backups`. O painel lista e deixa baixar em
**Backups** (só administradores) e avisa se o último tiver mais de 36h.

- **Antes do 1º deploy**, confira a versão do Postgres da VPS: `docker exec task_manager-postgres-1 postgres --version`.
  Se for maior que 16, troque `postgres:16-alpine` no `docker-compose.prod.yml` pela versão de lá.
- **Ver se está rodando:** `docker logs canella_ebooks_backup --tail 20`
- **Backup na hora (manual):** `docker exec -e BACKUP_UMA_VEZ=1 canella_ebooks_backup sh /backup.sh`
- **Fora da VPS:** os backups ficam na própria VPS. Uma vez por semana, baixe os mais recentes pelo painel e guarde no Drive.

### Restaurar
```bash
# 1) pare o app (o backup pode continuar)
docker stop canella_ebooks
# 2) banco: recria o banco vazio e restaura o .dump
docker exec -it task_manager-postgres-1 psql -U <usuario> -d gestao_vendas -c "DROP DATABASE ebooks;" -c "CREATE DATABASE ebooks;"
docker exec canella_ebooks_backup sh -c 'pg_restore --no-owner -d "$DATABASE_URL" /backups/db-AAAAMMDD-HHMM.dump'
# 3) PDFs e capas (o volume de dados está montado só-leitura no backup; use um container avulso):
docker run --rm -v canella_ebook_libby_ebooks_dados:/dados -v canella_ebook_libby_ebooks_backups:/backups alpine   sh -c 'rm -rf /dados/* && tar xzf /backups/arquivos-AAAAMMDD-HHMM.tgz -C /dados'
# 4) suba o app de novo
docker start canella_ebooks
```
(confira os nomes dos volumes com `docker volume ls | grep ebooks`.)

## Operação
- **Pedido de exclusão de dados (LGPD):** painel → Leads → Excluir.
