#!/bin/sh
# claude_translator installer
# - registers the MessageDisplay hook in ~/.claude/settings.json (merge, not overwrite)
# - installs the /translator slash command
# usage: ./install.sh        (or: ./uninstall.sh to remove)
set -eu

DIR=$(cd "$(dirname "$0")" && pwd)
HOOK="$DIR/hooks/translate_display.py"
SETTINGS="$HOME/.claude/settings.json"
CMD_DIR="$HOME/.claude/commands"

echo "== claude_translator install =="
echo "repo: $DIR"

# ---- 1. prerequisites -------------------------------------------------
command -v python3 >/dev/null || { echo "ERROR: python3 가 필요합니다."; exit 1; }
[ -f "$HOOK" ] || { echo "ERROR: $HOOK 이 없습니다."; exit 1; }

if ! command -v ollama >/dev/null; then
    echo "ERROR: ollama 가 설치되어 있지 않습니다. https://ollama.com 에서 설치하세요."
    exit 1
fi

MODEL=$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('model','gemma4:e2b'))" 2>/dev/null || echo gemma4:e2b)
if ! ollama list 2>/dev/null | grep -q "^$MODEL"; then
    echo "모델 '$MODEL' 이 없습니다. 내려받습니다..."
    ollama pull "$MODEL"
fi

if ! curl -s -m 3 "http://127.0.0.1:11434/api/version" >/dev/null; then
    echo "주의: ollama 서버가 응답하지 않습니다. 'ollama serve' 를 실행해 두세요."
fi

# ---- 2. merge the hook into settings.json (never overwrite others) ----
mkdir -p "$HOME/.claude"
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.before-translator.bak" \
    && echo "백업: $SETTINGS.before-translator.bak"

python3 - "$SETTINGS" "$HOOK" <<'PYEOF'
import json, os, sys
settings_path, hook_path = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(settings_path):
    with open(settings_path, encoding="utf-8") as fh:
        data = json.load(fh)
hooks = data.setdefault("hooks", {})
entries = hooks.setdefault("MessageDisplay", [])
cmd = 'python3 "%s"' % hook_path
# drop any earlier translator entry (old path), keep everything else
for group in entries:
    group["hooks"] = [h for h in group.get("hooks", [])
                      if "translate_display.py" not in h.get("command", "")]
entries[:] = [g for g in entries if g.get("hooks")]
entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 180}]})
with open(settings_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
print("훅 등록 완료: MessageDisplay ->", cmd)
PYEOF

# ---- 3. /translator slash command -------------------------------------
mkdir -p "$CMD_DIR"
cat > "$CMD_DIR/translator.md" <<CMDEOF
---
description: 표시 번역 훅(로컬 Ollama 윤문)을 켜고 끕니다
argument-hint: on | off | status
allowed-tools: Bash(python3 $HOOK:*)
---

다음 명령을 실행해서 표시 번역 훅 상태를 바꾸거나 확인하라.
인자가 없으면 \`--toggle\`을 쓴다.

\`\`\`
python3 $HOOK --\$ARGUMENTS
\`\`\`

실행 결과의 상태(on/off)를 한 줄로 보고하라. 다른 작업은 하지 않는다.
참고: 상태 변경은 다음 답변 표시부터 적용된다.
CMDEOF
echo "슬래시 커맨드 설치: $CMD_DIR/translator.md"

# ---- 4. turn it on -----------------------------------------------------
python3 "$HOOK" --on

echo ""
echo "설치 완료. 새 Claude Code 세션부터 적용됩니다."
echo "  끄기/켜기 : /translator off · /translator on  (또는 python3 $HOOK --toggle)"
echo "  설정      : $DIR/config.json"
echo "  제거      : $DIR/uninstall.sh"
