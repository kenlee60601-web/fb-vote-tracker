@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === 設定電腦插電時永不睡眠(螢幕可以關,程式照跑)===
powercfg /change standby-timeout-ac 0 2>nul
powercfg /change hibernate-timeout-ac 0 2>nul
echo === FB 投票自動更新:每 20 分鐘爬一次並自動上傳 GitHub ===
echo === 網頁上有人按「立即抓取」會提前更新 ===
echo === 關閉此視窗即停止 ===
python parsing_vote_v2.py --threshold 40 --loop 10 --push
pause
