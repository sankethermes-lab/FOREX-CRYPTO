#!/usr/bin/env sh
# Start instant breakout alerts (Mac / Linux / VPS). Keep it running.
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
while true; do
  python3 -m tracker alerts --loop
  echo "Scanner stopped - restarting in 10 seconds..."
  sleep 10
done
