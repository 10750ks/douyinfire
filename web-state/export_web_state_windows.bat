@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在准备导出抖音网页版登录态...
echo.
python --version >nul 2>nul
if errorlevel 1 (
  echo 未检测到 python，请先安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)
if not exist ".venv" (
  echo 首次运行，正在创建本地虚拟环境...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python export_web_state.py
echo.
echo 如果提示已保存 web_storage_state.json，就可以上传/替换到 GitHub 或服务器项目根目录。
pause
