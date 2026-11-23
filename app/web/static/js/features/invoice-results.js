// app/web/static/js/features/invoice-results.js
/** Безопасная финансовая сводка по документам, позиции и диагностический raw stream. */
import { copyText, download } from '../api.js';
import { completed } from '../state.js';

const statuses = { queued: 'В очереди', running: 'Идёт анализ', completed: 'Анализ завершён', completed_with_warnings: 'Завершён с предупреждениями', failed: 'Ошибка анализа', cancelled: 'Анализ отменён' };
const kinds = { invoice: 'Счёт', commercial_offer: 'Коммерческое предложение', specification: 'Спецификация', unknown: 'Вид не определён' };
const checks = { passed: 'Проверено', partially_checked: 'Частично проверено', failed: 'Расхождение', not_checkable: 'Недостаточно данных' };

/** Строит независимую таблицу: итоги разных контрагентов не складываются. */
export function setupInvoiceResults(root, state, navigate, notify) {
  const body = root.querySelector('[data-invoice-documents]');
  const items = root.querySelector('[data-invoice-items]');
  let previousResult = null;
  root.querySelectorAll('[data-invoice-export]').forEach(button => button.addEventListener('click', async () => {
    try { await download(state.status.links[button.dataset.invoiceExport], button.dataset.invoiceExport, 'invoices'); }
    catch (failure) { notify(failure.message); }
  }));
  root.querySelector('[data-invoice-copy]').addEventListener('click', () => copyText(JSON.stringify(state.result, null, 2)).then(() => notify('Подтверждённый результат скопирован')).catch(failure => notify(failure.message)));
  /** Обновляет counts/raw без пересоздания большой таблицы на каждый токен. */
  function render() {
    const status = state.status;
    root.querySelector('[data-invoice-status]').textContent = status ? `${statuses[status.status]}${status.error_code ? ` · ${status.error_code}` : ''}` : 'Ожидание файлов';
    root.querySelector('[data-invoice-status]').dataset.status = status?.status || '';
    root.querySelector('[data-invoice-metrics]').textContent = `Документов: ${status?.document_count || 0} · строк: ${status?.item_count || 0} · прогресс: ${status?.progress || 0}% · предупреждений: ${status?.warning_count || 0}`;
    root.querySelector('[data-invoice-document-count]').textContent = status?.document_count || 0;
    root.querySelector('[data-invoice-item-count]').textContent = status?.item_count || 0;
    root.querySelector('[data-invoice-progress]').textContent = `${status?.progress || 0}%`;
    root.querySelector('[data-invoice-warning-count]').textContent = status?.warning_count || 0;
    root.querySelector('[data-processing-label]').textContent = state.processing_file === null || state.processing_pdf_page === null ? 'Нет обработки' : `Модель: файл ${state.processing_file + 1}, лист ${state.processing_pdf_page}`;
    root.querySelector('[data-selection-label]').textContent = status ? `Хранится до ${new Date(status.expires_at).toLocaleString('ru-RU')}` : 'Все страницы каждого файла по умолчанию';
    const ready = completed(status?.status) && Boolean(state.result);
    root.querySelectorAll('[data-invoice-export]').forEach(button => { button.disabled = !ready; });
    root.querySelector('[data-invoice-copy]').disabled = !ready;
    root.querySelector('[data-invoice-code]').textContent = state.raw || (state.result ? JSON.stringify(state.result, null, 2) : 'Ожидание ответа shared-vlm.');
    if (state.result === previousResult) return;
    previousResult = state.result; body.replaceChildren(); items.replaceChildren();
    for (const file of state.result?.files || []) for (const [index, doc] of file.documents.entries()) {
      const row = document.createElement('tr');
      const values = [`${file.source_file}\n${kinds[doc.document_type]} № ${doc.number ?? '—'}`,
        `${doc.supplier.name ?? '—'}\n${doc.buyer.name ?? '—'}`, `${doc.date ?? '—'}\n${doc.items.length} строк`,
        `${doc.totals.without_vat ?? '—'} / ${doc.totals.vat ?? '—'} / ${doc.totals.with_vat ?? '—'} ${doc.currency ?? ''}`, checks[doc.validation.status]];
      for (const [i, value] of values.entries()) {
        const cell = document.createElement('td'); cell.textContent = value;
        if (i === 4) cell.className = `invoice-table__check--${doc.validation.status}`;
        row.append(cell);
      }
      row.title = doc.validation.warnings.join('\n'); body.append(row);
      for (const item of doc.items) {
        const entry = document.createElement('li');
        const button = document.createElement('button'); button.className = 'button button--soft';
        button.textContent = `Файл ${file.file_index + 1} · документ ${index + 1} · строка ${item.row_number} · лист ${item.source_pages.join(', ')}`;
        button.addEventListener('click', () => { navigate(file.file_index, item.source_pages[0]); root.querySelectorAll('[data-source-selected]').forEach(e => e.removeAttribute('data-source-selected')); entry.dataset.sourceSelected = 'true'; });
        const text = document.createElement('p'); text.textContent = `${item.caption}\nАртикул: ${item.article ?? '—'} · код поставщика: ${item.supplier_code ?? '—'}\n${item.quantity ?? '—'} ${item.units ?? ''} · сумма с НДС: ${item.line_total_with_vat ?? '—'}`;
        entry.append(button, text); items.append(entry);
      }
    }
  }
  return { render };
}
