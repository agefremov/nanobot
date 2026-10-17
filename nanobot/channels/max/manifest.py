"""MAX messenger management contract."""

from nanobot.channels._manifest import field, required_fields
from nanobot.channels.contracts import ChannelSetupSpec
from nanobot.channels.plugin import ChannelPlugin

SETUP_SPEC = ChannelSetupSpec(
    fields={
        "token": field("secret"),
        "allowFrom": field("list"),
        "welcomeMessage": field(),
    },
    required=required_fields("token"),
    official_url="https://dev.max.ru/docs-api",
)

PLUGIN = ChannelPlugin(
    name="max",
    display_name="MAX",
    runtime=f"{__package__}.runtime:MaxChannel",
    setup=SETUP_SPEC,
    webui="webui/index.ts",
)
