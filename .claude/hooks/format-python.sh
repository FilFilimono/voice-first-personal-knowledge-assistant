#!/usr/bin/env bash
# PostToolUse на Write/Edit: причёсывает и проверяет типы у изменённого файла.
#
# Форматирование и линт чинятся молча. Ошибка типов возвращается модели
# кодом 2 — чтобы правка не уезжала дальше в сломанном виде.

set -uo pipefail

input=$(cat)
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // ""')

# Не Python — не наше дело.
case "$file_path" in
  *.py) ;;
  *) exit 0 ;;
esac

[ -f "$file_path" ] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

uv run ruff format "$file_path" >/dev/null 2>&1
uv run ruff check --fix "$file_path" >/dev/null 2>&1

if ! mypy_output=$(uv run mypy "$file_path" 2>&1); then
  printf 'mypy недоволен файлом %s:\n%s\n' "$file_path" "$mypy_output" >&2
  exit 2
fi

exit 0
