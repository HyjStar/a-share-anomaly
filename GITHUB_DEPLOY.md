# GitHub Actions + Pages 部署清单

> 前提：这台电脑能够访问 GitHub。打不开时，请先使用 VPN/代理，或本工作区附带的
> `github-access/Open-GitHub.cmd`（仅影响专用浏览器窗口）。没有安装 Git 也可以完成部署。

## 最快方式：ZIP + 一次性 bootstrap workflow

不需要 Git，不需要逐个上传文件。

1. 在 GitHub 新建一个 **Public** 仓库，例如 `a-share-anomaly`，不要勾选初始化 README。
2. 在仓库中新建文件：
   - 路径：`.github/workflows/bootstrap.yml`
   - 内容复制项目根目录的 `github-bootstrap.yml`
   - 提交到 `main`。
3. 在仓库 **Add file → Upload files** 上传
   `a-share-anomaly-github.zip` 到仓库根目录，提交。
4. **Settings → Actions → General → Workflow permissions** 设为
   **Read and write permissions**。
5. 打开 **Actions → Bootstrap repository → Run workflow**。
6. 等它变绿。它会自动：
   - 解压 ZIP；
   - 把完整项目复制到仓库根目录；
   - 删除 ZIP 和 bootstrap workflow 自身；
   - 提交并 push。
7. 回到仓库首页，确认能看到 `README.md`、`anomaly/`、`web/`、`docs/` 和
   `.github/workflows/daily.yml`。
8. **Settings → Pages**：Source = Deploy from a branch，Branch = `main`，
   Folder = `/docs`，保存。
9. 打开 **Actions → 收盘异动扫描 → Run workflow** 跑一次真实扫描。

如果能在**今晚 24:00 前**完成第 9 步，可以抓到当天收盘数据；否则第一份数据会从下一个交易日开始。

---

## 备选方式：网页逐个上传 / Git 命令行

### 方式 A：GitHub 网页上传

1. 在 GitHub 新建仓库，例如 `a-share-anomaly`，不要初始化 README。
2. 解压发布包，进入 `a-share-anomaly/`。
3. 在仓库页面选择 **Add file → Upload files**，把项目内所有文件/文件夹拖进去（包括 `anomaly/`、`web/`、`tests/`、`docs/`、`assets/`、`run.py`、`config.yaml` 等）。
4. **注意 `.github` 是隐藏文件夹**，Windows 文件选择器可能看不到。最稳妥的做法：
   - 先上传其余文件；
   - 再在网页选择 **Add file → Create new file**；
   - 文件名输入 `.github/workflows/daily.yml`；
   - 把本机 `.github/workflows/daily.yml` 的内容粘贴进去并提交。
5. 检查仓库首页是否能看到 `.github/workflows/daily.yml`，以及 `docs/`、`anomaly/`。

> 如果使用 GitHub Desktop：直接 `Add local repository` 指向解压后的
> `a-share-anomaly/`，Publish repository 即可，`.github` 会自动包含。

### 方式 B：Git 命令行

```bash
cd a-share-anomaly
git init
git add .
git commit -m "init: A股异动监测系统"
git branch -M main
git remote add origin https://github.com/<你的用户名>/a-share-anomaly.git
git push -u origin main
```

## 通用设置（如果你已按最快方式做完第 4-9 步，可跳过）

## Actions 写权限

仓库 **Settings → Actions → General → Workflow permissions**：

- 选择 **Read and write permissions**
- 保存

否则 workflow 无法把每天的数据和网页提交回仓库。

## GitHub Pages

仓库 **Settings → Pages**：

- **Source**：Deploy from a branch
- **Branch**：`main`
- **Folder**：`/docs`
- 保存

等待 Pages 显示网址：

```text
https://<你的用户名>.github.io/a-share-anomaly/
```

首次打开页面会是空状态页，这是正常的，等第一个交易日扫描成功后才会有数据。

## 手动运行

仓库 **Actions → 收盘异动扫描 → Run workflow**。

- 非交易日或未收盘时，程序会正常跳过，不会写假数据。
- 第一次真正有数据的时间：**部署后的下一个交易日 15:45（北京时间）**。
- 想立刻验证 workflow 是否能启动，可以在非交易日手动跑；日志会显示“跳过”，属正常。

## 自动运行时间

`.github/workflows/daily.yml` 中：

```yaml
schedule:
  - cron: "45 7 * * 1-5"
```

`07:45 UTC = 15:45 北京时间`，只在周一至周五触发；节假日由交易日历跳过。

GitHub 定时任务可能延迟几分钟到几十分钟，属于正常现象。

## 运行后检查

手机打开 Pages 网址，确认：

- 今日异动：个股 / 板块两个标签页；
- 近 5/10/20 日汇总；
- 历史回看；
- 页脚数据日期是最近交易日。

也可以在仓库查看：

- `data/anomaly.db` 是否更新；
- `data/daily/YYYY-MM-DD.json` 是否出现；
- `docs/index.html` 是否变化；
- Actions 最近一次运行是否绿色。

## 常见问题

### workflow 失败在权限

- 检查 Actions 的 Workflow permissions 是否为 Read and write。

### 页面仍是空的

- Actions 是否成功；
- 是否在交易日 15:45 后运行；
- `docs/index.html` 是否在最近一次提交中发生变化。

### 东方财富限流

- 代码会重试、校验交易日和日线是否落盘；
- 若 GitHub Actions 出口被限流，可在 Actions 里重跑，或改用固定 VPS 执行同一套 `run.py`。

### 想马上看到本地页面

不要用模拟数据填生产页。等第一个交易日真实扫描完成后，页面会自动出现数据。
