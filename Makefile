STACKS := traefik bind core immich-app arr monitoring portainer
s ?= $(STACKS)

.PHONY: help network up down pull ps logs check

help:
	@echo "make up | down | pull | ps | check | logs s=arr"


network:
	@docker network inspect traefik > /dev/null 2>&1 || ./network/create-traefik.sh
	@docker network inspect direct-ip-macvlan > /dev/null 2>&1 || ./network/create-macvlan.sh
	# @docker network connect --ip 192.168.1.241 direct-ip-macvlan twingate-practical-crocodile
	# @docker network connect traefik twingate-wandering-hawk

up: network
	@for d in $(s); do \
		echo "Starting $$d"; \
		(cd $$d && docker compose up -d ) || exit 1; \
	done

down:
	@for d in $$(echo $(s) | tr " " "\n" | tac); do \
		echo "Stoping $$d"; \
		(cd $$d && docker compose down); \
	done

pull:
	@for d in $(s); do \
		echo "Pulling for $$d"; \
		(cd $$d && docker compose pull) || exit 1; \
	done

ps:
	@ docker ps --format="table {{.Names}}\t{{.Status}}\t{{.Ports}}"

check:
	@for d in $(s); do \
		(cd $$d && docker compose config -q) && echo "OK $$d" || exit 1; \
	done
	@grep -hE "^[A-Z_]+_IP=" */.env | cut -d= -f2 | sort | uniq -d | \
		grep . && echo "DUPLICATE IP above!" && exit 1 || echo "OK NO DUPLICATE IPs";

logs:
	@cd $(s) && docker compose logs -f --tail=50
