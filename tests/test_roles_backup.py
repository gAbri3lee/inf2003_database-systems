"""Least-privilege logins and backups, on the disposable databases only."""
import datetime as dt
import json
import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src import backup, experiments, quick, security
from src.loader import run_load
from src.mongo import MongoRepo
from src.pg import EDITOR_ROLE, READER_ROLE, PgRepo

USER_DB = 'hdb_test_roles_users'


def sale(tid, model='Brand New Model'):
    return dict(transaction_id=tid, block_id='TEST_BLOCK_0', month=dt.date(2025, 1, 1),
                flat_type='4 ROOM', flat_model=model, storey_range='01 TO 03',
                floor_area_sqm=100.0, resale_price=500000.0, lease_commence_year=2000,
                remaining_lease='74 years 06 months')


def login(role):
    import config        # not at module level: test_app.py sets the environment first
    return PgRepo(make_conninfo(os.environ['TEST_PG_DSN'], user=role,
                                password=config.PG_ROLE_PASSWORDS[role]))


@pytest.fixture
def roles(repos):
    import config
    pg, mongo = repos
    dsn = os.environ['TEST_PG_DSN']
    settings = conninfo_to_dict(dsn)
    security.provision_pg(dsn, settings['dbname'], settings['user'], config.PG_ROLE_PASSWORDS)
    security.provision_mongo(mongo.client, mongo.client, mongo.db_name, USER_DB,
                             config.MONGO_ROLE_PASSWORDS)
    yield pg, mongo, login(READER_ROLE), login(EDITOR_ROLE)
    mongo.client.drop_database(USER_DB)


def test_reader_can_read_but_not_write(roles):
    pg, mongo, reader, _ = roles
    assert reader.get_transaction('TEST_TX_00')[0]
    assert quick.singles(reader, {})[0]              # the view exists; it can't create it
    assert reader.price_check(dict(pg.get_transaction('TEST_TX_00')[0][0]), 12)[0]
    allowed, message = security.probe_delete(reader, mongo)['postgres']
    assert not allowed and 'permission denied' in message
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.create_transaction(sale('ROLE_READER'))


def test_editor_can_add_a_sale_with_a_new_flat_model(roles):
    """flat_models is an upsert on an identity column: it needs more than INSERT."""
    _, _, _, editor = roles
    editor.create_transaction(sale('ROLE_EDITOR'))
    assert editor.get_transaction('ROLE_EDITOR')[0][0]['flat_model'] == 'Brand New Model'
    editor.create_transaction(sale('ROLE_EDITOR_2'))             # existing model: conflict path
    assert editor.delete_transaction('ROLE_EDITOR').rows == 1
    assert editor.delete_transaction('ROLE_EDITOR_2').rows == 1


def test_editor_can_benchmark_and_run_index_experiments(roles):
    _, _, _, editor = roles
    assert editor.benchmark(1)
    result = experiments.run_experiments(None, editor, ['town_equals'], [5], 2)
    assert result.summary


def test_editor_cannot_change_the_schema(roles):
    _, _, _, editor = roles
    with editor.conn(autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('DROP TABLE transactions CASCADE')
    with editor.conn(autocommit=True) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute('CREATE TABLE public.not_allowed (id integer)')


def test_rights_survive_a_full_reload(roles, csv_path):
    pg, mongo, reader, editor = roles
    run_load(csv_path, pg, mongo)
    assert quick.singles(reader, {})[0]
    editor.create_transaction(sale('ROLE_RELOAD'))
    editor.delete_transaction('ROLE_RELOAD')


def test_provisioning_works_on_a_database_loaded_by_an_older_version(repos):
    """Older loads have no flat_models sequence, and the app must still start."""
    import config
    pg, _ = repos
    with pg.conn(autocommit=True) as c:
        c.execute('ALTER TABLE flat_models ALTER COLUMN flat_model_id DROP IDENTITY')
    dsn = os.environ['TEST_PG_DSN']
    settings = conninfo_to_dict(dsn)
    security.provision_pg(dsn, settings['dbname'], settings['user'], config.PG_ROLE_PASSWORDS)
    assert login(READER_ROLE).get_transaction('TEST_TX_00')[0]


def test_postgres_connections_cannot_stall_on_ipv6(monkeypatch):
    """localhost tried ::1 first on Windows: 130 s per connection before this."""
    import config
    settings = conninfo_to_dict(config.pg_dsn())
    assert int(settings['connect_timeout']) <= 10


def test_mongo_role_grants(roles):
    import config
    pg, mongo, _, _ = roles
    granted = {}
    for user in ('hdb_guest', 'hdb_member', 'hdb_editor', 'hdb_admin'):
        repo = MongoRepo(os.environ['TEST_MONGO_URI'], mongo.db_name, username=user,
                         password=config.MONGO_ROLE_PASSWORDS[user], authSource='admin')
        granted[user] = security.mongo_privileges(repo.client, mongo.db_name, USER_DB)
        repo.client.close()
    assert not granted['hdb_guest']['Edit resale data']
    assert not granted['hdb_guest']['Save profiles & searches']
    assert granted['hdb_member']['Save profiles & searches']
    assert not granted['hdb_member']['Edit resale data']
    assert granted['hdb_editor']['Edit resale data']
    assert not granted['hdb_editor']['Rebuild / restore the data']
    assert granted['hdb_admin']['Rebuild / restore the data']


# ------------------------------------------------------------------ backups ---- #
def snapshot(pg, mongo):
    with pg.conn() as c:
        digest = c.execute("SELECT md5(string_agg(t::text, '' ORDER BY transaction_id)) AS h "
                           "FROM transactions t").fetchone()['h']
    return pg.counts(), digest, mongo.coll.count_documents({}), sorted(mongo.coll.index_information())


@pytest.fixture
def backed_up(repos, tmp_path):
    pg, mongo = repos
    made, _ = backup.create_backup(pg, mongo.client, [mongo.db_name], tmp_path, label='test')
    return pg, mongo, made, tmp_path


def test_restore_brings_back_the_exact_data(backed_up):
    pg, mongo, made, _ = backed_up
    before = snapshot(pg, mongo)
    pg.delete_transaction('TEST_TX_00')
    mongo.delete_transaction('TEST_TX_00')
    assert not backup.verify(made)
    backup.restore_backup(made, pg, mongo.client)
    assert snapshot(pg, mongo) == before


def test_new_flat_model_works_after_a_restore(backed_up):
    """COPY restores explicit ids, so the identity sequence must be moved past them."""
    pg, mongo, made, _ = backed_up
    backup.restore_backup(made, pg, None)
    pg.create_transaction(sale('AFTER_RESTORE'))
    assert pg.get_transaction('AFTER_RESTORE')[0]


def test_tampered_backup_is_refused(backed_up):
    pg, mongo, made, _ = backed_up
    path = made.path / made.manifest['postgres']['towns']['file']
    path.write_bytes(path.read_bytes()[:-3] + b'xyz')
    assert backup.verify(made)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(made, pg, mongo.client)


def test_failed_restore_leaves_the_data_alone(backed_up):
    pg, mongo, made, _ = backed_up
    before = snapshot(pg, mongo)
    made.manifest['postgres']['transactions']['rows'] += 1
    with pytest.raises(backup.BackupError):
        backup.restore_backup(made, pg, None)
    made.manifest['mongodb'][mongo.db_name]['transactions']['docs'] += 1
    with pytest.raises(backup.BackupError):
        backup.restore_backup(made, None, mongo.client)
    assert snapshot(pg, mongo) == before
    assert not [c for c in mongo.client[mongo.db_name].list_collection_names()
                if c.endswith('__restore')]


def test_backup_skips_scratch_collections_and_prunes(repos, tmp_path):
    pg, mongo = repos
    scratch = mongo.client[mongo.db_name]['transactions_stage_abc']
    scratch.insert_one({'_id': 1})
    try:
        first, _ = backup.create_backup(pg, mongo.client, [mongo.db_name], tmp_path)
        second, _ = backup.create_backup(pg, mongo.client, [mongo.db_name], tmp_path)
    finally:
        scratch.drop()
    assert list(first.manifest['mongodb'][mongo.db_name]) == ['transactions']
    assert backup.prune(tmp_path, 1) == [first.name]
    assert [b.name for b in backup.list_backups(tmp_path)] == [second.name]
    assert json.loads((second.path / backup.MANIFEST).read_text(encoding='utf-8'))['label'] == ''
