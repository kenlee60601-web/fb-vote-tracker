@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === 第一次安裝:下載爬蟲需要的瀏覽器元件(約 3~5 分鐘)===
pip install playwright
python -m playwright install chromium
echo.
echo === 安裝完成!之後雙擊「自動更新票數.bat」即可開始自動更新 ===
echo === 注意:第一次自動上傳時會跳出 GitHub 登入視窗,登入一次即可 ===
pause
