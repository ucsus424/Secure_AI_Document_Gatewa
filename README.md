# Secure AI Document Gateway

Локальное Python-приложение для поиска чувствительных данных в документах, их замены на токены и последующего восстановления. Анализ выполняется локально через Ollama и Gemma; внешние AI API не используются.

## 1. Что нужно

- Windows, macOS или Linux.
- Python 3.9 или новее. Внешних Python-пакетов нет, `pip install` не нужен.
- Ollama 0.30.5 или новее.
- Около 8 ГБ свободного места для модели `gemma4:12b-it-qat` и достаточно оперативной памяти для её запуска.

Распакуйте ZIP в отдельную папку. В пути могут быть пробелы и русские буквы. Все команды ниже нужно выполнять из корневой папки проекта, где лежит `app.py`.

## 2. Установка Python, Ollama и Gemma

1. Установите Python с [python.org](https://www.python.org/downloads/) и проверьте:

   ```bash
   python3 --version
   ```

   На Windows команда обычно выглядит как `py --version` или `python --version`. При установке включите **Add Python to PATH**.

2. Установите Ollama с [ollama.com/download](https://ollama.com/download). На macOS и Windows запустите установленное приложение. Для Linux можно использовать команду со страницы загрузки. Проверка:

   ```bash
   ollama --version
   ```

3. Скачайте модель:

   ```bash
   ollama pull gemma4:12b-it-qat
   ollama list
   ```

Обычно Ollama запускает локальный сервис автоматически. Если приложение не видит модель, запустите в отдельном окне терминала `ollama serve` и оставьте его работать.

## 3. Запуск веб-интерфейса

macOS/Linux:

```bash
cd "/пут/к/Secure_AI_Document_Gateway"
python3 app.py ui --port 8765
```

Windows PowerShell:

```powershell
cd "C:\пут\к\Secure_AI_Document_Gateway"
py app.py ui --port 8765
```

Откройте [http://127.0.0.1:8765](http://127.0.0.1:8765). Остановка сервера: `Ctrl+C` в терминале. Веб-сервер слушает только `127.0.0.1` и не открывается в локальную сеть.

Порядок работы:

1. В блоке **Создать безопасную копию** загрузите TXT, DOCX, XLSX или PPTX. В ответ скачается ZIP с `*_SAFE`-документом и `*.mapping.json`.
2. Внешней AI-системе передавайте только SAFE-документ. **Mapping содержит исходные данные: его нельзя отправлять во внешние системы.**
3. Для восстановления выберите обработанный SAFE-файл и соответствующий mapping. Восстановление через UI поддерживает TXT и DOCX.

## 4. Промпт и настройки

Основной промпт лежит в `prompts/dlp_system_prompt.txt`. После его изменения перезапустите Python-сервер. Другой файл промпта можно указать без изменения кода:

```bash
python3 app.py ui --prompt prompts/my_prompt.txt --port 8765
```

Модель и адрес Ollama тоже можно переопределить:

```bash
python3 app.py ui --model gemma4:12b-it-qat --ollama-url http://localhost:11434
```

Дефолты находятся в `core/detector.py`. Логика замены — в `core/anonymizer.py`, работа с Office XML — в `core/office.py`, веб-интерфейс — в `web_ui.py`.

## 5. CLI и тесты

Приложение можно запускать без UI:

```bash
python3 app.py scan document.docx
python3 app.py anonymize document.docx --output-dir result
python3 app.py restore result/document_SAFE.docx result/document.mapping.json
python3 app.py --help
```

Проверка кода после изменений:

```bash
python3 -m unittest discover -s tests
```

Тесты не отправляют данные во внешнюю сеть. История решений и текущее состояние проекта описаны в `PROJECT_CONTEXT.md`.
