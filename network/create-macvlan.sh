#!/usr/bin/env bash
set -e
docker network create -d macvlan \
  --subnet=192.168.1.0/24 --gateway=192.168.1.1 \
  --ip-range=192.168.1.224/27 \
  -o parent=enp3s0 \
  direct-ip-macvlan
