// Botão "Compartilhar": no celular abre a folha nativa (WhatsApp, Instagram…);
// no computador copia o link. O link leva ?origem=indicacao para o painel
// saber que o lead veio de alguém que compartilhou.
document.addEventListener('click', async e => {
  const b = e.target.closest('.compartilhar');
  if (!b) return;
  const url = b.dataset.url + (b.dataset.url.includes('?') ? '&' : '?') + 'origem=indicacao';
  const titulo = b.dataset.titulo;
  if (navigator.share) {
    try { await navigator.share({title: titulo, text: titulo + ' — ebook gratuito da Canella & Santos', url}); } catch (_) {}
    return;
  }
  const avisar = msg => {
    const t = document.getElementById('toast');
    if (!t) return alert(msg);
    t.textContent = msg; t.classList.add('ver');
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('ver'), 2500);
  };
  try { await navigator.clipboard.writeText(url); avisar('Link copiado! É só colar onde quiser.'); }
  catch (_) { prompt('Copie o link:', url); }
});
