# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Tests for the Flocker REST API client.
"""

from uuid import uuid4

from bitmath import GiB

from zope.interface.verify import verifyObject

from pyrsistent import pmap

from twisted.trial.unittest import TestCase
from twisted.python.filepath import FilePath
from twisted.internet import reactor
from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.web.http import BAD_REQUEST

from .._client import (
    IFlockerAPIV1Client, FakeFlockerClient, Dataset, DatasetAlreadyExists,
    DatasetState, FlockerClient, ResponseError,
)
from ...ca import rest_api_context_factory
from ...ca.testtools import get_credential_sets
from ...testtools import find_free_port
from ...control._persistence import ConfigurationPersistenceService
from ...control._clusterstate import ClusterStateService
from ...control.httpapi import create_api_service
from ...control import NodeState


DATASET_SIZE = int(GiB(1).to_Byte().value)


def make_clientv1_tests():
    """
    Create a ``TestCase`` for testing ``IFlockerAPIV1Client``.

    The presumption is that the state of datasets is completely under
    control of this process. So when testing a real client it will be
    talking to a in-process server.

    The ``TestCase`` should have two 0-argument methods:

    create_client: Returns a ``IFlockerAPIV1Client`` provider.
    synchronize_state: Make state match the configuration.
    """
    class InterfaceTests(TestCase):
        def setUp(self):
            self.node_1 = uuid4()
            self.node_2 = uuid4()
            self.client = self.create_client()

        def test_interface(self):
            """
            The created client provides ``IFlockerAPIV1Client``.
            """
            self.assertTrue(verifyObject(IFlockerAPIV1Client, self.client))

        def assert_creates(self, client, dataset_id=None, **create_kwargs):
            """
            Create a dataset and ensure it shows up in the configuration and
            return result of the ``create_dataset`` call.

            :param IFlockerAPIV1Client client: Client to use.
            :param dataset_id: Dataset ID to use, or ``None`` if it should
                be generated.
            :param create_kwargs: Additional arguments to pass to
                ``create_dataset``.

            :return: ``Deferred`` firing with result of
                ``create_dataset``.
            """
            created = client.create_dataset(
                dataset_id=dataset_id, **create_kwargs)

            def got_result(dataset):
                if dataset_id is None:
                    expected_dataset_id = dataset.dataset_id
                else:
                    expected_dataset_id = dataset_id
                expected = Dataset(dataset_id=expected_dataset_id,
                                   **create_kwargs)
                self.assertEqual(expected, dataset)

                listed = client.list_datasets_configuration()
                listed.addCallback(
                    lambda result: self.assertIn(expected, result))
                listed.addCallback(lambda _: dataset)
                return listed

            created.addCallback(got_result)
            return created

        def test_create_assigns_dataset(self):
            """
            If no ``dataset_id`` is specified when calling ``create_dataset``,
            a new one is generated.
            """
            return self.assert_creates(self.client,
                                       primary=self.node_1,
                                       maximum_size=DATASET_SIZE)

        def test_create_given_dataset(self):
            """
            If a ``dataset_id`` is specified when calling ``create_dataset``,
            it is used as the ID for the resulting created dataset.
            """
            dataset_id = uuid4()
            d = self.assert_creates(self.client, primary=self.node_1,
                                    maximum_size=DATASET_SIZE,
                                    dataset_id=dataset_id)
            d.addCallback(lambda dataset: self.assertEqual(dataset.dataset_id,
                                                           dataset_id))
            return d

        def test_create_with_metadata(self):
            """
            The metadata passed to ``create_dataset`` is stored with the
            dataset.
            """
            d = self.assert_creates(self.client, primary=self.node_1,
                                    maximum_size=DATASET_SIZE,
                                    metadata={u"hello": u"there"})
            d.addCallback(lambda dataset: self.assertEqual(
                dataset.metadata, pmap({u"hello": u"there"})))
            return d

        def test_create_conflicting_dataset_id(self):
            """
            Creating two datasets with same ``dataset_id`` results in an
            ``DatasetAlreadyExists``.
            """
            d = self.assert_creates(self.client, primary=self.node_1,
                                    maximum_size=DATASET_SIZE)

            def got_result(dataset):
                d = self.client.create_dataset(primary=self.node_1,
                                               maximum_size=DATASET_SIZE,
                                               dataset_id=dataset.dataset_id)
                return self.assertFailure(d, DatasetAlreadyExists)
            d.addCallback(got_result)
            return d

        def test_move(self):
            """
            ``move_dataset`` changes the dataset's primary.
            """
            dataset_id = uuid4()

            d = self.assert_creates(self.client, primary=self.node_1,
                                    maximum_size=DATASET_SIZE,
                                    dataset_id=dataset_id)
            d.addCallback(
                lambda _: self.client.move_dataset(self.node_2, dataset_id))

            def got_result(dataset):
                listed = self.client.list_datasets_configuration()
                listed.addCallback(lambda l: (dataset, l))
                return listed
            d.addCallback(got_result)

            def got_listing(result):
                moved_result, listed_datasets = result
                expected = Dataset(dataset_id=dataset_id,
                                   primary=self.node_2,
                                   maximum_size=DATASET_SIZE)
                self.assertEqual((expected, expected in listed_datasets),
                                 (moved_result, True))
            d.addCallback(got_listing)
            return d

        def test_list_state(self):
            """
            ``list_datasets_state`` returns information about state.
            """
            dataset_id = uuid4()
            expected_path = FilePath(b"/flocker/{}".format(dataset_id))
            d = self.assert_creates(self.client, primary=self.node_1,
                                    maximum_size=DATASET_SIZE * 2,
                                    dataset_id=dataset_id)
            d.addCallback(lambda _: self.synchronize_state())
            d.addCallback(lambda _: self.client.list_datasets_state())
            d.addCallback(lambda states:
                          self.assertIn(
                              DatasetState(dataset_id=dataset_id,
                                           primary=self.node_1,
                                           maximum_size=DATASET_SIZE * 2,
                                           path=expected_path),
                              states))
            return d

    return InterfaceTests


class FakeFlockerClientTests(make_clientv1_tests()):
    """
    Interface tests for ``FakeFlockerClient``.
    """
    def create_client(self):
        return FakeFlockerClient()

    def synchronize_state(self):
        return self.client.synchronize_state()


class FlockerClientTests(make_clientv1_tests()):
    """
    Interface tests for ``FlockerClient``.
    """
    def create_client(self):
        """
        Create a new ``FlockerClient`` instance pointing at a running control
        service REST API.

        :return: ``FlockerClient`` instance.
        """
        _, port = find_free_port()
        self.persistence_service = ConfigurationPersistenceService(
            reactor, FilePath(self.mktemp()))
        self.persistence_service.startService()
        self.cluster_state_service = ClusterStateService(reactor)
        self.cluster_state_service.startService()
        self.addCleanup(self.cluster_state_service.stopService)
        self.addCleanup(self.persistence_service.stopService)
        credential_set, _ = get_credential_sets()
        credentials_path = FilePath(self.mktemp())
        credentials_path.makedirs()

        api_service = create_api_service(
            self.persistence_service,
            self.cluster_state_service,
            TCP4ServerEndpoint(reactor, port, interface=b"127.0.0.1"),
            rest_api_context_factory(
                credential_set.root.credential.certificate,
                credential_set.control))
        api_service.startService()
        self.addCleanup(api_service.stopService)

        credential_set.copy_to(credentials_path, user=True)
        return FlockerClient(reactor, b"127.0.0.1", port,
                             credentials_path.child(b"cluster.crt"),
                             credentials_path.child(b"user.crt"),
                             credentials_path.child(b"user.key"))

    def synchronize_state(self):
        deployment = self.persistence_service.get()
        node_states = [NodeState(uuid=node.uuid, hostname=unicode(node.uuid),
                                 manifestations=node.manifestations,
                                 paths={manifestation.dataset_id:
                                        FilePath(b"/flocker").child(bytes(
                                            manifestation.dataset_id))
                                        for manifestation
                                        in node.manifestations.values()},
                                 devices={})
                       for node in deployment.nodes]
        self.cluster_state_service.apply_changes(node_states)

    def test_unexpected_error(self):
        """
        If the ``FlockerClient`` receives an unexpected HTTP response code it
        returns a ``ResponseError`` failure.
        """
        d = self.client.create_dataset(
            primary=self.node_1, maximum_size=u"notint")
        self.assertFailure(d, ResponseError)
        d.addCallback(lambda exc: self.assertEqual(exc.code, BAD_REQUEST))
        return d
