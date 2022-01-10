import os.path
import time
import unittest
from urllib.parse import urljoin

import requests
from kinto_http import Client, KintoException

from kinto_remote_settings.signer.backends import local_ecdsa
from kinto_remote_settings.signer.serializer import canonical_json


__HERE__ = os.path.abspath(os.path.dirname(__file__))

SERVER_URL = "http://localhost:8888/v1"
DEFAULT_AUTH = ("user", "p4ssw0rd")


def user_principal(client):
    return client.server_info()["user"]["id"]


def create_records(client):
    # Create some data on the client collection and send it.
    with client.batch() as batch:
        for n in range(0, 10):
            batch.create_record(data={"newdata": n})


def flush_server(server_url):
    flush_url = urljoin(server_url, "/v1/__flush__")
    resp = requests.post(flush_url)
    resp.raise_for_status()


def trigger_signature(editor_client, reviewer_client=None):
    editor_client.patch_collection(data={"status": "to-review"})
    reviewer_client.patch_collection(data={"status": "to-sign"})


def fetch_history(client):
    url = client.get_endpoint("bucket") + "/history"
    body, headers = client.session.request("GET", url)
    return body["data"]


def create_group(client, name, members):
    endpoint = client.get_endpoint("collections")
    endpoint = endpoint.replace("/collections", "/groups/%s" % name)
    data = {"members": members}
    resp, headers = client.session.request("put", endpoint, data)
    return resp


class BaseTestFunctional(object):
    server_url = SERVER_URL

    @classmethod
    def setUpClass(cls):
        super(BaseTestFunctional, cls).setUpClass()
        cls.signer = local_ecdsa.ECDSASigner(private_key=cls.private_key)
        cls.source = Client(
            server_url=cls.server_url,
            auth=DEFAULT_AUTH,
            bucket=cls.source_bucket,
            collection=cls.source_collection,
        )
        cls.destination = Client(
            server_url=cls.server_url,
            auth=DEFAULT_AUTH,
            bucket=cls.destination_bucket,
            collection=cls.destination_collection,
        )
        cls.editor_client = Client(
            server_url=cls.server_url,
            auth=("editor", ""),
            bucket=cls.source_bucket,
            collection=cls.source_collection,
        )
        cls.someone_client = Client(
            server_url=cls.server_url,
            auth=("Sam", "Wan-Elss"),
            bucket=cls.source_bucket,
            collection=cls.source_collection,
        )

    @classmethod
    def tearDown(cls):
        # Delete all the created objects.
        flush_server(cls.server_url)

    def setUp(self):
        # Give the permission to write in collection to anybody
        self.source.create_bucket()
        perms = {"write": ["system.Authenticated"]}
        self.source.create_collection(permissions=perms)
        principals = [
            user_principal(self.editor_client),
            user_principal(self.someone_client),
            user_principal(self.source),
        ]
        create_group(self.source, "editors", members=principals)
        create_group(self.source, "reviewers", members=principals)

        # Create some data on the source collection and send it.
        create_records(self.source)

        self.source_records = self.source.get_records()
        assert len(self.source_records) == 10

        time.sleep(0.1)

        trigger_signature(editor_client=self.editor_client, reviewer_client=self.source)

    def test_groups_and_reviewers_are_forced(self):
        capability = self.source.server_info()["capabilities"]["signer"]
        assert capability["group_check_enabled"]
        assert capability["to_review_enabled"]

    def test_metadata_attributes(self):
        # Ensure the destination data is signed properly.
        destination_collection = self.destination.get_collection()["data"]
        signature = destination_collection["signature"]
        assert signature is not None

        # the status of the source collection should be "signed".
        source_collection = self.source.get_collection()["data"]
        assert source_collection["status"] == "signed"

    def test_distinct_users_can_trigger_signatures(self):
        collection = self.destination.get_collection()
        before = collection["data"]["signature"]

        self.source.create_record(data={"pim": "pam"})
        # Trigger a signature as someone else.
        trigger_signature(
            editor_client=self.editor_client, reviewer_client=self.someone_client
        )

        collection = self.destination.get_collection()
        after = collection["data"]["signature"]

        assert before != after


class AliceFunctionalTest(BaseTestFunctional, unittest.TestCase):
    private_key = os.path.join(__HERE__, "config/ecdsa.private.pem")
    source_bucket = "alice"
    destination_bucket = "alice"
    source_collection = "source"
    destination_collection = "destination"


# Signer is configured to use a different key for Bob and Alice.
class BobFunctionalTest(BaseTestFunctional, unittest.TestCase):
    private_key = os.path.join(__HERE__, "config/bob.ecdsa.private.pem")
    source_bucket = "bob"
    source_collection = "source"
    destination_bucket = "bob"
    destination_collection = "destination"


# Signoff is configured per bucket.
class PerBucketFunctionalTest(BaseTestFunctional, unittest.TestCase):
    private_key = os.path.join(__HERE__, "config/ecdsa.private.pem")
    source_bucket = "stage"
    source_collection = "cid"
    destination_bucket = "prod"
    destination_collection = "cid"


class WorkflowTest(unittest.TestCase):
    server_url = SERVER_URL

    @classmethod
    def setUpClass(cls):
        super(WorkflowTest, cls).setUpClass()
        client_kw = dict(server_url=cls.server_url, bucket="alice", collection="from")
        cls.client = Client(auth=DEFAULT_AUTH, **client_kw)
        cls.elsa_client = Client(auth=("elsa", ""), **client_kw)
        cls.anna_client = Client(auth=("anna", ""), **client_kw)
        cls.client_principal = user_principal(cls.client)
        cls.elsa_principal = user_principal(cls.elsa_client)
        cls.anna_principal = user_principal(cls.anna_client)

        private_key = os.path.join(__HERE__, "config/ecdsa.private.pem")
        cls.signer = local_ecdsa.ECDSASigner(private_key=private_key)

    def setUp(self):
        perms = {"write": ["system.Authenticated"]}
        self.client.create_bucket()
        create_group(
            self.client, "editors", members=[self.anna_principal, self.client_principal]
        )
        create_group(
            self.client,
            "reviewers",
            members=[self.elsa_principal, self.client_principal],
        )
        self.client.create_collection(permissions=perms)

    def tearDown(self):
        # Delete all the created objects.
        flush_server(self.server_url)

    def test_only_editors_can_ask_for_review(self):
        with self.assertRaises(KintoException):
            self.elsa_client.patch_collection(data={"status": "to-review"})

    def test_status_can_be_maintained_as_to_review(self):
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.elsa_client.patch_collection(data={"status": "to-review"})

    def test_preview_collection_is_updated_and_signed_on_to_review(self):
        create_records(self.client)
        self.anna_client.patch_collection(data={"status": "to-review"})

        collection = self.client.get_collection(id="preview")
        records = self.client.get_records(collection="preview")
        last_modified = self.client.get_records_timestamp(collection="preview")
        serialized_records = canonical_json(records, last_modified)

        signature = collection["data"]["signature"]
        assert signature is not None
        self.signer.verify(serialized_records, signature)

        assert len(records) == 10

    def test_same_editor_cannot_review(self):
        self.anna_client.patch_collection(data={"status": "to-review"})
        with self.assertRaises(KintoException):
            self.anna_client.patch_collection(data={"status": "to-sign"})

    def test_status_cannot_be_set_to_sign_without_review(self):
        create_records(self.client)
        with self.assertRaises(KintoException):
            self.elsa_client.patch_collection(data={"status": "to-sign"})

    def test_changes_can_be_rolledback(self):
        destination_records = self.client.get_records(collection="to")
        assert len(destination_records) == 0
        create_records(self.client)
        source_records = self.client.get_records(collection="from")
        assert len(source_records) == 10
        self.anna_client.patch_collection(data={"status": "to-review"})
        preview_records = self.client.get_records(collection="preview")
        assert len(preview_records) == 10

        self.anna_client.patch_collection(data={"status": "to-rollback"})

        source_records = self.client.get_records(collection="from")
        assert len(source_records) == len(destination_records)
        preview_records = self.client.get_records(collection="preview")
        assert len(preview_records) == len(destination_records)

    def test_review_can_be_cancelled_by_editor(self):
        create_records(self.client)
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.anna_client.patch_collection(data={"status": "work-in-progress"})
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.elsa_client.patch_collection(data={"status": "to-sign"})

    def test_review_can_be_cancelled_by_reviewer(self):
        create_records(self.client)
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.elsa_client.patch_collection(data={"status": "work-in-progress"})
        create_records(self.anna_client)
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.elsa_client.patch_collection(data={"status": "to-sign"})

    def test_must_ask_for_review_after_cancelled(self):
        create_records(self.client)
        self.anna_client.patch_collection(data={"status": "to-review"})
        self.elsa_client.patch_collection(data={"status": "work-in-progress"})
        with self.assertRaises(KintoException):
            self.elsa_client.patch_collection(data={"status": "to-sign"})

    def test_editors_can_be_different_after_cancelled(self):
        create_records(self.client)
        self.client.patch_collection(data={"status": "to-review"})

        resp = self.client.get_collection()
        assert resp["data"]["last_review_request_by"] == self.client_principal

        # Client cannot review since he is the last_editor.
        with self.assertRaises(KintoException):
            self.client.patch_collection(data={"status": "to-sign"})
        # Someone rejects the review.
        self.elsa_client.patch_collection(data={"status": "work-in-progress"})
        # Anna becomes the last_editor.
        self.anna_client.patch_collection(data={"status": "to-review"})
        # Client can now review because he is not the last_editor.
        self.client.patch_collection(data={"status": "to-sign"})

    def test_can_refresh_if_never_signed(self):
        create_records(self.elsa_client)
        source_data = self.client.get_collection()["data"]
        assert source_data["status"] == "work-in-progress"
        destination_data = self.client.get_collection(id="to")["data"]
        before_signature = destination_data.get("signature")

        self.elsa_client.patch_collection(data={"status": "to-resign"})

        source_data = self.client.get_collection()["data"]
        assert source_data["status"] == "work-in-progress"
        assert "last_review_request_date" not in source_data
        assert "last_signature_date" in source_data
        destination_data = self.client.get_collection(id="to")["data"]
        assert destination_data["signature"] != before_signature
        # Refresh does not copy records.
        destination_records = self.client.get_records(collection="to")
        assert len(destination_records) == 0

    def test_refresh_signs_preview_collection(self):
        preview_data = self.client.get_collection(id="preview")["data"]
        before_signature = preview_data.get("signature")

        self.elsa_client.patch_collection(data={"status": "to-resign"})

        preview_data = self.client.get_collection(id="preview")["data"]
        assert preview_data["signature"] != before_signature


class PerBucketTest(unittest.TestCase):
    server_url = SERVER_URL

    @classmethod
    def setUpClass(cls):
        super(PerBucketTest, cls).setUpClass()
        client_kw = dict(server_url=cls.server_url, bucket="stage")
        cls.client = Client(auth=DEFAULT_AUTH, **client_kw)
        cls.anon_client = Client(auth=tuple(), **client_kw)
        cls.julia_client = Client(auth=("julia", ""), **client_kw)
        cls.joan_client = Client(auth=("joan", ""), **client_kw)
        cls.julia_principal = user_principal(cls.julia_client)
        cls.joan_principal = user_principal(cls.joan_client)

    def setUp(self):
        self.client.create_bucket(
            permissions={
                "collection:create": ["system.Authenticated"],
                "group:create": ["system.Authenticated"],
            }
        )

    def tearDown(cls):
        # Delete all the created objects.
        flush_server(cls.server_url)

    def test_anyone_can_create_collections(self):
        collection = self.julia_client.create_collection(id="pim")
        assert self.julia_principal in collection["permissions"]["write"]

    def test_editors_and_reviewers_groups_are_created(self):
        self.julia_client.create_collection(id="pam")
        editors_group = self.julia_client.get_group(id="editors")
        reviewers_group = self.julia_client.get_group(id="reviewers")
        assert self.julia_principal in editors_group["data"]["members"]
        assert self.julia_principal not in reviewers_group["data"]["members"]

    def test_preview_and_destination_collections_are_signed(self):
        self.julia_client.create_collection(id="poum")

        preview_collection = self.anon_client.get_collection(
            bucket="preview", id="poum"
        )
        assert "signature" in preview_collection["data"]

        prod_collection = self.anon_client.get_collection(bucket="prod", id="poum")
        assert "signature" in prod_collection["data"]

    def test_collection_can_be_deleted(self):
        self.julia_client.create_collection(id="poum")

        self.julia_client.delete_collection(id="poum")

        # The following objects are still here, thus not raising:
        self.anon_client.get_collection(bucket="preview", id="poum")
        self.anon_client.get_collection(bucket="prod", id="poum")
        self.julia_client.get_group(id="editors")
        self.julia_client.get_group(id="reviewers")
