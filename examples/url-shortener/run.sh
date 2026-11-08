#!/usr/bin/env bash
# Runs the URL shortener example: the same three tasks with and without the plugin.
# Usage: examples/url-shortener/run.sh   (needs `claude` and a `python` with flask + pytest on PATH)
# Writes results/baseline and results/with-plugin. Agents run with bypassPermissions in a temp copy.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/results"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v claude >/dev/null || { echo "error: claude not on PATH" >&2; exit 1; }
python -c "import flask, pytest" 2>/dev/null || { echo "error: python needs flask and pytest" >&2; exit 1; }

TASKS=(
  "Add rate limiting per IP to POST /links."
  "Count clicks per link and include the count in GET /links/<code>."
  "Show a title preview for each link: GET /links/<code> should include the target page's title."
)

# --bare keeps the user's own plugins, CLAUDE.md and hooks out of both arms.
# --bare also skips plugin hooks, so the plugin arm gets the always-on rule the hook would inject.
agent() { # agent <arm> <prompt> [extra claude args...]
  local arm=$1 prompt=$2; shift 2
  local extra=()
  [[ $arm == with-plugin ]] && extra=(--plugin-dir "$REPO" --append-system-prompt-file "$REPO/AGENTS-snippet.md")
  (cd "$WORK/$arm" && timeout 900 claude -p --bare --permission-mode bypassPermissions ${extra[@]+"${extra[@]}"} "$@" "$prompt")
}

record() { # record <arm> <n> <title> <prompt> [extra claude args...]
  local arm=$1 n=$2 title=$3 prompt=$4; shift 4
  local file="$OUT/$arm/transcripts/$n-$title.md"
  echo "== $arm: $title"
  { printf '# %s\n\n**Prompt:** %s\n\n**Reply:**\n\n' "$title" "$prompt"; agent "$arm" "$prompt" "$@" || echo "(step failed or timed out)"; } > "$file"
  (cd "$WORK/$arm" && git add -A && git commit -qm "$title" --allow-empty \
    && printf '\n\n**Diff:**\n\n```diff\n%s\n```\n' "$(git show --format= HEAD -- . ':!docs')") >> "$file"
  # Snapshot after every step, so a failed later step keeps the earlier results.
  (cd "$WORK/$arm" && git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$OUT/$arm")
}

rm -rf "$OUT"
for arm in baseline with-plugin; do
  mkdir -p "$OUT/$arm/transcripts"
  cp -r "$HERE/start" "$WORK/$arm"
  (cd "$WORK/$arm" && git init -q && git add -A && git commit -qm start)
  for i in "${!TASKS[@]}"; do record "$arm" "$((i + 1))" "task-$((i + 1))" "${TASKS[$i]}"; done
done

record with-plugin 4 explain "Why is rate limiting built this way, and why not Redis?"
record with-plugin 5 interview "/what-did-agent-do:what-did-agent-do interview"
record with-plugin 6 drill-start "Yes, start the drill." --continue
record with-plugin 7 drill-answer "It's simpler." --continue

echo "done: $OUT"
