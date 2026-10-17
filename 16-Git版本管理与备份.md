# Git 版本管理与备份（零基础）--尝试giting

> 你现在的位置：仓库已建好，第一个快照 `d79b682`（标签 **v0.2.0**），分支 `main`。
> 本文教你**以后怎么自己用**。

---

## 一、git 到底在干什么

**给文件夹"拍快照"，随时能回到过去任何一个版本。**

你肯定见过这种文件名吧：

```
论文.docx
论文-修改版.docx
论文-最终版.docx
论文-最终版2.docx
论文-真的最终版.docx
```

git 就是用来**终结这种命名灾难**的：它把每次"值得记住的状态"存成一个快照（commit），你随时可以：
- 看每个快照改了什么
- 回到任何一个快照
- 找回误删的文件

对你这套测量系统尤其重要：**哪天改了代码发现不如以前好用，一条命令就能回到能用的版本。**

---

## 二、核心概念：三个区域

```
   工作区                 暂存区                  仓库
（你正在编辑的         （准备提交的清单）      （历史快照，永久保存）
   文件夹）
     │                     │                      │
     │      git add        │     git commit       │
     └────────────────────▶┴─────────────────────▶│
```

| 区域 | 是什么 | 类比 |
|---|---|---|
| **工作区** | 你实际编辑的文件（`D:\...\test-system\`） | 你手里的东西 |
| **暂存区** | "这批改动我准备存了"的清单 | 购物车 |
| **仓库** | 一次次快照组成的历史 | 收银台打印的存档 |

**为什么要中间加个"暂存区"？** 因为一次改动可能涉及很多文件，你可以挑着提交（比如"这次只提交界面改动，数据格式的改动下次再提"）。

---

## 三、每天只用到 5 个命令

| 命令 | 作用 | 什么时候用 |
|---|---|---|
| `git status` | 看现在有哪些改动 | **最常用**，心里没底就先敲它 |
| `git diff` | 看具体改了哪几行 | 提交前确认一下 |
| `git add .` | 把所有改动放进暂存区 | 准备提交 |
| `git commit -m "说明"` | 存一个快照 | 完成一小步就存 |
| `git log --oneline` | 看历史快照列表 | 想知道"存过哪些版本" |

---

## 四、标准工作流（照这个做就行）

每次改完代码，**四步**：

```powershell
cd D:\Ai_agent\deepseek-ha\test-system

git status                     # ① 看改了哪些文件（红色 = 未暂存，绿色 = 已暂存）
git diff                       # ② （可选）看具体改了什么，按 q 退出
git add .                      # ③ 全部加入暂存区
git commit -m "给界面加了温湿度子图"   # ④ 存快照
```

**提交信息怎么写**（中文可以，这个仓库已配好 UTF-8）：
- 说清楚"这次做了什么"，比如 `"修复 CSV 行数校验的 bug"`、`"第 3 期：接入 SHT31 温湿度"`
- 一句话就够，不必长篇大论

**多久提交一次？** —— **完成一个能说清楚的小改动就提交**。宁可多提交，不要攒一大坨。这样"回到过去"才有意义。

---

## 五、后悔药（最实用的部分）

| 你想干什么 | 命令 | 说明 |
|---|---|---|
| **扔掉还没提交的修改** | `git restore 文件名` | 恢复到上次提交的状态 ⚠️ 改动会真的消失 |
| 扔掉全部未提交的修改 | `git restore .` | 同上，对所有文件 |
| **撤销 `git add`**（还不想提交） | `git restore --staged 文件名` | 只从暂存区拿出来，文件内容不变 |
| **看看旧版本长什么样** | `git checkout v0.2.0` | 切到那个快照（只读状态），看完用 `git checkout main` 回来 |
| **找回误删的文件** | `git restore 文件名` | 若已提交过，就能恢复 |
| **改最后一次的提交信息** | `git commit --amend` | 会打开编辑器，改完保存 |
| **撤销某次提交（保留历史）** | `git revert <hash>` | 安全，会生成一个"反向"提交 |
| **彻底回退（危险）** | `git reset --hard <hash>` | ⚠️ 会丢弃之后的提交，新手别用 |

> 💡 **一条铁律**：只要你**提交过**，东西就丢不了。所以"多提交"本身就是最好的保险。

---

## 六、查看历史

```powershell
git log --oneline --decorate      # 一行一条，带标签（最常用）
git log --oneline --graph --all   # 图形化，能看分支
git show HEAD                     # 看最后一次提交的完整内容
git show d79b682 --stat           # 只看某次提交改了哪些文件
git show v0.2.0                   # 也可以用标签名
```

`HEAD` 是"当前位置"的代号，`HEAD~1` 是"上一个提交"，`HEAD~2` 再上一个……

---

## 七、标签：给重要版本起名字

标签是"给某个快照起个好记的名字"，比如发布一个版本时：

```powershell
git tag -a v0.3.0 -m "第 3 期：温湿度接入"   # 打标签
git tag                                      # 列出所有标签
git show v0.3.0                              # 看这个标签的详情
git checkout v0.3.0                          # 切到这个版本看看
```

**建议**：每完成一个"期"就打一个标签，将来想回到"第 2 期完成时的状态"就一条命令：

```powershell
git checkout v0.2.0
```

---

## 八、这个仓库已经帮你配好的设置

| 配置 | 值 | 为什么 |
|---|---|---|
| `core.quotepath` | `false` | 中文文件名正常显示（否则显示成 `\346\226\207` 这种乱码） |
| `i18n.commitEncoding` | `utf-8` | 中文提交信息不乱码 |
| `i18n.logOutputEncoding` | `utf-8` | `git log` 里的中文正常显示 |
| `core.autocrlf` | `false` | 换行符原样保存，避免"整个文件都显示被改动" |
| 默认分支名 | `main` | 现代惯例（老教程里叫 `master`） |

**`.gitignore`**（在项目根目录）已排除这些**不该进版本库**的东西：

- `__pycache__/` —— Python 每次运行都生成的缓存
- `out.csv` 等临时导出文件
- `build/`、`*.hex` —— Arduino 编译产物
- `data/`、`IV_data/` —— **测量数据绝不进版本库**（数据是数据，代码是代码）

> 想加新规则就编辑 `.gitignore`，一行一个模式。

---

## 九、常见问题

| 现象 | 原因 / 解决 |
|---|---|
| 中文文件名显示成 `\346\226\207...` | `git config --global core.quotepath false`（**已设**） |
| 提交信息或 log 中文乱码 | 终端先执行 `chcp 65001`；本仓库已配 UTF-8 |
| 提示 `LF will be replaced by CRLF` | 已用 `core.autocrlf=false` 规避 |
| 提示 `Please tell me who you are` | `git config --global user.name "你的名字"` 和 `user.email "邮箱"`（**已设**：guilin / guilinbai@126.com） |
| `git status` 里东西太多看着晕 | `git status --short` 更紧凑 |
| 想把某个文件从版本库删掉但保留在磁盘 | `git rm --cached 文件名`，并加进 `.gitignore` |
| `git log` 卡住不动 | 那是进入了分页器，按 `q` 退出（或按空格往下翻） |

---

## 十、以后想云端备份（可选，推荐）

本地仓库的问题是：**硬盘坏了就全没了**。想双保险就推到云端（Gitee 国内快，GitHub 国际通用）：

```powershell
# ① 在 gitee.com 注册，新建一个【空】仓库（别勾选初始化 README）
# ② 本地关联远程仓库（把网址换成你的）
git remote add origin https://gitee.com/你的用户名/iv-measurement.git

# ③ 首次推送
git push -u origin main
git push origin --tags

# ④ 以后每次提交完，一条命令同步
git push
```

查看远程配置：`git remote -v`

> ⚠️ 别把测量数据推上去（已在 `.gitignore` 里排除）—— 云端仓库适合放代码和文档，不适合放几百 MB 的数据。

---

## 十一、命令速查表（打印出来贴桌上）

```
【每天用】
git status                      看改动
git diff                        看具体内容
git add .                       暂存全部
git commit -m "说明"            存快照
git log --oneline               看历史

【后悔药】
git restore 文件                扔掉未提交的修改
git restore --staged 文件       撤销 add
git checkout v0.2.0             回到某个版本看看
git checkout main               回到最新
git revert <hash>               安全撤销某次提交

【版本标记】
git tag -a v0.3.0 -m "说明"     打标签
git tag                         列出标签

【云端（配好后）】
git remote add origin <网址>    关联远程
git push -u origin main         首次推送
git push                        以后同步
git push origin --tags          推送标签
```

---

## 十二、一句话总结

> **`git add .` + `git commit -m "说明"` 就是"存档"。**
> 每完成一小步就存一次，你就永远有时间机器：
> 想回到"第 2 期能用的版本"，`git checkout v0.2.0` 就行。
