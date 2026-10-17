import type { ChannelUiContribution } from "@/channel-plugins/types";

export default {
  presentation: {
    logoUrl: "https://max.ru/favicon.ico",
    displayName: "MAX",
    initials: "MX",
    color: "#4463F5",
    setup: {
      mode: "credentials",
      docsUrl: "https://dev.max.ru/docs-api",
      fields: [
        { key: "channels.max.token", section: "credentials" },
        { key: "channels.max.allowFrom", section: "access" },
        { key: "channels.max.welcomeMessage", section: "behavior" },
      ],
    },
  },
} satisfies ChannelUiContribution;
