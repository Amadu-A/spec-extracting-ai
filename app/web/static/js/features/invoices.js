// app/web/static/js/features/invoices.js
/** Контроллер счетов: API/SSE, восстановление, отмена и ограниченный retry без legacy state. */
import { api, eventStream } from '../api.js';
import { applyInvoiceEvent, invoiceState, restoreInvoice, selectInvoiceFile } from '../invoice-state.js';
import { running } from '../state.js';
import { setupViewer } from './viewer.js';
import { setupPrompt } from './prompt.js';
import { setupInvoiceFiles } from './invoice-files.js';
import { setupInvoiceResults } from './invoice-results.js';

const types = ['job_queued', 'job_started', 'page_started', 'stream_reset', 'text_delta', 'page_completed', 'stage_started',
  'stage_completed', 'document_validated', 'result_invalidated', 'cancellation_requested', 'job_completed', 'job_failed', 'job_cancelled', 'warning'];

/** Подключает общий viewer и отдельные файлы/результаты к новым REST use-case. */
export function setupInvoices(root, notify) {
  const state = invoiceState(); let source = null; let timer = null; let refreshTimer = null; let busy = false; let warningOffset = 0;
  const form = root.querySelector('[data-invoice-form]');
  const journal = root.querySelector('[data-invoice-journal]');
  const viewer = setupViewer(state, () => { persist(); }, notify, { root: root.querySelector('[data-viewer]'),
    preview: (page, width) => `/api/v1/invoice-extractions/${state.jobId}/files/${state.viewed_file}/pages/${page}/preview?width=${width}` });
  const files = setupInvoiceFiles(root, state, render, notify);
  const results = setupInvoiceResults(root, state, navigate, notify);
  setupPrompt(notify, { root, endpoint: '/invoice-extractions/prompt' });
  /** Сохраняет только указатель job и ручную навигацию; документы/промпты остаются серверными. */
  function persist() { if (state.jobId) sessionStorage.setItem('invoice.job', JSON.stringify({ jobId: state.jobId, viewed_file: state.viewed_file, viewed_pdf_page: state.viewed_pdf_page })); }
  /** Выбирает исходный файл и страницу для проверки строки, не влияя на модель. */
  function navigate(index, page) { if (selectInvoiceFile(state, index, page)) render(); }
  /** Отображает истинный lifecycle; ни status polling, ни text_delta не меняют ручной просмотр. */
  function render() {
    const active = running(state.status?.status);
    const processing = state.processing_pdf_page;
    state.processing_pdf_page = state.viewed_file === state.processing_file ? processing : null;
    viewer.render(); state.processing_pdf_page = processing;
    files.render(); results.render(); persist();
    root.querySelector('[data-invoice-start]').disabled = busy || active;
    root.querySelector('[data-cancel]').hidden = !active;
    root.querySelector('[data-retry]').disabled = !state.jobId || active;
  }
  /** Ограничивает DOM обновления при длинном JSON-stream. */
  function schedule() { timer ??= setTimeout(() => { timer = null; render(); }, 120); }
  /** Показывает безопасную ошибку рядом с формой. */
  function error(message = '') { const target = root.querySelector('[data-invoice-error]'); target.textContent = message; target.hidden = !message; }
  /** Загружает страницы предупреждений с переходом к конкретному источнику. */
  async function warnings(reset = true) {
    if (reset) { warningOffset = 0; root.querySelector('[data-invoice-warnings]').replaceChildren(); }
    const data = await api(`/invoice-extractions/${state.jobId}/warnings?offset=${warningOffset}&limit=100`);
    for (const warning of data.warnings) {
      const entry = document.createElement('article'); entry.className = 'warnings__entry';
      const button = document.createElement('button'); button.className = 'button button--soft';
      button.textContent = warning.file_index === null ? `Пакет документов · ${warning.code}` : `Файл ${warning.file_index + 1} · лист ${warning.pdf_page ?? '—'} · ${warning.code}`;
      button.disabled = warning.file_index === null || warning.pdf_page === null;
      button.addEventListener('click', () => navigate(warning.file_index ?? 0, warning.pdf_page ?? 1));
      const text = document.createElement('p'); text.textContent = warning.message; entry.append(button, text); root.querySelector('[data-invoice-warnings]').append(entry);
    }
    warningOffset += data.warnings.length; root.querySelector('[data-invoice-warnings-more]').hidden = warningOffset >= data.total;
  }
  /** Возвращает snapshot и продолжает после его cursor, без повторного POST. */
  async function restore(id, saved = {}) {
    source?.close();
    const snapshot = await api(`/invoice-extractions/${id}/snapshot`); restoreInvoice(state, snapshot, saved); render();
    journal.replaceChildren();
    for (const file of state.status.files) for (const page of file.pages) for (const stage of page.stages) {
      const entry = document.createElement('li'); entry.textContent = `Файл ${file.file_index + 1} · лист ${page.pdf_page} · ${stage.name} · ${stage.state}${stage.duration_ms === null ? '' : ` · ${stage.duration_ms} мс`}`; journal.append(entry);
    }
    await warnings();
    if (running(state.status.status)) connect();
  }
  /** Переоткрывает SSE из последнего подтверждённого cursor после сетевого сбоя. */
  function connect() {
    source?.close(); source = eventStream(`/invoice-extractions/${state.jobId}`, state.last_event_id);
    for (const type of types) source.addEventListener(type, async event => {
      try {
        const data = JSON.parse(event.data);
        if (!applyInvoiceEvent(state, type, data, event.lastEventId)) return;
        if (type !== 'text_delta') {
          const item = document.createElement('li'); item.textContent = `${type}${data.stage ? ` · ${data.stage}` : ''} · файл ${(data.file_index ?? 0) + 1}, лист ${data.pdf_page ?? '—'}`; journal.append(item);
          while (journal.children.length > 200) journal.firstChild.remove();
        }
        schedule();
        if (['job_completed', 'job_failed', 'job_cancelled'].includes(type)) { source.close(); await restore(state.jobId, state); }
        else if (type === 'page_completed' && !refreshTimer) refreshTimer = setTimeout(async () => {
          refreshTimer = null;
          try {
            const status = await api(`/invoice-extractions/${state.jobId}`);
            if (status.revision >= state.revision && status.last_event_id >= state.last_event_id) state.status = status;
            render();
          } catch (failure) { notify(failure.message); }
        }, 200);
      } catch (failure) { notify(failure.message); }
    });
    source.addEventListener('access_expired', () => { source.close(); error('Срок доступа истёк. Загрузите документы повторно.'); });
    source.onerror = () => { error('Связь с потоком прервана. Переподключение…'); };
    source.onopen = () => error();
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || running(state.status?.status)) return;
    try {
      error(); const body = files.payload(); busy = true; render();
      const status = await api('/invoice-extractions', { method: 'POST', body, headers: { 'Idempotency-Key': files.key() } });
      files.uploaded(); journal.replaceChildren(); await restore(status.job_id);
    } catch (failure) { error(failure.message); } finally { busy = false; render(); }
  });
  root.querySelector('[data-cancel]').addEventListener('click', async () => {
    try { state.status = await api(`/invoice-extractions/${state.jobId}/cancel`, { method: 'POST' }); render(); } catch (failure) { notify(failure.message); }
  });
  root.querySelector('[data-retry]').addEventListener('click', async () => {
    try { await api(`/invoice-extractions/${state.jobId}/files/${state.viewed_file}/pages/${state.viewed_pdf_page}/retry`, { method: 'POST' }); await restore(state.jobId, state); } catch (failure) { notify(failure.message); }
  });
  root.querySelector('[data-invoice-warnings-more]').addEventListener('click', () => warnings(false).catch(failure => notify(failure.message)));
  const saved = JSON.parse(sessionStorage.getItem('invoice.job') || 'null');
  if (saved?.jobId) restore(saved.jobId, saved).catch(failure => { sessionStorage.removeItem('invoice.job'); error(failure.message); });
  render();
  return { state, render, stop: () => { source?.close(); clearTimeout(timer); clearTimeout(refreshTimer); } };
}

/** Лениво подключает профиль, сохраняя оба задания при переключении вкладок. */
export function setupProfiles(notify) {
  const invoice = document.querySelector('[data-invoice-profile]'); const spec = document.querySelector('[data-spec-profile]');
  let controller;
  /** Меняет только видимую вкладку; обработка и prompts независимы. */
  function activate(profile) {
    if (profile === 'invoice' && !controller) { invoice.append(document.querySelector('[data-invoice-template]').content.cloneNode(true)); controller = setupInvoices(invoice, notify); }
    invoice.hidden = profile !== 'invoice'; spec.hidden = profile !== 'spec';
    document.querySelectorAll('[data-profile]').forEach(button => { const active = button.dataset.profile === profile; button.setAttribute('aria-pressed', String(active)); button.classList.toggle('button--primary', active); });
    sessionStorage.setItem('extract.profile', profile); controller?.render();
  }
  document.querySelectorAll('[data-profile]').forEach(button => { button.addEventListener('click', () => activate(button.dataset.profile)); button.dataset.profileReady = 'true'; });
  if (sessionStorage.getItem('extract.profile') === 'invoice') activate('invoice');
  return { reset: () => { controller?.stop(); sessionStorage.removeItem('invoice.job'); if (controller) { controller = null; invoice.replaceChildren(); } activate('spec'); } };
}
