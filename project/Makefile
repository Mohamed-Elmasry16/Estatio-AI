.PHONY: install check-db migrate migrate-new pipeline vectors test lint docker-build docker-migrate docker-pipeline

install:
	pip install -r requirements.txt

## Verify both Supabase connections (pooler + direct) work before doing anything else.
check-db:
	python scripts/check_db.py

## Apply all pending migrations (direct/session connection, port 5432).
migrate:
	alembic upgrade head

## Generate a new migration from model changes: make migrate-new msg="add foo column"
migrate-new:
	alembic revision --autogenerate -m "$(msg)"

pipeline:
	python data_pipeline.py

vectors:
	python build_vectors.py

test:
	pytest tests/ -v

lint:
	python -m ruff check src scripts tests *.py

docker-build:
	docker build -t real-estate-pipeline .

docker-migrate:
	docker compose run --rm migrate

docker-pipeline:
	docker compose run --rm pipeline
