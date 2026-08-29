#!/bin/bash

TOKEN=$(strings /dev/urandom |tr -dc A-Za-z0-9 | head -c20; echo)
TIME=$(python3 -c "import time;print(time.time())")

if [ "$1"x == ""x ]; then
	NAME=client1
else
	NAME=$1
fi

cat << EOF
  "${NAME}": {
    "token": "${TOKEN}",
    "description": "${NAME} pubilc IP",
    "created_at": ${TIME}
  }
EOF

