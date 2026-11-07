@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === 從 GitHub 取得最新版程式 ===
git pull --rebase --autostash
echo === 設定電腦插電時永不睡眠(螢幕可以關,程式照跑)===
powercfg /change standby-timeout-ac 0 2>nul
powercfg /change hibernate-timeout-ac 0 2>nul
echo === FB 投票自動更新:約每 10~12 分鐘更新一輪(細水長流防限流) ===
echo === 關閉此視窗即停止 ===
python parsing_vote_v2.py --threshold 10 --loop 2 --push
pause
