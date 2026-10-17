#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 备份配置助手
================================================================================
作用：把当前 git 仓库关联到你的 GitHub 远程仓库并推送。
      第一次运行会弹出浏览器让你登录 GitHub（Git Credential Manager），
      之后凭据会被记住，日常只需要 git push。

用法：
    cd D:\\Ai_agent\\deepseek-ha\\test-system\\上位机
    python setup_github.py

它会依次：
    ① 检查当前目录是不是 git 仓库、有没有未提交的改动
    ② 问你的 GitHub 用户名和仓库名 → 拼出远程地址
    ③ 配好 remote（若已存在则询问是否改地址）
    ④ 测试连通性（这一步完成 GitHub 登录授权）
    ⑤ 询问后推送 分支 + 标签
    ⑥ 打印仓库网页地址，方便你去核对
================================================================================
"""

import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 这个脚本在 上位机/ 下，git 仓库根在上一级
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)

SEP = "=" * 74


def head(t):
    print()
    print(SEP)
    print("  " + t)
    print(SEP)


def git(*args, check=False, capture=True):
    """执行 git 命令，返回 (returncode, stdout+stderr)"""
    cmd = ["git"] + list(args)
    try:
        p = subprocess.run(cmd, cwd=REPO_DIR, capture_output=capture,
                           text=True, encoding="utf-8", errors="replace")
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        if check and p.returncode != 0:
            print("  [!] git %s 失败：\n%s" % (" ".join(args), out))
        return p.returncode, out
    except FileNotFoundError:
        print("  [X] 找不到 git 命令，请先安装 Git for Windows")
        sys.exit(1)


def ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except EOFError:
        # 输入流结束（管道/重定向）——当作用户取消，干净退出而不是抛异常
        print()
        raise KeyboardInterrupt
    return s if s else default


def main():
    print(SEP)
    print("  GitHub 备份配置助手")
    print(SEP)
    print("  仓库目录：%s" % REPO_DIR)

    # ---------- ① 检查仓库 ----------
    head("① 检查当前仓库")
    rc, top = git("rev-parse", "--show-toplevel")
    if rc != 0:
        print("  [X] 这个目录不是 git 仓库。请先确认路径，或在项目里执行 git init。")
        return 1
    print("  仓库根目录：%s" % top)

    rc, branch = git("branch", "--show-current")
    branch = branch or "main"
    print("  当前分支：%s" % branch)

    rc, ncommit = git("rev-list", "--count", "HEAD")
    print("  提交数：%s" % (ncommit or "0"))

    rc, tags = git("tag", "-l")
    tag_list = [t for t in tags.splitlines() if t.strip()]
    print("  标签：%s" % (", ".join(tag_list) if tag_list else "(无)"))

    rc, dirty = git("status", "--porcelain")
    if dirty:
        print("\n  [!] 有未提交的改动：")
        for line in dirty.splitlines()[:15]:
            print("      %s" % line)
        ans = ask("\n  要先把这些改动提交吗？(y/n) [y]: ", "y").lower()
        if ans.startswith("y"):
            msg = ask("  提交信息: ", "备份：提交前保存改动")
            git("add", "-A")
            rc, out = git("commit", "-m", msg)
            print("  %s" % out.splitlines()[0] if out else "")
    else:
        print("  工作区干净 ✓")

    # ---------- ② 输入远程信息 ----------
    head("② 填写 GitHub 仓库信息")
    print("  提示：请先在 github.com 上建一个【空仓库】")
    print("        （⚠️ 不要勾选 Add README / .gitignore / license）")
    print()

    user = ask("  GitHub 用户名  : ")
    if not user:
        print("  [X] 用户名不能为空")
        return 1
    repo = ask("  仓库名 [iv-measurement]: ", "iv-measurement")

    url_https = "https://github.com/%s/%s.git" % (user, repo)
    web_url = "https://github.com/%s/%s" % (user, repo)
    print("\n  远程地址：%s" % url_https)

    # ---------- ③ 配置 remote ----------
    head("③ 配置远程仓库（git remote）")
    rc, cur = git("remote", "get-url", "origin")
    if rc == 0 and cur:
        print("  当前 origin = %s" % cur)
        if cur == url_https:
            print("  与要配置的地址相同，跳过 ✓")
        else:
            ans = ask("  要改成上面的新地址吗？(y/n) [y]: ", "y").lower()
            if ans.startswith("y"):
                git("remote", "set-url", "origin", url_https)
                print("  已更新 origin ✓")
    else:
        git("remote", "add", "origin", url_https)
        print("  已添加 origin ✓")

    rc, out = git("remote", "-v")
    for line in out.splitlines():
        print("      %s" % line)

    # ---------- ④ 测试连通性（会触发登录授权）----------
    head("④ 测试连通性（这一步会弹出浏览器让你登录 GitHub）")
    print("  正在连接 %s …" % url_https)
    print("  → 若弹出窗口，请选 GitHub，然后点 Sign in with your browser 完成授权")
    print()
    rc, out = git("ls-remote", "--heads", "origin")
    if rc != 0:
        print("  [X] 连接失败，输出如下：")
        for line in out.splitlines()[:12]:
            print("      %s" % line)
        print("\n  常见原因：")
        print("    · 仓库地址填错（用户名/仓库名拼写）")
        print("    · GitHub 上还没建这个仓库")
        print("    · 网络到 GitHub 不通（可试 Gitee，见《17-备份到GitHub.md》第九节）")
        print("    · 授权窗口被关掉了 → 可手动运行: git credential-manager github login")
        return 1
    print("  连接成功 ✓")

    # ---------- ⑤ 推送 ----------
    head("⑤ 推送")
    ans = ask("  现在推送 【分支 + 标签】 到 GitHub 吗？(y/n) [y]: ", "y").lower()
    if not ans.startswith("y"):
        print("  已跳过。以后可以手动执行：")
        print("      git push -u origin %s" % branch)
        print("      git push origin --tags")
        return 0

    print("\n  → 推送分支 %s …" % branch)
    rc, out = git("push", "-u", "origin", branch)
    for line in out.splitlines()[-8:]:
        print("      %s" % line)
    if rc != 0:
        print("\n  [!] 推送分支失败。若提示 rejected（远程非空），先执行：")
        print("      git pull --rebase origin %s" % branch)
        return 1

    if tag_list:
        print("\n  → 推送 %d 个标签 …" % len(tag_list))
        rc, out = git("push", "origin", "--tags")
        for line in out.splitlines()[-6:]:
            print("      %s" % line)

    # ---------- ⑥ 结果 ----------
    head("⑥ 完成")
    rc, out = git("branch", "-vv")
    for line in out.splitlines():
        print("      %s" % line)
    print()
    print("  仓库网页：%s" % web_url)
    print("  刷新页面核对一下：应该有 %s 个文件、%d 个标签" % (ncommit, len(tag_list)))
    print()
    print("  以后同步只需两步：")
    print("      git commit -m \"说明\"    （存本地快照）")
    print("      git push                 （同步到 GitHub）")
    print(SEP)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已取消")
        sys.exit(130)
