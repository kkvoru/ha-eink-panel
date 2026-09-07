# Первая публикация репозитория

## 1. Создание репозитория

На GitHub создайте публичный репозиторий со следующими параметрами:

- имя: `ha-eink-panel`;
- описание: `E-Ink dashboard for Home Assistant and old e-readers`;
- видимость: `Public`;
- не добавляйте README, `.gitignore` и лицензию — они уже находятся в архиве.

После создания добавьте темы репозитория:

`home-assistant`, `hacs`, `e-ink`, `onyx-boox`

## 2. Загрузка файлов

Распакуйте архив, откройте терминал в каталоге `ha-eink-panel` и выполните:

```bash
git init
git add .
git commit -m "Initial HACS release"
git branch -M main
git remote add origin https://github.com/kkvoru/ha-eink-panel.git
git push -u origin main
```

Если Git запросит авторизацию, войдите через открывшееся окно браузера или
используйте GitHub Desktop.

## 3. Первый выпуск

После успешного завершения проверок во вкладке `Actions` выполните:

```bash
git tag v0.5.0
git push origin v0.5.0
```

Workflow `Release` автоматически создаст выпуск `v0.5.0`. Затем репозиторий
можно добавить в HACS как пользовательский репозиторий типа `Интеграция`.

## 4. Последующие исправления

Для каждого обновления:

1. Измените исходники и номер `version` в `manifest.json`.
2. Выполните `git add`, `git commit` и `git push`.
3. Убедитесь, что проверка `Validate` завершилась успешно.
4. Создайте и отправьте тег с новым номером версии.

После автоматического создания Release новая версия появится в HACS.
