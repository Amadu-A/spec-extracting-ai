// app/web/static/js/app.js
/** Сборка браузерных модулей и автоматическая анонимная сессия без формы входа. */
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
let renewing;

/** Заменяет истёкший capability и очищает прежние данные без окна авторизации. */
async function renewSession() {
  analysis.stop();
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  const session = await api('/session'); setCsrf(session.csrf);
  sessionStorage.removeItem('spec.job'); Object.assign(state, initialState()); analysis.render();
  document.querySelector('[data-upload-form]').reset();
  document.querySelector('[data-file-name]').textContent = 'Выберите PDF или перетащите сюда';
  document.querySelector('[data-file-meta]').textContent = 'Сессия обновлена. Загрузите PDF повторно.';
}
onExpired(() => {
  renewing ??= renewSession().finally(() => { renewing = null; });
  return renewing;
});

/** Восстанавливает серверное задание после обновления страницы без нового запуска. */
async function initialize() {
  const session = await api('/session'); setCsrf(session.csrf);
  const saved = JSON.parse(sessionStorage.getItem('spec.job') || 'null');
  if (saved?.jobId) {
    try { await analysis.restore(saved.jobId, saved); }
    catch (failure) { sessionStorage.removeItem('spec.job'); notify(failure.message); }
  }
  analysis.render();
}
initialize().catch(failure => notify(`Сервис недоступен: ${failure.message}`));
