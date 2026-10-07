"""The instructions `decyde setup` writes into each agent's global instruction file."""
from __future__ import annotations

BEGIN, END = "<!-- decyde:begin -->", "<!-- decyde:end -->"

TEMPLATE = """## decyde (asking {name} for a decision)

{name} runs many agents at once and misses questions buried in terminal output.
When you need {name} to decide something (a product or design choice, scope,
spending, anything destructive or outward-facing, or a genuine ambiguity you
cannot resolve from the code or sensible defaults), post it with the `decyde`
CLI instead of only asking in your chat output.

**Ask** (always identify yourself and what you are working on):

```bash
decyde ask --agent claude|codex|grok|antigravity|gemini|other \\
  --name "<model + your role, e.g. 'Codex, billing lane'>" \\
  --task "<what you are working on right now>" \\
  --title "<one-line summary of the decision>" \\
  --question "<the full question>" \\
  --context "<what {name} needs to know to decide>" \\
  --option "<choice A>" --option "<choice B>" \\
  --recommend "<your pick and why>" \\
  --urgency high    # only if you are fully blocked; otherwise omit (normal) or use low
```

It prints a question number, e.g. `#12`. Run it from your project directory so
the project, git branch and terminal pane are captured automatically.

**While you wait:** keep working on anything the decision does not block. Do not
guess the answer to the blocked part.

**Getting the answer:**
- The answer may arrive on its own as a message starting with `[decyde]`
  (typed into your Herdr or tmux pane, or handed back by a Claude Code hook).
- Also check back yourself: `decyde check 12` exits 0 and prints `ANSWER:` when
  answered, 2 while still open, 3 if dismissed. Check at natural breakpoints and
  before you finish your turn.
- If you have nothing else to do, block on it: `decyde wait 12 --timeout 900`.
  Otherwise keep each command under a couple of minutes while a question is
  open: an answer handed over between tool calls waits for a running one.
- When you have the answer, run `decyde ack 12` and act on it.
- If the question stops mattering: `decyde cancel 12 --reason "..."`.

Before ending a turn with an open question, say which question number you are
waiting on."""


def block(name: str) -> str:
    return f"{BEGIN}\n{TEMPLATE.format(name=name)}\n{END}"


def upsert(text: str, name: str) -> str:
    """Insert or replace the decyde block in an instruction file's text."""
    new = block(name)
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        return head + new + rest.split(END, 1)[1]
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + new + "\n"


def remove(text: str) -> str:
    if BEGIN not in text or END not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    return head.rstrip("\n") + "\n" + rest.split(END, 1)[1].lstrip("\n")
