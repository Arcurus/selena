#!/bin/bash
cd /home/openclaw/openclaw/workspace/selena-project
nohup python3 code/api_server.py > /tmp/api_server.log 2>&1 &
