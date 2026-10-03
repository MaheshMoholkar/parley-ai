"""The CDK app: `npx aws-cdk deploy` runs this (see cdk.json)."""

import aws_cdk as cdk

from infra.config import DeployConfig
from infra.stack import ParleyStack

app = cdk.App()
config = DeployConfig.from_context(app.node.try_get_context("parley"))
ParleyStack(
    app,
    "Parley",
    config=config,
    # Account and region come from your AWS credentials and profile.
    env=cdk.Environment(
        account=app.node.try_get_context("account") or None,
        region=app.node.try_get_context("region") or None,
    ),
)
app.synth()
