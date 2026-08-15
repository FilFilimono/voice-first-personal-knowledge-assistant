#!/usr/bin/env bash
# PreToolUse на Bash: блокирует необратимое.
#
# Смысл хука в том, что он не может забыть и не может галлюцинировать —
# в отличие от инструкции в CLAUDE.md. Всё, что действительно критично,
# живёт здесь.
#
# Список запретов — раздел 5.3 технического плана.

set -uo pipefail

input=$(cat)
command=$(printf '%s' "$input" | jq -r '.tool_input.command // ""')

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
  printf '%s' "$command" | grep -Eqi "$1"
}

# Начало команды: строка, пробел или разделитель — чтобы `npm` не ловился на `rm`.
START='(^|[[:space:];&|(`])'

# rm с рекурсией и force. Флаги могут быть слитно (-rf, -fr), раздельно
# (-r -f) и длинными (--recursive --force), поэтому проверяется не написание,
# а наличие обоих смыслов внутри самой команды rm — до следующего разделителя,
# чтобы флаги соседней команды в цепочке не считались за свои.
rm_segment=$(printf '%s' "$command" | grep -Eio "${START}rm[[:space:]][^;&|]*" | head -1)
if [ -n "$rm_segment" ]; then
  has_recursive=$(printf '%s' "$rm_segment" | grep -Eqi -- '(^|[[:space:]])-[[:alnum:]]*r|--recursive' && echo yes)
  has_force=$(printf '%s' "$rm_segment" | grep -Eqi -- '(^|[[:space:]])-[[:alnum:]]*f|--force' && echo yes)
  if [ "$has_recursive" = yes ] && [ "$has_force" = yes ]; then
    deny "rm -rf заблокирован хуком. Удаляйте адресно и без рекурсии, либо попросите пользователя сделать это руками."
  fi
fi

if matches "${START}git[[:space:]]+push"; then
  deny "git push заблокирован хуком. Пушит пользователь, руками."
fi

if matches "${START}git[[:space:]]+commit"; then
  deny "git commit заблокирован хуком намеренно (раздел 7 плана): коммитит пользователь, прочитав дифф. Подготовьте изменения и опишите, что сделано."
fi

if matches "alembic[[:space:]]+downgrade"; then
  deny "alembic downgrade заблокирован хуком: откат миграции на базе с данными теряет данные. Нужен откат — пишите новую миграцию вперёд."
fi

# docker compose down -v сносит том с базой целиком
if matches "(docker[[:space:]]+compose|docker-compose)" &&
   matches "[[:space:]]down([[:space:]]|$)" &&
   matches "([[:space:]]-v([[:space:]]|$)|--volumes)"; then
  deny "docker compose down -v уничтожает том с базой. Используйте docker compose down без -v."
fi

exit 0
