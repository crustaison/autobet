#!/bin/bash
# Kill any existing autobet processes
pkill -f 'python3 autobet_main.py' 2>/dev/null
sleep 1
fuser -k 7778/tcp 2>/dev/null
sleep 1
cd /home/sean/autobet
nohup python3 -u autobet_main.py > /home/sean/autobet/autobet.log 2>&1 &
echo "[AUTOBET] Started PID $!"
