from __future__ import annotations

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
import tempfile
from typing import Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import quote
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from socketserver import ThreadingMixIn

from core.anonymizer import anonymize_text
from core.database import (
    DEFAULT_DB_PATH,
    DatabaseError,
    DocumentNotFoundError,
    init_db,
    load_mapping,
    new_document_id,
    save_document,
)
from core.detection import detect_entities
from core.document_lookup import AmbiguousDocumentError, resolve_document_id
from core.detector import DetectorError, OllamaDetector
from core.heuristics import require_no_obvious_entities
from core.office import (
    OfficeAnonymizationError,
    anonymize_office_file,
    extract_office_text,
    is_supported_office_file,
    restore_docx_file,
    write_document_id,
)
from core.restore import RestoreError, restore_text


MAX_UPLOAD_BYTES = 100 * 1024 * 1024
ANONYMIZE_SUFFIXES = {".txt", ".docx", ".xlsx", ".pptx"}
RESTORE_SUFFIXES = {".txt", ".docx"}
SAFE_SUFFIX_RE = re.compile(r"_SAFE(?:_[0-9A-Fa-f]{8})?")
CONTENT_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class WebRequestError(RuntimeError):
    """Raised when a local UI request is incomplete or unsupported."""


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class FormData:
    files: Dict[str, UploadedFile]
    fields: Dict[str, str]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class GatewayWebApp:
    def __init__(
        self,
        detector_factory: Optional[Callable[[], object]] = None,
        chunk_lines: Optional[int] = None,
        db_path: Path = DEFAULT_DB_PATH,
    ) -> None:
        self.detector_factory = detector_factory or (lambda: OllamaDetector())
        self.chunk_lines = chunk_lines
        self.db_path = db_path
        init_db(self.db_path)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        try:
            if method == "GET" and path == "/":
                return _respond(start_response, "200 OK", PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if method == "GET" and path == "/health":
                return _json_response(start_response, "200 OK", {"status": "ok", "local": True})
            if method == "POST" and path == "/anonymize":
                return self._anonymize(environ, start_response)
            if method == "POST" and path == "/restore":
                return self._restore(environ, start_response)
            return _json_response(start_response, "404 Not Found", {"error": "Страница не найдена."})
        except WebRequestError as exc:
            return _json_response(start_response, "400 Bad Request", {"error": str(exc)})
        except RestoreError as exc:
            return _json_response(start_response, "422 Unprocessable Entity", {"error": str(exc)})
        except AmbiguousDocumentError as exc:
            return _json_response(
                start_response,
                "409 Conflict",
                {
                    "error": "Файлу подходят несколько документов из базы. Выберите нужный.",
                    "candidates": [
                        {
                            "id": candidate.id,
                            "source_name": candidate.source_name,
                            "created_at": candidate.created_at,
                        }
                        for candidate in exc.candidates
                    ],
                },
            )
        except DocumentNotFoundError:
            return _json_response(
                start_response,
                "404 Not Found",
                {"error": "Для этого файла не найден документ в базе. Загрузите SAFE-файл, полученный из этой программы."},
            )
        except DetectorError:
            return _json_response(
                start_response,
                "502 Bad Gateway",
                {"error": "Локальная модель не завершила проверку. Выходной файл не создан."},
            )
        except (OSError, UnicodeDecodeError, DatabaseError, OfficeAnonymizationError):
            return _json_response(
                start_response,
                "422 Unprocessable Entity",
                {"error": "Файл не удалось безопасно обработать. Исходное содержимое не выводилось."},
            )
        except Exception:
            return _json_response(
                start_response,
                "500 Internal Server Error",
                {"error": "Внутренняя ошибка. Выходной файл не создан."},
            )

    def _anonymize(self, environ, start_response):
        form = _parse_form(environ)
        document = _required_upload(form, "document")
        source_name = _safe_filename(document.filename)
        suffix = Path(source_name).suffix.lower()
        if suffix not in ANONYMIZE_SUFFIXES:
            raise WebRequestError("Поддерживаются TXT, DOCX, XLSX и PPTX.")

        document_id = new_document_id()
        with tempfile.TemporaryDirectory(prefix="secure_gateway_") as tmp:
            workspace = Path(tmp)
            input_path = workspace / source_name
            input_path.write_bytes(document.data)
            text = extract_office_text(input_path) if is_supported_office_file(input_path) else input_path.read_text(encoding="utf-8")
            detector = self.detector_factory()
            entities = detect_entities(detector, text, self.chunk_lines)
            result = anonymize_text(text, entities, source_name=source_name)
            if text.strip() and not result.mapping.entities:
                raise DetectorError("detector returned no replaceable entities")
            require_no_obvious_entities(result.text)

            safe_path = workspace / f"{input_path.stem}_SAFE_{document_id}{input_path.suffix or '.txt'}"
            if is_supported_office_file(input_path):
                anonymize_office_file(input_path, safe_path, result.mapping)
                write_document_id(safe_path, document_id)
            else:
                safe_path.write_text(result.text, encoding="utf-8")
            safe_data = safe_path.read_bytes()
            save_document(document_id, result.mapping, self.db_path)

        return _download_response(
            start_response,
            safe_data,
            safe_path.name,
            CONTENT_TYPES.get(suffix, "application/octet-stream"),
            extra_headers=(("X-Document-Id", document_id),),
        )

    def _restore(self, environ, start_response):
        form = _parse_form(environ)
        document = _required_upload(form, "document")
        chosen_id = form.fields.get("document_id", "").strip() or None

        source_name = _safe_filename(document.filename)
        suffix = Path(source_name).suffix.lower()
        if suffix not in RESTORE_SUFFIXES:
            raise WebRequestError("Восстановление сейчас поддерживает TXT и DOCX.")

        with tempfile.TemporaryDirectory(prefix="secure_gateway_") as tmp:
            workspace = Path(tmp)
            input_path = workspace / source_name
            input_path.write_bytes(document.data)
            text = extract_office_text(input_path) if suffix == ".docx" else input_path.read_text(encoding="utf-8")

            document_id = resolve_document_id(input_path, text, self.db_path, explicit_id=chosen_id)
            mapping = load_mapping(document_id, self.db_path)
            final_path = workspace / f"{_strip_safe_suffix(input_path.stem)}_FINAL{input_path.suffix or '.txt'}"

            if suffix == ".docx":
                restore_docx_file(input_path, final_path, mapping)
            else:
                result = restore_text(text, mapping)
                final_path.write_text(result.text, encoding="utf-8")
            final_data = final_path.read_bytes()

        return _download_response(
            start_response,
            final_data,
            final_path.name,
            CONTENT_TYPES[suffix],
            extra_headers=(("X-Document-Id", document_id),),
        )


def serve(host: str, port: int, application: GatewayWebApp) -> None:
    with make_server(
        host,
        port,
        application,
        server_class=ThreadingWSGIServer,
        handler_class=WSGIRequestHandler,
    ) as server:
        server.serve_forever()


def _parse_form(environ) -> FormData:
    content_type = environ.get("CONTENT_TYPE", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise WebRequestError("Ожидалась форма с файлами.")

    try:
        content_length = int(environ.get("CONTENT_LENGTH", "0"))
    except ValueError as exc:
        raise WebRequestError("Некорректный размер запроса.") from exc
    if content_length < 1:
        raise WebRequestError("Файлы не выбраны.")
    if content_length > MAX_UPLOAD_BYTES:
        raise WebRequestError("Размер загрузки превышает 100 МБ.")

    body = environ["wsgi.input"].read(content_length)
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    if not message.is_multipart():
        raise WebRequestError("Не удалось прочитать форму загрузки.")

    files: Dict[str, UploadedFile] = {}
    fields: Dict[str, str] = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = part.get_param("name", header="content-disposition")
        if not field_name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[field_name] = UploadedFile(
                filename=filename,
                content_type=part.get_content_type(),
                data=payload,
            )
        else:
            fields[field_name] = payload.decode("utf-8", errors="replace")
    return FormData(files=files, fields=fields)


def _required_upload(form: FormData, field_name: str) -> UploadedFile:
    upload = form.files.get(field_name)
    if upload is None or not upload.data:
        raise WebRequestError("Выберите все необходимые файлы.")
    return upload


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/")
    safe_name = Path(normalized).name
    if not safe_name or safe_name in {".", ".."}:
        raise WebRequestError("Некорректное имя файла.")
    return safe_name


def _strip_safe_suffix(stem: str) -> str:
    return SAFE_SUFFIX_RE.sub("", stem)


def _respond(start_response, status: str, body: bytes, content_type: str, extra_headers: Iterable[Tuple[str, str]] = ()):
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
        ("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"),
    ]
    headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


def _json_response(start_response, status: str, payload: Dict[str, object]):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _respond(start_response, status, body, "application/json; charset=utf-8")


def _download_response(
    start_response,
    data: bytes,
    filename: str,
    content_type: str,
    extra_headers: Iterable[Tuple[str, str]] = (),
):
    ascii_name = "".join(character if ord(character) < 128 and character not in {'"', '\\'} else "_" for character in filename)
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return _respond(
        start_response,
        "200 OK",
        data,
        content_type,
        extra_headers=(("Content-Disposition", disposition), *extra_headers),
    )


PAGE_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Secure AI Document Gateway</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172033;
      --muted: #657087;
      --line: #dbe2ec;
      --paper: #ffffff;
      --canvas: #f2f5f9;
      --blue: #215cc7;
      --blue-dark: #17458f;
      --green: #177d55;
      --green-soft: #e8f6ef;
      --danger: #a52a2a;
      --shadow: 0 18px 50px rgba(30, 48, 78, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 0%, rgba(33, 92, 199, .09), transparent 34rem),
        var(--canvas);
      font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell { width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 56px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 24px; margin-bottom: 48px; }
    .brand { display: flex; align-items: center; gap: 12px; font-weight: 750; letter-spacing: -.02em; }
    .brand-mark { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: white; background: linear-gradient(135deg, var(--blue), #5688e2); box-shadow: 0 8px 18px rgba(33,92,199,.25); }
    .local-badge { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border: 1px solid #bfe3d2; border-radius: 999px; background: var(--green-soft); color: #126441; font-size: 13px; font-weight: 700; }
    .local-badge::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: #23a66f; box-shadow: 0 0 0 4px rgba(35,166,111,.12); }
    .hero { max-width: 780px; margin-bottom: 32px; }
    .eyebrow { color: var(--blue); font-size: 13px; font-weight: 800; letter-spacing: .09em; text-transform: uppercase; }
    h1 { margin: 10px 0 14px; font-size: clamp(34px, 6vw, 58px); line-height: 1.02; letter-spacing: -.045em; }
    .lead { margin: 0; max-width: 680px; color: var(--muted); font-size: 18px; line-height: 1.6; }
    .workflow { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
    .card { background: rgba(255,255,255,.94); border: 1px solid rgba(205,215,229,.9); border-radius: 24px; padding: 28px; box-shadow: var(--shadow); }
    .step { display: inline-grid; place-items: center; width: 30px; height: 30px; margin-bottom: 18px; border-radius: 9px; background: #eaf0fc; color: var(--blue); font-weight: 800; font-size: 14px; }
    h2 { margin: 0 0 8px; font-size: 24px; letter-spacing: -.025em; }
    .card-copy { min-height: 48px; margin: 0 0 22px; color: var(--muted); line-height: 1.5; }
    .drop-zone { display: block; position: relative; padding: 24px 18px; border: 1.5px dashed #aebbd0; border-radius: 16px; background: #f8faff; text-align: center; cursor: pointer; transition: .18s ease; }
    .drop-zone:hover, .drop-zone.dragover { border-color: var(--blue); background: #eef4ff; transform: translateY(-1px); }
    .drop-zone input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
    .drop-title { display: block; font-weight: 750; }
    .file-name { display: block; min-height: 20px; margin-top: 6px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .formats { margin: 14px 0 0; color: var(--muted); font-size: 12px; }
    .choices { display: grid; gap: 8px; margin-top: 12px; }
    .choices button { margin-top: 0; padding: 10px 14px; text-align: left; color: var(--ink); background: #eef4ff; border: 1px solid #c9d6ee; font-weight: 600; }
    .choices button:hover { background: #e2ecff; }
    .choices small { display: block; color: var(--muted); font-weight: 400; }
    button { width: 100%; margin-top: 18px; padding: 13px 18px; border: 0; border-radius: 13px; color: white; background: var(--blue); font: inherit; font-weight: 750; cursor: pointer; transition: .18s ease; }
    button:hover { background: var(--blue-dark); transform: translateY(-1px); }
    button:disabled { cursor: wait; opacity: .65; transform: none; }
    .status { min-height: 22px; margin-top: 12px; color: var(--muted); font-size: 13px; line-height: 1.4; }
    .status.error { color: var(--danger); }
    .status.success { color: var(--green); }
    .status strong { font-size: 15px; letter-spacing: .06em; }
    .privacy { display: grid; grid-template-columns: auto 1fr; gap: 14px; align-items: start; margin-top: 24px; padding: 18px 20px; border: 1px solid var(--line); border-radius: 16px; background: rgba(255,255,255,.72); color: var(--muted); line-height: 1.5; }
    .privacy strong { color: var(--ink); }
    .lock { font-size: 20px; }
    @media (max-width: 760px) {
      .shell { width: min(100% - 22px, 620px); padding-top: 20px; }
      .topbar { margin-bottom: 36px; align-items: flex-start; }
      .brand span:last-child { max-width: 180px; }
      .workflow { grid-template-columns: 1fr; }
      .card-copy { min-height: 0; }
      .local-badge { white-space: nowrap; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark">SG</span><span>Secure AI Document Gateway</span></div>
      <span class="local-badge">Только 127.0.0.1</span>
    </header>

    <section class="hero">
      <div class="eyebrow">Локальная обработка документов</div>
      <h1>Безопасная копия для работы с AI</h1>
      <p class="lead">Найдите чувствительные данные локальной моделью, замените их понятными токенами и восстановите документ после обработки. Убранные значения хранятся в локальной базе.</p>
    </section>

    <section class="workflow" aria-label="Операции с документами">
      <form class="card" data-endpoint="/anonymize">
        <span class="step">01</span>
        <h2>Создать безопасную копию</h2>
        <p class="card-copy">Загрузите рабочий документ. Скачается SAFE-файл, а убранные значения сохранятся в локальной базе под номером документа.</p>
        <label class="drop-zone">
          <input type="file" name="document" accept=".txt,.docx,.xlsx,.pptx" required>
          <span class="drop-title">Выберите или перетащите документ</span>
          <span class="file-name">Файл не выбран</span>
        </label>
        <p class="formats">TXT, DOCX, XLSX, PPTX · до 100 МБ</p>
        <button type="submit">Обезличить и скачать</button>
        <div class="status" role="status" aria-live="polite"></div>
      </form>

      <form class="card" data-endpoint="/restore">
        <span class="step">02</span>
        <h2>Восстановить DOCX</h2>
        <p class="card-copy">Загрузите обезличенный файл или ответ AI. Документ и его значения будут найдены в локальной базе автоматически.</p>
        <label class="drop-zone">
          <input type="file" name="document" accept=".txt,.docx" required>
          <span class="drop-title">SAFE-документ</span>
          <span class="file-name">Файл не выбран</span>
        </label>
        <input type="hidden" name="document_id" value="">
        <div class="choices" hidden></div>
        <button type="submit">Восстановить и скачать</button>
        <div class="status" role="status" aria-live="polite"></div>
      </form>
    </section>

    <aside class="privacy">
      <span class="lock" aria-hidden="true">🔒</span>
      <div><strong>Файлы и база остаются на этом компьютере.</strong> Интерфейс слушает только локальный адрес. Во внешнюю AI-систему передавайте только SAFE-файл.</div>
    </aside>
  </main>
  <script>
    const filenameFromResponse = (response, fallback) => {
      const value = response.headers.get("Content-Disposition") || "";
      const utf = value.match(/filename\\*=UTF-8''([^;]+)/i);
      if (utf) return decodeURIComponent(utf[1]);
      const plain = value.match(/filename="([^"]+)"/i);
      return plain ? plain[1] : fallback;
    };

    const resetChoice = form => {
      const idField = form.querySelector('input[name=document_id]');
      const choices = form.querySelector('.choices');
      if (idField) idField.value = '';
      if (choices) { choices.hidden = true; choices.replaceChildren(); }
    };

    const showChoices = (form, candidates) => {
      const choices = form.querySelector('.choices');
      const idField = form.querySelector('input[name=document_id]');
      choices.replaceChildren();
      candidates.forEach(candidate => {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = candidate.source_name;
        const details = document.createElement('small');
        details.textContent = `№ ${candidate.id} · ${candidate.created_at}`;
        button.appendChild(details);
        button.addEventListener('click', () => {
          idField.value = candidate.id;
          form.requestSubmit();
        });
        choices.appendChild(button);
      });
      choices.hidden = false;
    };

    document.querySelectorAll('.drop-zone').forEach(zone => {
      const input = zone.querySelector('input[type=file]');
      const label = zone.querySelector('.file-name');
      const form = zone.closest('form');
      const sync = () => {
        label.textContent = input.files[0]?.name || 'Файл не выбран';
        resetChoice(form);
      };
      input.addEventListener('change', sync);
      zone.addEventListener('dragover', event => { event.preventDefault(); zone.classList.add('dragover'); });
      zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
      zone.addEventListener('drop', event => {
        event.preventDefault();
        zone.classList.remove('dragover');
        if (event.dataTransfer.files.length) {
          const transfer = new DataTransfer();
          transfer.items.add(event.dataTransfer.files[0]);
          input.files = transfer.files;
          sync();
        }
      });
    });

    document.querySelectorAll('form[data-endpoint]').forEach(form => {
      form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        const status = form.querySelector('.status');
        const isAnonymize = form.dataset.endpoint === '/anonymize';
        button.disabled = true;
        status.className = 'status';
        status.textContent = isAnonymize
          ? 'Локальная модель анализирует документ…'
          : 'Проверяем токены и восстанавливаем документ…';
        try {
          const response = await fetch(form.dataset.endpoint, { method: 'POST', body: new FormData(form) });
          if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            if (response.status === 409 && payload.candidates) showChoices(form, payload.candidates);
            throw new Error(payload.error || 'Операция не выполнена.');
          }
          if (!isAnonymize) resetChoice(form);
          const blob = await response.blob();
          const link = document.createElement('a');
          link.href = URL.createObjectURL(blob);
          link.download = filenameFromResponse(response, isAnonymize ? 'SAFE' : 'FINAL');
          document.body.appendChild(link);
          link.click();
          link.remove();
          URL.revokeObjectURL(link.href);
          status.className = 'status success';
          if (isAnonymize) {
            const documentId = response.headers.get('X-Document-Id') || '';
            status.innerHTML = 'Готово. Документ сохранён в базе под номером <strong></strong>. Во внешнюю AI-систему передавайте только SAFE-файл.';
            status.querySelector('strong').textContent = documentId;
          } else {
            status.textContent = 'Готово. Файл отправлен в загрузки браузера.';
          }
        } catch (error) {
          status.className = 'status error';
          status.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
    });
  </script>
</body>
</html>
"""