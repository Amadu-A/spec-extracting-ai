// app/web/static/js/app.js
/** Composition root браузера: модули транспорта, state, PDF-вьюера и feature controls. */
import { api, onExpired, setCsrf } from './api.js';
import { initialState } from './state.js';
import { setupViewer } from './features/viewer.js';
import { setupResults } from './features/results.js';
import { setupPrompt } from './features/prompt.js';
import { setupAnalysis } from './features/analysis.js';

const state = initialState();
let notificationTimer;
/** Показывает безопасный текст сообщения, не интерпретируя HTML источника. */
function notify(message) {
  const element = document.querySelector('[data-notification]'); element.textContent = message; element.hidden = false;
  clearTimeout(notificationTimer); notificationTimer = setTimeout(() => { element.hidden = true; }, 6500);
}
let analysis;
const results = setupResults(state, notify);
const viewer = setupViewer(state, () => { analysis?.persist(); results.render(); }, notify);
analysis = setupAnalysis(state, viewer, results, notify);
setupPrompt(notify);
const login = document.querySelector('[data-login-dialog]');

/** Создаёт новую anonymous session при истечении и показывает настоящий вход. */
async function requireLogin() {
  analysis.stop();
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  const session = await api('/session'); setCsrf(session.csrf);
  sessionStorage.removeItem('spec.job'); Object.assign(state, initialState()); analysis.render();
  login.showModal(); document.querySelector('[data-login-password]').focus();
}
onExpired(() => requireLogin().catch(failure => notify(failure.message)));
login.addEventListener('cancel', event => event.preventDefault());
document.querySelector('[data-login-form]').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.currentTarget.querySelector('button'); button.disabled = true;
  try {
    const session = await api('/session/login', { method: 'POST', body: JSON.stringify({ password: document.querySelector('[data-login-password]').value }) });
    setCsrf(session.csrf); document.querySelector('[data-login-password]').value = ''; login.close();
    document.querySelector('[data-login-error]').hidden = true;
  } catch (failure) { const error = document.querySelector('[data-login-error]'); error.textContent = failure.message; error.hidden = false; }
  finally { button.disabled = false; }
});
document.querySelector('[data-logout]').addEventListener('click', async () => {
  try { await api('/session/logout', { method: 'POST' }); await requireLogin(); }
  catch (failure) { notify(failure.message); }
});
/** Восстанавливает серверное задание после refresh, не перезапуская анализ. */
async function initialize() {
  const session = await api('/session'); setCsrf(session.csrf);
  if (!session.authenticated) { login.showModal(); return; }
  const saved = JSON.parse(sessionStorage.getItem('spec.job') || 'null');
  if (saved?.jobId) {
    try { await analysis.restore(saved.jobId, saved); }
    catch (failure) { sessionStorage.removeItem('spec.job'); notify(failure.message); }
  }
  analysis.render();
}
initialize().catch(failure => notify(`Сервис недоступен: ${failure.message}`));
