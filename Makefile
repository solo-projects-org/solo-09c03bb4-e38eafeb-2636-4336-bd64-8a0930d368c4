.PHONY: up down build logs demo test psql clean restart

build:
	docker compose build

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api pacs

demo:
	docker compose run --rm demo

test:
	python -m pytest tests/ -v

restart:
	docker compose restart api

clean:
	docker compose down -v
