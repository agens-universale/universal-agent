"""``gc-stores migrate``: применить миграции схемы или только сверить."""

from gc_stores.cli import main


def test_migrate_then_check_sqlite(tmp_path, capsys):
    path = tmp_path / "t.db"
    assert main(["migrate", "--store", "sqlite", "--path", str(path)]) == 0
    assert "версия схемы" in capsys.readouterr().out
    assert (
        main(["migrate", "--check", "--store", "sqlite", "--path", str(path)])
        == 0
    )


def test_check_postgres_without_schema_fails_with_the_fix(
    postgres_dsn, schemas, capsys
):
    name = schemas.name()
    args = ["--store", "postgres", "--dsn", postgres_dsn, "--schema", name]
    assert main(["migrate", "--check", *args]) == 1
    assert "gc-stores migrate" in capsys.readouterr().err
    assert main(["migrate", *args]) == 0
    assert main(["migrate", "--check", *args]) == 0


def test_dsn_from_environment(postgres_dsn, schemas, monkeypatch):
    monkeypatch.setenv("GC_STORES_DSN", postgres_dsn)
    name = schemas.name()
    assert main(["migrate", "--store", "postgres", "--schema", name]) == 0


def test_missing_connection_is_a_usage_error(capsys, monkeypatch):
    monkeypatch.delenv("GC_STORES_DSN", raising=False)
    assert main(["migrate", "--store", "postgres"]) == 2
    assert "--dsn" in capsys.readouterr().err


def test_migrate_mysql(mysql_databases, capsys):
    import asyncio

    dsn = asyncio.run(mysql_databases.acreate(migrate=False))
    assert main(["migrate", "--check", "--store", "mysql", "--dsn", dsn]) == 1
    assert main(["migrate", "--store", "mysql", "--dsn", dsn]) == 0
    from gc_stores import migrations

    latest = migrations.latest(migrations.load("gc_stores.mysql"))
    assert f"версия схемы: {latest}" in capsys.readouterr().out


def test_migrate_redis_writes_the_key_format(
    redis_url, redis_prefixes, capsys
):
    args = [
        "--store",
        "redis",
        "--url",
        redis_url,
        "--prefix",
        redis_prefixes.new(),
    ]
    assert main(["migrate", "--check", *args]) == 1
    assert main(["migrate", *args]) == 0
    assert "версия схемы: 1" in capsys.readouterr().out
    assert main(["migrate", "--check", *args]) == 0


def test_migrate_s3_writes_the_key_format(s3_config, s3_prefixes, capsys):
    args = [
        "--store",
        "s3",
        "--bucket",
        s3_config["bucket"],
        "--prefix",
        s3_prefixes.new(),
        "--endpoint-url",
        s3_config["endpoint_url"],
        "--region",
        s3_config["region"],
    ]
    assert main(["migrate", "--check", *args]) == 1
    assert main(["migrate", *args]) == 0
    assert "версия схемы: 1" in capsys.readouterr().out
