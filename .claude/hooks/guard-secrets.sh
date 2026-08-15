#!/usr/bin/env bash
# PreToolUse на Write/Edit: не даёт записать секрет в файл репозитория.
#
# Инвариант 6: никаких секретов в коде, только переменные окружения.
# Ловятся форматы ключей, а не слово «key» — иначе хук начнёт мешать
# и его отключат, что хуже, чем его отсутствие.

set -uo pipefail

input=$(cat)

# Все строковые значения аргумента инструмента: content у Write,
# new_string у Edit, edits[].new_string у пакетных правок.
payload=$(printf '%s' "$input" | jq -r '[.tool_input | .. | strings] | join("\n")')

deny() {
  jq -n --arg reason "$1" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

matches() {
  printf '%s' "$payload" | grep -Eq "$1"
}

if matches 'sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{20,}'; then
  deny "Похоже на ключ Anthropic. Секреты живут в .env, который не в git. В коде — только os.environ / brain.config."
fi

if matches 'AIza[0-9A-Za-z_-]{35}'; then
  deny "Похоже на ключ Google API. Секреты живут в .env, который не в git. В коде — только brain.config."
fi

if matches '[0-9]{8,10}:AA[A-Za-z0-9_-]{32,}'; then
  deny "Похоже на токен Telegram-бота. Он должен приходить из .env через brain.config, а не лежать в файле."
fi

if matches '\-\-\-\-\-BEGIN [A-Z ]*PRIVATE KEY\-\-\-\-\-'; then
  deny "Приватный ключ не хранится в репозитории."
fi

if matches 'AKIA[0-9A-Z]{16}'; then
  deny "Похоже на ключ доступа AWS. Секреты живут в .env, который не в git."
fi

if matches 'xox[baprs]-[A-Za-z0-9-]{10,}'; then
  deny "Похоже на токен Slack. Секреты живут в .env, который не в git."
fi

exit 0
