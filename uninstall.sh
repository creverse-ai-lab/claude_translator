#!/bin/sh
# claude_translator uninstaller: removes the hook entry and the slash command.
# The repo itself and other hooks in settings.json are left untouched.
set -eu
SETTINGS="$HOME/.claude/settings.json"

if [ -f "$SETTINGS" ]; then
    python3 - "$SETTINGS" <<'PYEOF'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
entries = data.get("hooks", {}).get("MessageDisplay", [])
for group in entries:
    group["hooks"] = [h for h in group.get("hooks", [])
                      if "translate_display.py" not in h.get("command", "")]
entries[:] = [g for g in entries if g.get("hooks")]
if not entries:
    data.get("hooks", {}).pop("MessageDisplay", None)
if not data.get("hooks"):
    data.pop("hooks", None)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
print("훅 등록 해제 완료")
PYEOF
fi
rm -f "$HOME/.claude/commands/translator.md" && echo "슬래시 커맨드 제거"
rm -rf "$HOME/.claude/message-display-translator" && echo "상태/버퍼 정리"
echo "제거 완료. 새 세션부터 적용됩니다."
