# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Dict, Iterable, Optional, Tuple, Union
from unittest.mock import Mock

import synapse.rest.admin
import synapse.storage
from synapse.appservice import ApplicationService
from synapse.rest.client import login, receipts, room
from synapse.util.stringutils import random_string
from synapse.types import JsonDict

from tests import unittest


class ApplicationServiceEphemeralEventsTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor, clock, hs):
        # Mock the application service scheduler so that we can track any outgoing transactions
        self.mock_scheduler = Mock()
        self.mock_scheduler.submit_ephemeral_events_for_as = Mock()

        hs.get_application_service_handler().scheduler = self.mock_scheduler

        self.user1 = self.register_user("user1", "password")
        self.token1 = self.login("user1", "password")

        self.user2 = self.register_user("user2", "password")
        self.token2 = self.login("user2", "password")

    def test_application_services_receive_read_receipts(self):
        """
        Test that when a user sends a read receipt in a room with another
        user, and that is in an application service's user namespace, that
        application service will receive that read receipt.
        """
        (
            interested_service,
            _,
        ) = self._register_interested_and_uninterested_application_services()

        # Create a room with both user1 and user2
        room_id = self.helper.create_room_as(
            self.user1, tok=self.token1, is_public=True
        )
        self.helper.join(room_id, self.user2, tok=self.token2)

        # Have user2 send a message into the room
        response_dict = self.helper.send(room_id, body="read me", tok=self.token2)

        # Have user1 send a read receipt for the message with an empty body
        self._send_read_receipt(room_id, response_dict["event_id"], self.token1)

        # user2 should have been the recipient of that read receipt.
        # Check if our application service - that is interested in user2 - received
        # the read receipt as part of an AS transaction.
        #
        # The uninterested application service should not have been notified.
        last_call = self.mock_scheduler.submit_ephemeral_events_for_as.call_args_list[
            0
        ]
        service, events = last_call[0]
        self.assertEqual(service, interested_service)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "m.receipt")
        self.assertEqual(events[0]["room_id"], room_id)

        # Assert that this was a read receipt from user1
        read_receipts = list(events[0]["content"].values())
        self.assertIn(self.user1, read_receipts[0]["m.read"])

        # Clear mock stats
        self.mock_scheduler.submit_ephemeral_events_for_as.reset_mock()

        # Send 2 pairs of messages + read receipts
        response_dict_1 = self.helper.send(room_id, body="read me1", tok=self.token2)
        response_dict_2 = self.helper.send(room_id, body="read me2", tok=self.token2)
        self._send_read_receipt(room_id, response_dict_1["event_id"], self.token1)
        self._send_read_receipt(room_id, response_dict_2["event_id"], self.token1)

        # Assert each transaction that was sent to the application service is as expected
        self.assertEqual(2, self.mock_scheduler.submit_ephemeral_events_for_as.call_count)

        first_call, second_call = self.mock_scheduler.submit_ephemeral_events_for_as.call_args_list
        service, events = first_call[0]
        self.assertEqual(service, interested_service)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "m.receipt")
        self.assertEqual(
            self._event_id_from_read_receipt(events[0]), response_dict_1["event_id"]
        )

        service, events = second_call[0]
        self.assertEqual(service, interested_service)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "m.receipt")
        self.assertEqual(
            self._event_id_from_read_receipt(events[0]), response_dict_2["event_id"]
        )

    def test_application_services_receive_to_device(self):
        """
        Test that when a user sends a to-device message to another user, and
        that is in an application service's user namespace, that application
        service will receive it.
        """
        (
            interested_service,
            _,
        ) = self._register_interested_and_uninterested_application_services()

        # Create a room with both user1 and user2
        room_id = self.helper.create_room_as(
            self.user1, tok=self.token1, is_public=True
        )
        self.helper.join(room_id, self.user2, tok=self.token2)

        # Have user2 send a typing notification into the room
        response_dict = self.helper.send(room_id, body="read me", tok=self.token2)

        # Have user1 send a read receipt for the message with an empty body
        channel = self.make_request(
            "POST",
            "/rooms/%s/receipt/m.read/%s" % (room_id, response_dict["event_id"]),
            access_token=self.token1,
        )
        self.assertEqual(channel.code, 200)

        # user2 should have been the recipient of that read receipt.
        # Check if our application service - that is interested in user2 - received
        # the read receipt as part of an AS transaction.
        #
        # The uninterested application service should not have been notified.
        service, events = self.mock_scheduler.submit_ephemeral_events_for_as.call_args[
            0
        ]
        self.assertEqual(service, interested_service)
        self.assertEqual(events[0]["type"], "m.receipt")
        self.assertEqual(events[0]["room_id"], room_id)

        # Assert that this was a read receipt from user1
        read_receipts = list(events[0]["content"].values())
        self.assertIn(self.user1, read_receipts[0]["m.read"])

    def _register_interested_and_uninterested_application_services(
        self,
    ) -> Tuple[ApplicationService, ApplicationService]:
        # Create an application service with exclusive interest in user2
        interested_service = self._make_application_service(
            namespaces={
                ApplicationService.NS_USERS: [
                    {
                        "regex": "@user2:.+",
                        "exclusive": True,
                    }
                ],
            },
        )
        uninterested_service = self._make_application_service()

        # Register this application service, along with another, uninterested one
        services = [
            uninterested_service,
            interested_service,
        ]
        self.hs.get_datastore().get_app_services = Mock(return_value=services)

        return interested_service, uninterested_service

    def _make_application_service(
        self,
        namespaces: Optional[
            Dict[
                Union[
                    ApplicationService.NS_USERS,
                    ApplicationService.NS_ALIASES,
                    ApplicationService.NS_ROOMS,
                ],
                Iterable[Dict],
            ]
        ] = None,
        supports_ephemeral: Optional[bool] = True,
    ) -> ApplicationService:
        return ApplicationService(
            token=None,
            hostname="example.com",
            id=random_string(10),
            sender="@as:example.com",
            rate_limited=False,
            namespaces=namespaces,
            supports_ephemeral=supports_ephemeral,
        )

    def _send_read_receipt(self, room_id: str, event_id_to_read: str, tok: str) -> None:
        """
        Send a read receipt of an event into a room.

        Args:
            room_id: The room to event is part of.
            event_id_to_read: The ID of the event being read.
            tok: The access token of the sender.
        """
        channel = self.make_request(
            "POST",
            "/rooms/%s/receipt/m.read/%s" % (room_id, event_id_to_read),
            access_token=tok,
            content="{}",
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _event_id_from_read_receipt(self, read_receipt_dict: JsonDict):
        """
        Extracts the first event ID from a read receipt. Read receipt dictionaries
        are in the form:

        {
            'type': 'm.receipt',
            'room_id': '!PEzCqHyycBVxqMKIjI:test',
            'content': {
                '$DETIeTEH651c1N7sP_j-YZiaQqCaayHhYwmhZDVWDY8': {  # We want this
                    'm.read': {
                        '@user1:test': {
                            'ts': 1300,
                            'hidden': False
                        }
                    }
                }
            }
        }

        Args:
            read_receipt_dict: The dictionary returned from a POST read receipt call.

        Returns:
            The (first) event ID the read receipt refers to.
        """
        return list(read_receipt_dict["content"].keys())[0]

    # TODO: Test that ephemeral messages aren't sent to application services that have
    #  ephemeral: false
