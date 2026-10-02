import boto3
from botocore.stub import Stubber

from parley.adapters.channels.email_ses import SesEmailChannel
from parley.ports.channel import OutboundMessage


def test_send_builds_the_ses_request() -> None:
    client = boto3.client(
        "sesv2", region_name="ap-south-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    channel = SesEmailChannel(
        "reminders@acme.example", "replies.acme.example", "ap-south-1", client
    )
    expected = {
        "FromEmailAddress": "reminders@acme.example",
        "Destination": {"ToAddresses": ["asha@example.com"]},
        "ReplyToAddresses": ["reply+abc123@replies.acme.example"],
        "Content": {
            "Simple": {
                "Subject": {"Data": "Invoice A-1", "Charset": "UTF-8"},
                "Body": {"Text": {"Data": "Dear Asha", "Charset": "UTF-8"}},
                "Headers": [{"Name": "X-Parley-Idempotency-Key", "Value": "key-1"}],
            }
        },
    }
    with Stubber(client) as stub:
        stub.add_response("send_email", {"MessageId": "ses-42"}, expected)
        provider_id = channel.send(
            OutboundMessage("key-1", "asha@example.com", "Invoice A-1", "Dear Asha", "abc123")
        )
        stub.assert_no_pending_responses()
    assert provider_id == "ses-42"
