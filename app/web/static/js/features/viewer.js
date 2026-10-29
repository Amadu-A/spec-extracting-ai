// app/web/static/js/features/viewer.js
/** Просмотр настоящих PNG: масштаб, ручная навигация, lazy thumbnails, pan/fullscreen. */

/** Подключает controls PDF-вьюера к общей модели состояния. */
export function setupViewer(state, changed, notify) {
  const root = document.querySelector('[data-viewer]');
  const image = root.querySelector('[data-page-image]');
  const canvas = root.querySelector('[data-canvas]');
  const strip = root.querySelector('[data-thumbnails]');
  let zoom = 1; let fit = true; let pan = false; let renderedDocument = null; let lastUrl = ''; let renderedPage = null;
  let origin = null;
  /** Разрешает preview URL только для текущего серверного документа. */
  function preview(page, width) { return `/api/v1/documents/${state.document.id}/pages/${page}/preview?width=${width}`; }
  /** Применяет масштаб через CSSOM, не перестраивая и не загружая весь PDF. */
  function applyZoom() {
    canvas.classList.toggle('viewer__canvas--fit', fit);
    image.style.width = fit ? '' : `${zoom * 100}%`;
    root.querySelector('[data-zoom]').textContent = `${Math.round(zoom * 100)}%`;
  }
  /** Выбирает физический PDF-лист, не меняя processing_pdf_page модели. */
  function navigate(page) {
    if (!state.document) return;
    if (!Number.isInteger(page) || page < 1 || page > state.document.page_count) {
      root.querySelector('[data-page-number]').value = state.viewed_pdf_page;
      notify(`Допустимы PDF-страницы 1–${state.document.page_count}`); return;
    }
    state.viewed_pdf_page = page;
    render(); changed();
  }
  root.querySelector('[data-page-prev]').addEventListener('click', () => navigate(state.viewed_pdf_page - 1));
  root.querySelector('[data-page-next]').addEventListener('click', () => navigate(state.viewed_pdf_page + 1));
  root.querySelector('[data-page-number]').addEventListener('change', (event) => navigate(Number(event.target.value)));
  root.querySelector('[data-zoom-in]').addEventListener('click', () => { fit = false; zoom = Math.min(4, zoom + .25); applyZoom(); });
  root.querySelector('[data-zoom-out]').addEventListener('click', () => { fit = false; zoom = Math.max(.25, zoom - .25); applyZoom(); });
  root.querySelector('[data-fit]').addEventListener('click', () => { fit = !fit; zoom = 1; applyZoom(); });
  root.querySelector('[data-fullscreen]').addEventListener('click', async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await root.requestFullscreen(); }
    catch { notify('Браузер не разрешил полноэкранный режим'); }
  });
  root.querySelector('[data-hand]').addEventListener('click', (event) => {
    pan = !pan; canvas.classList.toggle('viewer__canvas--hand', pan); event.currentTarget.setAttribute('aria-pressed', String(pan));
  });
  canvas.addEventListener('pointerdown', (event) => {
    if (!pan) return;
    origin = { x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop };
    canvas.setPointerCapture(event.pointerId); event.preventDefault();
  });
  canvas.addEventListener('pointermove', (event) => {
    if (origin) { canvas.scrollLeft = origin.left - event.clientX + origin.x; canvas.scrollTop = origin.top - event.clientY + origin.y; }
  });
  canvas.addEventListener('pointerup', () => { origin = null; });
  canvas.addEventListener('pointercancel', () => { origin = null; });
  image.addEventListener('error', () => { root.querySelector('[data-preview-error]').hidden = false; root.querySelector('[data-preview-error]').textContent = 'Не удалось загрузить PDF-лист. Проверьте сессию и доступность сервиса.'; });
  image.addEventListener('load', () => { root.querySelector('[data-preview-error]').hidden = true; });
  /** Обновляет только нужную страницу; tokens/reconnect не сбрасывают позицию просмотра. */
  function render() {
    const doc = state.document;
    image.hidden = !doc;
    root.querySelector('[data-viewer-empty]').hidden = Boolean(doc);
    root.querySelector('[data-page-total]').textContent = `из ${doc?.page_count || 0}`;
    const pageNumber = root.querySelector('[data-page-number]');
    // Токены/status не стирают ещё не подтверждённый ввод пользователя.
    if (document.activeElement !== pageNumber || renderedPage !== state.viewed_pdf_page) pageNumber.value = state.viewed_pdf_page;
    renderedPage = state.viewed_pdf_page;
    root.querySelector('[data-page-number]').max = doc?.page_count || 1;
    root.querySelector('[data-page-prev]').disabled = !doc || state.viewed_pdf_page <= 1;
    root.querySelector('[data-page-next]').disabled = !doc || state.viewed_pdf_page >= doc.page_count;
    if (!doc) { strip.replaceChildren(); renderedDocument = null; lastUrl = ''; return; }
    const url = preview(state.viewed_pdf_page, 1800);
    if (url !== lastUrl) {
      image.src = url; image.alt = `PDF-страница ${state.viewed_pdf_page} · ${doc.filename}`;
      lastUrl = url; canvas.scrollTop = 0; canvas.scrollLeft = 0;
    }
    if (renderedDocument !== doc.id) {
      strip.replaceChildren();
      for (let page = 1; page <= doc.page_count; page++) {
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'thumbnails__button'; button.dataset.pdfPage = page;
        button.setAttribute('aria-label', `Открыть PDF-страницу ${page}`);
        const thumbnail = document.createElement('img'); thumbnail.className = 'thumbnails__image';
        thumbnail.alt = ''; thumbnail.loading = 'lazy'; thumbnail.src = preview(page, 140);
        button.append(thumbnail, document.createTextNode(String(page)));
        button.addEventListener('click', () => navigate(page)); strip.append(button);
      }
      renderedDocument = doc.id;
    }
    for (const button of strip.children) {
      const active = Number(button.dataset.pdfPage) === state.viewed_pdf_page;
      button.classList.toggle('thumbnails__button--active', active);
      button.classList.toggle('thumbnails__button--processing', Number(button.dataset.pdfPage) === state.processing_pdf_page);
      button.setAttribute('aria-current', String(active));
      if (active && lastUrl === url) {
        const left = button.offsetLeft - strip.offsetLeft;
        if (left < strip.scrollLeft || left + button.offsetWidth > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = left - strip.clientWidth / 2;
      }
    }
    applyZoom();
  }
  return { render, navigate };
}
