#!/bin/sh
# ============================================================================
# Backup diário da Biblioteca de Ebooks (container canella_ebooks_backup).
#
# Roda numa imagem postgres:<versão>-alpine (que já traz o pg_dump) e guarda
# em /backups (volume ebooks_backups, que o painel lista em /admin/backups):
#   db-AAAAMMDD-HHMM.dump        banco inteiro (pg_dump -Fc: restaura com pg_restore)
#   arquivos-AAAAMMDD-HHMM.tgz   PDFs e capas (volume ebooks_dados, montado só leitura)
#
# Variáveis (vêm do .env.production):
#   DATABASE_URL        o mesmo do app
#   BACKUP_HORA=3       hora do dia (0-23, horário de Brasília)
#   BACKUP_DIAS=14      quantos dias de cópias manter
#   BACKUP_UMA_VEZ=1    faz uma cópia e sai (teste / backup manual)
# ============================================================================
set -u
HORA="${BACKUP_HORA:-3}"
DIAS="${BACKUP_DIAS:-14}"
DESTINO=/backups
ORIGEM=/dados

log() { echo "[backup $(date '+%d/%m/%Y %H:%M:%S')] $*"; }

fazer_backup() {
  carimbo=$(date +%Y%m%d-%H%M)
  # Grava com nome provisório e só renomeia no fim: o painel nunca lista
  # (nem oferece para baixar) um arquivo pela metade.
  if pg_dump "$DATABASE_URL" -Fc -f "$DESTINO/.db-$carimbo.tmp"; then
    mv "$DESTINO/.db-$carimbo.tmp" "$DESTINO/db-$carimbo.dump"
    log "banco ok: db-$carimbo.dump ($(du -h "$DESTINO/db-$carimbo.dump" | cut -f1))"
  else
    rm -f "$DESTINO/.db-$carimbo.tmp"
    log "ERRO no pg_dump do banco (confira DATABASE_URL e a versão do Postgres)"
  fi
  if tar czf "$DESTINO/.arquivos-$carimbo.tmp" -C "$ORIGEM" .; then
    mv "$DESTINO/.arquivos-$carimbo.tmp" "$DESTINO/arquivos-$carimbo.tgz"
    log "arquivos ok: arquivos-$carimbo.tgz ($(du -h "$DESTINO/arquivos-$carimbo.tgz" | cut -f1))"
  else
    rm -f "$DESTINO/.arquivos-$carimbo.tmp"
    log "ERRO ao compactar os PDFs"
  fi
  # Faxina: apaga cópias com mais de $DIAS dias.
  find "$DESTINO" -maxdepth 1 \( -name 'db-*.dump' -o -name 'arquivos-*.tgz' \) -mtime +"$DIAS" -print -delete \
    | sed 's/^/[backup] removido (antigo): /'
}

mkdir -p "$DESTINO"

if [ "${BACKUP_UMA_VEZ:-0}" = "1" ]; then
  fazer_backup
  exit 0
fi

# Ao subir: se não houver cópia das últimas 24h (1º deploy, servidor ficou
# desligado), faz uma já, sem esperar a madrugada.
if [ -z "$(find "$DESTINO" -maxdepth 1 -name 'db-*.dump' -mtime -1 2>/dev/null)" ]; then
  log "nenhuma cópia nas últimas 24h: fazendo uma agora"
  fazer_backup
fi

while true; do
  agora=$(date +%s)
  alvo=$(date -d "$(date +%Y-%m-%d) $(printf '%02d' "$HORA"):00:00" +%s 2>/dev/null)
  [ "$alvo" -le "$agora" ] && alvo=$((alvo + 86400))
  log "próxima cópia em $(( (alvo - agora) / 3600 ))h$(( (alvo - agora) % 3600 / 60 ))min"
  sleep $((alvo - agora))
  fazer_backup
done
