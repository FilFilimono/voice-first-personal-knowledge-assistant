#!/usr/bin/env bash
# Stop: сессия не заканчивается с красными тестами.
#
# Гоняются только быстрые тесты — без БД, без сети (маркер integration
# исключён в pyproject.toml). Хук обязан отрабатывать за секунды, иначе
# каждая остановка превращается в ожидание.

set -uo pipefail

input=$(cat)

# Защита от петли: если сессию уже продлил этот же хук, второй раз не мешаем.
if [ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

if ! output=$(uv run pytest 2>&1); then
  printf 'Быстрые тесты красные — чинить до конца сессии:\n%s\n' "$output" >&2
  exit 2
fi

exit 0
