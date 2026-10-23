// Demonstração estática (GitHub Pages): troca os links do sistema pelos arquivos
// gerados e simula os formulários. No sistema real nada disso existe.
(function () {
  const DEMO = window.DEMO || {};
  const liberado = () => { try { return sessionStorage.getItem('demo_liberado') === '1'; } catch (_) { return false; } };
  const liberar = () => { try { sessionStorage.setItem('demo_liberado', '1'); } catch (_) {} };

  function mapear(href) {
    if (!href || !href.startsWith('/')) return null;
    const u = new URL(href, 'https://demo.local');
    const p = u.pathname, perfil = u.searchParams.get('perfil');
    let m;
    if (p === '/') return liberado() ? 'estante-liberada.html' : 'index.html';
    if (p.startsWith('/acesso')) {
      if (liberado()) return 'estante-liberada.html';
      // Guarda o livro que a pessoa clicou (?next=) e a aba, como no sistema real.
      const qs = new URLSearchParams();
      if (u.searchParams.get('next')) qs.set('next', u.searchParams.get('next'));
      if (u.searchParams.get('aba')) qs.set('aba', 'entrar');
      return (perfil === 'estudante' ? 'acesso-estudante.html' : 'acesso.html') + (qs.toString() ? '?' + qs : '');
    }
    if ((m = p.match(/^\/ler\/([a-z0-9-]+)\/download$/))) return 'pdfs/' + m[1] + '.pdf';
    if ((m = p.match(/^\/ler\/([a-z0-9-]+)$/))) return (liberado() || (DEMO.presentes || []).includes(m[1])) ? 'ler-' + m[1] + '.html' : 'acesso.html?next=' + encodeURIComponent(p);
    if ((m = p.match(/^\/ebook\/([a-z0-9-]+)$/))) return 'ebook-' + m[1] + '.html';
    if (p.startsWith('/e/')) return 'evento.html';
    if (p === '/sair') { try { sessionStorage.removeItem('demo_liberado'); } catch (_) {} return 'index.html'; }
    if (p.startsWith('/admin')) {
      if (/^\/admin\/eventos\/\d+\/imprimir/.test(p)) return 'admin-cartaz.html';
      if (/^\/admin\/ebooks\/\d+/.test(p)) return 'admin-ebook.html';
      const telas = {'/admin': 'admin-ebooks.html', '/admin/ebooks': 'admin-ebooks.html', '/admin/eventos': 'admin-eventos.html',
        '/admin/leads': 'admin-leads.html', '/admin/usuarios': 'admin-usuarios.html', '/admin/atividade': 'admin-atividade.html',
        '/admin/backups': 'admin-backups.html', '/admin/conta': 'admin-conta.html', '/admin/sair': 'admin-login.html',
        '/admin/login': 'admin-login.html'};
      return telas[p] || 'admin-ebooks.html';
    }
    return 'index.html';
  }

  window.demoMapear = mapear;  // usado pelo 404.html

  const avisar = msg => {
    let t = document.getElementById('demo-aviso');
    if (!t) { t = document.createElement('div'); t.id = 'demo-aviso'; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add('ver');
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('ver'), 3200);
  };

  // Links: os do HTML e os que a estante cria por JavaScript (ficha do livro).
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]');
    if (!a) return;
    const destino = mapear(a.getAttribute('href'));
    if (!destino) return;
    e.preventDefault();
    if (a.target === '_blank') window.open(destino, '_blank'); else location.href = destino;
  }, true);

  // Formulários: simulam o resultado.
  document.addEventListener('submit', e => {
    const f = e.target, acao = f.getAttribute('action') || '';
    e.preventDefault(); e.stopImmediatePropagation();
    const proximo = (f.querySelector('[name=next]') || {}).value || '/';
    if (acao === '/acesso' || acao === '/entrar') {
      liberar();
      const d = mapear(proximo);
      location.href = (d && d.startsWith('ler-') ? d : 'estante-liberada.html') + '?bemvindo=1';
    } else if (location.pathname.endsWith('evento.html')) {
      location.href = 'ler-' + DEMO.evento_slug + '.html?presente=1';
    } else if (acao.startsWith('/admin/login') || acao === '/admin/primeiro-acesso') {
      location.href = 'admin-ebooks.html';
    } else {
      avisar('Demonstração: no sistema real esta ação é salva.');
    }
  }, true);

  // ?next= e ?aba=entrar nas telas de acesso
  const q = new URLSearchParams(location.search);
  if (q.get('next')) document.querySelectorAll('input[name=next]').forEach(i => i.value = q.get('next'));
  if (q.get('aba') === 'entrar') { const t = document.getElementById('t-ent'); if (t) t.click(); }
  // /acesso?perfil=estudante: marca o perfil de estudante (no sistema real o servidor faz isso).
  if (q.get('perfil') === 'estudante') {
    const r = document.querySelector('input[name=perfil][value=estudante]');
    if (r && !r.checked) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
  }

  // Painel: login já preenchido (na demonstração qualquer senha entra).
  if (location.pathname.endsWith('admin-login.html')) {
    const u = document.querySelector('input[name=usuario]'), s = document.querySelector('input[name=senha]');
    if (u) u.value = 'equipe';
    if (s) s.value = 'demonstracao';
    const nota = document.querySelector('.nota');
    if (nota) nota.innerHTML = '<b>Demonstração:</b> é só clicar em Entrar.';
  }
})();
