// Funcoes compartilhadas entre o portal do usuario e o painel da TI
const TZ = 'America/Sao_Paulo';
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const dataHora = iso => iso ? new Date(iso).toLocaleString('pt-BR', { timeZone: TZ, day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
function nomeHoras(h) {
  if (!h) return 'Tempo indeterminado';
  const d = Math.floor(h / 24), r = h % 24;
  const th = `${r} hora${r > 1 ? 's' : ''}`, td = `${d} dia${d > 1 ? 's' : ''}`;
  return d === 0 ? th : (r === 0 ? td : `${td} e ${th}`);
}
const NOMES_STATUS = { pendente: 'Aguardando a TI', aprovada: 'Liberado', negada: 'Negado', expirada: 'Encerrado (prazo)', revogada: 'Bloqueado pela TI', cancelada: 'Cancelado' };
const LOGO_WIFI = `<svg class="logo-padrao" viewBox="0 0 64 64" aria-hidden="true"><rect x="2" y="2" width="60" height="60" rx="16" fill="rgba(255,255,255,.12)"/><path d="M14 27a26 26 0 0 1 36 0" stroke="#fff" stroke-width="4.5" fill="none" stroke-linecap="round"/><path d="M21 34a16 16 0 0 1 22 0" stroke="#fff" stroke-width="4.5" fill="none" stroke-linecap="round"/><path d="M27.5 41a7 7 0 0 1 9 0" stroke="#fff" stroke-width="4.5" fill="none" stroke-linecap="round"/><circle cx="32" cy="48" r="4" fill="#ff8f1f"/></svg>`;
// Emblema da Gestao TI (monitor com engrenagem). O portal do Wi-Fi troca pelo LOGO_WIFI.
const LOGO_GESTAO = `<svg class="logo-padrao" viewBox="0 0 64 64" aria-hidden="true"><rect x="2" y="2" width="60" height="60" rx="16" fill="rgba(255,255,255,.12)"/><rect x="11" y="13" width="42" height="29" rx="4.5" stroke="#fff" stroke-width="3.6" fill="none"/><path d="M32 42v7M23 50h18" stroke="#fff" stroke-width="3.6" stroke-linecap="round" fill="none"/><path d="M39.06 26.10 L41.51 26.22 L41.51 28.78 L39.06 28.90 L37.99 31.50 L39.63 33.32 L37.82 35.13 L36.00 33.49 L33.40 34.56 L33.28 37.01 L30.72 37.01 L30.60 34.56 L28.00 33.49 L26.18 35.13 L24.37 33.32 L26.01 31.50 L24.94 28.90 L22.49 28.78 L22.49 26.22 L24.94 26.10 L26.01 23.50 L24.37 21.68 L26.18 19.87 L28.00 21.51 L30.60 20.44 L30.72 17.99 L33.28 17.99 L33.40 20.44 L36.00 21.51 L37.82 19.87 L39.63 21.68 L37.99 23.50 Z" fill="#ff8f1f" stroke="#ff8f1f" stroke-width="1" stroke-linejoin="round"/><circle cx="32" cy="27.5" r="3" fill="#2722a3"/></svg>`;
let LOGO_PADRAO = LOGO_GESTAO;
let CONFIG = { horas_permitidas: [1, 2, 4, 8, 24], horas_admin: [1, 2, 4, 8, 12, 24, 48, 168], permitir_indeterminado: true, horas_maximas: 720,
  personalizado_usuario: true, horas_maximas_usuario: 168 };

function restante(iso) {
  const ms = new Date(iso) - new Date();
  if (ms <= 0) return 'encerrando';
  const m = Math.round(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24);
  if (d >= 1) return `${d}d ${h % 24}h restantes`;
  if (h >= 1) return `${h}h ${m % 60}min restantes`;
  return `${m} min restantes`;
}
function prazoTexto(s) {
  if (s.status !== 'aprovada') return '';
  return s.indeterminado ? 'Liberado por tempo indeterminado, até a TI bloquear' : `Liberado até ${dataHora(s.valido_ate)} · ${restante(s.valido_ate)}`;
}

async function api(caminho, opcoes = {}, aoExpirar) {
  const r = await fetch(caminho, { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...opcoes });
  if (r.status === 401 && !caminho.includes('/login') && aoExpirar) { aoExpirar(); throw new Error('Sessão expirada. Entre novamente.'); }
  const corpo = await r.json().catch(() => ({}));
  if (!r.ok) {
    let msg = corpo.detail || `Erro ${r.status}`;
    if (Array.isArray(msg)) msg = msg.map(e => e.msg).join('; ');
    const falha = new Error(msg); falha.status = r.status;   // a tela pode reagir ao codigo (ex.: 428 = pedir a senha)
    throw falha;
  }
  return corpo;
}
const toast = (title, icon = 'success') => Swal.fire({ toast: true, position: 'top-end', timer: 3000, showConfirmButton: false, icon, title });
const erro = e => Swal.fire({ icon: 'error', title: 'Não foi possível concluir', text: e.message, confirmButtonText: 'Entendi' });

async function carregarMarca() {
  try { CONFIG = await api('/api/config-publica'); } catch { /* usa o padrao */ }
  const logo = CONFIG.logo ? `<img src="${esc(CONFIG.logo)}" alt="">` : LOGO_PADRAO;
  document.querySelectorAll('[data-logo]').forEach(el => el.innerHTML = logo);
  document.querySelectorAll('[data-texto]').forEach(el => { const v = CONFIG[el.dataset.texto]; if (v) el.textContent = v; });
  return CONFIG;
}

function ligarLogin(form, aoEnviar) {
  const olho = form.querySelector('[data-olho]');
  if (olho) olho.addEventListener('click', () => {
    const campo = form.querySelector('input[name=senha]');
    campo.type = campo.type === 'password' ? 'text' : 'password';
  });
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const botao = form.querySelector('button[type=submit]');
    const f = new FormData(form);
    botao.disabled = true;
    try { await aoEnviar(f.get('usuario'), f.get('senha')); form.reset(); }
    catch (e) { erro(e); }
    finally { botao.disabled = false; }
  });
}

// Lista de prazos em grupos: Horas, Dias e Outros (indeterminado e personalizado)
function opcoesPrazo(lista, selecionada, comPersonalizado, comIndeterminado = true) {
  const sel = Number(selecionada);
  const valores = [...new Set([...lista.map(Number), ...(sel > 0 ? [sel] : [])])].filter(h => h > 0).sort((a, b) => a - b);
  const opt = h => `<option value="${h}" ${h === sel ? 'selected' : ''}>${nomeHoras(h)}</option>`;
  const horas = valores.filter(h => h < 24), dias = valores.filter(h => h >= 24);
  let html = '';
  if (horas.length) html += `<optgroup label="Horas">${horas.map(opt).join('')}</optgroup>`;
  if (dias.length) html += `<optgroup label="Dias">${dias.map(opt).join('')}</optgroup>`;
  let outros = '';
  if (comIndeterminado && CONFIG.permitir_indeterminado) outros += `<option value="indeterminado" ${selecionada === 'indeterminado' ? 'selected' : ''}>Tempo indeterminado (até a TI bloquear)</option>`;
  if (comPersonalizado) outros += '<option value="personalizado">Personalizado…</option>';
  if (outros) html += `<optgroup label="Outros">${outros}</optgroup>`;
  return html;
}
// Caixa do tempo personalizado (quantidade + horas/dias), escondida ate escolher "Personalizado"
function campoPersonalizado(id, maxHoras) {
  return `<div class="campo oculto" id="${id}-caixa"><label for="${id}-qtd">Tempo personalizado</label>
    <div class="linha-prazo"><input class="entrada" id="${id}-qtd" type="number" min="1" step="1" inputmode="numeric" placeholder="Quantidade">
    <select class="entrada" id="${id}-unidade"><option value="1">horas</option><option value="24">dias</option></select></div>
    <p class="ajuda">Máximo: ${nomeHoras(maxHoras)}.</p></div>`;
}
function ligarPersonalizado(id) {
  const sel = document.getElementById(id), caixa = document.getElementById(id + '-caixa');
  if (!sel || !caixa) return;
  sel.addEventListener('change', () => {
    caixa.classList.toggle('oculto', sel.value !== 'personalizado');
    if (sel.value === 'personalizado') document.getElementById(id + '-qtd').focus();
  });
}
// Devolve { horas, indeterminado } ou { erro: 'mensagem' }
function lerPrazo(id, maxHoras) {
  const sel = document.getElementById(id);
  if (sel.value === 'indeterminado') return { horas: null, indeterminado: true };
  if (sel.value === 'personalizado') {
    const qtd = Number(document.getElementById(id + '-qtd').value);
    const unidade = Number(document.getElementById(id + '-unidade').value);
    if (!Number.isInteger(qtd) || qtd < 1) return { erro: 'Informe a quantidade do tempo personalizado (número inteiro).' };
    const horas = qtd * unidade;
    if (horas > maxHoras) return { erro: `O tempo máximo é ${nomeHoras(maxHoras)}.` };
    return { horas, indeterminado: false };
  }
  return { horas: Number(sel.value), indeterminado: false };
}

// =========================================================================== expiracao da sessao por inatividade
// So uso real conta (mouse, teclado, toque, rolagem). As atualizacoes automaticas das telas nao renovam a sessao.
// O horario da ultima renovacao fica no navegador e vale para todas as abas: usar uma aba mantem as outras vivas.
let _vigia = null;
function pararVigiaInatividade() { if (_vigia) { _vigia.parar(); _vigia = null; } }
function vigiarInatividade({ minutos, renovar, aoExpirar }) {
  pararVigiaInatividade();
  if (!minutos || minutos <= 0) return;
  const LIMITE = minutos * 60000, AVISO = Math.min(120000, LIMITE / 2), INTERVALO_RENOVAR = Math.min(60000, LIMITE / 6);
  const chave = 'gt_sessao_renovada:' + renovar;
  const lerRenovada = () => { try { return Number(localStorage.getItem(chave)) || 0; } catch { return 0; } };
  const gravarRenovada = v => { try { localStorage.setItem(chave, String(v)); } catch { /* sem armazenamento: vale so esta aba */ } };
  let renovadaAqui = 0, avisando = false, encerrado = false, pedindo = false, relogioAviso = null;
  const renovadaEm = () => Math.max(renovadaAqui, lerRenovada());
  const resta = () => LIMITE - (Date.now() - renovadaEm());
  const mmss = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;

  async function renovarAgora() {
    if (pedindo || encerrado) return;
    pedindo = true;
    try {
      const r = await fetch(renovar, { method: 'POST', credentials: 'same-origin' });
      if (r.status === 401) return expirar();
      if (r.ok) { renovadaAqui = Date.now(); gravarRenovada(renovadaAqui); }
    } catch { /* sem rede agora: tenta de novo na proxima atividade */ }
    finally { pedindo = false; }
  }
  function atividade() {
    if (avisando || encerrado) return;              // com o aviso na tela, so o botao "Continuar conectado" renova
    if (Date.now() - renovadaEm() > INTERVALO_RENOVAR) renovarAgora();
  }
  function expirar() {
    if (encerrado) return;
    encerrado = true;
    clearInterval(relogio); clearInterval(relogioAviso);
    if (avisando && Swal.isVisible()) Swal.close();
    _vigia = null;
    aoExpirar();
  }
  function mostrarAviso() {
    avisando = true;
    Swal.fire({ icon: 'warning', title: 'Sua sessão vai expirar',
      html: `Você está sem usar o sistema há um tempo. Por segurança, a sessão será encerrada em <b id="sw-resta">${mmss(resta())}</b>.`,
      showCancelButton: true, confirmButtonText: 'Continuar conectado', cancelButtonText: 'Sair agora',
      allowOutsideClick: false, allowEscapeKey: false,
      didOpen: () => { relogioAviso = setInterval(() => { const el = document.getElementById('sw-resta'); if (el) el.textContent = mmss(Math.max(0, resta())); }, 500); },
      willClose: () => clearInterval(relogioAviso),
    }).then(r => {
      if (encerrado) return;
      avisando = false;
      if (r.isConfirmed) renovarAgora();
      else if (r.dismiss === Swal.DismissReason.cancel) expirar();
    });
  }
  const eventos = ['pointerdown', 'mousemove', 'keydown', 'wheel', 'scroll', 'touchstart'];
  eventos.forEach(ev => addEventListener(ev, atividade, { capture: true, passive: true }));
  const relogio = setInterval(() => {
    if (encerrado) return;
    const r = resta();
    if (r <= 0) return expirar();
    if (r <= AVISO && !avisando) mostrarAviso();
    else if (avisando && r > AVISO && Swal.isVisible()) { avisando = false; Swal.close(); }   // usou o sistema em outra aba
  }, 1000);
  renovarAgora();                                   // abrir ou recarregar a tela conta como uso
  _vigia = { parar() { encerrado = true; clearInterval(relogio); clearInterval(relogioAviso); eventos.forEach(ev => removeEventListener(ev, atividade, { capture: true })); } };
}

// =========================================================================== Gestao TI: sessao, topo e modulos
let EU = null;
const ICONES = {
  sair: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  registro: '<path d="M9 4h6a1 1 0 0 1 1 1v1H8V5a1 1 0 0 1 1-1Z"/><path d="M8 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/><path d="M8 11h2M8 15h2M13 11h3M13 15h3"/>',
  wifi: '<path d="M2 9a16 16 0 0 1 20 0"/><path d="M5.5 12.5a11 11 0 0 1 13 0"/><path d="M9 16a5.5 5.5 0 0 1 6 0"/><circle cx="12" cy="19.5" r="1.2" fill="currentColor"/>',
  moeda: '<circle cx="12" cy="12" r="9"/><path d="M14.8 9.2A2.8 2.8 0 0 0 12 8h-.6a2.4 2.4 0 0 0 0 4.8h1.2a2.4 2.4 0 0 1 0 4.8H12a2.8 2.8 0 0 1-2.8-1.2M12 6v2M12 17.6V19"/>',
  email: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
  lista: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
  chave: '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9M17 6l3 3M15 8l2 2"/>',
  visitante: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  usuarios: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M21.5 20a6.5 6.5 0 0 0-4-6"/>',
  inicio: '<path d="m3 11 9-8 9 8"/><path d="M5 10v10h14V10"/>',
  engrenagem: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
};
const icone = (nome, tam = 22) => `<svg width="${tam}" height="${tam}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONES[nome] || ''}</svg>`;
const irParaInicio = () => { location.href = '/'; };
const textoMinutos = n => `${n} minuto${Number(n) === 1 ? '' : 's'}`;

// Carrega a sessao; sem sessao (ou sem o modulo) volta para a tela inicial
async function exigirSessao(...modulosAceitos) {
  try { EU = await api('/api/auth/eu', {}, irParaInicio); }
  catch { irParaInicio(); return null; }
  if (modulosAceitos.length && !modulosAceitos.some(m => EU.modulos.includes(m))) { irParaInicio(); return null; }
  return EU;
}
async function sair() {
  pararVigiaInatividade();
  await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
  irParaInicio();
}
// Cabecalho padrao das paginas dos modulos: [voltar] [logo] Gestão TI / (grupo) / Módulo ............ [avatar]
function montarTopo(titulo, iconeModulo, voltar = null) {
  if (!_vigia) vigiarInatividade({ minutos: EU.inatividade_minutos, renovar: '/api/auth/renovar', aoExpirar: async () => {
    await fetch('/api/auth/logout?inatividade=true', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
    location.href = '/?inatividade=1';
  } });
  const el = document.getElementById('topo');
  el.className = 'topo topo-modulo';
  // Voltar: volta para a pagina anterior do sistema; se veio de fora (favorito, link), vai para a pagina "pai"
  const pai = voltar ? voltar.url : '/';
  const naInicio = location.pathname === '/';
  const iniciais = (EU.nome || EU.usuario || '?').split(/\s+/).filter(Boolean);
  const sigla = ((iniciais[0] || '?')[0] + (iniciais.length > 1 ? iniciais[iniciais.length - 1][0] : '')).toUpperCase();
  const sep = '<span class="sep">/</span>';
  el.innerHTML = `<div class="faixa">
    ${naInicio ? '' : `<a href="${esc(pai)}" class="btn-voltar" id="btn-voltar" title="Voltar" aria-label="Voltar"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m15 18-6-6 6-6"/></svg></a>`}
    <a href="/" class="inicio-link" title="Início">${CONFIG.logo ? `<img src="${esc(CONFIG.logo)}" alt="">` : LOGO_PADRAO}</a>
    <nav class="trilha" aria-label="Você está em">
      ${naInicio ? `<strong>${esc(CONFIG.app_nome || 'Gestão TI')}</strong><span class="org">${esc(CONFIG.orgao_linha2 || '')}</span>`
        : `<a href="/" class="raiz">${esc(CONFIG.app_nome || 'Gestão TI')}</a>${sep}${voltar ? `<a href="${esc(voltar.url)}" class="grupo">${esc(voltar.texto)}</a>${sep}` : ''}<strong>${esc(titulo)}</strong>`}
    </nav>
    <div class="conta">
      <button type="button" class="avatar" id="btn-conta" aria-haspopup="true" aria-expanded="false" title="${esc(EU.nome)}">${esc(sigla)}</button>
      <div class="menu-conta oculto" id="menu-conta" role="menu">
        <div class="menu-quem"><b>${esc(EU.nome)}</b><span>${esc(EU.usuario)} · ${EU.origem === 'ad' ? esc(EU.unidade || 'AD') : 'acesso local'}</span></div>
        <a href="/" role="menuitem">${icone('inicio', 16)}Início</a>
        <button type="button" role="menuitem" id="btn-sair">${icone('sair', 16)}Sair</button>
      </div>
    </div>
  </div>`;
  document.getElementById('btn-sair').onclick = sair;
  const btnConta = document.getElementById('btn-conta'), menu = document.getElementById('menu-conta');
  const fechar = () => { menu.classList.add('oculto'); btnConta.setAttribute('aria-expanded', 'false'); };
  btnConta.onclick = ev => { ev.stopPropagation(); const abrir = menu.classList.contains('oculto'); menu.classList.toggle('oculto', !abrir); btnConta.setAttribute('aria-expanded', String(abrir)); };
  document.addEventListener('click', ev => { if (!ev.target.closest('.conta')) fechar(); });
  document.addEventListener('keydown', ev => { if (ev.key === 'Escape') fechar(); });
  const bv = document.getElementById('btn-voltar');
  if (bv) bv.onclick = ev => {
    let veioDoSistema = false;
    try { veioDoSistema = !!document.referrer && new URL(document.referrer).origin === location.origin && history.length > 1; } catch {}
    if (veioDoSistema) { ev.preventDefault(); history.back(); }
  };
}
// Nome do modulo com o grupo na frente (ex.: "Rede Sem Fio › Wi-Fi Visitante")
const nomeCompleto = m => m.grupo_nome ? `${m.grupo_nome} › ${m.nome}` : m.nome;
const tamanhoArquivo = b => b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
const numero = n => Number(n || 0).toLocaleString('pt-BR');
