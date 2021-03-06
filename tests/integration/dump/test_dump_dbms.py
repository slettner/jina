import os
from pathlib import Path

import numpy as np
import pytest

from jina import Flow, Document
from jina.drivers.index import DBMSIndexDriver
from jina.executors.indexers.dump import import_vectors, import_metas
from jina.executors.indexers.query import BaseQueryIndexer
from jina.executors.indexers.query.compound import CompoundQueryExecutor
from jina.logging.profile import TimeContext


def get_documents(nr=10, index_start=0, emb_size=7):
    for i in range(index_start, nr + index_start):
        with Document() as d:
            d.id = i
            d.text = f'hello world {i}'
            d.embedding = np.random.random(emb_size)
            d.tags['tag_field'] = f'tag data {i}'
        yield d


def basic_benchmark(tmpdir, docs, validate_results_nonempty, error_callback, nr_search):
    os.environ['BASIC_QUERY_WS'] = os.path.join(tmpdir, 'basic_query')
    os.environ['BASIC_INDEX_WS'] = os.path.join(tmpdir, 'basic_index')
    with Flow().add(uses='basic/query.yml') as flow:
        flow.index(docs)

    with Flow().add(uses='basic/query.yml') as flow:
        with TimeContext(
            f'### baseline - query time with {nr_search} on {len(docs)} docs'
        ):
            flow.search(
                docs[:nr_search],
                on_done=validate_results_nonempty,
                on_error=error_callback,
            )

    with Flow().add(uses='basic/index.yml') as flow_dbms:
        with TimeContext(f'### baseline - indexing: {len(docs)} docs'):
            flow_dbms.index(docs)


def assert_dump_data(dump_path, docs, shards, pea_id):
    size_shard = len(docs) // shards
    size_shard_modulus = len(docs) % shards
    ids_dump, vectors_dump = import_vectors(
        dump_path,
        str(pea_id),
    )
    if pea_id == shards - 1:
        docs_expected = docs[
            (pea_id) * size_shard : (pea_id + 1) * size_shard + size_shard_modulus
        ]
    else:
        docs_expected = docs[(pea_id) * size_shard : (pea_id + 1) * size_shard]
    print(f'### pea {pea_id} has {len(docs_expected)} docs')

    ids_dump = list(ids_dump)
    vectors_dump = list(vectors_dump)
    np.testing.assert_equal(ids_dump, [d.id for d in docs_expected])
    np.testing.assert_allclose(vectors_dump, [d.embedding for d in docs_expected])

    _, metas_dump = import_metas(
        dump_path,
        str(pea_id),
    )
    metas_dump = list(metas_dump)
    np.testing.assert_equal(
        metas_dump,
        [
            DBMSIndexDriver._doc_without_embedding(d).SerializeToString()
            for d in docs_expected
        ],
    )

    # assert with Indexers
    # TODO currently metas are only passed to the parent Compound, not to the inner components
    with TimeContext(f'### reloading {len(docs_expected)}'):
        # noinspection PyTypeChecker
        cp: CompoundQueryExecutor = BaseQueryIndexer.load_config(
            'indexer_query.yml',
            pea_id=pea_id,
            metas={
                'workspace': os.path.join(dump_path, 'new_ws'),
                'dump_path': dump_path,
            },
        )
    for c in cp.components:
        assert c.size == len(docs_expected)

    # test with the inner indexers separate from the Compound
    for i, indexer_file in enumerate(['basic/query_np.yml', 'basic/query_kv.yml']):
        indexer = BaseQueryIndexer.load_config(
            indexer_file,
            pea_id=pea_id,
            metas={
                'workspace': os.path.realpath(os.path.join(dump_path, f'new_ws-{i}')),
                'dump_path': dump_path,
            },
        )
        assert indexer.size == len(docs_expected)


def path_size(dump_path):
    dir_size = (
        sum(f.stat().st_size for f in Path(dump_path).glob('**/*') if f.is_file()) / 1e6
    )
    return dir_size


@pytest.mark.parametrize('shards', [6, 3, 1])
@pytest.mark.parametrize('nr_docs', [7])
@pytest.mark.parametrize('emb_size', [10])
def test_dump_keyvalue(tmpdir, shards, nr_docs, emb_size, run_basic=False):
    docs = list(get_documents(nr=nr_docs, index_start=0, emb_size=emb_size))
    assert len(docs) == nr_docs
    nr_search = 1

    os.environ['USES_AFTER'] = '_merge_matches' if shards > 1 else '_pass'
    os.environ['SHARDS'] = str(shards)

    def _validate_results_nonempty(resp):
        assert len(resp.docs) == nr_search
        for d in resp.docs:
            if nr_docs < 10:
                assert len(d.matches) == nr_docs
            else:
                # TODO does it return all of them no matter how many?
                assert len(d.matches) > 0
            for m in d.matches:
                assert m.embedding.shape[0] == emb_size
                assert (
                    DBMSIndexDriver._doc_without_embedding(m).SerializeToString()
                    is not None
                )
                assert 'hello world' in m.text
                assert f'tag data' in m.tags['tag_field']

    def error_callback(resp):
        raise Exception('error callback called')

    if run_basic:
        basic_benchmark(
            tmpdir, docs, _validate_results_nonempty, error_callback, nr_search
        )

    dump_path = os.path.join(str(tmpdir), 'dump_dir')
    os.environ['DBMS_WORKSPACE'] = os.path.join(str(tmpdir), 'index_ws')
    with Flow.load_config('flow_dbms.yml') as flow_dbms:
        with TimeContext(f'### indexing {len(docs)} docs'):
            flow_dbms.index(docs)

        with TimeContext(f'### dumping {len(docs)} docs'):
            flow_dbms.dump('indexer_dbms', dump_path, shards=shards, timeout=-1)

        dir_size = path_size(dump_path)
        print(f'### dump path size: {dir_size} MBs')

    # assert data dumped is correct
    for pea_id in range(shards):
        assert_dump_data(dump_path, docs, shards, pea_id)


# benchmark only
@pytest.mark.skipif(
    'GITHUB_WORKFLOW' in os.environ, reason='skip the benchmark test on github workflow'
)
def test_benchmark(tmpdir):
    nr_docs = 100000
    return test_dump_keyvalue(
        tmpdir, shards=1, nr_docs=nr_docs, emb_size=128, run_basic=True
    )
