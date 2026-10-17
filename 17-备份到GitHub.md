# 17 · 备份到 GitHub（含一键配置脚本）

> 你的环境已就绪：**Git Credential Manager 已启用**（v2.9.1），
> 第一次推送会**弹出浏览器让你登录 GitHub**，不需要手动创建 Token。

---

## 一、先理解：备份到 GitHub 是三件事

```
① GitHub 网页上建一个【空仓库】          ← 相当于云端的文件夹
② 本地仓库和它"连线"（git remote add）    ← 建立关系
③ 把提交推上去（git push）               ← 上传
```

做完这三步，以后每次只要 **`git push`** 一条命令就同步了。

---

## 二、第 1 步：在 GitHub 上建一个空仓库（网页操作）

1. 浏览器打开 **github.com** 并登录
2. 点右上角的 **`+`** → **New repository**
3. 按下面填：

| 字段 | 填什么 | 注意 |
|---|---|---|
| Repository name | `iv-measurement` | 名字随意，建议英文 |
| Description | `4 路 IV 测量系统（Arduino + 2×DMM6500）` | 可留空 |
| **Public / Private** | 建议选 **Private**（私人） | 私人仓库只有你能看；以后想公开随时能改 |
| Add a README file | ⚠️ **不要勾** | |
| Add .gitignore | ⚠️ **不要勾** | |
| Choose a license | ⚠️ **不要勾** | |

> ### ⚠️ 为什么那三个都不要勾？
> 因为**你本地已经有内容了**。如果网页上先建了 README，云端仓库就"非空"，
> 你推送时会被拒绝：`! [rejected] main -> main (fetch first)`。
> **记住：本地有内容时，云端一定要建"空仓库"。**
> （万一勾了也不怕，见第六节的处理办法。）

4. 点 **Create repository**
5. 创建成功后页面会显示仓库地址，形如：
   ```
   https://github.com/你的用户名/iv-measurement.git
   ```
   把这个地址记下来（或者直接复制）。

---

## 三、第 2 步 & 第 3 步：用脚本一键完成（推荐）

我写了个配置助手，它会：
- 检查当前 git 状态
- 让你输入 GitHub 用户名和仓库名
- 自动配好远程地址（`git remote add`）
- **测试连通性**（这一步就会弹出浏览器让你登录 GitHub）
- 询问后自动推送提交和标签
- 最后打印仓库网页地址

```powershell
cd D:\Ai_agent\deepseek-ha\test-system\上位机
python setup_github.py
```

跟着提示走就行。

### 如果你想手动做（两条命令）

```powershell
cd D:\Ai_agent\deepseek-ha\test-system

# ② 连线（把网址换成你的）
git remote add origin https://github.com/你的用户名/iv-measurement.git
git remote -v                     # 验证：应该显示 origin 两行

# ③ 推送
git push -u origin main           # -u 记住关联，以后直接 git push
git push origin --tags            # 把 v0.2.0 / v0.2.1 标签也推上去
```

**第一次推送时会发生什么**：
1. 弹出一个小窗口，让你选账户类型 → 选 **GitHub**
2. 再点 **Sign in with your browser** → 浏览器打开
3. 在浏览器里点 **Authorize**（授权）
4. 回到命令行，看到 `Writing objects: 100%` ✓

之后凭据被安全保存在 Windows 凭据管理器里，**再也不用输密码**。

---

## 四、验证备份成功

**命令行**：
```powershell
git log --oneline -1              # 看最新提交
git remote -v                     # 看远程地址
git branch -vv                    # 应该显示 main ... [origin/main]，说明已跟踪远程
```

**浏览器**：刷新你的仓库页面，应该能看到：
- 37 个文件（`上位机/`、`arduino测试/`、各种 `.md` 和插图）
- 右上角 **2 个 tag**（v0.2.0、v0.2.1）
- 提交历史 3 条

---

## 五、以后每天怎么用

```powershell
cd D:\Ai_agent\deepseek-ha\test-system

git status                    # 看改了啥
git add .                     # 暂存
git commit -m "说明改了什么"   # 存快照
git push                      # ★ 同步到 GitHub（一条命令）
```

**建议习惯**：每完成一小步就 `commit`，一天结束或告一段落时 `push`。
本地提交可以很多次，推送可以攒一起。

---

## 六、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| `error: remote origin already exists` | 已经配过了。改地址：`git remote set-url origin <新地址>` |
| `! [rejected] main -> main (fetch first)` | **云端仓库不是空的**（建仓库时勾了 README）→ 执行 `git pull --rebase origin main` 再 `git push` |
| `Authentication failed` / 反复要求登录 | 凭据错了 → 打开「控制面板 → 凭据管理器 → Windows 凭据」，删掉 `git:https://github.com` 再重试；或运行 `git credential-manager github logout` |
| 没有弹出浏览器窗口 | 手动运行 `git credential-manager github login` |
| `Support for password authentication was removed` | 说明你在手动输密码。GitHub 2021 年起不接受密码 → 用 GCM 弹窗登录，或建 Personal Access Token 当密码 |
| 想换账号 | `git credential-manager github logout` 然后重推 |
| push 很慢 / 卡住 | 网络问题（GitHub 在国内时快时慢）；可换 Gitee（见第九节） |
| 想看远程和本地的差异 | `git fetch origin` 然后 `git log origin/main..main --oneline` |

---

## 七、数据会上传吗？（重要，先看这条）

**不会。** `.gitignore` 已经排除了不该上传的东西：

| 排除项 | 原因 |
|---|---|
| `IV_data/`、`data/` | **测量数据**（几百 MB 且是你的实验成果） |
| `__pycache__/`、`build/`、`*.hex` | 运行/编译产生的中间文件 |
| `out.csv`、`*.tmp`、`*.log` | 临时导出与日志 |

**上传的只有代码和文档**（当前约 1 MB）。

**自查方法**：
```powershell
git ls-files                       # 列出将要上传的全部文件（37 个）
git ls-files | Measure-Object      # 数一下
```

> 💡 万一你不小心把不该传的文件提交了：先 `git rm --cached 文件名`（从版本库移除但保留本地），
> 把它加进 `.gitignore`，再提交一次即可。

---

## 八、想让仓库更漂亮（可选）

在仓库根目录建一个 `README.md`，GitHub 会自动把它显示为仓库首页。可以写：

```markdown
# 4 路 IV 测量系统

基于 Keithley DMM6500 ×2 + Arduino Nano 的多通道 IV 测量系统。

## 组成
- `arduino测试/` Arduino 固件（v1.1）
- `上位机/` Python 上位机（命令行 + 图形界面）
- `*.md` 设计文档、接线教程、采购清单

## 快速开始
见 `上位机/README.md`
```

---

## 九、附录：改用 Gitee（国内更快，可选）

GitHub 在国内访问有时不稳定。如果 `git push` 经常失败，可以改用 Gitee（码云）：

```powershell
# 在 gitee.com 建一个空仓库，然后：
git remote set-url origin https://gitee.com/你的用户名/iv-measurement.git
git push -u origin main
git push origin --tags
```

**也可以两个都配**（同时备份到两处）：

```powershell
git remote add github https://github.com/用户名/iv-measurement.git
git remote add gitee  https://gitee.com/用户名/iv-measurement.git
git push github main && git push github --tags
git push gitee  main && git push gitee  --tags
```

---

## 十、附录：改用 SSH（长期更省事，可选）

HTTPS + GCM 已经很好用了。如果你喜欢更"极客"的方式：

```powershell
# ① 生成密钥（一路回车，密码可留空）
ssh-keygen -t ed25519 -C "guilinbai@126.com"

# ② 复制公钥内容
Get-Content ~/.ssh/id_ed25519.pub | Set-Clipboard

# ③ 粘贴到 GitHub → Settings → SSH and GPG keys → New SSH key

# ④ 改远程地址为 SSH 形式
git remote set-url origin git@github.com:你的用户名/iv-measurement.git
ssh -T git@github.com        # 测试：出现 "Hi 用户名!" 就成功
```

---

## 十一、命令速查

```
【首次配置（做一次）】
git remote add origin <网址>     关联远程仓库
git push -u origin main          首次推送并记住关联
git push origin --tags           推送标签

【以后每次】
git add .                        暂存全部改动
git commit -m "说明"             存快照
git push                         同步到云端

【查看】
git remote -v                    看远程地址
git branch -vv                   看本地分支跟踪哪个远程
git ls-files                     看会上传哪些文件
git log origin/main..main        看还没推送的提交

【换地址 / 换账号】
git remote set-url origin <新网址>
git credential-manager github logout
```
