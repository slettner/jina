import os
import threading
import pytest
import numpy as np

from jina import Document
from jina.flow import Flow

cur_dir = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def config(tmpdir):
    os.environ['JINA_REPLICA_DIR'] = str(tmpdir)
    yield
    del os.environ['JINA_REPLICA_DIR']


def get_doc(i):
    return Document(text=f'doc {i}', embedding=np.array([i] * 5))


def test_normal(config):
    # this test is a bit hacky.
    # It uses the score field to pass the information of the used replica during search.
    # Please don't use it that way in application code
    used_replicas = []

    def handle_search_result(resp):
        used_replicas.append(list(resp.search.docs)[0].matches[0].score.value)

    flow = Flow().add(
        name='pod1',
        uses=os.path.join(cur_dir, 'yaml/mock_index_vector.yml'),
        replicas=3,
        parallel=2,
    )
    with flow:
        for i in range(20):
            # test rolling update does not hang
            flow.search(get_doc(0), on_done=handle_search_result)

    # 20 time one of the replicas is called
    assert len(used_replicas) == 20

    # there are three replicas in total
    assert set(used_replicas) == {0.0, 1.0, 2.0}


@pytest.mark.timeout(30)
def test_simple_run():
    flow = Flow().add(
        name='pod1',
        replicas=2,
        parallel=3,
    )
    with flow:
        # test rolling update does not hang
        flow.search(get_doc(0))
        flow.rolling_update('pod1')
        flow.search(get_doc(1))


def test_thread_run():
    flow = Flow().add(
        name='pod1',
        replicas=2,
        parallel=2,
    )
    with flow:
        x = threading.Thread(target=flow.rolling_update, args=('pod1',))
        x.start()
        # TODO remove the join to make it asynchronous again
        x.join()
        # TODO there is a problem with the gateway even after request times out - open issue
        for i in range(600):
            flow.search(get_doc(i))


def test_vector_indexer_thread(config):
    with Flow().add(
        name='pod1',
        uses=os.path.join(cur_dir, 'yaml/mock_index_vector.yml'),
        replicas=2,
        parallel=3,
    ) as flow:
        for i in range(5):
            flow.search(get_doc(i))
        x = threading.Thread(target=flow.rolling_update, args=('pod1',))
        x.start()
        # TODO there is a problem with the gateway even after request times out - open issue
        # TODO remove the join to make it asynchronous again
        x.join()
        for i in range(40):
            flow.search(get_doc(i))


def test_workspace(config, tmpdir):
    with Flow().add(
        name='pod1',
        uses=os.path.join(cur_dir, 'yaml/simple_index_vector.yml'),
        replicas=2,
        parallel=3,
    ) as flow:
        # in practice, we don't send index requests to the compound pod this is just done to test the workspaces
        for i in range(10):
            flow.index(get_doc(i))

        # validate created workspaces
        dirs = set(os.listdir(tmpdir))
        expected_dirs = {
            'vecidx-0-0',
            'vecidx-0-1',
            'vecidx-0-2',
            'vecidx-1-0',
            'vecidx-1-1',
            'vecidx-1-2',
        }
        assert dirs == expected_dirs


@pytest.mark.parametrize(
    'replicas_and_parallel',
    (
        ((3, 1),),
        ((2, 3),),
        ((2, 3), (3, 4), (2, 2), (2, 1)),
    ),
)
def test_port_configuration(replicas_and_parallel):
    def extract_pod_args(pod):
        if 'replicas' not in pod.args or int(pod.args.replicas) == 1:
            head_args = pod.peas_args['head']
            tail_args = pod.peas_args['tail']
            middle_args = pod.peas_args['peas']
        else:
            head_args = pod.head_args
            tail_args = pod.tail_args
            middle_args = pod.replicas_args
        return pod, head_args, tail_args, middle_args

    def get_outer_ports(pod, head_args, tail_args, middle_args):

        if not 'replicas' in pod.args or int(pod.args.replicas) == 1:
            if not 'parallel' in pod.args or int(pod.args.parallel) == 1:
                assert tail_args is None
                assert head_args is None
                replica = middle_args[0]  # there is only one
                return replica.port_in, replica.port_out
            else:
                return pod.head_args.port_in, pod.tail_args.port_out
        else:
            assert pod.args.replicas == len(middle_args)
            return pod.head_args.port_in, pod.tail_args.port_out

    def validate_ports_pods(pods):
        for i in range(len(pods) - 1):
            _, port_out = get_outer_ports(*extract_pod_args(pods[i]))
            port_in_next, _ = get_outer_ports(*extract_pod_args(pods[i + 1]))
            assert port_out == port_in_next

    def validate_ports_replica(replica, replica_port_in, replica_port_out, parallel):
        assert replica_port_in == replica.args.port_in
        assert replica.args.port_out == replica_port_out
        peas_args = replica.peas_args
        peas = peas_args['peas']
        assert len(peas) == parallel
        if parallel == 1:
            assert peas_args['head'] is None
            assert peas_args['tail'] is None
            assert peas[0].port_in == replica_port_in
            assert peas[0].port_out == replica_port_out
        else:
            shard_head = peas_args['head']
            shard_tail = peas_args['tail']
            assert replica.args.port_in == shard_head.port_in
            assert replica.args.port_out == shard_tail.port_out
            for pea in peas:
                assert shard_head.port_out == pea.port_in
                assert pea.port_out == shard_tail.port_in

    flow = Flow()
    for i, (replicas, parallel) in enumerate(replicas_and_parallel):
        flow.add(
            name=f'pod{i}',
            replicas=replicas,
            parallel=parallel,
            port_in=f'51{i}00',
            # info: needs to be set in this test since the test is asserting pod args with pod tail args
            port_out=f'51{i + 1}00',  # outside this test, it don't have to be set
            copy_flow=False,
        )

    with flow:
        pods = flow._pod_nodes
        validate_ports_pods(
            [pods['gateway']]
            + [pods[f'pod{i}'] for i in range(len(replicas_and_parallel))]
            + [pods['gateway']]
        )
        for pod_name, pod in pods.items():
            if pod_name == 'gateway':
                continue
            if pod.args.replicas == 1:
                if int(pod.args.parallel) == 1:
                    assert len(pod.peas_args['peas']) == 1
                else:
                    assert len(pod.peas_args) == 3
                replica_port_in = pod.args.port_in
                replica_port_out = pod.args.port_out
            else:
                replica_port_in = pod.head_args.port_out
                replica_port_out = pod.tail_args.port_in

            assert pod.head_pea.args.port_in == pod.args.port_in
            assert pod.head_pea.args.port_out == replica_port_in
            assert pod.tail_pea.args.port_in == replica_port_out
            assert pod.tail_pea.args.port_out == pod.args.port_out
            if pod.args.replicas > 1:
                for replica in pod.replicas:
                    validate_ports_replica(
                        replica,
                        replica_port_in,
                        replica_port_out,
                        getattr(pod.args, 'parallel', 1),
                    )
        assert pod


def test_num_peas(config):
    with Flow().add(
        name='pod1',
        uses=os.path.join(cur_dir, 'yaml/simple_index_vector.yml'),
        replicas=3,
        parallel=4,
    ) as flow:
        assert flow.num_peas == (
            3 * (4 + 1 + 1)  # replicas 3  # parallel 4  # pod head  # pod tail
            + 1  # compound pod head
            + 1  # compound pod tail
            + 1  # gateway
        )
