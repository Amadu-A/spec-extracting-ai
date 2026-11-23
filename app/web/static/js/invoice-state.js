// app/web/static/js/invoice-state.js
/** Чистое состояние счетов: файлы и страницы не смешиваются с профилем спецификаций. */

/** Создаёт независимый profile state без секретов и содержимого исходных файлов в storage. */
export function invoiceState() {
  return { jobId: null, status: null, result: null, raw: '', last_event_id: 0, revision: 0,
    viewed_file: 0, viewed_pdf_page: 1, processing_file: null, processing_pdf_page: null, document: null, processingKey: null };
}

/** Готовит общий viewer к выбранному источнику, не меняя текущую страницу worker. */
export function selectInvoiceFile(state, index, page = 1) {
  const file = state.status?.files[index];
  if (!file) return false;
  state.viewed_file = index; state.viewed_pdf_page = Math.max(1, Math.min(page, file.page_count));
  state.document = { id: `${state.jobId}:${index}`, filename: file.source_file, page_count: file.page_count };
  return true;
}

/** Применяет события один раз; ручной просмотр сохраняется до следующего перехода модели. */
export function applyInvoiceEvent(state, type, data, id) {
  const sequence = Number(id);
  if (sequence <= state.last_event_id || data.revision < state.revision) return false;
  state.last_event_id = sequence;
  if (data.revision > state.revision) {
    state.revision = data.revision; state.result = null; state.raw = ''; state.processingKey = null;
  }
  if (type === 'page_started') {
    const key = `${data.file_index}:${data.pdf_page}`;
    state.processing_file = data.file_index; state.processing_pdf_page = data.pdf_page;
    if (key !== state.processingKey) selectInvoiceFile(state, data.file_index, data.pdf_page);
    state.processingKey = key;
  }
  if (type === 'stream_reset') state.raw = '';
  if (state.status && ['job_started', 'page_started'].includes(type)) state.status.status = 'running';
  if (state.status && ['job_completed', 'job_failed', 'job_cancelled'].includes(type)) state.status.status = data.status || type.slice(4);
  if (type === 'text_delta') state.raw = (state.raw + data.text).slice(-2000000);
  if (type === 'result_invalidated') state.result = null;
  return true;
}

/** Восстанавливает серверную ревизию и только действительные ручные координаты просмотра. */
export function restoreInvoice(state, snapshot, saved = {}) {
  state.status = snapshot.status; state.jobId = snapshot.status.job_id;
  state.result = snapshot.result; state.raw = snapshot.raw_text; state.last_event_id = snapshot.last_event_id;
  state.revision = snapshot.status.revision;
  state.processing_file = snapshot.status.current_file_index; state.processing_pdf_page = snapshot.status.current_pdf_page;
  state.processingKey = state.processing_file === null ? null : `${state.processing_file}:${state.processing_pdf_page}`;
  const index = saved.viewed_file ?? state.processing_file ?? 0;
  if (!selectInvoiceFile(state, index, saved.viewed_pdf_page ?? state.processing_pdf_page ?? 1)) selectInvoiceFile(state, 0);
}
