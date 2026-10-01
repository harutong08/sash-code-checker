#!/usr/bin/env python3
"""Claude Code のセッションを Obsidian 用 Markdown ノートにして notes/<ジャンル>/ に書く。

Stop フックから呼ばれる（stdin に hook 入力の JSON）。ノートはセッションごとに1ファイルで、
毎ターン上書きする（ジャンルや題名が変われば古いファイルを消して移す）。
コミット・push はしない（通常の作業と一緒にコミットされ、GitHub Actions が main に同期する）。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

NOTES_DIR = "notes"
MAX_DIFF_LINES = 1500   # 差分が長すぎるとObsidianが重くなるので打ち切る
MAX_REPLY_CHARS = 1200  # 各ターンの最終応答の抜粋の長さ
JST = timezone(timedelta(hours=9))


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else ""


def read_transcript(path):
    """人間の依頼と、それに対する最終テキスト応答の組を返す。"""
    turns, title, current = [], "", None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return title, turns
    for line in lines:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("type")
        if t == "ai-title":
            title = d.get("aiTitle") or title
        elif t == "user" and (d.get("origin") or {}).get("kind") == "human":
            c = d.get("message", {}).get("content")
            text = c if isinstance(c, str) else "\n".join(
                b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                current = {"time": d.get("timestamp", ""), "prompt": text.strip(), "reply": ""}
                turns.append(current)
        elif t == "user" and isinstance(d.get("message", {}).get("content"), str):
            current = None  # フックの指摘など人間以外の発言の後の応答は、依頼の結果に含めない
        elif t == "assistant" and current is not None:
            texts = [b.get("text", "") for b in d.get("message", {}).get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(s.strip() for s in texts):
                current["reply"] = "\n".join(texts).strip()  # ターン内で最後のテキストを残す
    return title, turns


def default_branch(cwd):
    ref = git(cwd, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")
    if ref:
        return ref.rsplit("/", 1)[-1]
    for b in ("main", "master"):
        if git(cwd, "rev-parse", "-q", "--verify", f"origin/{b}"):
            return b
    return ""


# ジャンル判定。上から順に最初に当たったものを採用する。
GENRE_RULES = [
    ("バグ修正", r"バグ|不具合|直して|直す|エラー|おかしい|動かない|ずれ|誤読|壊れ|\bfix"),
    ("設定・自動化", r"フック|設定|自動|hook|ワークフロー|workflow|\bCI\b|権限|環境"),
    ("機能追加", r"追加|作って|作る|実装|機能|ほしい|欲しい|したい|対応"),
]
AUTOMATION_PATHS = (".claude/", ".github/", "automation/")


def changed_files(cwd, ref, exclude):
    files = git(cwd, "diff", "--name-only", ref, "--", ".", exclude).splitlines()
    files += git(cwd, "ls-files", "--others", "--exclude-standard", "--", ".", exclude).splitlines()
    return [f for f in files if f]


def classify(title, turns, files):
    if not files:
        return "調査・相談"
    if all(f.endswith(".md") for f in files):
        return "ドキュメント"
    if all(f.startswith(AUTOMATION_PATHS) for f in files):
        return "設定・自動化"
    text = title + "\n" + "\n".join(t["prompt"] for t in turns)
    for genre, pat in GENRE_RULES:
        if re.search(pat, text, re.IGNORECASE):
            return genre
    return "開発"


def base_ref(cwd):
    base_branch = default_branch(cwd)
    base = git(cwd, "merge-base", "HEAD", f"origin/{base_branch}") if base_branch else ""
    return base_branch, base


def code_section(cwd, base):
    exclude = f":(exclude){NOTES_DIR}"  # ノート自身を差分に含めない
    ref = base or "HEAD"
    out = []
    if base:
        log = git(cwd, "log", "--format=- `%h` %s", f"{base}..HEAD", "--", ".", exclude)
        out.append(f"## コミット（セッション開始時点 `{base[:7]}` から）\n\n{log or '（なし）'}\n")
    stat = git(cwd, "diff", "--stat", ref, "--", ".", exclude)  # コミット済み＋未コミット
    diff = git(cwd, "diff", "--no-color", ref, "--", ".", exclude)
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard", "--", ".", exclude)
    if untracked:
        stat += ("\n" if stat else "") + "\n".join(f" {f} (新規・未コミット)" for f in untracked.splitlines())
    if stat:
        out.append(f"## 変更ファイル\n\n```\n{stat}\n```\n")
    if diff:
        lines = diff.splitlines()
        cut = ""
        if len(lines) > MAX_DIFF_LINES:
            cut = f"\n（差分が長いため {MAX_DIFF_LINES}/{len(lines)} 行で省略）"
            lines = lines[:MAX_DIFF_LINES]
        body = "\n".join(lines).replace("````", "` ` ` `")
        out.append(f"## コード差分\n\n````diff\n{body}\n````{cut}\n")
    else:
        out.append("## コード差分\n\n（変更なし）\n")
    return "\n".join(out)


def main():
    hook = json.load(sys.stdin)
    if hook.get("stop_hook_active"):
        return  # 他の Stop フックの指摘で続行したターンでは書き直さない（無限ループ防止）
    cwd = Path(hook.get("cwd") or os.getcwd())
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return
    root = Path(root)

    title, turns = read_transcript(hook.get("transcript_path", ""))
    if not turns:
        return
    repo = re.sub(r"^.*github\.com[/:]|\.git$", "", git(root, "remote", "get-url", "origin")) or root.name
    branch = git(root, "branch", "--show-current") or "(detached)"
    sid = hook.get("session_id", "unknown")
    remote_sid = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")
    link = f"https://claude.ai/code/session_{remote_sid.split('_', 1)[-1]}" if remote_sid else f"`{sid}`"
    now = datetime.now(JST)
    started = turns[0]["time"] or now.isoformat()
    title = title or turns[0]["prompt"].splitlines()[0][:40]
    # 差分の起点はセッション最初のノート作成時に決めて保存し、以後は使い回す
    # （作業ブランチを main にマージした後も、差分とジャンルが消えないようにするため）
    existing = list((root / NOTES_DIR).rglob(f"*_{sid[:8]}.md"))
    m = re.search(r"^base: ([0-9a-f]{40})$", existing[0].read_text(encoding="utf-8"), re.M) if existing else None
    base = m.group(1) if m else (base_ref(root)[1] or git(root, "rev-parse", "HEAD"))
    genre = classify(title, turns, changed_files(root, base or "HEAD", f":(exclude){NOTES_DIR}"))

    parts = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"repo: {repo}",
        f"branch: {branch}",
        f"session: {sid}",
        f"started: {started}",
        f"updated: {now.isoformat(timespec='seconds')}",
        f"genre: {genre}",
        f"base: {base}",
        f"tags: [claude-code, session, genre/{genre.replace('・', '_')}]",
        "---",
        f"# {title}\n",
        f"- リポジトリ: `{repo}`（ブランチ `{branch}`）",
        f"- ジャンル: {genre}",
        f"- セッション: {link}",
        "",
        "## 依頼と結果\n",
    ]
    for i, t in enumerate(turns, 1):
        reply = t["reply"]
        if len(reply) > MAX_REPLY_CHARS:
            reply = reply[:MAX_REPLY_CHARS] + " …"
        quoted = "\n".join("> " + l for l in t["prompt"].splitlines())
        parts.append(f"### {i}. 依頼\n\n{quoted}\n\n**結果**\n\n{reply or '（応答なし）'}\n")
    parts.append(code_section(root, base))

    safe_title = re.sub(r'[\\/:*?"<>|#^\[\]\s]+', "_", title).strip("_")[:40] or "session"
    target = root / NOTES_DIR / genre / f"{started[:10]}_{safe_title}_{sid[:8]}.md"
    for old in (root / NOTES_DIR).rglob(f"*_{sid[:8]}.md"):  # ジャンル・題名が変わった古いノート
        if old != target:
            old.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # ノート作成の失敗で作業を止めない
