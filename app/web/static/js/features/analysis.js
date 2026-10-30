// app/web/static/js/features/analysis.js
/** Upload, durable job lifecycle, SSE reconnect и восстановление серверного snapshot. */
import { api, download, idempotencyKey } from '../api.js';
import { applyEvent, completed, initialState, parsePages, restoreSnapshot, running } from '../state.js';

const eventNames = ['job_queued', 'job_started', 'page_started', 'stream_reset', 'text_delta', 'item_validated',
  'page_completed', 'warning', 'stage_started', 'stage_completed', 'retry_scheduled', 'cancellation_requested',
  'result_invalidated', 'job_completed', 'job_failed', 'job_cancelled'];

/** Подключает форму к публичному API, не выполняя извлечение в браузере. */
export function setupAnalysis(state, viewer, results, notify) {
  const form = document.querySelector('[data-upload-form]');
  const fileInput = document.querySelector('[data-file]');
  const pages = document.querySelector('[data-pages]');
  const project = document.querySelector('[data-project]');
  const formError = document.querySelector('[data-form-error]');
  let source = null; let busy = false; let paintTimer = null; let statusTimer = null;
  let warningOffset = 0;
  /** Сохраняет только безопасные UI pointers, никогда PDF, API keys или prompt. */
  function persist() {
    if (state.jobId) sessionStorage.setItem('spec.job', JSON.stringify({ jobId: state.jobId, viewed_pdf_page: state.viewed_pdf_page }));
  }
  /** Отображает ошибки формы рядом с полями; серверный код остаётся доступен в API. */
  function error(message = '') { formError.textContent = message; formError.hidden = !message; }
  /** Обновляет только view-state; manual navigation не влияет на worker. */
  function render() {
    viewer.render(); results.render(); persist();
    if (state.document && state.status) document.querySelector('[data-file-meta]').textContent = `${state.document.page_count} PDF-страниц · хранится до ${new Date(state.status.expires_at).toLocaleString('ru-RU')}`;
    const active = running(state.status?.status);
    document.querySelector('[data-start]').disabled = busy || active;
    document.querySelector('[data-file-choose]').disabled = busy || active;
    document.querySelector('[data-file-remove]').disabled = busy || active;
    pages.disabled = active; project.disabled = active;
  }
  /** Ограничивает перерисовки raw stream, сохраняя все токены в state. */
  function scheduleRender() { if (!paintTimer) paintTimer = setTimeout(() => { paintTimer = null; render(); }, 100); }
  /** Получает фактическую телеметрию, не заменяя viewed_pdf_page. */
  async function refreshStatus() {
    if (!state.jobId) return;
    try { state.status = await api(`/extractions/${state.jobId}`); render(); }
    catch (failure) { notify(failure.message); }
  }
  /** Восстанавливает неизменяемый server snapshot и начинает SSE после его event cursor. */
  async function restore(jobId, saved = null) {
    stop();
    const snapshot = await api(`/extractions/${jobId}/snapshot`);
    restoreSnapshot(state, snapshot, saved);
    document.querySelector('[data-file-name]').textContent = state.document.filename;
    document.querySelector('[data-file-meta]').textContent = `${state.document.page_count} PDF-страниц · хранится до ${new Date(snapshot.status.expires_at).toLocaleString('ru-RU')}`;
    pages.value = state.status.selected_pdf_pages.join(', '); project.value = state.status.project_code;
    render();
    if (running(state.status.status)) connect();
  }
  /** EventSource использует Last-Event-ID при reconnect; старые события пропускаются reducer. */
  function connect() {
    source?.close();
    source = new EventSource(`/api/v1/extractions/${state.jobId}/events?after=${state.last_event_id}`);
    for (const type of eventNames) source.addEventListener(type, async event => {
      const data = JSON.parse(event.data);
      if (!applyEvent(state, type, data, event.lastEventId)) return;
      if (type === 'text_delta') scheduleRender(); else render();
      if (['page_started', 'page_completed', 'warning', 'stage_started', 'stage_completed'].includes(type)) {
        if (!statusTimer) statusTimer = setTimeout(() => { statusTimer = null; refreshStatus(); }, 250);
      }
      if (['job_completed', 'job_failed', 'job_cancelled'].includes(type)) {
        source.close();
        await restore(state.jobId, { viewed_pdf_page: state.viewed_pdf_page });
        if (type === 'job_failed') notify(`Ошибка анализа: ${state.status.error_code}. Подробности — в предупреждениях.`);
        if (state.status.status === 'completed_with_warnings') notify('Анализ завершён. Проверьте предупреждения по оригиналу.');
      }
    });
    source.addEventListener('access_expired', () => { source.close(); notify('Срок доступа к заданию истёк'); });
    source.onerror = () => { document.querySelector('[data-stream-state]').textContent = 'Переподключение к потоку…'; refreshStatus(); };
    source.onopen = () => { document.querySelector('[data-stream-state]').textContent = '●  Стриминг ответа VLM'; };
  }
  /** Закрывает лишь клиентские соединения, durable работа сохраняется на сервере. */
  function stop() { source?.close(); source = null; if (paintTimer) clearTimeout(paintTimer); paintTimer = null; }
  /** Загружает реальный PDF для preview; шифр предложения требует проверки перед запуском. */
  async function selectFile(file) {
    if (!file || busy || running(state.status?.status)) return;
    busy = true; error(); render();
    try {
      if (!file.name.toLowerCase().endsWith('.pdf')) throw new Error('Выберите PDF-файл');
      const body = new FormData(); body.append('file', file);
      const uploaded = await api('/documents', { method: 'POST', body });
      stop(); Object.assign(state, initialState(), { document: uploaded }); sessionStorage.removeItem('spec.job');
      pages.value = uploaded.page_count === 1 ? '1' : `1-${uploaded.page_count}`;
      project.value = uploaded.suggested_project_code || '';
      document.querySelector('[data-file-name]').textContent = uploaded.filename;
      document.querySelector('[data-file-meta]').textContent = `${uploaded.page_count} PDF-страниц · ${Math.ceil(file.size / 1024)} КБ`;
      document.querySelector('[data-project-hint]').textContent = uploaded.suggested_project_code ? 'Предложено по PDF: проверьте перед запуском' : 'Введите шифр вручную';
    } catch (failure) { error(failure.message); }
    finally { busy = false; render(); }
  }
  document.querySelector('[data-file-choose]').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => selectFile(fileInput.files[0]));
  document.querySelector('[data-file-remove]').addEventListener('click', () => {
    stop(); Object.assign(state, initialState()); fileInput.value = ''; pages.value = ''; project.value = '';
    sessionStorage.removeItem('spec.job'); document.querySelector('[data-file-name]').textContent = 'Выберите PDF или перетащите сюда';
    document.querySelector('[data-file-meta]').textContent = 'PDF · физические страницы с 1'; render();
  });
  const drop = document.querySelector('[data-drop-zone]');
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('upload__picker--drag'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('upload__picker--drag'));
  drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('upload__picker--drag'); selectFile(event.dataTransfer.files[0]); });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || running(state.status?.status)) return;
    error();
    try {
      if (!state.document) throw new Error('Сначала загрузите PDF');
      parsePages(pages.value, state.document.page_count);
      if (!project.value.trim()) throw new Error('Подтвердите шифр проекта');
      busy = true; render();
      const body = new FormData(); body.append('document_id', state.document.id); body.append('pages', pages.value); body.append('project_code', project.value.trim());
      const job = await api('/extractions', { method: 'POST', body, headers: { 'Idempotency-Key': idempotencyKey() } });
      await restore(job.job_id);
    } catch (failure) { error(failure.message); }
    finally { busy = false; render(); }
  });
  pages.addEventListener('input', () => {
    if (!state.document) return;
    try { const selected = parsePages(pages.value, state.document.page_count); error(); document.querySelector('[data-selection-label]').textContent = `Выбрано PDF-страниц: ${selected.length}`; }
    catch (failure) { error(failure.message); }
  });
  document.querySelector('[data-cancel]').addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await api(`/extractions/${state.jobId}/cancel`, { method: 'POST' }); await refreshStatus(); }
    catch (failure) { notify(failure.message); }
    finally { button.disabled = false; }
  });
  document.querySelector('[data-retry]').addEventListener('click', async () => {
    if (!window.confirm('Повторить этот лист и зависимые последующие страницы? Их старые результаты будут заменены.')) return;
    try { await api(`/extractions/${state.jobId}/pages/${state.viewed_pdf_page}/retry`, { method: 'POST' }); await restore(state.jobId, { viewed_pdf_page: state.viewed_pdf_page }); }
    catch (failure) { notify(failure.message); }
  });
  document.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', async () => {
    if (!completed(state.status?.status)) return;
    try { await download(state.status.links[button.dataset.export], button.dataset.export); await refreshStatus(); }
    catch (failure) { notify(failure.message); }
  }));
  const warningsDialog = document.querySelector('[data-warnings-dialog]');
  /** Читает следующую страницу diagnostics, включая настоящие непроверенные строки. */
  async function loadWarnings() {
    const response = await api(`/extractions/${state.jobId}/warnings?offset=${warningOffset}&limit=100`);
    const list = document.querySelector('[data-warnings]');
    if (!warningOffset) list.replaceChildren();
    if (!response.total) { const empty = document.createElement('p'); empty.textContent = 'Предупреждений нет. Автоматическая проверка схемы не гарантирует абсолютную полноту распознавания.'; list.append(empty); }
    for (const warning of response.warnings) {
      const entry = document.createElement('article'); entry.className = 'warnings__entry';
      const page = document.createElement('button'); page.className = 'button button--soft'; page.textContent = `Открыть PDF-лист ${warning.pdf_page}`;
      page.addEventListener('click', () => { viewer.navigate(warning.pdf_page); render(); warningsDialog.close(); });
      const message = document.createElement('p'); message.textContent = warning.message;
      const code = document.createElement('p'); code.className = 'warnings__code'; code.textContent = warning.code;
      entry.append(page, message, code);
      if (warning.row) { const row = document.createElement('p'); row.textContent = `${warning.row.caption} · ${warning.row.units} · ${warning.row.quantity}`; entry.append(row); }
      list.append(entry);
    }
    warningOffset += response.warnings.length;
    document.querySelector('[data-warnings-more]').hidden = warningOffset >= response.total;
  }
  document.querySelector('[data-warnings-open]').addEventListener('click', async () => {
    if (!state.jobId) { notify('Сначала запустите анализ документа'); return; }
    warningOffset = 0;
    try { await loadWarnings(); warningsDialog.showModal(); } catch (failure) { notify(failure.message); }
  });
  document.querySelector('[data-warnings-close]').addEventListener('click', () => warningsDialog.close());
  document.querySelector('[data-warnings-more]').addEventListener('click', () => loadWarnings().catch(failure => notify(failure.message)));
  return { render, restore, persist, stop };
}
