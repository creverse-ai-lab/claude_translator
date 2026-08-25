#!/usr/bin/env python3
"""
Claude Code MessageDisplay hook: rewrite Claude's answer through a local
Ollama model before it is shown in the terminal.

Display-only. The stored transcript and what the model sees are untouched.

Protocol (verified against Claude Code 2.1.235):
  stdin  <- {"hook_event_name":"MessageDisplay","session_id":...,
             "turn_id":...,"message_id":...,"index":int,
             "final":bool,"delta":str}
  stdout -> {"hookSpecificOutput":{"hookEventName":"MessageDisplay",
             "displayContent":"..."}}

Claude Code concatenates the displayContent of every flush in index order.
So: every non-final flush returns "" (shows nothing), and the final flush
returns the whole rewritten message at once.
"""

import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------- config


def load_config():
    cfg = {
        "enabled": True,
        "ollama_url": "http://127.0.0.1:11434",
        "model": "gemma4:e2b",
        "prompt_file": "prompt.en.md",
        "keep_alive": "30m",
        "temperature": 0.0,
        "num_ctx": 8192,
        "request_timeout_sec": 150,
        "min_chars": 200,        # shorter answers pass through untouched
        "max_chunk_chars": 1200,   # e2b handles small pieces far better
        "min_chunk_chars": 400,    # do not leave a lonely tail chunk
        "parallel_chunks": 3,      # chunks translated at the same time
        "mark_chunks": False,      # draw a line where each chunk split
        "min_output_ratio": 0.6,   # reject a rewrite shorter than this
        "max_retries": 1,          # retries before keeping the original
        "mark_fallback": True,     # say so when a chunk was kept as-is
        "collect_actions": True,   # copy action sentences to the end
        "actions_min": 2,          # only add the block for 2+ actions
        "keep_original": False,   # append the original text below the rewrite
        "part_wait_sec": 3.0,     # how long to wait for sibling flushes
        "debug": False,
    }
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    for key in list(cfg):
        env = os.environ.get("CT_" + key.upper())
        if env is None:
            continue
        if isinstance(cfg[key], bool):
            cfg[key] = env.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(cfg[key], int):
            try:
                cfg[key] = int(env)
            except ValueError:
                pass
        elif isinstance(cfg[key], float):
            try:
                cfg[key] = float(env)
            except ValueError:
                pass
        else:
            cfg[key] = env
    return cfg


CFG = load_config()
STATE_ROOT = os.path.join(
    os.path.expanduser("~"), ".claude", "message-display-translator")
LOG_PATH = os.path.join(STATE_ROOT, "debug.log")


STATE_FILE = os.path.join(STATE_ROOT, "state")


def runtime_enabled():
    """The state file (written by `--on`/`--off`) overrides config.json,
    so the hook can be toggled mid-session like a skill."""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            val = fh.read().strip().lower()
        if val in ("on", "1", "true"):
            return True
        if val in ("off", "0", "false"):
            return False
    except OSError:
        pass
    return CFG["enabled"]


def set_state(on):
    os.makedirs(STATE_ROOT, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        fh.write("on" if on else "off")


def log(msg):
    if not CFG["debug"]:
        return
    try:
        os.makedirs(STATE_ROOT, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def emit(text):
    """Print the hook result and exit. Never raises."""
    try:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "MessageDisplay",
                "displayContent": text,
            }
        }, ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        pass
    sys.exit(0)


# ------------------------------------------------ mechanical rewrites
# Patterns that a regex does perfectly and a 2B model does not. These run on
# the source text before it reaches the model. `N배 줄었다` is ambiguous in
# Korean - it can read as "reduced to 1/N" or "reduced by N" - and the model
# either ignored the rule or over-applied it to `N번으로 줄었다`, which is not
# ambiguous at all. So it is handled here instead.

SHRINK_RE = re.compile(r"(\d+)배(\s*)(줄|감소|작아|축소|낮아|짧아|적어)")

# double passives: always wrong in Korean, safe to fix blindly
DOUBLE_PASSIVE = [
    (re.compile(r"보여집니다"), "보입니다"),
    (re.compile(r"보여진다"), "보인다"),
    (re.compile(r"되어집니다"), "됩니다"),
    (re.compile(r"되어진다"), "된다"),
    (re.compile(r"불리워"), "불려"),
    (re.compile(r"쓰여집니다"), "쓰입니다"),
]


# translationese with one fixed correct form - no judgement needed
FIXED_PHRASES = [
    (re.compile(r"요구됩니다"), "필요합니다"),
    (re.compile(r"요구되었습니다"), "필요했습니다"),
    (re.compile(r"당신의\s*"), ""),
    (re.compile(r"우리는\s*"), ""),
    (re.compile(r"우리가\s*"), ""),
    (re.compile(r"인 것으로 확인됩니다"), "입니다"),
    (re.compile(r"인 것으로 확인되었습니다"), "였습니다"),
    (re.compile(r"수 있을 것입니다"), "수 있습니다"),
    (re.compile(r"할 것으로 생각됩니다"), "할 것 같습니다"),
    (re.compile(r"필요할 것으로 생각됩니다"), "필요해 보입니다"),
    (re.compile(r"이 이루어졌습니다"), "했습니다"),
    (re.compile(r"\s*작업을 수행할 예정입니다"), "할 예정입니다"),
    (re.compile(r"을 진행했습니다"), "했습니다"),
    (re.compile(r"를 진행했습니다"), "했습니다"),
]

# a first sentence that says nothing - drop it and start at the conclusion
FILLER_OPENER_RE = re.compile(
    r"^\s*(?:네[,.!]?\s*)?(?:조사해\s?봤습니다|확인해\s?봤습니다|확인했습니다|"
    r"알겠습니다|살펴봤습니다|검토해\s?봤습니다)[.!]?\s*"
    r"(?:결론부터 말하면[,]?\s*|결론은\s*)?")


def apply_mechanical(text):
    text = FILLER_OPENER_RE.sub("", text, count=1)
    text = SHRINK_RE.sub(r"\1분의 1로\2\3", text)
    for pat, rep in DOUBLE_PASSIVE + FIXED_PHRASES:
        text = pat.sub(rep, text)
    return text


# ------------------------------------------------- code-block protection

FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
INLINE_RE = re.compile(r"`[^`\n]+`")


def mask_code(text):
    """Replace code with placeholders so the small model cannot corrupt it."""
    blocks = []

    def take(match):
        blocks.append(match.group(0))
        return "[[CODE_%d]]" % (len(blocks) - 1)

    masked = FENCE_RE.sub(take, text)
    masked = INLINE_RE.sub(take, masked)
    return masked, blocks


def unmask_code(text, blocks):
    """Put the code back. Any placeholder the model dropped is appended."""
    used = set()

    def put(match):
        idx = int(match.group(1))
        if idx >= len(blocks):
            return match.group(0)
        used.add(idx)
        block = blocks[idx]
        if not block.startswith("```"):
            return block
        # a fence only renders when it starts its own line - the model
        # sometimes glues the placeholder onto the end of a sentence
        at_line_start = (match.start() == 0
                         or match.string[match.start() - 1] == "\n")
        return block if at_line_start else "\n\n" + block

    out = re.sub(r"\[\[\s*CODE_(\d+)\s*\]\]", put, text)
    missing = [i for i in range(len(blocks)) if i not in used]
    if missing:
        out += "\n\n> (아래 코드/명령어는 번역 모델이 위치를 잃어버려 끝에" \
               " 모아둡니다. 원문에서의 위치는 위 본문을 참고하세요.)\n\n"
        out += "\n\n".join(blocks[i] for i in missing)
    return out


# ------------------------------------------------------------- chunking


SENT_END_RE = re.compile(r"(?<=[.!?])\s+|(?<=다\.)\s*|(?<=요\.)\s*|(?<=니다\.)\s*")


def hard_split(block, limit):
    """Last resort: cut one oversized paragraph at sentence boundaries."""
    pieces, cur = [], ""
    for sentence in SENT_END_RE.split(block):
        if not sentence:
            continue
        candidate = (cur + " " + sentence).strip() if cur else sentence
        if len(candidate) > limit and cur:
            pieces.append(cur)
            cur = sentence
        else:
            cur = candidate
    if cur:
        pieces.append(cur)
    return pieces


def split_chunks(text, limit, min_tail):
    """Cut the message into pieces small enough for a 2B-class model.

    Boundaries follow markdown structure: a heading always starts a new
    chunk, blank-line blocks are never broken apart, and a chunk that is
    only a stub gets merged back into the one before it.
    """
    blocks = [b for b in text.split("\n\n")]
    units = []
    for block in blocks:
        if len(block) > limit:
            units.extend(hard_split(block, limit))
        else:
            units.append(block)

    chunks, cur = [], ""
    for unit in units:
        is_heading = unit.lstrip().startswith("#")
        candidate = (cur + "\n\n" + unit) if cur else unit
        if cur and (is_heading or len(candidate) > limit):
            chunks.append(cur)
            cur = unit
        else:
            cur = candidate
    if cur:
        chunks.append(cur)

    # a tiny trailing chunk has no context to work with - fold it back in.
    # pop first: an in-place `chunks[-2] = ... + chunks.pop()` would resolve
    # the assignment target against the already-shortened list.
    if len(chunks) >= 2 and len(chunks[-1]) < min_tail:
        if len(chunks[-2]) + len(chunks[-1]) <= limit * 1.5:
            tail = chunks.pop()
            chunks[-1] = chunks[-1] + "\n\n" + tail

    result = [c for c in chunks if c.strip()]
    # guard: chunking must never lose or duplicate a character
    squash = lambda t: re.sub(r"\s+", "", t)
    if squash("".join(result)) != squash(text):
        log("chunker lost content (%d -> %d chars); using one chunk"
            % (len(text), sum(len(c) for c in result)))
        return [text]
    return result


# --------------------------------------------------------------- ollama


def read_prompt():
    with open(os.path.join(HERE, CFG["prompt_file"]), encoding="utf-8") as fh:
        return fh.read()


def call_ollama(system_prompt, user_text):
    payload = {
        "model": CFG["model"],
        "stream": False,
        "keep_alive": CFG["keep_alive"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "options": {
            "temperature": CFG["temperature"],
            "num_ctx": CFG["num_ctx"],
        },
    }
    req = urllib.request.Request(
        CFG["ollama_url"].rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
            req, timeout=CFG["request_timeout_sec"]) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return (body.get("message") or {}).get("content", "").strip()


PREAMBLE_RE = re.compile(
    r"^\s*(다음은[^\n]*\n|번역[:：][^\n]*\n|정리하면[^\n]*\n|"
    r"here is[^\n]*\n|sure[,!][^\n]*\n)", re.IGNORECASE)


PLACEHOLDER_RE = re.compile(r"\[\[\s*CODE_(\d+)\s*\]\]")

RETRY_HINT = (
    "\n\n[경고] 직전 시도에서 내용이 누락되었다. 다시 쓴다.\n"
    "- 원문의 모든 문장을 하나도 빼지 않고 다시 쓴다.\n"
    "- 원문에 없는 제목이나 문단을 새로 만들지 않는다.\n"
    "- `[[CODE_0]]` 같은 표시는 그 자리에 글자 그대로 남긴다.\n"
)


def clean_output(text):
    text = PREAMBLE_RE.sub("", text)
    text = re.sub(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$",
                  r"\1", text, flags=re.DOTALL)
    return text.strip()


NUM_RE = re.compile(r"\d[\d,._:]*\d|\d")


def missing_numbers(chunk, result):
    """Every number in the source must survive, with at least the same count.
    Extra numbers are allowed - the `N배 -> N분의 1` rewrite adds one."""
    from collections import Counter
    want = Counter(NUM_RE.findall(chunk))
    got = Counter(NUM_RE.findall(result))
    return [n for n, c in want.items() if got[n] < c]


def check_output(chunk, result):
    """Reject a rewrite that lost content. Returns None or a reason string."""
    if not result:
        return "empty output"
    want = set(PLACEHOLDER_RE.findall(chunk))
    got = set(PLACEHOLDER_RE.findall(result))
    missing = sorted(want - got, key=int)
    if missing:
        return "dropped placeholders: %s" % ", ".join(
            "[[CODE_%s]]" % m for m in missing)
    floor = int(len(chunk) * CFG["min_output_ratio"])
    if len(result) < floor:
        return "too short: %d chars from %d (floor %d)" % (
            len(result), len(chunk), floor)
    lost = missing_numbers(chunk, result)
    if lost:
        return "lost numbers: %s" % ", ".join(lost)
    return None


def translate_chunk(args):
    """Rewrite one chunk. Falls back to the original chunk if the model
    loses content, so nothing is ever silently dropped."""
    system_prompt, chunk, i, total = args
    tag = "%d/%d" % (i + 1, total)
    header = ""
    if total > 1:
        header = ("(이것은 전체 %d조각 중 %d번째 조각이다. 이 조각만 다시 쓴다. "
                  "앞뒤 조각을 언급하지 않는다. 앞뒤 내용을 추측해서 덧붙이지 "
                  "않는다. 원문에 없는 제목을 새로 만들지 않는다.)\n\n"
                  "---\n\n" % (total, i + 1))

    last_reason = "unknown"
    for attempt in range(CFG["max_retries"] + 1):
        prompt = system_prompt + (RETRY_HINT if attempt else "")
        try:
            result = clean_output(call_ollama(prompt, header + chunk))
        except Exception as exc:
            last_reason = "request failed: %s" % exc
            log("chunk %s attempt %d %s" % (tag, attempt + 1, last_reason))
            continue
        if CFG["debug"]:
            dump = os.path.join(STATE_ROOT, "chunks")
            os.makedirs(dump, exist_ok=True)
            with open(os.path.join(dump, "%02d_a%d.txt" % (i, attempt)),
                      "w", encoding="utf-8") as fh:
                fh.write("### IN\n" + chunk + "\n\n### OUT\n" + result)
        reason = check_output(chunk, result)
        if reason is None:
            log("chunk %s ok on attempt %d (%d -> %d chars)"
                % (tag, attempt + 1, len(chunk), len(result)))
            return result
        last_reason = reason
        log("chunk %s attempt %d rejected - %s" % (tag, attempt + 1, reason))

    log("chunk %s FELL BACK to original - %s" % (tag, last_reason))
    if CFG["mark_fallback"]:
        return ("> (이 부분은 번역 모델이 내용을 누락해서 Claude 원문 그대로"
                " 둡니다. 사유: %s)\n\n%s" % (last_reason, chunk))
    return chunk


def translate(text):
    system_prompt = read_prompt()
    masked, blocks = mask_code(apply_mechanical(text))
    chunks = split_chunks(masked, CFG["max_chunk_chars"],
                          CFG["min_chunk_chars"])
    log("chunks=%d sizes=%s" % (len(chunks), [len(c) for c in chunks]))

    jobs = [(system_prompt, c, i, len(chunks)) for i, c in enumerate(chunks)]
    workers = max(1, min(int(CFG["parallel_chunks"]), len(jobs)))
    if workers == 1:
        parts = [translate_chunk(j) for j in jobs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(translate_chunk, jobs))

    joiner = "\n\n---\n\n" if CFG["mark_chunks"] else "\n\n"
    return collect_actions(unmask_code(joiner.join(parts), blocks))


# ---------------------------------------------------- action collector
# An analyst must not miss a "do this" sentence buried in prose. Copy every
# action sentence into one block at the end. Copies, not a summary - the
# body keeps them too.

ACTION_RE = re.compile(
    r"[^.\n]*(?:"
    r"하세요|해\s?주세요|해주시기 바랍니다|필요합니다|해야 합니다|"
    r"확인이 필요|결정이 필요|검토가 필요|논의가 필요|권합니다|추천합니다|"
    r"주십시오|바랍니다"
    r")[^.\n]*\.")


def collect_actions(text):
    if not CFG["collect_actions"]:
        return text
    body = FENCE_RE.sub("", text)              # never scan code blocks
    seen, actions = set(), []
    for m in ACTION_RE.finditer(body):
        sent = m.group(0).strip().lstrip("-*> ").strip()
        if len(sent) < 6 or sent in seen:
            continue
        seen.add(sent)
        actions.append(sent)
    if len(actions) < CFG["actions_min"]:
        return text
    block = "\n\n---\n\n**확인·조치 항목** (본문에서 그대로 모음)\n\n"
    block += "\n".join("- %s" % a for a in actions)
    return text + block


# ---------------------------------------------------------- flush buffer


def msg_dir(session_id, message_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", "%s__%s" % (session_id, message_id))
    return os.path.join(STATE_ROOT, "buffer", safe)


def write_part(directory, index, delta):
    """Store one flush. Retries once: a sibling flush may be pruning the
    directory at the same moment."""
    last = None
    for _ in range(2):
        try:
            os.makedirs(directory, exist_ok=True)
            tmp = os.path.join(directory, "%06d.part.tmp" % index)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(delta)
            os.replace(tmp, os.path.join(directory, "%06d.part" % index))
            return
        except OSError as exc:
            last = exc
    raise last


def collect_parts(directory, final_index, wait_sec):
    """Wait for flushes 0..final_index to land, then join them in order."""
    deadline = time.time() + wait_sec
    while True:
        have = {int(name[:6]) for name in os.listdir(directory)
                if name.endswith(".part")}
        if all(i in have for i in range(final_index + 1)):
            break
        if time.time() >= deadline:
            log("timed out waiting for parts; have=%s need=0..%d"
                % (sorted(have), final_index))
            break
        time.sleep(0.05)
    pieces = []
    for i in range(final_index + 1):
        path = os.path.join(directory, "%06d.part" % i)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                pieces.append(fh.read())
    return "".join(pieces)


def prune_old_buffers(max_age_sec=600):
    base = os.path.join(STATE_ROOT, "buffer")
    if not os.path.isdir(base):
        return
    now = time.time()
    for name in os.listdir(base):
        path = os.path.join(base, name)
        try:
            if now - os.path.getmtime(path) > max_age_sec:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# ----------------------------------------------------------------- main


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        emit("")

    delta = data.get("delta", "")
    is_final = bool(data.get("final"))
    index = int(data.get("index", 0))
    session_id = str(data.get("session_id", "nosession"))
    message_id = str(data.get("message_id", "nomessage"))

    if not runtime_enabled():
        emit(delta)

    directory = msg_dir(session_id, message_id)

    try:
        write_part(directory, index, delta)
    except Exception as exc:
        log("write_part failed: %s" % exc)
        emit(delta)

    if not is_final:
        emit("")          # hide the raw stream; the final flush prints it all

    # ---- final flush: rebuild, rewrite, print --------------------------
    original = delta
    try:
        original = collect_parts(directory, index, CFG["part_wait_sec"])
    except Exception as exc:
        log("collect_parts failed: %s" % exc)

    try:
        prune_old_buffers()
    except Exception:
        pass

    if len(original.strip()) < CFG["min_chars"]:
        emit(original)

    started = time.time()
    try:
        rewritten = translate(original)
    except urllib.error.URLError as exc:
        log("ollama unreachable: %s" % exc)
        emit(original + "\n\n> (번역 훅: Ollama에 연결하지 못해 원문을 그대로"
                        " 표시합니다. `ollama serve` 실행 여부를 확인하세요.)")
    except Exception as exc:
        log("translate failed: %s" % exc)
        emit(original + "\n\n> (번역 훅 실패: %s — 원문을 그대로 표시합니다.)"
             % exc)

    log("ok model=%s chars=%d->%d in %.1fs"
        % (CFG["model"], len(original), len(rewritten), time.time() - started))

    if CFG["keep_original"]:
        emit("── 전 · Claude 원문 " + "─" * 40 + "\n\n" + original.strip()
             + "\n\n── 후 · " + CFG["model"] + " " + "─" * 40 + "\n\n"
             + rewritten.strip() + "\n")
    emit(rewritten)


def cli(arg):
    arg = arg.lstrip("-").lower()
    if arg == "on":
        set_state(True)
    elif arg == "off":
        set_state(False)
    elif arg == "toggle":
        set_state(not runtime_enabled())
    elif arg != "status":
        print("usage: translate_display.py [--on|--off|--toggle|--status]")
        sys.exit(2)
    state = "on" if runtime_enabled() else "off"
    print("translator: %s (model=%s)" % (state, CFG["model"]))
    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli(sys.argv[1])
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:      # never break the display
        log("fatal: %s" % exc)
        emit("")
