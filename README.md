# 接龙管家网页端每日自动打卡

[![Tests](https://github.com/MikeJack302/jielong-daily-checkin/actions/workflows/test.yml/badge.svg)](https://github.com/MikeJack302/jielong-daily-checkin/actions/workflows/test.yml)

Windows 本机自动化工具：首次使用微信扫码登录接龙管家网页端，之后由 Windows
计划任务按时启动独立的 Edge 会话完成每日打卡。

## 特点

- 首次扫码后复用本机网页登录态，不需要每天打开微信；
- 已打卡时自动跳过，避免重复提交；
- 每天 06:00、06:15、06:35、07:00、07:30、08:00 分批尝试；前一次因网络或页面加载失败时，后续时段会自动补试；
- 网页登录过期时会明确识别二维码，并打开可见的 Edge 窗口等待扫码后自动续跑；
- 日常执行默认短暂打开可见的 Edge（完成后自动关闭），以复用稳定的正常网页登录会话；
- 可选接入 Server酱免费微信通知；每天成功最多一条、首次失败/需扫码最多一条；
- 只点击白名单中的精确按钮名称；
- 必填项为空、页面变化或结果无法确认时立即停止并保存截图；
- 不导出 Cookie、Token，不调用未公开的私有接口；
- 登录状态、活动网址、日志和截图默认不会被 Git 提交。

## 运行条件

- Windows 10/11；
- Microsoft Edge；
- Python 3.11；
- 首次配置时可以使用手机微信扫码；
- 定时运行时电脑开机、联网，并保持 Windows 用户登录。

计划任务会尝试在计划时间唤醒处于睡眠状态的电脑。如果计划时间电脑处于关机状态，
当天在计划时间之后登录 Windows 时会自动补跑；在计划时间之前登录不会提前打卡。
电脑完全关机时无法自行开机。

接龙管家可能要求定期重新微信扫码，这是网站自身的登录安全机制，脚本不会也不能绕过。
检测到时会打开二维码窗口并等待 5 分钟；若无人扫码，下一轮重试会再次尝试。

## 快速开始

在 PowerShell 中执行：

```powershell
git clone https://github.com/MikeJack302/jielong-daily-checkin.git
cd jielong-daily-checkin
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\install_web_task.ps1" `
  -TargetTitle "你的打卡活动完整标题" `
  -ActiveFrom "2026-01-01" `
  -ActiveUntil "2026-12-31"
```

安装程序会创建独立虚拟环境并打开 Edge：

1. 使用微信扫码并在手机端确认登录；
2. 如果脚本没有自动找到活动，请在 Edge 中手动进入目标打卡页面；
3. 识别成功后，Edge 自动关闭并创建计划任务。

`TargetTitle` 必须与页面显示的活动标题完全一致。日期格式必须是 `YYYY-MM-DD`。
默认使用 06:00 至 08:00 的多轮补试时间。`Times` 也支持自定义一个或多个时间：

```powershell
.\install_web_task.ps1 -SkipSetup -Times "06:00","21:00"
```

## 日常管理

查看计划时间：

```powershell
(Get-ScheduledTask -TaskName "Jielong-Daily-Web-Checkin").Triggers |
  Select-Object StartBoundary, Enabled
```

查看下次运行时间：

```powershell
Get-ScheduledTaskInfo -TaskName "Jielong-Daily-Web-Checkin" |
  Select-Object NextRunTime, LastRunTime, LastTaskResult
```

只检查页面，不执行打卡：

```powershell
& ".\.web-venv\Scripts\python.exe" ".\web_checkin.py" --diagnose
```

登录失效后重新扫码：

```powershell
& ".\.web-venv\Scripts\python.exe" ".\web_checkin.py" --setup
```

删除计划任务：

```powershell
.\install_web_task.ps1 -Remove
```

日志与失败截图位于 `logs`。网页登录数据保存在 `.browser-profile`。
任务返回码 `4` 表示需要重新扫码；其他非零返回码可结合同一时刻的日志和截图排查。

## 免费微信通知

Server酱Turbo 免费会员每天有 5 条额度，本项目通过每日去重最多使用 2 条：首次失败或
需要扫码时一条，最终成功时一条。先到 <https://sct.ftqq.com/sendkey/> 用微信扫码登录，
生成以 `SCT` 开头的 SendKey，然后运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\configure_wechat_notify.ps1"
```

脚本会隐藏输入内容，把 SendKey 保存为当前 Windows 用户的 `SERVERCHAN_SENDKEY`
环境变量，并立即发送一条测试通知。SendKey 不会写入项目文件或日志；泄露后应在
Server酱控制台重置。

## 配置与安全

首次成功配置后会生成 `web_config.json`，其中包含具体活动网址；此文件与
`.browser-profile` 均已加入 `.gitignore`。不要复制、上传或分享它们。

配置结构参见 `web_config.example.json`。如果网站修改按钮文案，应先查看失败截图，
确认新文案无歧义后再加入白名单。不要加入“确定”等过于宽泛的按钮名。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m py_compile web_checkin.py
```

## 免责声明

请仅在你本人有权参与、且活动规则允许自动化的场景使用。网站更新、登录过期、网络
故障、电脑休眠或新增必填字段都可能导致任务失败，请定期检查日志和实际打卡记录。

本项目与接龙管家及其运营方无隶属、授权或背书关系。
