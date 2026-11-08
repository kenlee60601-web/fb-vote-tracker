@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === Step 1: Update code from GitHub ===
git pull --rebase --autostash
echo === Step 2: Keep PC awake while plugged in ===
powercfg /change standby-timeout-ac 0 2>nul
powercfg /change hibernate-timeout-ac 0 2>nul
echo === Step 3: Start FB vote auto-update (close window to stop) ===
python parsing_vote_v2.py --threshold 10 --loop 10 --push
pause
