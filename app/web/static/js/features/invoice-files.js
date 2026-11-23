// app/web/static/js/features/invoice-files.js
/** Мультизагрузка, отдельные диапазоны страниц и переключение источника viewer. */
import { running } from '../state.js';
import { idempotencyKey } from '../api.js';
import { selectInvoiceFile } from '../invoice-state.js';

/** Подключает dialog/drop к одному набору файлов; не отправляет их в сторонние сервисы. */
export function setupInvoiceFiles(root, state, changed, notify) {
  const input = root.querySelector('[data-invoice-files]');
  const drop = root.querySelector('[data-invoice-drop]');
  const list = root.querySelector('[data-invoice-file-list]');
  const maximum = Number(root.querySelector('[data-invoice-form]').dataset.maxFiles);
  let selected = []; let filters = {}; let signature = ''; let requestKey = null;
  /** Меняет подготовленный batch, сохраняя уже выполняющееся задание до нового запуска. */
  function choose(files) {
    if (running(state.status?.status)) return;
    const next = Array.from(files);
    if (!next.length || next.length > maximum) { notify(`Выберите от 1 до ${maximum} PDF/JPEG/PNG`); return; }
    selected = next; filters = {}; requestKey = idempotencyKey(); render();
  }
  input.addEventListener('change', () => choose(input.files));
  drop.addEventListener('dragover', event => { event.preventDefault(); drop.classList.add('invoice-upload__drop--active'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('invoice-upload__drop--active'));
  drop.addEventListener('drop', event => { event.preventDefault(); drop.classList.remove('invoice-upload__drop--active'); choose(event.dataTransfer.files); });
  /** Показывает исходные имена как текст и адресует страницы индексом конкретного файла. */
  function render() {
    const active = running(state.status?.status);
    input.disabled = active;
    const files = selected.length && !active ? selected.map((f, i) => ({ source_file: f.name, file_index: i })) : state.status?.files || [];
    const nextSignature = JSON.stringify([files, filters, active, state.viewed_file, selected.length]);
    if (signature === nextSignature) return;
    signature = nextSignature;
    list.replaceChildren();
    for (const file of files) {
      const index = file.file_index;
      const entry = document.createElement('article'); entry.className = 'invoice-files__entry';
      entry.classList.toggle('invoice-files__entry--active', index === state.viewed_file);
      const button = document.createElement('button'); button.type = 'button'; button.className = 'button button--soft'; button.textContent = file.source_file;
      button.title = file.source_file; button.disabled = !state.status?.files[index] || Boolean(selected.length && !active);
      button.addEventListener('click', () => { selectInvoiceFile(state, index); changed(); });
      const label = document.createElement('label'); label.textContent = `Страницы файла ${index + 1} (пусто — все)`;
      const pages = document.createElement('input'); pages.type = 'text'; pages.placeholder = 'Все страницы'; pages.dataset.invoicePages = index;
      pages.value = filters[index] ?? file.selected_pages?.join(', ') ?? ''; pages.disabled = active || !selected.length;
      pages.addEventListener('input', () => { filters[index] = pages.value; requestKey = idempotencyKey(); }); label.append(pages);
      const info = document.createElement('small'); info.textContent = file.page_count ? `${file.page_count} стр. · ${file.pages.filter(p => p.state === 'completed').length} обработано` : 'Готов к загрузке';
      entry.append(button, label, info); list.append(entry);
    }
  }
  /** Формирует multipart с одним полем files на каждый источник и явной картой диапазонов. */
  function payload() {
    if (!selected.length) throw new Error('Сначала выберите PDF/JPEG/PNG');
    const body = new FormData(); selected.forEach(file => body.append('files', file));
    body.append('page_selection', JSON.stringify(Object.fromEntries(Object.entries(filters).filter(([, value]) => value.trim()))));
    return body;
  }
  /** После admission источником истины становится durable status сервера. */
  function uploaded() { selected = []; filters = {}; requestKey = null; input.value = ''; }
  return { render, payload, uploaded, key: () => requestKey };
}
