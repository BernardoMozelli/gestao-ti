// Acoes em contas do AD (redefinir senha, desbloquear, reativar), usadas na Gestão de Usuários.
// c = { login, nome, unidade, bloqueado, ativo }   depois = funcao chamada quando a acao der certo
const NOMES_CONTA = { senha_redefinida: 'Senha redefinida', conta_desbloqueada: 'Conta desbloqueada', conta_reativada: 'Conta reativada no AD',
  senha_reset_falhou: 'Tentativa recusada pelo AD' };

const _chamarConta = (c, o) => api(c, o, irParaInicio);

function _camposSenha(prefixo, marcadoTrocar = true) {
  return `<div class="opcoes-radio">
      <label><span><input type="radio" name="sw-modo" value="gerar" checked><b>Gerar senha temporária</b></span><small>O sistema cria uma senha forte e fácil de ditar.</small></label>
      <label><span><input type="radio" name="sw-modo" value="digitar"><b>Digitar a senha</b></span><small>Você escolhe a nova senha.</small></label>
    </div>
    <div class="campo oculto" id="sw-caixa-senha"><label for="sw-senha">Nova senha</label><input class="entrada" id="sw-senha" type="text" autocomplete="off"></div>
    <label class="marcar"><input type="checkbox" id="sw-trocar" ${marcadoTrocar ? 'checked' : ''}> Exigir que a pessoa troque a senha no próximo logon</label>`;
}
function _ligarModoSenha() {
  document.querySelectorAll('input[name=sw-modo]').forEach(x => x.addEventListener('change', () =>
    $('#sw-caixa-senha').classList.toggle('oculto', document.querySelector('input[name=sw-modo]:checked').value !== 'digitar')));
}
function _lerSenha() {
  const gerar = document.querySelector('input[name=sw-modo]:checked').value === 'gerar', senha = $('#sw-senha').value;
  if (!gerar && senha.length < 8) { Swal.showValidationMessage('A senha precisa ter pelo menos 8 caracteres.'); return null; }
  return { gerar, senha: gerar ? null : senha, exigir_troca: $('#sw-trocar').checked };
}
const _campoMotivo = '<div class="campo"><label for="sw-motivo">Motivo / nº do chamado</label><input class="entrada" id="sw-motivo" placeholder="ex.: chamado 1234 - esqueceu a senha"></div>';
function _lerMotivo() {
  const m = $('#sw-motivo').value.trim();
  if (m.length < 3) { Swal.showValidationMessage('Informe o motivo ou o número do chamado.'); return null; }
  return m;
}
async function _mostrarSenhaNova(c, senha, exigiuTroca, titulo) {
  await Swal.fire({ icon: 'success', title: titulo, width: 560, confirmButtonText: 'Já passei a senha',
    html: `<p>Nova senha de <b>${esc(c.nome)}</b>:</p>
      <div class="senha-gerada">${esc(senha)}</div>
      <button class="btn" id="sw-copiar" type="button">Copiar</button>
      <p style="font-size:14px;color:var(--suave)">Passe a senha por telefone ou pessoalmente. ${exigiuTroca ? 'A pessoa vai criar uma senha nova no primeiro login.' : ''}<br>Ela não será mostrada de novo.</p>`,
    didOpen: () => { $('#sw-copiar').onclick = () => navigator.clipboard?.writeText(senha).then(() => toast('Senha copiada.')); } });
}
const _cabecalho = c => `<p style="margin:0;color:var(--suave)">Conta <b>${esc(c.login)}</b>${c.unidade ? ' · ' + esc(c.unidade) : ''}${c.matricula ? ' · matrícula ' + esc(c.matricula) : ''}</p>`;

async function redefinirSenhaAD(c, depois) {
  const propria = typeof EU !== 'undefined' && EU && c.login === EU.usuario;
  const avisos = (propria ? `<div class="aviso-sei"><b>Esta é a sua própria conta.</b> Depois de trocar, use a senha nova no Windows,
      no e-mail e nos sistemas do AD. Computadores e celulares onde você está conectado podem pedir a senha de novo.</div>` : '')
    + (c.protegida ? `<div class="aviso-sei"><b>Conta de administrador do AD.</b> Se o AD recusar, a conta de serviço da Gestão TI
      precisa de permissão no AdminSDHolder (a mensagem de erro explica como).</div>` : '');
  const r = await Swal.fire({ title: propria ? 'Redefinir a minha senha' : `Redefinir a senha de ${esc(c.nome)}`, width: 620, showCancelButton: true, confirmButtonText: 'Redefinir senha', cancelButtonText: 'Voltar',
    html: `<div style="text-align:left" class="pilha">${_cabecalho(c)}${avisos}${_camposSenha(undefined, !propria)}
      ${c.bloqueado ? '<label class="marcar"><input type="checkbox" id="sw-desbloquear" checked> Desbloquear a conta</label>' : ''}${_campoMotivo}</div>`,
    didOpen: _ligarModoSenha,
    preConfirm: () => {
      const s = _lerSenha(); if (!s) return false;
      const motivo = _lerMotivo(); if (!motivo) return false;
      return { login: c.login, ...s, desbloquear: $('#sw-desbloquear')?.checked ?? false, motivo };
    } });
  if (!r.isConfirmed) return;
  try {
    Swal.fire({ title: 'Trocando a senha no AD…', allowOutsideClick: false, didOpen: () => Swal.showLoading() });
    const res = await _chamarConta('/api/senhas/redefinir', { method: 'POST', body: JSON.stringify(r.value) });
    if (res.senha) await _mostrarSenhaNova(c, res.senha, r.value.exigir_troca, 'Senha redefinida');
    else await Swal.fire({ icon: 'success', title: 'Senha redefinida', text: r.value.exigir_troca ? 'A pessoa vai criar uma senha nova no primeiro login.' : '' });
    depois && depois();
  } catch (e) { Swal.fire({ icon: 'error', title: 'O AD não aceitou', text: e.message }); }
}

async function desbloquearContaAD(c, depois) {
  const r = await Swal.fire({ title: `Desbloquear a conta de ${esc(c.nome)}?`, input: 'text', inputLabel: 'Motivo / nº do chamado', showCancelButton: true,
    confirmButtonText: 'Desbloquear', cancelButtonText: 'Voltar', inputValidator: v => (!v || v.trim().length < 3) ? 'Informe o motivo.' : undefined });
  if (!r.isConfirmed) return;
  try { await _chamarConta('/api/senhas/desbloquear', { method: 'POST', body: JSON.stringify({ login: c.login, motivo: r.value }) }); toast('Conta desbloqueada.'); depois && depois(); }
  catch (e) { Swal.fire({ icon: 'error', title: 'O AD não aceitou', text: e.message }); }
}

async function reativarContaAD(c, depois) {
  const r = await Swal.fire({ title: `Reativar a conta de ${esc(c.nome)} no AD`, width: 620, showCancelButton: true, confirmButtonText: 'Reativar conta', cancelButtonText: 'Voltar',
    html: `<div style="text-align:left" class="pilha">${_cabecalho(c)}
      <p style="margin:0;font-size:14px">A conta volta a entrar no Windows, no e-mail e nos sistemas que usam o AD.</p>
      <label class="marcar"><input type="checkbox" id="sw-nova" checked> Definir uma nova senha (recomendado: a pessoa pode não lembrar da antiga)</label>
      <div id="sw-bloco-senha" class="pilha">${_camposSenha()}</div>
      ${c.exige_sei ? `<div class="aviso-sei">
        <b>Conta sem uso há ${c.dias_sem_uso == null ? 'mais de 90 dias' : c.dias_sem_uso + ' dias'}.</b>
        Contas inativas há mais de 90 dias só podem ser reativadas com processo SEI solicitando a reativação.
        <label class="marcar"><input type="checkbox" id="sw-ciente"> Estou ciente e a reativação deste usuário possui processo SEI.</label>
        <div class="campo"><label for="sw-sei">Nº do processo SEI</label><input class="entrada" id="sw-sei" placeholder="ex.: 01234.000567/2026-89" autocomplete="off"></div>
      </div>` : ''}${_campoMotivo}</div>`,
    didOpen: () => { _ligarModoSenha(); $('#sw-nova').addEventListener('change', ev => $('#sw-bloco-senha').classList.toggle('oculto', !ev.target.checked)); },
    preConfirm: () => {
      const nova = $('#sw-nova').checked;
      const s = nova ? _lerSenha() : { gerar: false, senha: null, exigir_troca: false }; if (!s) return false;
      if (c.exige_sei && !$('#sw-ciente').checked) { Swal.showValidationMessage('Confirme que a reativação possui processo SEI.'); return false; }
      const motivo = _lerMotivo(); if (!motivo) return false;
      return { login: c.login, nova_senha: nova, ...s, motivo, ciente_sei: !!c.exige_sei, processo_sei: $('#sw-sei')?.value.trim() || null };
    } });
  if (!r.isConfirmed) return;
  try {
    Swal.fire({ title: 'Reativando no AD…', allowOutsideClick: false, didOpen: () => Swal.showLoading() });
    const res = await _chamarConta('/api/senhas/reativar', { method: 'POST', body: JSON.stringify(r.value) });
    if (res.senha) await _mostrarSenhaNova(c, res.senha, r.value.exigir_troca, 'Conta reativada');
    else await Swal.fire({ icon: 'success', title: 'Conta reativada', text: r.value.nova_senha && r.value.exigir_troca ? 'A pessoa vai criar uma senha nova no primeiro login.' : '' });
    depois && depois();
  } catch (e) { Swal.fire({ icon: 'error', title: 'O AD não aceitou', text: e.message }); }
}

// Texto curto para a lista: ha quanto tempo a conta desativada esta sem uso
function etiquetaUso(c) {
  if (c.ativo || c.dias_sem_uso === undefined) return '';
  return c.exige_sei ? `<span class="tag pendente" title="Reativação exige processo SEI">Sem uso há ${c.dias_sem_uso == null ? '+90' : c.dias_sem_uso} dias · exige SEI</span>`
    : (c.dias_sem_uso != null ? `<span class="tag">Sem uso há ${c.dias_sem_uso} dias</span>` : '');
}
