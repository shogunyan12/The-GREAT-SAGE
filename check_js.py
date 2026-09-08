"""Catch inline-script damage that makes the HUD render nothing.

Two failures, both of which have actually shipped, and both of which look
identical from outside: the page is black or blank, the WebSocket never
opens, and the Python side logs a completely healthy startup because the
fault is entirely in the browser.

1. A STRING LITERAL BROKEN ACROSS LINES. A single unescaped newline inside
   a '...' or "..." literal is a SyntaxError, and it kills the whole
   inline script. That shipped once: the literal was written through a
   bash heredoc, which ate the backslash in an escape and left a real line
   break inside quotes.

2. UNBALANCED BRACES. An edit that removes a block but leaves one "}"
   behind is the same failure with a different cause. That shipped too - a
   region was cut by index arithmetic that landed inside a function body,
   and the string-literal check passed it because every literal was fine.

Both need a real scanner rather than a character count: "//" inside
'ws://host' is not a comment, a double quote inside a single-quoted string
is not a delimiter, and - the one that actually bit this file - the "\\//"
at the end of /^image\\// is not a comment either. Treating it as one
swallowed the rest of the line, including an opening brace, and reported a
perfectly good file as broken.

So regex literals are parsed properly, using the standard rule: a "/" is a
regex when the last significant thing before it cannot end an expression,
and division when it can.

Template literals legitimately span lines, so they are tracked but never
reported as broken.
"""

import io
import re
import sys

BS = chr(92)
NL = chr(10)

# Characters that cannot END an expression. A "/" following one of these
# starts a regex literal; a "/" following an identifier, a number, ")" or
# "]" is division.
REGEX_PREV_CHARS = set("(,=:[!&|?{};+-*%~^<>" + NL)
REGEX_PREV_WORDS = {"return", "typeof", "instanceof", "in", "of", "new",
                    "delete", "void", "do", "else", "case", "yield", "await"}


def _prev_significant(js, i):
    """(last non-space char before i, the word ending there)."""
    j = i - 1
    while j >= 0 and js[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return "", ""
    c = js[j]
    word = ""
    if c.isalnum() or c == "_":
        k = j
        while k >= 0 and (js[k].isalnum() or js[k] == "_"):
            k -= 1
        word = js[k + 1:j + 1]
    return c, word


def _skip_regex(js, i):
    """i is at the opening '/'. Index just past the closing '/', or None.

    None means this was not a regex after all (it hit a line break, which
    a regex literal cannot contain), so the caller should treat it as
    division and carry on.
    """
    j, n = i + 1, len(js)
    in_class = False
    while j < n:
        c = js[j]
        if c == BS:
            j += 2
            continue
        if c == NL:
            return None
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "/":
            return j + 1
        j += 1
    return None


def scan(js):
    """Walk the script once.

    Returns (broken_literal_lines, end_depth, first_negative_line).
    """
    bad = []
    state = None              # None | "'" | '"' | "`" | "block"
    depth = 0
    negative_at = None
    line_no = 1
    line_start = 0
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c == NL:
            if state in ("'", '"'):
                bad.append((line_no, js[line_start:i].strip()[:100]))
                state = None      # resync so one break is not reported twice
            line_no += 1
            line_start = i + 1
            i += 1
            continue
        if state == "block":
            if c == "*" and i + 1 < n and js[i + 1] == "/":
                state = None
                i += 2
                continue
            i += 1
            continue
        if state in ("'", '"', "`"):
            if c == BS:
                i += 2
                continue
            if c == state:
                state = None
            i += 1
            continue
        # Not inside a string or a block comment.
        if c == "/" and i + 1 < n:
            if js[i + 1] == "/":
                j = js.find(NL, i)
                i = n if j < 0 else j
                continue
            if js[i + 1] == "*":
                state = "block"
                i += 2
                continue
            prev_char, prev_word = _prev_significant(js, i)
            if prev_char in REGEX_PREV_CHARS or prev_word in REGEX_PREV_WORDS \
                    or prev_char == "":
                end = _skip_regex(js, i)
                if end is not None:
                    line_no += js.count(NL, i, end)
                    i = end
                    continue
        if c in ("'", '"', "`"):
            state = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0 and negative_at is None:
                negative_at = line_no
        i += 1
    return bad, depth, negative_at


def blank(js):
    """The script with strings, comments and regexes replaced by spaces.

    Same length and same line numbering as the original, so an offset
    found in the result points at the same place in the real file. Lets
    the check below match identifiers without ever matching one that is
    really inside a string.
    """
    out = list(js)
    state = None
    i, n = 0, len(js)

    def wipe(a, b):
        for k in range(a, b):
            if out[k] != NL:
                out[k] = " "

    while i < n:
        c = js[i]
        if state == "block":
            if c == "*" and i + 1 < n and js[i + 1] == "/":
                wipe(i, i + 2)
                state = None
                i += 2
                continue
            wipe(i, i + 1)
            i += 1
            continue
        if state in ("'", '"', "`"):
            if c == BS:
                wipe(i, i + 2)
                i += 2
                continue
            wipe(i, i + 1)
            if c == state:
                state = None
            i += 1
            continue
        if c == NL:
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if js[i + 1] == "/":
                j = js.find(NL, i)
                j = n if j < 0 else j
                wipe(i, j)
                i = j
                continue
            if js[i + 1] == "*":
                state = "block"
                wipe(i, i + 2)
                i += 2
                continue
            prev_char, prev_word = _prev_significant(js, i)
            if prev_char in REGEX_PREV_CHARS or prev_word in REGEX_PREV_WORDS                     or prev_char == "":
                end = _skip_regex(js, i)
                if end is not None:
                    wipe(i, end)
                    i = end
                    continue
        if c in ("'", '"', "`"):
            state = c
            wipe(i, i + 1)
        i += 1
    return "".join(out)


# An identifier, but not one that is a property (obj.name) or a key.
_IDENT = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)")
_DECL = re.compile(r"(?:^|[;{}()\s])(?:const|let|class)\s+([A-Za-z_$][\w$]*)")
# Words that are syntax, not references.
_KEYWORDS = set("""var let const function class return if else for while do
switch case break continue new delete typeof instanceof in of this null
true false undefined try catch finally throw void yield await async
window document Math JSON Object Array String Number Boolean Date Promise
console setTimeout setInterval requestAnimationFrame THREE""".split())


def _is_object_key(src, start, end):
    """radius in "{ radius: 2.15 }" is a key, not a read of anything.

    An object literal opens a brace like any block, so its keys land at
    the same depth as real statements and match a same-named const
    further down. Preceded by "{" or "," and followed by ":" is the
    shape of a key and not of a ternary, whose ":" follows an operand.
    """
    j = end
    while j < len(src) and src[j] in " 	":
        j += 1
    if j >= len(src) or src[j] != ":":
        return False
    k = start - 1
    while k >= 0 and src[k] in " 	" + NL:
        k -= 1
    return k >= 0 and src[k] in "{,"


def check_tdz(js):
    """Straight-line code that names a const/let declared further down.

    THIS ONE SHIPPED, TWICE - most recently an array built near the top of
    the scene setup that listed starField, which is declared some five
    hundred lines below it. Reading a const before its declaration runs is
    a ReferenceError, and because it happens while the script is still
    initialising it takes the whole script with it: a blank HUD, a healthy
    Python log, and nothing in the page-error channel either, because the
    handler that forwards page errors is installed further down the very
    same script and never got to run.

    What counts is being in the SAME function body, not at some absolute
    depth - this whole HUD lives inside one big "(function () {", so every
    declaration in it is already one function deep. Each body gets its own
    id, and a reference is only reported when it sits in the same body as
    the declaration and comes before it. A reference from a nested
    function is fine: that body runs later, by which time the declaration
    has executed. That is also what keeps this quiet enough to gate on.
    """
    src = blank(js)
    n = len(src)

    # Which function body every offset belongs to. 0 is the script itself.
    body_at = [0] * (n + 1)
    depth_at = [0] * (n + 1)
    # A function's PARAMETERS sit outside its own braces, so they land at
    # the depth of the code around it - which puts "function f(i)" at the
    # same depth as a "for (let i = 0; ...)" further down and reports the
    # parameter as reading it early. Parameters are names being bound, not
    # values being read, so they are masked out here.
    in_params = [False] * (n + 1)
    body_of_brace = {}     # brace depth -> body id opened at that depth
    depth = 0
    stack = [0]
    next_id = 1
    pending_fn = False     # a "function" or "=>" seen, its "{" not yet
    i = 0
    while i < n:
        c = src[i]
        if c == "{":
            depth += 1
            if pending_fn:
                stack.append(next_id)
                body_of_brace[depth] = next_id
                next_id += 1
                pending_fn = False
        elif c == "}":
            if body_of_brace.get(depth) == stack[-1] and len(stack) > 1:
                body_of_brace.pop(depth, None)
                stack.pop()
            depth -= 1
        elif c == "=" and i + 1 < n and src[i + 1] == ">":
            pending_fn = True
            body_at[i] = stack[-1]
            body_at[i + 1] = stack[-1]
            i += 2
            continue
        elif (c == "f" and src.startswith("function", i)
              and not (i and (src[i - 1].isalnum() or src[i - 1] in "_$."))):
            pending_fn = True
            open_paren = src.find("(", i)
            brace = src.find("{", i)
            if open_paren != -1 and (brace == -1 or open_paren < brace):
                par, j = 0, open_paren
                while j < n:
                    if src[j] == "(":
                        par += 1
                    elif src[j] == ")":
                        par -= 1
                        if par == 0:
                            break
                    j += 1
                for k in range(open_paren, min(j + 1, n)):
                    in_params[k] = True
        body_at[i] = stack[-1]
        depth_at[i] = depth
        i += 1

    # Keyed by body AND brace depth, which is what makes this quiet
    # enough to gate on. Without the depth, a function's PARAMETER called
    # radius matches some unrelated "const radius" nested three blocks
    # down inside another function, and the check drowns in noise about
    # code that has always been fine. Same body, same depth means the two
    # really are the same straight-line sequence of statements.
    declared = {}
    for m in _DECL.finditer(src):
        at = m.start(1)
        declared.setdefault((body_at[at], depth_at[at], m.group(1)), at)

    bad = []
    for m in _IDENT.finditer(src):
        name = m.group(1)
        at = m.start(1)
        if (name in _KEYWORDS or in_params[at]
                or _is_object_key(src, m.start(1), m.end(1))):
            continue
        decl_at = declared.get((body_at[at], depth_at[at], name))
        if decl_at is None or at >= decl_at:
            continue
        bad.append((src.count(NL, 0, at) + 1, name,
                    src.count(NL, 0, decl_at) + 1))
    return bad


def check(path):
    html = io.open(path, encoding="utf-8", newline="").read()
    html = html.replace(chr(13) + NL, NL)
    problems = 0
    for m in re.finditer(r"<script([^>]*)>([\s\S]*?)</script>", html):
        if "src=" in m.group(1):
            continue
        base = html[:m.start()].count(NL)
        bad, depth, negative_at = scan(m.group(2))
        for ln, text in bad:
            print("  %s:%d: unterminated string literal" % (path, base + ln))
            print("      " + text)
            problems += 1
        if negative_at is not None:
            print("  %s:%d: a closing brace with nothing open"
                  % (path, base + negative_at))
            problems += 1
        elif depth != 0:
            print("  %s: script ends %d brace(s) %s"
                  % (path, abs(depth), "open" if depth > 0 else "over-closed"))
            problems += 1
        for ln, name, decl_ln in check_tdz(m.group(2)):
            print("  %s:%d: %s is read here but not declared until line %d"
                  % (path, base + ln, name, base + decl_ln))
            print("      reading a const before its declaration throws, and "
                  "at load time that kills the whole script")
            problems += 1
    return problems


if __name__ == "__main__":
    targets = sys.argv[1:] or ["hud_prototype.html"]
    total = sum(check(t) for t in targets)
    if total:
        print(NL + "FAILED - %d problem(s). The inline script will not "
              "parse, so the HUD renders nothing." % total)
        raise SystemExit(1)
    print("OK - literals terminated, braces balanced, nothing read "
          "before it is declared in " + ", ".join(targets))
