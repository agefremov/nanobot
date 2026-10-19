import { describe, expect, it } from "vitest";

import {
  groupSessions,
  type ChatGroupingOptions,
  type ChatGroupLabels,
  type SessionGroup,
} from "@/lib/chat-groups";
import type { ChatSummary } from "@/lib/types";

const LABELS: ChatGroupLabels = {
  pinned: "Pinned",
  all: "All",
  today: "Today",
  yesterday: "Yesterday",
  earlier: "Earlier",
  archived: "Archived",
  projects: "Projects",
  channels: "Channels",
  fallbackTitle: "New chat",
};

function topic(overrides: Partial<ChatSummary>): ChatSummary {
  const key = overrides.key ?? "telegram:100";
  return {
    key,
    channel: key.split(":")[0],
    chatId: key.split(":").slice(1).join(":"),
    createdAt: "2026-05-20T10:00:00Z",
    updatedAt: "2026-05-20T10:00:00Z",
    preview: "",
    readonly: true,
    ...overrides,
  };
}

function options(overrides: Partial<ChatGroupingOptions> = {}): ChatGroupingOptions {
  return {
    pinnedKeys: [],
    archivedKeys: [],
    titleOverrides: {},
    projectNameOverrides: {},
    sessionOrder: [],
    showArchived: false,
    sort: "updated_desc",
    ...overrides,
  };
}

function channelGroup(groups: SessionGroup[], channel: string): SessionGroup | undefined {
  return groups.find((group) => group.id === `channel:${channel}`);
}

describe("groupSessions channel topics", () => {
  it("hides archived channel topics while the archive view is off", () => {
    const groups = groupSessions(
      [
        topic({ key: "telegram:100", title: "Old topic" }),
        topic({ key: "telegram:200", title: "Live topic" }),
      ],
      LABELS,
      options({ archivedKeys: ["telegram:100"] }),
    );

    const group = channelGroup(groups, "telegram");
    expect(group?.sessions.map((session) => session.key)).toEqual(["telegram:200"]);
  });

  it("drops channel groups whose topics are all archived and hidden", () => {
    const groups = groupSessions(
      [
        topic({ key: "telegram:100" }),
        topic({ key: "max:200", title: "Live max topic" }),
      ],
      LABELS,
      options({ archivedKeys: ["telegram:100"] }),
    );

    expect(channelGroup(groups, "telegram")).toBeUndefined();
    expect(channelGroup(groups, "max")?.sessions.map((session) => session.key))
      .toEqual(["max:200"]);
  });

  it("keeps archived channel topics inside their channel group, sorted last", () => {
    const groups = groupSessions(
      [
        topic({ key: "telegram:100", title: "Old topic" }),
        topic({
          key: "telegram:200",
          title: "Newer archived topic",
          updatedAt: "2026-05-21T10:00:00Z",
        }),
      ],
      LABELS,
      options({ archivedKeys: ["telegram:200"], showArchived: true }),
    );

    const group = channelGroup(groups, "telegram");
    expect(group?.sessions.map((session) => session.key))
      .toEqual(["telegram:100", "telegram:200"]);
  });

  it("routes archived chats to the archive group while channel topics stay in their channel", () => {
    const groups = groupSessions(
      [
        topic({ key: "telegram:100", title: "Archived topic" }),
        {
          key: "websocket:chat-a",
          channel: "websocket",
          chatId: "chat-a",
          createdAt: "2026-05-20T10:00:00Z",
          updatedAt: "2026-05-20T10:00:00Z",
          preview: "",
          title: "Archived chat",
        },
      ],
      LABELS,
      options({ archivedKeys: ["telegram:100", "websocket:chat-a"], showArchived: true }),
    );

    expect(channelGroup(groups, "telegram")?.sessions.map((session) => session.key))
      .toEqual(["telegram:100"]);
    const archivedGroup = groups.find((group) => group.id === "archived");
    expect(archivedGroup?.sessions.map((session) => session.key))
      .toEqual(["websocket:chat-a"]);
  });
});
