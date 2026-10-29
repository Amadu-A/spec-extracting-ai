// app/web/static/js/features/prompt.js
/** Временное редактирование prompt с немедленным server restore и dirty-state защитой. */
import { api } from '../api.js';

/** Подключает native dialog; браузер обеспечивает focus trap и возврат фокуса. */
export function setupPrompt(notify) {
  const dialog = document.querySelector('[data-prompt-dialog]');
  const text = dialog.querySelector('[data-prompt-text]');
  const error = dialog.querySelector('[data-prompt-error]');
  let saved = '';
  /** Запрашивает подтверждение потери локальных правок, не сохраняя их автоматически. */
  function close() {
    if (text.value !== saved && !window.confirm('Закрыть без сохранения изменений промпта?')) return;
    dialog.close();
  }
  document.querySelector('[data-prompt-open]').addEventListener('click', async () => {
    try {
      const current = await api('/session/prompt'); saved = current.prompt; text.value = saved;
      error.hidden = true; dialog.showModal(); text.focus();
    } catch (failure) { notify(failure.message); }
  });
  dialog.querySelectorAll('[data-prompt-close]').forEach(button => button.addEventListener('click', close));
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.querySelector('[data-prompt-save]').addEventListener('click', async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const current = await api('/session/prompt', { method: 'PUT', body: JSON.stringify({ prompt: text.value }) });
      saved = current.prompt; dialog.close(); notify('Промпт сохранён для новых заданий этой сессии');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
  dialog.querySelector('[data-prompt-restore]').addEventListener('click', async (event) => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const current = await api('/session/prompt/restore', { method: 'POST' }); saved = current.prompt; text.value = saved;
      error.hidden = true; notify('Стандартный промпт уже восстановлен на сервере');
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
}
