# 面试题库助手（Java 后端 / Agent）

本地单用户题库，包含 40 道带答案的通用题。首次打开须验证并配置自己的 DeepSeek API Key；调用 AI 会消耗你自己的 API 额度。简历可跳过，跳过后仍可练习通用题并使用通用 AI 出题。个人项目深挖题需先确认简历事实。

## 安装与使用

macOS：下载与你的 Mac 芯片匹配的 Apple Silicon 或 Intel 构建中的 `面试题库助手.app`，双击打开。首次运行若 macOS 阻止未签名应用，在系统设置的“隐私与安全性”中允许打开。
Windows：下载 Windows 构建产物中的 `面试题库助手` 文件夹，双击其中的 `面试题库助手.exe`。请保留整个文件夹。两平台都无需另装 Python；程序仅在 `127.0.0.1` 的随机可用端口启动，浏览器自动打开页面，在设置中点击“退出应用”关闭服务。

API Key 存于本地。题库、练习记录、已确认简历事实等存于当前用户的系统应用数据目录（`InterviewQuestionBankOpen`）。

## 备份和升级

退出应用后，复制当前用户应用数据目录中的 `interview.sqlite3` 作备份。恢复时退出应用，先备份现有数据库，再用备份文件覆盖。升级时替换应用包或程序文件夹，保留用户数据目录即可。API Key 在系统凭据库中单独管理，数据库备份不包含 Key。

## IF：对源代码进行开发

使用 Python 3.11 或 3.12：`python -m venv .venv`，安装 `pip install -r requirements.txt pyinstaller httpx`。
设定 `INTERVIEW_DATA_DIR` 为独立测试目录，再运行 `python launcher.py`。执行 `python -m unittest discover -s tests` 验证，`python packaging/build.py` 在当前系统生成双击程序。
Windows 包必须在 Windows 上构建，macOS 包必须在 macOS 上构建。仓库的手动工作流可在两平台构建并上传产物。

## 使用指南
1. 接入 DeepSeek API Key
2. 如果希望 AI 结合个人简历出题，可以上传简历（目前支持 PDF 文字及 docx 格式）自动解析 或 直接将简历文本保存
3. 题库分为：个人题库（可以导入自己真实遇到的面经）、网络题库（网络上分享的面经），这两个题库都支持单题/多题导入，导入后需要自行确认/删除；AI 题库由每日随机出题时 AI 生成并保存，刷题过程中点击跳过可避免未来出现类似题
