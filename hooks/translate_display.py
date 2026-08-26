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
        "mode": "regex",           # regex: 정규식만, 스트리밍 유지, 모델 없음
                                   # dispatch: detect->micro-tasks->merge
                                   # rewrite: legacy whole-chunk rewriting
        "keep_alive": "30m",
        "temperature": 0.0,
        "num_ctx": 8192,
        "request_timeout_sec": 150,
        "min_chars": 200,        # shorter answers pass through untouched
        "max_chunk_chars": 1200,   # e2b handles small pieces far better
        "min_chunk_chars": 400,    # do not leave a lonely tail chunk
        "parallel_chunks": 3,      # chunks translated at the same time
        "parallel_tasks": 6,       # concurrent micro-tasks (dispatch mode)
        "gloss_terms": "slang",    # slang: 영어어근+한국어어미만 / all / off
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
    (re.compile(r"성공적으로\s+"), ""),
    (re.compile(r"귀하의\s*"), ""),
    (re.compile(r"필요에 따라"), "필요하면"),
    (re.compile(r"추가적으로"), "추가로"),
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

def _jong(ch):
    """Final-consonant index of a Hangul syllable (0 = none, 8 = rieul)."""
    return (ord(ch) - 0xAC00) % 28 if "가" <= ch <= "힣" else -1


def _through_eul(m):
    # "툴을 통해" -> "툴로" (rieul final), "본문을 통해" -> "본문으로"
    ch = m.group(1)
    return ch + ("로" if _jong(ch) == 8 else "으로")


# ported from im-not-ai quick-rules (A-family translation-ese only; the
# humanizing rules that REMOVE structure/parenthesized terms are the
# opposite of this tool's goal and are deliberately not ported)
PORTED_PHRASES = [
    # A-2: "~를 통해" -> "~로" (조사 자동 선택, "통해서만" 같은 한정은 제외)
    (re.compile(r"([가-힣])를 통해(?:서)?(?!서|만)"), r"\1로"),
    (re.compile(r"([가-힣])을 통해(?:서)?(?!서|만)"), _through_eul),
    # A-7: "X를 가지고 있다" -> "X가 있다"
    (re.compile(r"([가-힣])를 가지고 있"), r"\1가 있"),
    (re.compile(r"([가-힣])을 가지고 있"), r"\1이 있"),
    # A-19: 이중 조사
    (re.compile(r"([가-힣])에서의 "), r"\1에서 "),
    (re.compile(r"([가-힣])으로의 "), r"\1으로 가는 "),
    # D-3: 채움말
    (re.compile(r"본질적으로,?\s*"), ""),
    (re.compile(r"기본적으로,?\s*"), ""),
]

# a first sentence that says nothing - drop it and start at the conclusion
FILLER_OPENER_RE = re.compile(
    r"^\s*(?:네[,.!]?\s*)?(?:조사해\s?봤습니다|확인해\s?봤습니다|확인했습니다|"
    r"알겠습니다|살펴봤습니다|검토해\s?봤습니다)[.!]?\s*"
    r"(?:결론부터 말하면[,]?\s*|결론은\s*)?")


def apply_mechanical(text, at_start=True):
    if at_start:
        text = FILLER_OPENER_RE.sub("", text, count=1)
    text = SHRINK_RE.sub(r"\1분의 1로\2\3", text)
    for pat, rep in DOUBLE_PASSIVE + FIXED_PHRASES + PORTED_PHRASES:
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


# ------------------------------------------------ regex streaming mode
# No model at all: every delta is transformed by the regex layer and shown
# immediately, so streaming stays live. Code fences are tracked across
# deltas via the part buffer; fenced lines pass through untouched.


def fence_parity_before(directory, index, wait_sec=0.5):
    """Number of ``` fences seen in flushes 0..index-1 (odd = inside)."""
    text = collect_parts(directory, index - 1, wait_sec) if index else ""
    return text.count("```") % 2


def regex_transform(delta, inside_fence, at_start):
    out_lines = []
    for line in delta.split("\n"):
        if line.lstrip().startswith("```"):
            inside_fence = not inside_fence
            out_lines.append(line)
            continue
        if inside_fence:
            out_lines.append(line)
            continue
        masked, blocks = [], []
        def take(m):
            blocks.append(m.group(0))
            return "[[CODE_%d]]" % (len(blocks) - 1)
        body = INLINE_RE.sub(take, line)
        body = apply_mechanical(body, at_start=at_start)
        at_start = False
        body = re.sub(r"\[\[CODE_(\d+)\]\]",
                      lambda m: blocks[int(m.group(1))], body)
        out_lines.append(body)
    return "\n".join(out_lines), inside_fence


# ---------------------------------------------------- dispatch pipeline
# The user-designed pipeline: Python detects what needs fixing, the small
# LLM gets one tiny task per finding, and Python merges the results back.
# The model never holds the whole text, so it can neither summarize nor
# hallucinate structure - the failure modes we measured in whole-chunk mode.

# Claude-slang: an English root inflected with a Korean ending
# ("robust한", "graceful하게", "flaky할 수"). Bare English nouns (mutex,
# failover) read fine for a developer and are deliberately left alone -
# blanket glossing was tried and turned out to be noise.
SLANG_RE = re.compile(
    r"[A-Za-z][A-Za-z+#.-]*"
    r"(?:스러운|스럽게|하게|하지|하다|하며|하고|해서|해도|합니다|했|한|할|함|해)"
    r"(?=[\s,.)]|$)")

# gloss scope: "slang" (default) / "all" (every bare English term) / "off"
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
CONNECTIVE_RE = re.compile(r"(?:았|었|이|하)?(?:고|는데|아서|어서|으며|지만),?\s")


ENG_RUN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9+#.'-]*(?:[ -][A-Za-z][A-Za-z0-9+#.'-]*)*")
TERM_SKIP = {
    "api", "cpu", "gpu", "ram", "llm", "gpt", "mcp", "sdk", "cli", "url",
    "http", "https", "json", "yaml", "sql", "db", "id", "ui", "ux", "os",
    "ci", "cd", "pr", "npm", "pip", "git", "ok",
}


def find_term_tasks(text):
    """Claude-slang spans (영어 어근+한국어 어미) - first occurrence each."""
    tasks, seen = [], set()
    scope = CFG["gloss_terms"]
    if scope == "off":
        return tasks
    if scope == "slang":
        for m in SLANG_RE.finditer(text):
            key = m.group(0).lower()
            if key in seen:
                continue
            seen.add(key)
            tasks.append({"kind": "slang", "start": m.start(),
                          "end": m.end(), "src": m.group(0)})
        return tasks
    # scope == "all": the old blanket behaviour, kept as an option
    for m in ENG_RUN_RE.finditer(text):
        t = m.group(0)
        words = t.split()
        if len(t) < 4 and len(words) == 1:
            continue
        if t.isupper():
            continue
        if len(words) == 1 and (t[0].isupper() or t.lower() in TERM_SKIP):
            continue
        if text[max(0, m.start() - 1):m.start()] == "(":
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        tasks.append({"kind": "term", "start": m.start(), "end": m.end(),
                      "src": t})
    return tasks


BULLET_RE = re.compile(r"^(\s*(?:[-*]|\d+\.)\s+)(.*)$")


def find_sentence_tasks(text):
    """Units that need model help: too-long sentences, wrong register.
    Bullets are units of their own - one task per item is ~5x faster than
    sending the whole list, and a bad result spoils one item, not the list."""
    # register is judged on prose only: bullets are often terse-style
    # (개조식) even in a polite document, and must not flip the verdict
    prose = "\n".join(ln for ln in text.split("\n")
                      if not BULLET_RE.match(ln))
    total_formal = len(re.findall(r"니다[.!?]", prose))
    total_plain = len(re.findall(r"(?<!니)다[.!?]", prose))
    doc_formal = total_formal >= total_plain
    tasks = []

    def scan(body, offset):
        pos = 0
        for sent in SENT_SPLIT_RE.split(body):
            start = body.find(sent, pos)
            pos = start + len(sent)
            wants = []
            if len(sent) >= 90 and len(CONNECTIVE_RE.findall(sent)) >= 2:
                wants.append("뜻과 단어와 시제를 그대로 유지하며 "
                             "2~3개의 문장으로 나눈다")
            if doc_formal and re.search(r"(?<!니)다[.!?]\s*$", sent.strip()):
                wants.append("시제는 유지하고 문장 끝만 '~합니다'체로 바꾼다")
            if wants:
                tasks.append({"kind": "sent", "start": offset + start,
                              "end": offset + start + len(sent), "src": sent,
                              "wants": wants})

    for line_m in re.finditer(r"[^\n]+", text):
        line = line_m.group(0)
        # inline-code placeholders travel inside the sentence; the per-task
        # validator rejects any result that loses one, so no need to skip
        b = BULLET_RE.match(line)
        if b:                           # bullet: task the item body only
            scan(b.group(2), line_m.start() + len(b.group(1)))
            continue
        if line.lstrip().startswith(("#", "|", ">")):
            continue                    # headings/tables/quotes stay verbatim
        scan(line, line_m.start())
    return tasks


SLANG_FEWSHOT = [
    {"role": "user", "content": "영어 어근에 한국어 어미가 붙은 표현을 자연스러운 "
                                "한국어로 바꿔 답해. 답만 한 줄.\n표현: robust한"},
    {"role": "assistant", "content": "안정적인"},
    {"role": "user", "content": "표현: graceful하게"},
    {"role": "assistant", "content": "매끄럽게"},
    {"role": "user", "content": "표현: flaky할"},
    {"role": "assistant", "content": "불안정할"},
]

TERM_FEWSHOT = [
    {"role": "user", "content": "소프트웨어 용어의 한국어 번역어만 답해.\n"
                                "용어: garbage collection"},
    {"role": "assistant", "content": "가비지 컬렉션"},
    {"role": "user", "content": "용어: connection pool"},
    {"role": "assistant", "content": "커넥션 풀"},
    {"role": "user", "content": "용어: race condition"},
    {"role": "assistant", "content": "경쟁 조건"},
]

# a sane gloss: short, Hangul-only (음차 포함), no leftover English
TERM_ANSWER_RE = re.compile(r"^[가-힣][가-힣0-9· ]{0,24}$")


def run_term_task(task):
    """Few-shot micro-call: the model answers with the Korean only and
    Python assembles the `한국어(원어)` gloss."""
    if task["kind"] == "slang":
        fewshot, label = SLANG_FEWSHOT, "표현: "
    else:
        fewshot, label = TERM_FEWSHOT, "용어: "
    out = call_ollama(None, None,
                      messages=fewshot + [
                          {"role": "user", "content": label + task["src"]}],
                      num_predict=24, stop=["\n"], num_ctx=1024)
    out = clean_output(out).strip().strip('"').rstrip(".")
    # strip a parenthesized echo of the source term if the model added one
    out = re.sub(r"\s*\([^)]*\)\s*$", "", out).strip()
    if not TERM_ANSWER_RE.match(out):
        log("term task rejected %r -> %r" % (task["src"], out))
        return None
    if re.search(r"않|없", out) and not re.search(r"않|없", task["src"]):
        log("term task rejected (negation added) %r -> %r"
            % (task["src"], out))
        return None
    if out.replace(" ", "").lower() == task["src"].replace(" ", "").lower():
        return None                     # model echoed the term - keep as-is
    root = re.match(r"[A-Za-z+#.-]+", task["src"])
    orig = root.group(0) if task["kind"] == "slang" and root else task["src"]
    return "%s(%s)" % (out, orig)


def run_sent_task(task):
    # generous runaway guard: Korean tokenizes to ~4.5 tok/char, so this
    # never truncates a faithful answer but stops a pathological loop
    cap = len(task["src"]) * 6 + 48
    out = call_ollama(
        "너는 한국어 문장 교정기다. 지시받은 것만 고치고 다른 단어는 바꾸지 "
        "않는다. [[CODE_숫자]] 표시는 그 자리에 그대로 둔다. 고친 문장만 "
        "출력한다. 설명을 붙이지 않는다.\n지시: "
        + "; ".join(task["wants"]),
        task["src"], num_predict=cap)
    out = clean_output(out).strip()
    if not out or missing_numbers(task["src"], out):
        return None
    ratio = len(out) / max(1, len(task["src"]))
    if not 0.7 <= ratio <= 1.9:
        log("sent task rejected (ratio %.2f): %r" % (ratio, task["src"][:40]))
        return None
    if any("합니다" in w for w in task["wants"]) and \
            not re.search(r"(니다|시오)[.!?]\s*$", out.strip()):
        log("sent task rejected (register): %r" % out[-20:])
        return None
    if re.search(r"\[\[\s*CODE_", task["src"]) and \
            sorted(PLACEHOLDER_RE.findall(task["src"])) != \
            sorted(PLACEHOLDER_RE.findall(out)):
        return None
    return out


# particle pairs: (needs-batchim form, no-batchim form)
PARTICLES = [("으로", "로"), ("이", "가"), ("을", "를"), ("은", "는"),
             ("과", "와")]


def fix_particle(text, pos):
    """After replacing a span ending at pos, re-select the particle that
    follows so it agrees with the new final Hangul syllable. For a glossed
    term `한국어(term)` the syllable before the parenthesis governs."""
    head = text[:pos]
    base = re.sub(r"\([^()]*\)$", "", head)     # ignore the (원어) tail
    if not base or not ("가" <= base[-1] <= "힣"):
        return text
    has_batchim = _jong(base[-1]) not in (0,)
    rieul = _jong(base[-1]) == 8
    for with_b, without_b in PARTICLES:
        for cand in (with_b, without_b):
            if text[pos:pos + len(cand)] != cand:
                continue
            if cand in ("으로", "로"):
                right = "로" if (not has_batchim or rieul) else "으로"
            else:
                right = with_b if has_batchim else without_b
            if cand != right:
                return text[:pos] + right + text[pos + len(cand):]
            return text
    return text


def apply_merges(text, results):
    """Replace spans back into the text, last span first so offsets hold."""
    for task, out in sorted(results, key=lambda r: -r[0]["start"]):
        if out is None:
            continue
        text = text[:task["start"]] + out + text[task["end"]:]
        if task["kind"] in ("term", "slang"):
            text = fix_particle(text, task["start"] + len(out))
    return text


def run_wave(tasks, runner):
    # micro-tasks are small enough for ollama to actually run concurrently
    workers = max(1, min(int(CFG["parallel_tasks"]), len(tasks)))
    if not tasks:
        return []
    if workers == 1:
        outs = [runner(t) for t in tasks]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outs = list(pool.map(runner, tasks))
    return list(zip(tasks, outs))


def translate_dispatch(text):
    masked, blocks = mask_code(apply_mechanical(text))
    terms = find_term_tasks(masked)
    log("dispatch: %d term task(s): %s"
        % (len(terms), [t["src"] for t in terms]))
    masked = apply_merges(masked, run_wave(terms, run_term_task))
    sents = find_sentence_tasks(masked)
    log("dispatch: %d sentence task(s)" % len(sents))
    masked = apply_merges(masked, run_wave(sents, run_sent_task))
    return collect_actions(unmask_code(masked, blocks))


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


def call_ollama(system_prompt, user_text, messages=None, num_predict=None,
                stop=None, num_ctx=None):
    payload = {
        "model": CFG["model"],
        "stream": False,
        "keep_alive": CFG["keep_alive"],
        "messages": messages or [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "options": {
            "temperature": CFG["temperature"],
            "num_ctx": num_ctx or CFG["num_ctx"],
            **({"num_predict": num_predict} if num_predict else {}),
            **({"stop": stop} if stop else {}),
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


THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)


def clean_output(text):
    text = THINK_RE.sub("", text)      # reasoning models leak their thinking
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
    if CFG["mode"] == "dispatch":
        return translate_dispatch(text)
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

    if CFG["mode"] == "regex":
        inside = fence_parity_before(directory, index) == 1
        shown, _ = regex_transform(delta, inside, at_start=(index == 0))
        if is_final:
            try:
                full = collect_parts(directory, index, CFG["part_wait_sec"])
                with_actions = collect_actions(full)
                if len(with_actions) > len(full):
                    shown += with_actions[len(full):]
            except Exception as exc:
                log("action block failed: %s" % exc)
            prune_old_buffers()
        emit(shown)

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
